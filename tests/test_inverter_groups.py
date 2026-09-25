"""Reale AC-Gruppen und das zusätzliche Anlagenlimit gemeinsam prüfen."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.pv_forecast.calculations import (
    InvalidConfigurationError,
    apply_inverter_limits,
    calculate_forecast,
)
from custom_components.pv_forecast.configuration import inverter_groups_from_options
from custom_components.pv_forecast.coordinator import PvForecastCoordinator
from custom_components.pv_forecast.model_context import configuration_id
from custom_components.pv_forecast.models import (
    AcInverterGroup,
    PvRoof,
    WeatherInterval,
)

from .helpers import persisted_roof, roof, weather
from .test_coordinator import _entry

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


def test_group_clipping_respects_partial_interval_duration():
    raw = grouped_forecast(minutes=30)
    assert raw.total.today == 4


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
    result = calculate_forecast(roofs, values, 5.7, END.date(), UTC, **kwargs)
    assert result.total.today.hex() == "0x1.6cccccccccccdp+2"
    assert result.roofs["a"].intervals[0].dc_power_kw.hex() == "0x1.ba9dee548dca2p+2"
    assert result.roofs["b"].intervals[0].dc_power_kw.hex() == "0x1.087dd131ce68cp+1"
    assert result.roofs["a"].daily.today.hex() == "0x1.18e0efcf94b04p+2"
    assert result.roofs["b"].daily.today.hex() == "0x1.4faf73f4e0724p+0"


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


def test_grouping_changes_physical_identity_but_names_and_empty_options_do_not():
    options = group_options()
    entry = SimpleNamespace(
        data={"latitude": 52, "longitude": 13, "time_zone": "UTC"}, options=options
    )
    grouped = configuration_id(entry)
    options["inverter_groups"][0]["name"] = "Neuer Anzeigename"
    options["inverter_groups"][0]["roof_ids"].reverse()
    assert configuration_id(entry) == grouped
    options["inverter_groups"][0]["max_power_kw"] = 5
    assert configuration_id(entry) != grouped
    options["inverter_groups"][0]["max_power_kw"] = 6
    options["inverter_groups"][0]["roof_ids"] = ["a", "c"]
    assert configuration_id(entry) != grouped
    options["inverter_groups"] = []
    empty = configuration_id(entry)
    options.pop("inverter_groups")
    assert configuration_id(entry) == empty
    assert empty == "00ed3467101985fee78023526b13e00377929266cfc9a8d84e4cae9a7d3cea5c"


async def test_coordinator_groups_need_no_extra_http(hass):
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
    assert client.async_fetch_roofs.await_count == 1
