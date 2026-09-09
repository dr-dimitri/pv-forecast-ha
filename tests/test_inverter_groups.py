"""Reale AC-Gruppen, unveränderte Altdaten und Faktoren vor beiden Begrenzungen."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.pv_forecast.calculations import (
    InvalidConfigurationError,
    apply_calibration,
    apply_inverter_limits,
    calculate_forecast,
    calibrated_energy,
    fit_calibration_factor,
    forecast_basis,
)
from custom_components.pv_forecast.configuration import inverter_groups_from_options
from custom_components.pv_forecast.coordinator import PvForecastCoordinator
from custom_components.pv_forecast.history import HistoryArchive
from custom_components.pv_forecast.history_runtime import _configuration_id
from custom_components.pv_forecast.models import (
    AcInverterGroup,
    ForecastCalibrationBasis,
    PvRoof,
    WeatherInterval,
)

from .helpers import persisted_roof, roof, weather
from .test_coordinator import _entry
from .test_history import SOURCE

END = datetime(2026, 9, 9, 13, tzinfo=UTC)
SHARED = AcInverterGroup("shared", "Gemeinsamer Wechselrichter", 6, ("a", "b"))


def grouped_forecast(groups=(SHARED,), limit=None, *, minutes=60):
    roofs = (roof("a", power=8), roof("b", power=4), roof("c", power=2))
    return calculate_forecast(
        roofs,
        {item.id: (weather(end=END, minutes=minutes),) for item in roofs},
        limit,
        END.date(),
        UTC,
        inverter_groups=groups,
    )


def group_options():
    return {
        "roofs": [persisted_roof(key) for key in ("a", "b", "c")],
        "inverter_groups": [
            {
                "id": "shared",
                "name": "Gemeinsam",
                "max_power_kw": 6,
                "roof_ids": ["a", "b"],
            }
        ],
    }


def test_two_roofs_share_one_ac_limit_without_limiting_an_independent_roof():
    result = grouped_forecast()
    assert {key: value.daily.today for key, value in result.roofs.items()} == {
        "a": 4,
        "b": 2,
        "c": 2,
    }
    assert result.total.today == 8


def test_independent_inverters_do_not_reallocate_their_clipping_to_each_other():
    groups = (
        AcInverterGroup("first", "Erstes Gerät", 3, ("a",)),
        AcInverterGroup("second", "Zweites Gerät", 5, ("b",)),
    )
    result = grouped_forecast(groups)
    assert [result.roofs[key].daily.today for key in ("a", "b", "c")] == [3, 4, 2]
    assert result.total.today == 9


def test_common_plant_limit_is_applied_after_group_limits_exactly_once():
    result = grouped_forecast(limit=6)
    assert [result.roofs[key].daily.today for key in ("a", "b", "c")] == [3, 1.5, 1.5]
    assert result.total.today == 6
    assert result.total_intervals[0].ac_power_kw == 6


def test_calibration_scales_original_dc_before_group_and_plant_limits():
    raw = grouped_forecast()
    corrected = apply_calibration(raw, 0.5, None, UTC)
    assert corrected.total.today == 7
    assert [corrected.roofs[key].daily.today for key in ("a", "b", "c")] == [4, 2, 1]
    assert corrected.roofs["a"].intervals[0].dc_power_kw == 8
    assert apply_calibration(raw, 1.0, None, UTC) is raw
    combined = apply_calibration(grouped_forecast(limit=6), 0.5, 6, UTC)
    assert combined.total.today == pytest.approx(6)
    assert combined.roofs["a"].daily.today == pytest.approx(24 / 7)


def test_group_basis_preserves_preclipping_power_and_reproduces_each_factor():
    raw = grouped_forecast()
    basis = forecast_basis(raw, END - timedelta(hours=1), END, None)
    assert basis.group_limits == (("shared", 6),)
    assert basis.has_ungrouped_roofs is True
    assert basis.intervals[0].dc_power_kw == 14
    assert basis.intervals[0].group_dc_power_kw == (12,)
    assert basis.intervals[0].ungrouped_dc_power_kw == 2
    encoded = basis.to_dict()
    assert encoded["schema_version"] == 2
    restored = ForecastCalibrationBasis.from_dict(encoded)
    assert restored == basis
    for factor in (0.5, 0.8, 1.0, 1.2, 1.5):
        assert calibrated_energy(restored, factor) == pytest.approx(
            apply_calibration(raw, factor, None, UTC).total.today
        )
    assert fit_calibration_factor([(restored, 8.4)] * 30) == 1.2


def test_group_clipping_and_basis_respect_partial_interval_duration():
    raw = grouped_forecast(minutes=30)
    assert raw.total.today == 4
    basis = forecast_basis(raw, END - timedelta(minutes=30), END, None)
    assert calibrated_energy(basis, 0.5) == 3.5


def test_zero_and_missing_roof_data_remain_distinguishable_with_groups():
    assert apply_inverter_limits({"a": 0, "b": 0, "c": 0}, 4, (SHARED,)) == {
        "a": 0,
        "b": 0,
        "c": 0,
    }
    raw = calculate_forecast(
        (roof("a", power=8), roof("b", power=4)),
        {"a": (weather(end=END),)},
        None,
        END.date(),
        UTC,
        inverter_groups=(SHARED,),
    )
    assert raw.total_intervals[0].is_complete is False
    assert forecast_basis(raw, END - timedelta(hours=1), END, None) is None
    corrected = apply_calibration(raw, 0.5, None, UTC)
    assert corrected.total.today == 4
    assert corrected.total_intervals[0].is_complete is False


@pytest.mark.parametrize("explicit_empty", [False, True])
def test_without_groups_outputs_match_pre_extension_float_bits(explicit_empty):
    """Die Vergleichsbits stammen aus dem main-Stand vor Einführung der Gruppen."""

    roofs = (
        PvRoof("a", "Dach A", 10.1, 180, 30, 0.1),
        PvRoof("b", "Dach B", 7.3, 90, 45, 0.2),
    )
    values = {
        "a": (WeatherInterval(END - timedelta(hours=1), END, 777.7, 31.2),),
        "b": (WeatherInterval(END - timedelta(hours=1), END, 345.6, 18.2),),
    }
    kwargs = {"inverter_groups": ()} if explicit_empty else {}
    result = calculate_forecast(
        roofs, values, 5.7, END.date(), UTC, calibration_factor=0.83, **kwargs
    )
    assert result.total.today.hex() == "0x1.6cccccccccccdp+2"
    assert result.roofs["a"].intervals[0].dc_power_kw.hex() == "0x1.ba9dee548dca2p+2"
    assert result.roofs["b"].intervals[0].dc_power_kw.hex() == "0x1.087dd131ce68cp+1"
    assert result.roofs["a"].daily.today.hex() == "0x1.18e0efcf94b04p+2"
    assert result.roofs["b"].daily.today.hex() == "0x1.4faf73f4e0724p+0"
    basis = forecast_basis(result, END - timedelta(hours=1), END, 5.7)
    assert "schema_version" not in basis.to_dict()
    assert "inverter_groups" not in basis.to_dict()
    assert ForecastCalibrationBasis.from_dict(basis.to_dict()) == basis


@pytest.mark.parametrize(
    "limit", [True, False, 0, -1, float("nan"), float("inf"), "3", 10**400]
)
def test_group_options_reject_invalid_ac_limits(limit):
    options = group_options()
    options["inverter_groups"][0]["max_power_kw"] = limit
    with pytest.raises(InvalidConfigurationError):
        inverter_groups_from_options(options)


@pytest.mark.parametrize("members", [[], ["missing"], ["a", "a"], [True], "a", None])
def test_group_options_reject_incomplete_or_duplicate_roof_membership(members):
    options = group_options()
    options["inverter_groups"][0]["roof_ids"] = members
    with pytest.raises(InvalidConfigurationError):
        inverter_groups_from_options(options)


def test_options_do_not_assign_one_roof_to_two_real_ac_groups():
    options = group_options()
    options["inverter_groups"].append(
        {"id": "other", "name": "Anderes Gerät", "max_power_kw": 2, "roof_ids": ["b"]}
    )
    with pytest.raises(InvalidConfigurationError):
        inverter_groups_from_options(options)
    options["inverter_groups"][1]["roof_ids"] = ["c"]
    assert len(inverter_groups_from_options(options)) == 2
    options["inverter_groups"][1]["id"] = "shared"
    with pytest.raises(InvalidConfigurationError):
        inverter_groups_from_options(options)


@pytest.mark.parametrize(
    "corruption",
    [
        "version",
        "boolean_version",
        "group_limit",
        "boolean_flag",
        "duplicate",
        "missing_group",
        "sum",
        "boolean_power",
        "unknown_ungrouped",
        "missing_flag",
    ],
)
def test_group_basis_rejects_ambiguous_or_inconsistent_stored_data(corruption):
    raw = grouped_forecast()
    stored = forecast_basis(raw, END - timedelta(hours=1), END, None).to_dict()
    if corruption == "version":
        stored["schema_version"] = 3
    elif corruption == "boolean_version":
        stored["schema_version"] = True
    elif corruption == "group_limit":
        stored["inverter_groups"][0]["max_power_kw"] = True
    elif corruption == "boolean_flag":
        stored["has_ungrouped_roofs"] = 1
    elif corruption == "duplicate":
        stored["inverter_groups"].append(deepcopy(stored["inverter_groups"][0]))
    elif corruption == "missing_group":
        stored["intervals"][0]["group_dc_power_kw"] = []
    elif corruption == "sum":
        stored["intervals"][0]["dc_power_kw"] = 100
    elif corruption == "boolean_power":
        stored["intervals"][0]["group_dc_power_kw"] = [True]
    elif corruption == "unknown_ungrouped":
        stored["has_ungrouped_roofs"] = False
    else:
        stored.pop("has_ungrouped_roofs")
    with pytest.raises(ValueError):
        ForecastCalibrationBasis.from_dict(stored)


def test_grouping_changes_physical_identity_but_names_and_empty_options_do_not():
    options = group_options()
    entry = SimpleNamespace(
        data={"latitude": 52, "longitude": 13, "time_zone": "UTC"}, options=options
    )
    grouped = _configuration_id(entry)
    options["inverter_groups"][0]["name"] = "Neuer Anzeigename"
    options["inverter_groups"][0]["roof_ids"].reverse()
    assert _configuration_id(entry) == grouped
    options["inverter_groups"][0]["max_power_kw"] = 5
    assert _configuration_id(entry) != grouped
    options["inverter_groups"][0]["max_power_kw"] = 6
    options["inverter_groups"][0]["roof_ids"] = ["a", "c"]
    assert _configuration_id(entry) != grouped
    options["inverter_groups"] = []
    empty = _configuration_id(entry)
    options.pop("inverter_groups")
    assert _configuration_id(entry) == empty
    assert empty == "00ed3467101985fee78023526b13e00377929266cfc9a8d84e4cae9a7d3cea5c"


@pytest.mark.parametrize("value", [None, True, -1, float("inf"), float("nan"), 10**400])
def test_stored_group_power_must_be_a_finite_nonnegative_real_number(value):
    basis = forecast_basis(grouped_forecast(), END - timedelta(hours=1), END, None)
    stored = basis.to_dict()
    stored["intervals"][0]["group_dc_power_kw"] = [value]
    with pytest.raises(ValueError):
        ForecastCalibrationBasis.from_dict(stored)


@pytest.mark.parametrize("group_id", ["", " ", True, 5, None])
def test_stored_group_identity_cannot_be_invented_by_string_coercion(group_id):
    basis = forecast_basis(grouped_forecast(), END - timedelta(hours=1), END, None)
    stored = basis.to_dict()
    stored["inverter_groups"][0]["id"] = group_id
    with pytest.raises(ValueError):
        ForecastCalibrationBasis.from_dict(stored)


def test_archived_group_basis_preserves_raw_applied_and_candidate_energy():
    forecast = grouped_forecast()
    archive = HistoryArchive("UTC")
    observed = END - timedelta(hours=2)
    archive.capture(
        forecast,
        observed,
        observed,
        "group-configuration",
        [SOURCE],
        applied_factor=0.5,
        applied_candidate_id="applied",
        trial_factor=1.2,
        trial_candidate_id="candidate",
    )
    record = next(
        record for record in archive.records.values() if record.horizon == "hourly_1h"
    )
    assert record.raw_energy_kwh == 8
    assert record.calibrated_energy_kwh == 7
    assert record.candidate_energy_kwh == pytest.approx(8.4)
    restored = HistoryArchive.from_dict(archive.to_dict(), "UTC")
    assert restored.records[record.record_id].to_dict() == record.to_dict()


async def test_coordinator_groups_and_local_factor_change_need_no_extra_http(hass):
    entry = _entry(hass, "UTC")
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "inverter_groups": [
                {
                    "id": "a-only",
                    "name": "Eigenes Gerät",
                    "max_power_kw": 3,
                    "roof_ids": ["a"],
                }
            ],
        },
    )
    client = AsyncMock(retry_after=None)
    client.async_fetch_roofs.return_value = {
        "a": (weather(end=END),),
        "b": (weather(end=END),),
    }
    coordinator = PvForecastCoordinator(hass, entry, client)
    with patch(
        "custom_components.pv_forecast.coordinator.dt_util.now", return_value=END
    ):
        await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data.total.today == 13
    fetched = coordinator.last_update_success_time
    coordinator.async_set_calibration(0.5, "candidate")
    assert coordinator.data.total.today == 8
    assert coordinator.last_update_success_time == fetched
    assert client.async_fetch_roofs.await_count == 1
