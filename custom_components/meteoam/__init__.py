"""The met component."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from dateutil.parser import parser
import logging
from random import randrange
from types import MappingProxyType
from typing import Any, Self

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_LATITUDE,
    CONF_LONGITUDE,
    EVENT_CORE_CONFIG_UPDATE,
    Platform,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_TRACK_HOME,
    DEFAULT_HOME_LATITUDE,
    DEFAULT_HOME_LONGITUDE,
    DOMAIN,
)

# Dedicated Home Assistant endpoint - do not change!
URL = "https://api.meteoam.it/deda-meteograms/api/GetMeteogram/preset1/{lat},{lon}"
#URL = "https://api.meteoam.it/deda-meteograms/meteograms?request=GetMeteogram&layers=preset1&latlon={lat},{lon}"

PLATFORMS = [Platform.WEATHER]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up MeteoAM as config entry."""
    # Don't setup if tracking home location and latitude or longitude isn't set.
    # Also, filters out our onboarding default location.
    if config_entry.data.get(CONF_TRACK_HOME, False) and (
        (not hass.config.latitude and not hass.config.longitude)
        or (
            hass.config.latitude == DEFAULT_HOME_LATITUDE
            and hass.config.longitude == DEFAULT_HOME_LONGITUDE
        )
    ):
        _LOGGER.warning(
            "Skip setting up met.no integration; No Home location has been set"
        )
        return False

    coordinator = MeteoAMDataUpdateCoordinator(hass, config_entry)
    await coordinator.async_config_entry_first_refresh()

    if config_entry.data.get(CONF_TRACK_HOME, False):
        coordinator.track_home()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][config_entry.entry_id] = coordinator

    config_entry.async_on_unload(config_entry.add_update_listener(async_update_entry))

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    )

    hass.data[DOMAIN][config_entry.entry_id].untrack_home()
    hass.data[DOMAIN].pop(config_entry.entry_id)

    return unload_ok


async def async_update_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    """Reload MeteoAM component when options changed."""
    await hass.config_entries.async_reload(config_entry.entry_id)


class CannotConnect(HomeAssistantError):
    """Unable to connect to the web site."""


class MeteoAMDataUpdateCoordinator(DataUpdateCoordinator["MeteoAMWeatherData"]):
    """Class to manage fetching MeteoAM data."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize global MeteoAM data updater."""
        self._unsub_track_home: Callable[[], None] | None = None
        self.weather = MeteoAMWeatherData(hass, config_entry.data)
        self.weather.set_coordinates()

        update_interval = timedelta(minutes=randrange(55, 65))

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=update_interval)

    async def _async_update_data(self) -> MeteoAMWeatherData:
        """Fetch data from MeteoAM."""
        try:
            return await self.weather.fetch_data()
        except Exception as err:
            raise UpdateFailed(f"Update failed: {err}") from err

    def track_home(self) -> None:
        """Start tracking changes to HA home setting."""
        if self._unsub_track_home:
            return

        async def _async_update_weather_data(_event: Event | None = None) -> None:
            """Update weather data."""
            if self.weather.set_coordinates():
                await self.async_refresh()

        self._unsub_track_home = self.hass.bus.async_listen(
            EVENT_CORE_CONFIG_UPDATE, _async_update_weather_data
        )

    def untrack_home(self) -> None:
        """Stop tracking changes to HA home setting."""
        if self._unsub_track_home:
            self._unsub_track_home()
            self._unsub_track_home = None


class MeteoAMWeatherData:
    """Keep data for MeteoAM.no weather entities."""

    def __init__(self, hass: HomeAssistant, config: MappingProxyType[str, Any]) -> None:
        """Initialise the weather entity data."""
        self.hass = hass
        self._config = config
        self._weather_data: None
        self.current_weather_data: dict = {}
        self.daily_forecast: list[dict] = []
        self.hourly_forecast: list[dict] = []
        self._coordinates: dict[str, str] | None = None

    def set_coordinates(self) -> bool:
        """Weather data initialization - set the coordinates."""
        if self._config.get(CONF_TRACK_HOME, False):
            latitude = self.hass.config.latitude
            longitude = self.hass.config.longitude
        else:
            latitude = self._config[CONF_LATITUDE]
            longitude = self._config[CONF_LONGITUDE]

        coordinates = {
            "lat": str(latitude),
            "lon": str(longitude),
        }
        if coordinates == self._coordinates:
            return False
        self._coordinates = coordinates

        self._weather_data = async_get_clientsession(self.hass)
        return True

    async def fetch_data(self) -> Self:
        """Fetch data from API - (current weather and forecast)."""
        _LOGGER.warning(URL.format(
                    lat=self._coordinates["lat"],
                    lon=self._coordinates["lon"],
                ))
        resp = await self._weather_data.get(
            URL.format(
                    lat=self._coordinates["lat"],
                    lon=self._coordinates["lon"],
                ), timeout=60
        )
        if not resp or resp.status != 200:
            raise CannotConnect()

        try:
            data = await resp.json()
            self.daily_forecast = []
            for tidx, t in enumerate(data['extrainfo']['stats']):
                dt = parser().parse(t['localDate'])
                element = {
                    'localDateTime': dt,
                    '2t': t['maxCelsius'],
                    '2t_min': t['minCelsius'],
                    '2tf': t['maxFahrenheit'],
                    '2tf_min': t['minFahrenheit'],
                    'icon': t['icon']
                }
                self.daily_forecast.append(element)

            hourly_forecast = []
            timeseries_data = data['timeseries']
            paramlist_data = data['paramlist']
            for tidx, t in enumerate(timeseries_data):
                dt = parser().parse(t)
                element = {
                    'localDateTime': dt.isoformat()
                }
                for pidx, p in enumerate(paramlist_data):
                    element[p] = data['datasets']['0'][str(pidx)][str(tidx)]
                if dt.replace(tzinfo=None) >= datetime.now():
                    hourly_forecast.append(element)
                if dt.replace(tzinfo=None) <= datetime.now():
                    self.current_weather_data = element
            self.hourly_forecast = hourly_forecast
            
            #_LOGGER.warning(self.current_weather_data)
            #_LOGGER.warning(self.daily_forecast)
            #_LOGGER.warning(self.hourly_forecast)

        except Exception as exc:
            _LOGGER.error(exc)
            raise exc
        return self
