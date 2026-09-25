"""Kleine, deterministische Testdaten-Helfer."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from custom_components.pv_forecast.const import (
    CONF_AZIMUTH,
    CONF_INSTALLED_POWER_KWP,
    CONF_LOSS_FACTOR,
    CONF_NAME,
    CONF_ROOF_ID,
    CONF_TILT,
)
from custom_components.pv_forecast.measurements import SourceConfig
from custom_components.pv_forecast.models import (
    DailyYield,
    ForecastResult,
    PvRoof,
    RoofForecast,
    RoofForecastInterval,
    TotalForecastInterval,
    WeatherInterval,
)

TIMEZONE = ZoneInfo("Europe/Berlin")

HOUR = timedelta(hours=1)
DAY = date(2026, 9, 9)
SOURCE = SourceConfig(
    "pv", "sensor.pv", "total", "Gesamte AC-PV ohne Speicher", "registry-pv"
)


def forecast(
    day: date = DAY,
    power: float = 1,
    timezone: str = "UTC",
    *,
    dc_power: float | None = None,
) -> ForecastResult:
    """Zwei lokale Tage mit UTC-Stunden und begrenzten Randintervallen."""
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=2), time.min, zone).astimezone(UTC)
    cursor = start.replace(minute=0, second=0, microsecond=0)
    intervals = []
    while cursor < end:
        left, right = max(start, cursor), min(end, cursor + HOUR)
        intervals.append(
            TotalForecastInterval(
                left, right, power * (right - left).total_seconds() / 3600, power
            )
        )
        cursor += HOUR
    roofs = {}
    if dc_power is not None:
        roof = PvRoof("roof", "Dach", 5, 180, 30, 0.1)
        roofs[roof.id] = RoofForecast(
            roof,
            tuple(
                RoofForecastInterval(
                    item.start, item.end, dc_power, item.ac_power_kw, item.energy_kwh
                )
                for item in intervals
            ),
            DailyYield(0, 0),
        )
    return ForecastResult(day, roofs, DailyYield(0, 0), tuple(intervals))


def roof(
    roof_id: str = "roof_1",
    *,
    name: str = "Süddach",
    power: float = 10.0,
    azimuth: float = 180.0,
    tilt: float = 35.0,
    loss: float = 0.0,
) -> PvRoof:
    """Gültige Dachfläche erzeugen."""

    return PvRoof(roof_id, name, power, azimuth, tilt, loss)


def persisted_roof(
    roof_id: str = "roof_1", *, name: str = "Süddach"
) -> dict[str, object]:
    """Gültige persistierte Dachkonfiguration erzeugen."""

    return {
        CONF_ROOF_ID: roof_id,
        CONF_NAME: name,
        CONF_INSTALLED_POWER_KWP: 10.0,
        CONF_AZIMUTH: 180.0,
        CONF_TILT: 35.0,
        CONF_LOSS_FACTOR: 0.0,
    }


def weather(
    gti: float = 1000.0,
    temperature: float | None = 25.0,
    *,
    end: datetime | None = None,
    minutes: int = 60,
) -> WeatherInterval:
    """Wetterintervall mit expliziter Dauer erzeugen."""

    interval_end = end or datetime(2026, 8, 23, 12, tzinfo=TIMEZONE)
    return WeatherInterval(
        start=interval_end - timedelta(minutes=minutes),
        end=interval_end,
        gti_w_m2=gti,
        ambient_temperature_c=temperature,
    )


async def configure_options(hass, flow_id, user_input):
    """Bestehende Fachtests durch die tatsächlich angebotene Optionsgruppe führen."""
    manager = hass.config_entries.options
    current = manager._progress[flow_id].cur_step
    step = user_input.get("next_step_id")
    if current["step_id"] == "init" and step not in current["menu_options"]:
        group = (
            "plant_options"
            if step in ("add_roof", "edit_roof", "remove_roof", "system")
            else "advanced_options"
        )
        await manager.async_configure(flow_id, {"next_step_id": group})
    return await manager.async_configure(flow_id, user_input)
