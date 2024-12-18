"""Asynchronous Python client for the Polestar API.""" ""

import json
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

import httpx

from .auth import PolestarAuth
from .const import (
    BASE_URL,
    BASE_URL_V2,
    BATTERY_DATA,
    CACHE_TIME,
    CAR_INFO_DATA,
    ODO_METER_DATA,
)
from .exception import (
    PolestarApiException,
    PolestarAuthException,
    PolestarNoDataException,
    PolestarNotAuthorizedException,
)

_LOGGER = logging.getLogger(__name__)


class PolestarApi:
    """Main class for handling connections with the Polestar API."""

    def __init__(
        self,
        username: str,
        password: str,
        client_session: httpx.AsyncClient | None = None,
        vins: list[str] | None = None,
        unique_id: str | None = None,
    ) -> None:
        """Initialize the Polestar API."""
        self.client_session = client_session or httpx.AsyncClient()
        self.username = username
        self.auth = PolestarAuth(username, password, self.client_session)
        self.updating = threading.Lock()
        self.latest_call_code = None
        self.latest_call_code_2 = None
        self.next_update = None
        self.car_data_by_vin: dict[str, dict] = {}
        self.cache_data_by_vin: dict[str, dict] = defaultdict(dict)
        self.cache_ttl = timedelta(seconds=CACHE_TIME)
        self.next_update_delay = timedelta(seconds=5)
        self.configured_vins = set(vins) if vins else None
        self.logger = _LOGGER.getChild(unique_id) if unique_id else _LOGGER

    async def async_init(self) -> None:
        """Initialize the Polestar API."""
        await self.auth.async_init()
        await self.auth.get_token()

        if self.auth.access_token is None:
            self.logger.warning("No access token %s", self.username)
            return

        if not (car_data := await self._get_vehicle_data()):
            self.logger.warning("No cars found for %s", self.username)
            return

        for data in car_data:
            vin = data["vin"]
            if self.configured_vins and vin not in self.configured_vins:
                continue
            self.car_data_by_vin[vin] = data
            self.cache_data_by_vin[vin][CAR_INFO_DATA] = {
                "data": self.car_data_by_vin[vin],
                "timestamp": datetime.now(),
            }
            self.logger.debug("API setup for VIN %s", vin)

    @property
    def vins(self) -> list[str]:
        return list(self.car_data_by_vin.keys())

    def get_latest_data(
        self, vin: str, query: str, field_name: str
    ) -> dict or bool or None:
        """Get the latest data from the Polestar API."""
        if self.cache_data_by_vin and self.cache_data_by_vin[vin][query]:
            data = self.cache_data_by_vin[vin][query]["data"]
            if data is None:
                return False
            return self._get_field_name_value(field_name, data)
        return None

    async def get_ev_data(self, vin: str) -> None:
        """
        Get the latest ev data from the Polestar API.

        Currently updates data for all VINs (this might change in the future).
        """

        if not self.updating.acquire(blocking=False):
            self.logger.debug("Skipping update, already in progress")
            return

        if self.next_update is not None and self.next_update > datetime.now():
            self.logger.debug("Skipping update, next update at %s", self.next_update)
            self.updating.release()
            return

        self.logger.debug("Starting update for VIN %s", vin)
        t1 = time.perf_counter()

        try:
            if self.auth.token_expiry is None:
                raise PolestarAuthException("No token expiry found", 500)
            if (self.auth.token_expiry - datetime.now()).total_seconds() < 300:
                await self.auth.get_token(refresh=True)
        except PolestarAuthException as e:
            self._set_latest_call_code(BASE_URL, 500)
            self.logger.warning("Auth Exception: %s", str(e))
            self.updating.release()
            return

        async def call_api(func):
            try:
                await func()
            except PolestarNotAuthorizedException:
                await self.auth.get_token()
            except PolestarApiException as e:
                self._set_latest_call_code(BASE_URL_V2, 500)
                self.logger.warning("Failed to get %s data %s", func.__name__, str(e))

        try:
            await call_api(lambda: self._get_odometer_data(vin))
            await call_api(lambda: self._get_battery_data(vin))
            self.next_update = datetime.now() + self.next_update_delay
        finally:
            self.updating.release()

        t2 = time.perf_counter()
        self.logger.debug("Update took %.2f seconds", t2 - t1)

    def get_cache_data(
        self, vin: str, query: str, field_name: str, skip_cache: bool = False
    ) -> dict | None:
        """Get the latest data from the cache."""
        if query is None:
            return None
        self.logger.debug("get_cache_data %s %s %s", vin, query, field_name)
        if self.cache_data_by_vin and self.cache_data_by_vin[vin].get(query):
            cache_entry = self.cache_data_by_vin[vin][query]
            data = cache_entry["data"]
            if data is not None and (
                skip_cache is True
                or cache_entry["timestamp"] + self.cache_ttl > datetime.now()
            ):
                return self._get_field_name_value(field_name, data)
        return None

    @staticmethod
    def _get_field_name_value(field_name: str, data: dict) -> str or bool or None:
        if field_name is None or data is None:
            return None

        if "/" in field_name:
            field_names = field_name.split("/")
            for key in field_names:
                if isinstance(data, dict) and key in data:
                    data = data[key]
                else:
                    return None
            return data

        if isinstance(data, dict) and field_name in data:
            return data[field_name]

        return None

    async def _get_odometer_data(self, vin: str) -> None:
        """Get the latest odometer data from the Polestar API."""
        params = {
            "query": "query GetOdometerData($vin:String!){getOdometerData(vin:$vin){averageSpeedKmPerHour eventUpdatedTimestamp{iso unix}odometerMeters tripMeterAutomaticKm tripMeterManualKm}}",
            "operationName": "GetOdometerData",
            "variables": '{"vin":"' + vin + '"}',
        }
        result = await self._get_graph_ql(params, BASE_URL_V2)

        if result and result["data"]:
            # put result in cache
            self.cache_data_by_vin[vin][ODO_METER_DATA] = {
                "data": result["data"][ODO_METER_DATA],
                "timestamp": datetime.now(),
            }

    async def _get_battery_data(self, vin: str) -> None:
        params = {
            "query": "query GetBatteryData($vin:String!){getBatteryData(vin:$vin){averageEnergyConsumptionKwhPer100Km batteryChargeLevelPercentage chargerConnectionStatus chargingCurrentAmps chargingPowerWatts chargingStatus estimatedChargingTimeMinutesToTargetDistance estimatedChargingTimeToFullMinutes estimatedDistanceToEmptyKm estimatedDistanceToEmptyMiles eventUpdatedTimestamp{iso unix}}}",
            "operationName": "GetBatteryData",
            "variables": '{"vin":"' + vin + '"}',
        }

        result = await self._get_graph_ql(params, BASE_URL_V2)

        if result and result["data"]:
            # put result in cache
            self.cache_data_by_vin[vin][BATTERY_DATA] = {
                "data": result["data"][BATTERY_DATA],
                "timestamp": datetime.now(),
            }

    async def _get_vehicle_data(self) -> dict | None:
        """Get the latest vehicle data from the Polestar API."""
        # get Vehicle Data
        params = {
            "query": "query GetConsumerCarsV2 { getConsumerCarsV2 { vin internalVehicleIdentifier salesType currentPlannedDeliveryDate market originalMarket pno34 modelYear registrationNo metaOrderNumber factoryCompleteDate registrationDate deliveryDate serviceHistory { claimType market mileage mileageUnit operations { id code description quantity performedDate } orderEndDate orderNumber orderStartDate parts { id code description quantity performedDate } statusDMS symptomCode vehicleAge workshopId } content { exterior { code name description excluded } exteriorDetails { code name description excluded } interior { code name description excluded } performancePackage { code name description excluded } performanceOptimizationSpecification { power { value unit } torqueMax { value unit } acceleration { value unit description } } wheels { code name description excluded } plusPackage { code name description excluded } pilotPackage { code name description excluded } motor { name description excluded } model { name code } images { studio { url angles resolutions } location { url angles resolutions } interior { url angles resolutions } } specification { battery bodyType brakes combustionEngine electricMotors performance suspension tireSizes torque totalHp totalKw trunkCapacity { label value } } dimensions { wheelbase { label value } groundClearanceWithPerformance { label value } groundClearanceWithoutPerformance { label value } dimensions { label value } } towbar { code name description excluded } } primaryDriver primaryDriverRegistrationTimestamp owners { id registeredAt information { polestarId ownerType } } wltpNedcData { wltpCO2Unit wltpElecEnergyConsumption wltpElecEnergyUnit wltpElecRange wltpElecRangeUnit wltpWeightedCombinedCO2 wltpWeightedCombinedFuelConsumption wltpWeightedCombinedFuelConsumptionUnit } energy { elecRange elecRangeUnit elecEnergyConsumption elecEnergyUnit weightedCombinedCO2 weightedCombinedCO2Unit weightedCombinedFuelConsumption weightedCombinedFuelConsumptionUnit } fuelType drivetrain numberOfDoors numberOfSeats motor { description code } maxTrailerWeight { value unit } curbWeight { value unit } hasPerformancePackage numberOfCylinders cylinderVolume cylinderVolumeUnit transmission numberOfGears structureWeek software { version versionTimestamp performanceOptimization { value description timestamp } } latestClaimStatus { mileage mileageUnit registeredDate vehicleAge } internalCar { origin registeredAt } edition commonStatusPoint { code timestamp description } brandStatus { code timestamp description } intermediateDestinationCode partnerDestinationCode features { type code name description excluded galleryImage { url alt } thumbnail { url alt } } electricalEngineNumbers { number placement } } }",
            "operationName": "GetConsumerCarsV2",
            "variables": '{"locale":"en_GB"}',
        }

        result = await self._get_graph_ql(params)
        if result and result["data"]:
            # check if there are cars in the account
            if (
                result["data"][CAR_INFO_DATA] is None
                or len(result["data"][CAR_INFO_DATA]) == 0
            ):
                self.logger.exception("No cars found in account")
                # throw new exception
                raise PolestarNoDataException("No cars found in account")

            return result["data"][CAR_INFO_DATA]
        return None

    def _set_latest_call_code(self, url: str, code: int) -> None:
        if url == BASE_URL:
            self.latest_call_code = code
        else:
            self.latest_call_code_2 = code

    async def _get_graph_ql(self, params: dict, url: str = BASE_URL):
        """Get the latest data from the Polestar API."""
        headers = {
            "Content-Type": "application/json",
            "authorization": f"Bearer {self.auth.access_token}",
        }

        self.logger.debug("GraphQL URL: %s", url)
        self.logger.debug("GraphQL Query: %s", json.dumps(params))

        result = await self.client_session.get(url, params=params, headers=headers)
        self._set_latest_call_code(url, result.status_code)

        if result.status_code == 401:
            raise PolestarNotAuthorizedException("Unauthorized Exception")

        if result.status_code != 200:
            raise PolestarApiException(f"Get GraphQL error: {result.text}")

        resultData = result.json()
        if resultData.get("errors"):
            self._set_latest_call_code(url, 500)
            error_message = resultData["errors"][0]["message"]
            if error_message == "User not authenticated":
                raise PolestarNotAuthorizedException("Unauthorized Exception")
            self.logger.error("Error: %s", resultData.get("errors"))
            raise PolestarApiException(error_message)

        self.logger.debug("GraphQL Result: %s", json.dumps(resultData))
        return resultData
