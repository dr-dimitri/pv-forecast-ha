"""Ross-Referenzwerte und prospektiven Vergleich ohne Modellwechsel prüfen."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.pv_forecast.calculations import (
    InvalidConfigurationError,
    calculate_dc_power_kw,
    calculate_forecast,
    ross_cell_temperature,
)
from custom_components.pv_forecast.coordinator import PvForecastCoordinator
from custom_components.pv_forecast.history import HistoryArchive
from custom_components.pv_forecast.models import AcInverterGroup
from custom_components.pv_forecast.temperature_comparison import (
    MODEL,
    comparison_report,
    mountings_from_options,
    parameter_id,
    validate_comparison,
)

from .helpers import TIMEZONE, roof, weather
from .test_coordinator import _entry
from .test_history import DAY, SOURCE, forecast
from .test_uncertainty import day_record


@pytest.mark.parametrize(
    "coefficient,expected",
    [(0.02, 36), (0.0208, 36.64), (0.026, 40.8), (0.0342, 47.36)],
)
def test_ross_reference_at_800_watts_matches_published_equation(coefficient, expected):
    assert ross_cell_temperature(20, 800, coefficient) == pytest.approx(expected)
    assert ross_cell_temperature(20, 0, coefficient) == 20
    assert ross_cell_temperature(None, 800, coefficient) is None


@pytest.mark.parametrize("coefficient", [0, -1, float("nan"), float("inf"), True])
def test_invalid_comparison_parameters_are_rejected(coefficient):
    with pytest.raises(InvalidConfigurationError):
        ross_cell_temperature(20, 800, coefficient)


def test_same_kwp_model_and_loss_are_preserved_with_optional_thermal_factor():
    assert calculate_dc_power_kw(roof(), weather()) == 10
    assert calculate_dc_power_kw(
        roof(), weather(), ross_coefficient=0.02
    ) == pytest.approx(9.3)
    assert calculate_dc_power_kw(
        roof(loss=0.1), weather(), ross_coefficient=0.02
    ) == pytest.approx(8.37)
    assert calculate_dc_power_kw(roof(), weather(0), ross_coefficient=0.02) == 0


def test_optional_comparison_keeps_group_clipping_and_marks_missing_temperature():
    roofs = (roof("a"), roof("b"))
    inputs = {name: (weather(),) for name in ("a", "b")}
    day = weather().start.astimezone(TIMEZONE).date()
    args = (roofs, inputs, 12, day, TIMEZONE)
    unchanged = calculate_forecast(*args)
    assert calculate_forecast(*args, temperature_coefficients=None) == unchanged
    group = AcInverterGroup("device", "Gerät", 10, ("a", "b"))
    variant = calculate_forecast(
        *args,
        inverter_groups=(group,),
        temperature_coefficients={"a": 0.02, "b": 0.0342},
    )
    assert variant.total_intervals[0].ac_power_kw == 10
    with pytest.raises(InvalidConfigurationError):
        calculate_forecast(*args, temperature_coefficients={"a": 0.02})
    missing = {"a": (replace(weather(), ambient_temperature_c=None),)}
    variant = calculate_forecast(
        (roofs[0],), missing, None, day, TIMEZONE, temperature_coefficients={"a": 0.02}
    )
    assert variant.total_intervals[0].quality_flags == ("missing_temperature",)


def test_roof_names_and_order_do_not_change_parameter_identity():
    roofs = (roof("a"), roof("b"))
    options = {
        "temperature_mountings": {
            "a": "free_standing",
            "b": "flat_ventilated",
            "deleted": "free_standing",
        }
    }
    chosen = mountings_from_options(options, roofs)
    assert set(chosen) == {"a", "b"}
    assert parameter_id(chosen) == parameter_id(dict(reversed(list(chosen.items()))))
    assert chosen == mountings_from_options(
        options, (replace(roofs[0], name="Neu"), roofs[1])
    )
    assert mountings_from_options({}, roofs) is None
    assert mountings_from_options(options, (*roofs, roof("c"))) is None


async def test_coordinator_computes_variant_without_extra_fetch_or_product_change(hass):
    entry = _entry(hass)
    client = AsyncMock(retry_after=None)
    client.async_fetch_roofs.return_value = {"a": (weather(600),), "b": (weather(600),)}
    coordinator = PvForecastCoordinator(hass, entry, client)
    with patch(
        "custom_components.pv_forecast.coordinator.dt_util.now",
        return_value=datetime(2026, 8, 23, 10, tzinfo=TIMEZONE),
    ):
        baseline = await coordinator._async_update_data()
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                "history_enabled": True,
                "temperature_comparison_enabled": True,
                "temperature_mountings": {"a": "free_standing", "b": "free_standing"},
            },
        )
        compared = await coordinator._async_update_data()
    assert compared == baseline
    assert coordinator.temperature_data.total.today < baseline.total.today
    assert coordinator.temperature_mountings == {
        "a": "free_standing",
        "b": "free_standing",
    }
    assert client.async_fetch_roofs.await_count == 2


def test_alternative_is_frozen_with_its_original_weather_and_parameter_choice():
    archive = HistoryArchive("UTC")
    now = datetime.combine(DAY - timedelta(days=1), datetime.min.time(), UTC).replace(
        hour=18
    )
    original = forecast(DAY - timedelta(days=1), power=1, dc_power=1)
    alternative = forecast(DAY - timedelta(days=1), power=0.9, dc_power=0.9)
    args = (original, now, now, "configuration-a", [SOURCE])
    archive.capture(*args)
    assert all(
        record.temperature_comparison is None for record in archive.records.values()
    )
    mounts = {"roof": "free_standing"}
    archive.capture(
        *args, temperature_forecast=alternative, temperature_mountings=mounts
    )
    target = next(
        record
        for record in archive.records.values()
        if record.horizon == "daily_previous_18" and record.target_date == DAY
    )
    assert target.temperature_comparison["energy_kwh"] == pytest.approx(21.6)
    before = deepcopy(target.to_dict())
    archive.capture(
        original,
        now + timedelta(minutes=1),
        now + timedelta(minutes=1),
        "configuration-a",
        [SOURCE],
        temperature_forecast=original,
        temperature_mountings=mounts,
    )
    assert archive.records[target.record_id].to_dict() == before
    assert (
        HistoryArchive.from_dict(archive.to_dict(), "UTC").records[target.record_id]
        == target
    )


def test_report_uses_identical_pairs_and_separates_parameters_and_calibration():
    mounts = {"roof": "free_standing"}
    comparison = {
        "schema_version": 1,
        "model": MODEL,
        "parameter_id": parameter_id(mounts),
        "mountings": mounts,
        "energy_kwh": 18,
    }
    first = replace(
        day_record(0, prediction=20, actual=18),
        calibrated_energy_kwh=100,
        temperature_comparison=comparison,
    )
    second = replace(
        day_record(1, prediction=20, actual=18),
        temperature_comparison={**comparison, "parameter_id": "other"},
    )
    now = first.end + timedelta(days=5)
    report = comparison_report([first, second], now, first.configuration_id, mounts)
    group = report["horizons"]["daily_previous_18"]
    assert group["count"] == 1
    assert group["raw_mae_kwh"] == 2
    assert group["alternative_mae_kwh"] == 0
    assert group["excluded_count"] == 1
    assert report["applied"] is False


@pytest.mark.parametrize("change", ["version", "parameter", "energy"])
def test_invalid_saved_comparison_is_rejected(change):
    mounts = {"roof": "free_standing"}
    value = {
        "schema_version": 1,
        "model": MODEL,
        "parameter_id": parameter_id(mounts),
        "mountings": mounts,
        "energy_kwh": 18,
    }
    assert validate_comparison(value) == value
    if change == "version":
        value["schema_version"] = 2
    elif change == "parameter":
        value["mountings"]["roof"] = "flat_ventilated"
    else:
        value["energy_kwh"] = float("nan")
    with pytest.raises(ValueError):
        validate_comparison(value)
