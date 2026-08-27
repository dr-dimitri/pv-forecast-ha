"""Konstanten der PV-Forecast-Integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "pv_forecast"
PLATFORMS: Final = ["sensor"]

CONF_LATITUDE: Final = "latitude"
CONF_LONGITUDE: Final = "longitude"
CONF_TIME_ZONE: Final = "time_zone"
CONF_LOCATION_SOURCE: Final = "location_source"
CONF_LOCATION_NAME: Final = "location_name"
CONF_POSTAL_CODE: Final = "postal_code"
CONF_STREET: Final = "street"
CONF_COUNTRY: Final = "country"
CONF_ROOFS: Final = "roofs"
CONF_ROOF_ID: Final = "id"
CONF_NAME: Final = "name"
CONF_INSTALLED_POWER_KWP: Final = "installed_power_kwp"
CONF_AZIMUTH: Final = "azimuth"
CONF_TILT: Final = "tilt"
CONF_LOSS_FACTOR: Final = "loss_factor"
CONF_SYSTEM_EFFICIENCY: Final = "system_efficiency"
CONF_INVERTER_MAX_POWER_KW: Final = "inverter_max_power_kw"
CONF_ADD_ANOTHER: Final = "add_another"
CONF_CONFIRM_REMOVE: Final = "confirm_remove"

DEFAULT_LOSS_PERCENT: Final = 10.0
DEFAULT_SYSTEM_EFFICIENCY_PERCENT: Final = 100 - DEFAULT_LOSS_PERCENT
DEFAULT_TEMPERATURE_COEFFICIENT: Final = -0.0035
REFERENCE_TEMPERATURE_C: Final = 25.0
UPDATE_INTERVAL: Final = timedelta(minutes=30)
REQUEST_TIMEOUT_SECONDS: Final = 20

OPEN_METEO_FORECAST_URL: Final = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ATTRIBUTION_URL: Final = "https://open-meteo.com/"
NOMINATIM_SEARCH_URL: Final = "https://nominatim.openstreetmap.org/search"
NOMINATIM_ATTRIBUTION_URL: Final = "https://www.openstreetmap.org/copyright"
NOMINATIM_USER_AGENT: Final = (
    "pv-forecast-ha (https://github.com/dr-dimitri/pv-forecast-ha)"
)

LOCATION_SOURCE_HOME_ASSISTANT: Final = "home_assistant"
LOCATION_SOURCE_ADDRESS: Final = "address"

DIRECTION_TO_COMPASS_AZIMUTH: Final[dict[str, float]] = {
    "north": 0.0,
    "north_east": 45.0,
    "east": 90.0,
    "south_east": 135.0,
    "south": 180.0,
    "south_west": 225.0,
    "west": 270.0,
    "north_west": 315.0,
}
