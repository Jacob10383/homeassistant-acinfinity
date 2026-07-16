import asyncio
import hashlib
import json
import logging
import time
from urllib.parse import urlencode

import aiohttp
import async_timeout
from homeassistant.exceptions import HomeAssistantError

from custom_components.ac_infinity.const import (
    AdvancedSettingsKey,
    AtType,
    ControllerType,
    DeviceControlKey,
    ModeAndSettingKeys,
)

_LOGGER = logging.getLogger(__name__)

API_URL_LOGIN = "/api/user/appUserLogin"
API_URL_GET_DEVICE_INFO_LIST_ALL = "/api/user/devInfoListAll"
API_URL_GET_DEV_MODE_SETTING = "/api/dev/getdevModeSettingList"
API_URL_ADD_DEV_MODE = "/api/dev/addDevMode"
API_URL_MODE_AND_SETTINGS = "/api/dev/modeAndSetting"
API_URL_GET_DEV_SETTING = "/api/dev/getDevSetting"
API_URL_UPDATE_ADV_SETTING = "/api/dev/updateAdvSetting"
API_URL_UPDATE_MASTER_PORT = "/api/dev/updateMsterByNums"
API_URL_REFRESH_TOKEN = "/api/auth/refresh"

ANDROID_APP_VERSION = "2.0.6"
AI_CONTROLLER_MIN_VERSION = "3.5"
TOKEN_REFRESH_WINDOW_SECONDS = 300
AI_READBACK_ATTEMPTS = 4
AI_READBACK_DELAY_SECONDS = 0.5

# QueryMap is built from these Android NetDeviceMode/NetDeviceSetting model
# fields.  Filtering to the app's model prevents response-only fields such as
# updateAllPort from being echoed into a physical-port write.
AI_MODE_AND_SETTING_MODEL_KEYS = frozenset(
    getattr(model, attr)
    for model in (ModeAndSettingKeys, AdvancedSettingsKey)
    for attr in dir(model)
    if not attr.startswith("_")
).union(
    {
        "calibrationTime",
        "devCsm1",
        "devMacAddr",
        "devTimeZone",
        "deviceColor",
        "deviceLanguage",
        "hOsc",
        "insidePort",
        "insideRoomName",
        "insideType",
        "isAdvTempTrigger",
        "isLeafBulitIn",
        "isLeafSensor1",
        "isLeafSensor2",
        "leafTempOutside",
        "lkType",
        "matterCode",
        "matterSta",
        "modeSetid",
        "outsidePort",
        "outsideRoomName",
        "outsideType",
        "qrPayLoad",
        "sensorDisplay",
        "sensorFlag",
        "sensorName",
        "sensorPort",
        "sensorType",
        "standardMode",
        "uuid",
        "uuidType",
        "vOsc",
    }
)


class ACInfinityClient:
    """Encapsulates http calls to the AC Infinity API"""

    def __init__(self, host: str, email: str, password: str) -> None:
        """
        Args:
            host: The base host of the AC Infinity API
            email: The e-mail to log in as, as configured by the user via config_flow
            password: The password to log in with, as configured by the user via config_flow
        """
        self._host = host
        self._email = email
        self._password = password
        self._user_id: str | None = None
        self._refresh_token: str | None = None
        self._secret_id: str | None = None
        self._request_app: str | None = None
        self._token_expires_at: int | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ai_update_lock = asyncio.Lock()

    async def login(self):
        """Call the log in endpoint with the configured email and password, and obtain the user id to use for subsequent calls"""
        headers = self.__create_headers(use_auth_token=False)

        # AC Infinity API does not accept passwords greater than 25 characters.
        # The Android/iOS app truncates passwords to accommodate for this.  We must do the same.
        normalized_password: str = self._password[0:25]

        response = await self.__post(
            API_URL_LOGIN,
            {"appEmail": self._email, "appPasswordl": normalized_password},
            headers,
        )
        self.__save_login_data(response["data"])

    def is_logged_in(self):
        """returns true if the user id is set, false otherwise"""
        return True if self._user_id else False

    def __ensure_logged_in(self) -> None:
        """Raise when a request requires an authenticated client."""
        if not self.is_logged_in():
            raise ACInfinityClientCannotConnect("AC Infinity client is not logged in.")

    def __ensure_signed_auth(self) -> None:
        """Raise when the API did not return the credentials required to sign a request."""
        self.__ensure_logged_in()
        if not self._secret_id or not self._request_app:
            raise ACInfinityClientCannotConnect(
                "AC Infinity login did not return signed-request credentials."
            )

    def __save_login_data(self, data: dict) -> None:
        """Retain the access and signing data returned by login or token refresh."""
        self._user_id = data["appId"]
        self._refresh_token = data.get("refreshToken") or self._refresh_token
        self._secret_id = data.get("secretId") or self._secret_id
        self._request_app = data.get("requestApp") or self._request_app
        self._token_expires_at = data.get("timeOut") or self._token_expires_at

    async def get_account_controllers(self):
        """Obtains a list of controllers, including metadata and some sensor values.
        Does not include information related to settings.
        """
        self.__ensure_logged_in()

        headers = self.__create_headers(use_auth_token=True)
        body = await self.__post(
            API_URL_GET_DEVICE_INFO_LIST_ALL, {"userId": self._user_id}, headers
        )
        return body["data"]

    async def get_device_mode_settings(self, controller_id: str | int, device_port: int):
        """Obtains the settings for a particular port on a controller, which includes information
        like speed, sensor triggers, mode timers, etc...

        Args:
            controller_id: The parent controller id of the port
            device_port: The port on the controller of the settings list to grab
        """
        self.__ensure_logged_in()

        headers = self.__create_headers(use_auth_token=True)
        body = await self.__post(
            API_URL_GET_DEV_MODE_SETTING, {"devId": controller_id, "port": device_port}, headers
        )
        return body["data"]

    @staticmethod
    def __transfer_values(device_control_keys: list[str], new_values: dict, existing_values: dict):
        updated: dict[str, str | int | bool] = {}
        for key in device_control_keys:
            value = new_values.get(key, existing_values.get(key, 0))
            if value is None:
                updated[key] = 0
            elif isinstance(value, (dict, list)):
                updated[key] = json.dumps(value)
            elif isinstance(value, bool):
                updated[key] = str(value).lower()
            else:
                updated[key] = value

        return updated

    async def update_device_controls(
        self, controller_id: str | int, device_port: int, key_values: dict[str, int]
    ):
        """Sets the provided settings on a port to a new values

        Args:
            controller_id: The parent controller id
            device_port: The port on the controller the device is plugged into
            key_values: The key value pairs of settings to set
        """
        self.__ensure_logged_in()

        headers = self.__create_headers(use_auth_token=True)
        body = await self.__post(
            API_URL_GET_DEV_MODE_SETTING, {"devId": controller_id, "port": device_port}, headers
        )
        existing_values = body["data"]

        device_control_keys: list[str] = [
            getattr(DeviceControlKey, attr)
            for attr in dir(DeviceControlKey)
            if not attr.startswith('_')
        ]

        updated = self.__transfer_values(device_control_keys, key_values, existing_values)
        _ = await self.__post(f"{API_URL_ADD_DEV_MODE}?{urlencode(updated)}", None, headers)

    async def update_device_settings(
        self, controller_id: str | int, device_port: int, device_name: str, key_values: dict[str, int]
    ):
        """Sets the provided settings on a port to a new values

        Args:
            controller_id: The parent controller id
            device_port: The port on the controller the device is plugged into
            device_name: The name of the device
            key_values: The key value pairs of settings to set
        """
        self.__ensure_logged_in()

        headers = self.__create_headers(use_auth_token=True)
        body = await self.__post(
            API_URL_GET_DEV_SETTING, {"devId": controller_id, "port": device_port}, headers
        )
        existing_values = body["data"]

        device_settings_keys: list[str] = [
            getattr(AdvancedSettingsKey, attr)
            for attr in dir(AdvancedSettingsKey)
            if not attr.startswith('_')
        ]

        updated = self.__transfer_values(device_settings_keys, key_values, existing_values)
        updated[AdvancedSettingsKey.DEV_NAME] = device_name

        _ = await self.__post(f"{API_URL_UPDATE_ADV_SETTING}?{urlencode(updated)}", None, headers)

    async def update_ai_device_control_and_settings(
        self,
        controller_id: str | int,
        device_port: int,
        key_values: dict[str, int],
        controller_type: int | None = None,
    ):
        """Sets the provided settings on a port to a new values

        Args:
            controller_id: id of the controller
            device_port: port of the device
            key_values: The key value pairs of settings to set
        """
        self.__ensure_logged_in()

        # Controller AI+ writes must target the selected physical port.  A write
        # to the controller's virtual "ALL" profile succeeds but does not drive
        # the attached device.
        if controller_type == ControllerType.UIS_89_AI_PLUS:
            async with self._ai_update_lock:
                await self.__update_ai_plus_device_control_and_settings(
                    controller_id, device_port, key_values, controller_type
                )
            return

        headers = self.__create_headers(use_auth_token=True, use_min_version=True)
        body = await self.__post(
            API_URL_GET_DEV_MODE_SETTING, {"devId": controller_id, "port": device_port}, headers
        )
        existing_values = body["data"]

        flattened = existing_values[DeviceControlKey.DEV_SETTING].copy()
        flattened.update(existing_values)

        device_control_keys: list[str] = [
            getattr(ModeAndSettingKeys, attr)
            for attr in dir(ModeAndSettingKeys)
            if not attr.startswith('_')
        ]

        updated = self.__transfer_values(device_control_keys, key_values, flattened)

        at_type = updated[DeviceControlKey.AT_TYPE]
        match at_type:
            case AtType.OFF:
                updated[ModeAndSettingKeys.MODE_AND_SETTING_ID_STR] = "[16,17]"
            case AtType.ON:
                updated[ModeAndSettingKeys.MODE_AND_SETTING_ID_STR] = "[16,18]"
            case AtType.AUTO:
                updated[ModeAndSettingKeys.MODE_AND_SETTING_ID_STR] = "[112,16,19,32,98,99]"
            case AtType.TIMER_TO_ON | AtType.TIMER_TO_OFF:
                updated[ModeAndSettingKeys.MODE_AND_SETTING_ID_STR] = "[16,20,21]"
            case AtType.CYCLE | AtType.SCHEDULE:
                updated[ModeAndSettingKeys.MODE_AND_SETTING_ID_STR] = "[16,22,23,40]"
            case AtType.VPD:
                updated[ModeAndSettingKeys.MODE_AND_SETTING_ID_STR] = "[16,81,32,98,99]"
            case _:
                raise ValueError(f"Unable to find setting id string - Unknown atType {at_type}")

        url = f"{API_URL_MODE_AND_SETTINGS}?{urlencode(updated)}"
        _ = await self.__put(url, headers)

    async def __update_ai_plus_device_control_and_settings(
        self,
        controller_id: str | int,
        device_port: int,
        key_values: dict[str, int],
        controller_type: int,
    ) -> None:
        """Atomically select and update a physical Controller AI+ port."""
        self.__ensure_signed_auth()
        await self.__refresh_access_token_if_needed()

        await self.__signed_post(
            f"{API_URL_UPDATE_MASTER_PORT}?{urlencode({'devId': controller_id, 'nums': device_port})}",
            None,
            controller_type,
        )
        existing_values = await self.__signed_post(
            f"{API_URL_GET_DEV_MODE_SETTING}?{urlencode({'devId': controller_id, 'port': device_port})}",
            None,
            controller_type,
        )
        updated = self.__build_ai_mode_and_settings_payload(
            controller_id, device_port, key_values, existing_values["data"]
        )

        await self.__signed_put(
            f"{API_URL_MODE_AND_SETTINGS}?{urlencode(updated)}", controller_type
        )

        for attempt in range(AI_READBACK_ATTEMPTS):
            readback = await self.__signed_post(
                f"{API_URL_GET_DEV_MODE_SETTING}?{urlencode({'devId': controller_id, 'port': device_port})}",
                None,
                controller_type,
            )
            try:
                self.__verify_ai_port_readback(
                    device_port, key_values, readback["data"]
                )
                return
            except ACInfinityClientRequestFailed:
                if attempt == AI_READBACK_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(AI_READBACK_DELAY_SECONDS)

    @classmethod
    def __build_ai_mode_and_settings_payload(
        cls,
        controller_id: str | int,
        device_port: int,
        key_values: dict[str, int],
        existing_values: dict,
    ) -> dict:
        """Build the Android app's full, non-destructive mode/settings payload."""
        flattened: dict = {}

        # The Android app starts with the mode object and overlays non-null
        # values from devSetting.  Preserve every modeled setting returned by
        # the API instead of synthesizing zeroes for unknown or absent keys.
        for source in (existing_values, existing_values.get(DeviceControlKey.DEV_SETTING) or {}):
            for key, value in source.items():
                if key in AI_MODE_AND_SETTING_MODEL_KEYS and value is not None:
                    flattened[key] = value

        flattened.update(key_values)
        flattened[ModeAndSettingKeys.DEV_ID] = str(controller_id)
        flattened[ModeAndSettingKeys.PORT] = device_port
        flattened[ModeAndSettingKeys.EXTERNAL_PORT] = device_port
        flattened[ModeAndSettingKeys.MASTER_PORT] = device_port

        at_type = flattened[DeviceControlKey.AT_TYPE]
        match at_type:
            case AtType.OFF:
                flattened[ModeAndSettingKeys.MODE_AND_SETTING_ID_STR] = "[16,17]"
            case AtType.ON:
                flattened[ModeAndSettingKeys.MODE_AND_SETTING_ID_STR] = "[16,18]"
            case AtType.AUTO:
                flattened[ModeAndSettingKeys.MODE_AND_SETTING_ID_STR] = "[112,16,19,32,98,99]"
            case AtType.TIMER_TO_ON | AtType.TIMER_TO_OFF:
                flattened[ModeAndSettingKeys.MODE_AND_SETTING_ID_STR] = "[16,20,21]"
            case AtType.CYCLE | AtType.SCHEDULE:
                flattened[ModeAndSettingKeys.MODE_AND_SETTING_ID_STR] = "[16,22,23,40]"
            case AtType.VPD:
                flattened[ModeAndSettingKeys.MODE_AND_SETTING_ID_STR] = "[16,81,32,98,99]"
            case _:
                raise ValueError(
                    f"Unable to find setting id string - Unknown atType {at_type}"
                )

        return {
            key: cls.__serialize_query_value(value)
            for key, value in flattened.items()
        }

    @staticmethod
    def __serialize_query_value(value):
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        if isinstance(value, bool):
            return str(value).lower()
        return value

    @classmethod
    def __verify_ai_port_readback(
        cls, device_port: int, key_values: dict[str, int], readback: dict
    ) -> None:
        """Verify both desired settings and deterministic physical output."""
        dev_setting = readback.get(DeviceControlKey.DEV_SETTING) or {}

        for key, expected_value in key_values.items():
            returned_values = [
                source[key]
                for source in (readback, dev_setting)
                if key in source and source[key] is not None
            ]
            if not returned_values or any(
                cls.__comparable_value(value) != cls.__comparable_value(expected_value)
                for value in returned_values
            ):
                raise ACInfinityClientRequestFailed(
                    {"code": 409, "msg": "Controller AI+ desired setting readback mismatch"}
                )

        returned_ports = [
            source[ModeAndSettingKeys.EXTERNAL_PORT]
            for source in (readback, dev_setting)
            if source.get(ModeAndSettingKeys.EXTERNAL_PORT) is not None
        ]
        if not returned_ports or any(int(port) != device_port for port in returned_ports):
            raise ACInfinityClientRequestFailed(
                {"code": 409, "msg": "Controller AI+ physical port readback mismatch"}
            )

        if cls.__comparable_value(
            readback.get(ModeAndSettingKeys.MASTER_PORT)
        ) != cls.__comparable_value(device_port):
            raise ACInfinityClientRequestFailed(
                {"code": 409, "msg": "Controller AI+ physical selector readback mismatch"}
            )

        at_type = readback.get(DeviceControlKey.AT_TYPE)
        desired_speed = readback.get(
            DeviceControlKey.ON_SELF_SPEED,
            dev_setting.get(DeviceControlKey.ON_SELF_SPEED),
        )
        expected_actual = None
        if at_type == AtType.OFF:
            expected_actual = 0
        elif at_type == AtType.ON and desired_speed is not None:
            expected_actual = desired_speed

        if expected_actual is not None and cls.__comparable_value(
            readback.get(DeviceControlKey.SPEAK)
        ) != cls.__comparable_value(expected_actual):
            raise ACInfinityClientRequestFailed(
                {"code": 409, "msg": "Controller AI+ physical output readback mismatch"}
            )

    @staticmethod
    def __comparable_value(value):
        if isinstance(value, bool):
            return int(value)
        try:
            return int(value)
        except (TypeError, ValueError):
            return value

    async def __refresh_access_token_if_needed(self) -> None:
        if (
            self._token_expires_at
            and self._token_expires_at - int(time.time()) <= TOKEN_REFRESH_WINDOW_SECONDS
        ):
            await self.__refresh_access_token()

    async def __refresh_access_token(self) -> None:
        """Refresh signed API credentials without recursively refreshing."""
        self.__ensure_signed_auth()
        if not self._refresh_token:
            raise ACInfinityClientCannotConnect(
                "AC Infinity login did not return a refresh token."
            )

        response = await self.__post(
            f"{API_URL_REFRESH_TOKEN}?{urlencode({'refreshToken': self._refresh_token})}",
            None,
            self.__create_headers(use_auth_token=True, use_signature=True),
        )
        self.__save_login_data(response["data"])

    async def __signed_post(
        self, path: str, post_data, controller_type: int, retry_auth: bool = True
    ):
        try:
            return await self.__post(
                path,
                post_data,
                self.__create_headers(
                    use_auth_token=True,
                    use_min_version=True,
                    use_signature=True,
                    controller_type=controller_type,
                ),
            )
        except ACInfinityClientRequestFailed as ex:
            if retry_auth and self.__is_expired_auth_response(ex):
                await self.__refresh_access_token()
                return await self.__signed_post(
                    path, post_data, controller_type, retry_auth=False
                )
            raise

    async def __signed_put(
        self, path: str, controller_type: int, retry_auth: bool = True
    ):
        try:
            return await self.__put(
                path,
                self.__create_headers(
                    use_auth_token=True,
                    use_min_version=True,
                    use_signature=True,
                    controller_type=controller_type,
                ),
            )
        except ACInfinityClientRequestFailed as ex:
            if retry_auth and self.__is_expired_auth_response(ex):
                await self.__refresh_access_token()
                return await self.__signed_put(path, controller_type, retry_auth=False)
            raise

    @staticmethod
    def __is_expired_auth_response(ex: "ACInfinityClientRequestFailed") -> bool:
        return bool(
            ex.args
            and isinstance(ex.args[0], dict)
            and ex.args[0].get("code") == 403
        )

    async def close(self) -> None:
        """Close the session when done"""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __get_session(self) -> aiohttp.ClientSession:
        """Get or create the HTTP session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(raise_for_status=False)
        return self._session

    async def __post(self, path, post_data, headers):
        """generically make a post request to the AC Infinity API"""
        session = await self.__get_session()
        async with async_timeout.timeout(10), session.post(
            f"{self._host}{path}", data=post_data, headers=headers
        ) as response:
            if response.status != 200:
                raise ACInfinityClientCannotConnect

            body = await response.json()
            if body["code"] != 200:
                if path == API_URL_LOGIN:
                    raise ACInfinityClientInvalidAuth
                else:
                    raise ACInfinityClientRequestFailed(body)

            return body

    async def __put(self, path, headers):
        """generically make a put request to the AC Infinity API"""
        session = await self.__get_session()
        async with async_timeout.timeout(10), session.put(
            f"{self._host}{path}", headers=headers
        ) as response:
            if response.status != 200:
                raise ACInfinityClientCannotConnect

            body = await response.json()
            if body["code"] != 200:
                raise ACInfinityClientRequestFailed(body)

            return body

    def __create_headers(
        self,
        use_auth_token: bool,
        use_min_version: bool = False,
        use_signature: bool = False,
        controller_type: int | None = None,
    ) -> dict:
        """Creates a header object to use in a request to the AC Infinity API"""
        # noinspection SpellCheckingInspection
        headers: dict = {
            "User-Agent": "okhttp/4.12.0",
        }

        if use_auth_token:
            headers["token"] = self._user_id

        if use_min_version:
            headers["minversion"] = AI_CONTROLLER_MIN_VERSION

        if use_signature:
            self.__ensure_signed_auth()
            request_id = str(time.time_ns() // 1_000_000)
            first_hash = self.__md5(f"{self._user_id}{ANDROID_APP_VERSION}")
            second_hash = self.__md5(
                f"{self._secret_id}{self._request_app}{request_id}"
            )
            headers.update(
                {
                    "appVersion": ANDROID_APP_VERSION,
                    "phoneType": "2",
                    "minversion": (
                        AI_CONTROLLER_MIN_VERSION if use_min_version else ""
                    ),
                    "devType": str(controller_type) if controller_type is not None else "",
                    "requestApp": self._request_app,
                    "version": ANDROID_APP_VERSION,
                    "requestId": request_id,
                    "sign": self.__md5(f"{first_hash}{second_hash}"),
                }
            )

        return headers

    @staticmethod
    def __md5(value: str) -> str:
        return hashlib.md5(value.encode("utf-8"), usedforsecurity=False).hexdigest()


class ACInfinityClientCannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class ACInfinityClientInvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class ACInfinityClientRequestFailed(HomeAssistantError):
    """Error to indicate a request failed"""
