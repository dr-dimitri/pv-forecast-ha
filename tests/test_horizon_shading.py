"""Horizontmodell, echte Strahlungsgrößen und unveränderte Standardanlagen prüfen."""

import math
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_forecast.api import (
    OpenMeteoClient,
    OpenMeteoDataError,
    parse_open_meteo_response,
)
from custom_components.pv_forecast.calculations import (
    apply_calibration,
    calculate_forecast,
    calibrated_energy,
    forecast_basis,
)
from custom_components.pv_forecast.card_data import build_forecast_view
from custom_components.pv_forecast.configuration import roofs_from_options
from custom_components.pv_forecast.const import DOMAIN
from custom_components.pv_forecast.coordinator import PvForecastCoordinator
from custom_components.pv_forecast.history_runtime import _configuration_id
from custom_components.pv_forecast.models import (
    AcInverterGroup,
    ForecastCalibrationBasis,
)
from custom_components.pv_forecast.services import _serialize_forecast
from custom_components.pv_forecast.shading import (
    adjusted_weather,
    horizon_elevation,
    parse_profile,
    solar_position,
    validate_profile,
)

from .helpers import configure_options, persisted_roof, roof, weather
from .test_api import _hourly_payload, _Response, _Session

WINTER = datetime(2026, 12, 21, 13, tzinfo=UTC)
SUMMER = datetime(2026, 6, 21, 13, tzinfo=UTC)


def point(end=WINTER, *, gti=600, dni=900, dhi=100):
    return replace(
        weather(gti, end=end),
        direct_normal_irradiance_w_m2=dni,
        diffuse_radiation_w_m2=dhi,
    )


def shaded_roof(height=30, **kwargs):
    return replace(roof(**kwargs), horizon_profile=(height,) * 12)


@pytest.mark.parametrize("count", [12, 24])
def test_profile_format_north_clockwise_and_wrap(count):
    values = tuple(float(index) for index in range(count))
    assert parse_profile(";\n".join(str(value) for value in values)) == values
    assert horizon_elevation(values, 0) == 0
    assert horizon_elevation(values, 90) == count / 4
    assert horizon_elevation(values, 180) == count / 2
    assert horizon_elevation(values, 270) == count * 3 / 4
    assert horizon_elevation(values, 360 - 180 / count) == (count - 1) / 2
    assert horizon_elevation(values, 360) == 0
    assert validate_profile([0] * count) == ()
    assert parse_profile(", ".join(["2.5"] * count)) == (2.5,) * count


@pytest.mark.parametrize(
    "bad",
    [
        "1,2,3",
        "-1 " * 12,
        "91 " * 12,
        "nan " * 12,
        "inf " * 12,
        "0:10 " * 12,
        "1 " * 1025,
        "0,5 " * 24,
        None,
    ],
)
def test_invalid_profile_is_rejected(bad):
    with pytest.raises(ValueError):
        parse_profile(bad)
    assert parse_profile("  ") == ()


@pytest.mark.parametrize(
    "bad", [[True] * 12, [None] * 12, [float("nan")] * 12, [10**400] * 12, {"0": 10}]
)
def test_persisted_profiles_do_not_accept_invalid_numeric_values(bad):
    with pytest.raises(ValueError):
        roofs_from_options(
            {"roofs": [persisted_roof()], "horizon_profiles": {"roof_1": bad}}
        )


def test_solar_position_against_independent_published_spa_example():
    # NREL/TP-560-34302, Anhang A.5: 17.10.2003, 12:30:30 MST,
    # 39,742476° N / 105,1786° W; scheinbare Höhe 39,88838°, Azimut 194,34024°.
    # NOAA ist eine gröbere Näherung ohne Refraktion: hier explizit 0,6° Toleranz.
    # https://docs.nlr.gov/docs/fy08osti/34302.pdf
    elevation, azimuth = solar_position(
        datetime(2003, 10, 17, 19, 30, 30, tzinfo=UTC), 39.742476, -105.1786
    )
    assert elevation == pytest.approx(39.88838, abs=0.6)
    assert azimuth == pytest.approx(194.34024, abs=0.6)


def test_solar_seasons_hemispheres_east_west_and_polar_night():
    assert 15 < solar_position(WINTER - timedelta(minutes=30), 50, 0)[0] < 18
    assert 61 < solar_position(SUMMER - timedelta(minutes=30), 50, 0)[0] < 64
    assert solar_position(WINTER, -50, 0)[0] > 60
    assert solar_position(WINTER, 80, 0)[0] < 0
    assert solar_position(WINTER, -80, 0)[0] > 0
    assert 0 < solar_position(SUMMER.replace(hour=8), 50, 0)[1] < 180
    assert 180 < solar_position(SUMMER.replace(hour=16), 50, 0)[1] < 360
    for lat in (-90, 90):
        assert all(math.isfinite(value) for value in solar_position(SUMMER, lat, 180))
    with pytest.raises(ValueError):
        solar_position(WINTER.replace(tzinfo=None), 50, 0)


def test_winter_blocked_summer_clear_retains_tilted_diffuse_floor():
    target = shaded_roof()
    winter, summer = point(), point(SUMMER)
    diffuse = 100 * (1 + math.cos(math.radians(35))) / 2
    assert adjusted_weather(target, winter, 50, 0).gti_w_m2 == pytest.approx(diffuse)
    assert adjusted_weather(target, summer, 50, 0) is summer
    # Ein konstanter Verlust kann diese beiden verschiedenen Verhältnisse
    # nicht darstellen. Das ist ein Mechanismusnachweis, keine reale Gütestudie.
    assert diffuse / 600 < 0.2
    assert adjusted_weather(shaded_roof(90), summer, 50, 0).gti_w_m2 == pytest.approx(
        diffuse
    )


def test_direct_projection_cannot_remove_diffuse_or_backside_light():
    diffuse_only = point(dni=0, dhi=600)
    assert adjusted_weather(shaded_roof(90), diffuse_only, 50, 0) is diffuse_only
    low_dni = point(dni=10, dhi=0)
    assert 590 <= adjusted_weather(shaded_roof(90), low_dni, 50, 0).gti_w_m2 < 600
    backside = shaded_roof(90, azimuth=0, tilt=90)
    assert adjusted_weather(backside, point(), 50, 0).gti_w_m2 == 600
    low_gti = point(gti=20, dhi=100)
    assert adjusted_weather(shaded_roof(90), low_gti, 50, 0).gti_w_m2 == 20
    assert adjusted_weather(shaded_roof(90), point(gti=0), 50, 0).gti_w_m2 == 0
    assert adjusted_weather(shaded_roof(90), point(), 80, 0).gti_w_m2 == 600


def test_partial_interval_samples_and_absolute_dst_times():
    source = point()
    # Die Sonne sinkt in diesem Winterintervall durch einen 16°-Horizont.
    partial = adjusted_weather(shaded_roof(16), source, 50, 0)
    assert 100 < partial.gti_w_m2 < 600
    for end in (
        datetime(2026, 10, 25, 1, tzinfo=UTC),
        datetime(2026, 10, 25, 2, tzinfo=UTC),
        datetime(2026, 3, 29, 2, tzinfo=UTC),
    ):
        utc = point(end)
        berlin = replace(
            utc,
            start=utc.start.astimezone(ZoneInfo("Europe/Berlin")),
            end=utc.end.astimezone(ZoneInfo("Europe/Berlin")),
        )
        assert (
            adjusted_weather(shaded_roof(), utc, 50, 0).gti_w_m2
            == adjusted_weather(shaded_roof(), berlin, 50, 0).gti_w_m2
        )
    short = replace(point(SUMMER), start=SUMMER - timedelta(minutes=15))
    assert adjusted_weather(shaded_roof(), short, 50, 0) is short


@pytest.mark.parametrize(
    "dni,dhi",
    [(None, 100), (900, None), (float("nan"), 100), (-1, 100), (900, float("inf"))],
)
def test_missing_radiation_preserves_gti_with_visible_fallback(dni, dhi):
    source = point(dni=dni, dhi=dhi)
    result = adjusted_weather(shaded_roof(), source, 50, 0)
    assert result.gti_w_m2 == source.gti_w_m2
    assert result.quality_flags == ("horizon_input_fallback",)
    assert adjusted_weather(roof(), source, 50, 0) is source


def test_per_roof_shading_precedes_calibration_and_both_clipping_stages():
    a, b = shaded_roof(90, roof_id="a", tilt=0), roof("b", tilt=0)
    source = point(SUMMER)
    data = calculate_forecast(
        (a, b),
        {"a": (source,), "b": (source,)},
        6,
        SUMMER.date(),
        UTC,
        latitude=50,
        longitude=0,
        inverter_groups=(AcInverterGroup("g", "Gerät", 1.25, ("a",)),),
    )
    assert data.roofs["a"].intervals[0].dc_power_kw == pytest.approx(1)
    assert data.roofs["b"].intervals[0].dc_power_kw == 6
    effective = apply_calibration(data, 1.5, 6, UTC)
    assert effective.total.today == pytest.approx(6)
    assert effective.roofs["a"].daily.today == pytest.approx(1.25 * 6 / 10.25)
    basis = forecast_basis(data, source.start, source.end, 6)
    assert basis is not None
    assert calibrated_energy(basis, 1.5) == pytest.approx(effective.total.today)
    assert ForecastCalibrationBasis.from_dict(basis.to_dict()) == basis
    assert effective.horizon_shading
    envelope = _serialize_forecast(effective, "UTC", SUMMER, True)
    assert envelope["horizon_shading"]["experimental"]
    view = build_forecast_view(effective, "UTC", "Anlage", SUMMER, SUMMER, True)
    assert view["horizon_shading"]["measured_improvement"] == "unavailable"


def test_no_profile_and_zero_profile_preserve_results_and_fingerprint():
    options = {"roofs": [persisted_roof()], "inverter_max_power_kw": 3}
    entry = SimpleNamespace(
        domain=DOMAIN,
        data={"latitude": 50, "longitude": 0, "time_zone": "UTC"},
        options=options,
    )
    original_id = _configuration_id(entry)
    original = calculate_forecast(
        roofs_from_options(options), {"roof_1": (point(),)}, 3, WINTER.date(), UTC
    )
    zero = options | {"horizon_profiles": {"roof_1": [0] * 12}}
    assert (
        calculate_forecast(
            roofs_from_options(zero), {"roof_1": (point(),)}, 3, WINTER.date(), UTC
        )
        == original
    )
    entry.options = zero
    assert _configuration_id(entry) == original_id
    entry.options = options | {"horizon_profiles": {"roof_1": [30] * 12}}
    assert _configuration_id(entry) != original_id
    changed = _configuration_id(entry)
    renamed = deepcopy(dict(entry.options))
    renamed["roofs"][0]["name"] = "Neuer Dachname"
    entry.options = renamed
    assert _configuration_id(entry) == changed
    with pytest.raises(ValueError):
        calculate_forecast(
            (shaded_roof(),), {"roof_1": (point(),)}, None, WINTER.date(), UTC
        )


class ShadingSession(_Session):
    """Angeforderte Variablen und UTC-Grenzen mit Offline-Werten beantworten."""

    def __init__(self, *, malformed=False):
        super().__init__(_Response({}))
        self.requests = []
        self.malformed = malformed

    def get(self, *args, **kwargs):
        params = kwargs["params"]
        self.requests.append(dict(params))
        payload = _hourly_payload(params["start_hour"], params["end_hour"])
        if "direct_normal_irradiance" in params["hourly"]:
            size = len(payload["hourly"]["time"])
            payload["hourly"]["direct_normal_irradiance"] = [None] + [900] * (size - 1)
            payload["hourly"]["diffuse_radiation"] = [100] * size
            if self.malformed:
                payload["hourly"].pop("diffuse_radiation")
        self.response = _Response(payload)
        return super().get(*args, **kwargs)


async def test_additional_variables_only_for_affected_shared_geometry():
    session = ShadingSession()
    a, b, c = shaded_roof(roof_id="a"), roof("b"), roof("c", tilt=10)
    data = await OpenMeteoClient(session).async_fetch_roofs(
        50, 0, "UTC", (a, b, c), local_date=WINTER.date(), forecast_days=7
    )
    assert session.calls == 2
    assert data["a"] is data["b"]
    assert data["a"][0].direct_normal_irradiance_w_m2 is None
    assert data["a"][1].direct_normal_irradiance_w_m2 == 900
    assert len(data["a"]) == 168
    assert [
        "direct_normal_irradiance" in req["hourly"] for req in session.requests
    ] == [True, False]
    assert all("direct_radiation," not in req["hourly"] for req in session.requests)


@pytest.mark.parametrize("values", [None, [], [100], "100"])
def test_malformed_additional_series_is_controlled_error(values):
    payload = _hourly_payload("2026-12-21T01:00", "2026-12-23T00:00")
    payload["hourly"].update(
        direct_normal_irradiance=values, diffuse_radiation=[100] * 48
    )
    with pytest.raises(OpenMeteoDataError):
        parse_open_meteo_response(payload, "UTC", include_horizon=True)


async def test_coordinator_uses_location_and_retains_old_data_on_invalid_extra_series(
    hass, freezer
):
    freezer.move_to(WINTER)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"latitude": 50, "longitude": 0, "time_zone": "UTC"},
        options={
            "roofs": [persisted_roof()],
            "horizon_profiles": {"roof_1": [30] * 12},
        },
    )
    entry.add_to_hass(hass)
    session = ShadingSession()
    coordinator = PvForecastCoordinator(hass, entry, OpenMeteoClient(session))
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    data = coordinator.data
    assert data.horizon_shading
    assert "horizon_input_fallback" in data.total_intervals[0].quality_flags
    assert data.roofs["roof_1"].intervals[12].dc_power_kw < 10
    session.malformed = True
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert coordinator.data is data
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    await coordinator.async_shutdown()


async def test_profile_options_validate_confirm_preserve_and_remove_by_stable_id(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"latitude": 50, "longitude": 0, "time_zone": "UTC"},
        options={
            "roofs": [persisted_roof("a"), persisted_roof("b", name="West")],
            "forecast_days": 7,
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(
        hass, result["flow_id"], {"next_step_id": "horizon_profile"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"id": "a"}
    )
    assert result["description_placeholders"]["roof"] == "Süddach"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"profile": "20", "confirm_horizon": True}
    )
    assert result["errors"]["base"] == "invalid_horizon_profile"
    values = ", ".join(["20"] * 24)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"profile": values, "confirm_horizon": False}
    )
    assert result["errors"]["base"] == "confirm_horizon_required"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"profile": values, "confirm_horizon": True}
    )
    assert result["data"]["horizon_profiles"] == {"a": [20] * 24}
    assert result["data"]["forecast_days"] == 7
    assert len(result["data"]["roofs"]) == 2
    assert (entry.version, entry.minor_version) == (1, 1)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(
        hass, result["flow_id"], {"next_step_id": "horizon_profile"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"id": "a"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"profile": "", "confirm_horizon": False}
    )
    assert "horizon_profiles" not in result["data"]


async def test_roof_delete_removes_only_its_profile(hass):
    from custom_components.pv_forecast.config_flow import PvForecastOptionsFlow

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"time_zone": "UTC"},
        options={
            "roofs": [persisted_roof("a"), persisted_roof("b")],
            "horizon_profiles": {"a": [30] * 12, "b": [20] * 12},
        },
    )
    entry.add_to_hass(hass)
    flow = PvForecastOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    result = flow._finish([persisted_roof("b")], None)
    assert result["data"]["horizon_profiles"] == {"b": [20] * 12}
