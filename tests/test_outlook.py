"""Messpräfix, geschätzte Brücke und Resttag dürfen sich nicht überschneiden."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_forecast.measurements import (
    SourceConfig,
    SourceHistory,
    aggregate_energy,
)
from custom_components.pv_forecast.outlook import build_day_outlook

from .test_solar_window import forecast

START = datetime(2026, 9, 9, tzinfo=UTC)
NOON = START + timedelta(hours=12)


def measurements(source_id="a", timezone="UTC", start=START, max_interval_minutes=60):
    history = SourceHistory(
        SourceConfig(
            source_id,
            f"sensor.{source_id}",
            "total",
            "AC-PV",
            max_interval_minutes=max_interval_minutes,
        ),
        timezone,
        20,
    )
    history.add_reading(start, 100, "kWh")
    return history


def outlook(histories, *, now=NOON, data=None, fetched_at=NOON, **kwargs):
    return build_day_outlook(
        data or forecast([1] * 24, START),
        histories,
        "UTC",
        now,
        fetched_at,
        True,
        **kwargs,
    )


def test_eight_measured_plus_twelve_future_make_twenty():
    history = measurements()
    history.add_reading(NOON, 108, "kWh")
    result = outlook([history])
    assert result["status"] == "available"
    assert result["measured_kwh"] == 8
    assert result["bridge_kwh"] == 0
    assert result["remaining_kwh"] == 12
    assert result["total_kwh"] == 20
    assert result["correction"] == "off"


def test_unmeasured_bridge_is_forecast_and_never_actual():
    history = measurements()
    last = NOON - timedelta(minutes=30)
    history.add_reading(last, 108, "kWh")
    result = outlook([history])
    assert result["measured_until"] == last.isoformat()
    assert result["measured_kwh"] == 8
    assert result["bridge_kwh"] == 0.5
    assert result["remaining_kwh"] == 12
    assert result["total_kwh"] == 20.5


def test_silent_counter_keeps_outlook_with_explicit_measurement_age():
    history = measurements()
    last = NOON - timedelta(hours=6)
    for minute in range(5, 361, 5):
        history.add_reading(START + timedelta(minutes=minute), 100 + minute / 60, "kWh")
    result = outlook([history])
    assert result["schema_version"] == 1
    assert result["status"] == "available"
    assert result["reason"] is None
    assert result["measured_until"] == last.isoformat()
    assert result["measurement_age_minutes"] == 360
    assert result["measurement_stale"] is True
    assert result["measurement_quality_flags"] == ["stale"]
    assert result["forecast_quality_flags"] == []
    assert result["quality_flags"] == ["stale"]
    assert result["measured_kwh"] == 6
    assert result["bridge_kwh"] == 6
    assert result["remaining_kwh"] == 12
    assert result["total_kwh"] == 24


@pytest.mark.parametrize("max_interval_minutes", [5, 30, 60, 180])
@pytest.mark.parametrize("overdue_seconds", [-60, 0, 1])
def test_measurement_staleness_uses_confirmed_source_threshold(
    max_interval_minutes, overdue_seconds
):
    history = measurements(max_interval_minutes=max_interval_minutes)
    age = timedelta(minutes=max_interval_minutes, seconds=overdue_seconds)
    history.add_reading(NOON - age, 108, "kWh")
    result = outlook([history])
    assert result["status"] == "available"
    assert result["measurement_age_minutes"] == age.total_seconds() / 60
    assert result["measurement_stale"] is (overdue_seconds > 0)
    assert ("stale" in result["measurement_quality_flags"]) is (overdue_seconds > 0)


def test_sources_use_their_own_freshness_limit_and_preserve_common_boundary():
    first = measurements(max_interval_minutes=5)
    second = measurements("b", max_interval_minutes=60)
    common = NOON - timedelta(minutes=30)
    first.add_reading(common, 103, "kWh")
    second.add_reading(common, 104, "kWh")
    first.add_reading(NOON - timedelta(minutes=2), 104, "kWh")
    result = outlook([first, second])
    assert result["measurement_age_minutes"] == 30
    assert result["measurement_stale"] is False
    assert "stale" not in result["measurement_quality_flags"]
    assert result["measured_until"] == common.isoformat()
    assert result["measured_kwh"] == 7

    # Gleiche Messungen, jetzt eine überschrittene individuelle Meldefrist.
    later = NOON + timedelta(minutes=4)
    result = outlook([first, second], now=later, fetched_at=later)
    assert result["measurement_age_minutes"] == 34
    assert result["measurement_stale"] is True
    assert result["status"] == "available"
    assert result["measured_until"] == common.isoformat()
    assert result["measured_kwh"] == 7


def test_fresh_offset_sources_do_not_become_stale_from_old_common_boundary():
    first, second = measurements(max_interval_minutes=5), measurements(
        "b", max_interval_minutes=5
    )
    common = NOON - timedelta(hours=2)
    first.add_reading(common, 104, "kWh")
    second.add_reading(common, 103, "kWh")
    first.add_reading(NOON - timedelta(minutes=1), 105, "kWh")
    second.add_reading(NOON - timedelta(minutes=2), 104, "kWh")
    result = outlook([first, second])
    assert result["measurement_age_minutes"] == 120
    assert result["measurement_stale"] is False
    assert "stale" not in result["quality_flags"]
    assert result["bridge_kwh"] == 2


def test_measurement_and_forecast_quality_remain_distinguishable():
    history = measurements()
    history.add_reading(NOON - timedelta(hours=2), 108, "kWh")
    data = forecast([1] * 24, START)
    data = replace(
        data,
        total_intervals=tuple(
            replace(interval, quality_flags=("missing_temperature",))
            for interval in data.total_intervals
        ),
    )
    result = outlook([history], data=data)
    assert result["status"] == "unavailable"
    assert result["reason"] == "input_fallbacks"
    assert result["measurement_stale"] is True
    assert "stale" in result["measurement_quality_flags"]
    assert "missing_temperature" not in result["measurement_quality_flags"]
    assert result["forecast_quality_flags"] == ["missing_temperature"]
    assert result["quality_flags"] == sorted(
        {*result["measurement_quality_flags"], "missing_temperature"}
    )

    stale_forecast = outlook([history], data=data, fetched_at=NOON - timedelta(hours=2))
    assert stale_forecast["reason"] == "stale_forecast"
    assert stale_forecast["measurement_stale"] is True
    assert stale_forecast["forecast_quality_flags"] == ["missing_temperature"]
    # Die bisherige Liste wird an frühen Rückgaben nicht nachträglich erweitert.
    assert stale_forecast["quality_flags"] == result["measurement_quality_flags"]


def test_disjoint_sources_need_common_exact_measurement_boundary():
    first, second = measurements(), measurements("b")
    first.add_reading(NOON - timedelta(hours=1), 104, "kWh")
    second.add_reading(NOON - timedelta(hours=1), 103, "kWh")
    first.add_reading(NOON, 105, "kWh")
    second.add_reading(NOON - timedelta(minutes=15), 104, "kWh")
    result = outlook([first, second])
    assert result["measured_until"] == (NOON - timedelta(hours=1)).isoformat()
    assert result["measured_kwh"] == 7
    assert result["bridge_kwh"] == 1
    assert result["total_kwh"] == 20


def test_missing_prefix_and_missing_common_boundary_remain_unknown():
    first, second = measurements(), measurements("b")
    first.add_reading(NOON, 108, "kWh")
    assert outlook([first, second])["reason"] == "no_common_measurement_boundary"
    missing = measurements(start=START + timedelta(hours=1))
    missing.add_reading(NOON, 108, "kWh")
    result = outlook([missing])
    assert result["reason"] == "incomplete_measurements"
    assert result["total_kwh"] is None
    assert result["remaining_kwh"] == 12


def test_current_view_never_adds_yield_from_previous_location():
    history = measurements()
    history.bind_location("old", "UTC", START)
    history.add_reading(START + timedelta(hours=6), 108, "kWh")
    history.bind_location("new", "UTC", START + timedelta(hours=7))
    history.add_reading(START + timedelta(hours=7), 108, "kWh")
    history.add_reading(NOON, 110, "kWh")
    result = outlook([history.current_location_view()])
    assert result["status"] == "unavailable"
    assert result["reason"] == "incomplete_measurements"
    assert result["measured_kwh"] is None
    assert result["total_kwh"] is None


def test_counter_decrease_and_source_change_do_not_create_complete_day():
    history = measurements()
    history.add_reading(START + timedelta(hours=6), 104, "kWh")
    history.add_reading(START + timedelta(hours=7), 0, "kWh")
    history.add_reading(NOON, 4, "kWh")
    assert outlook([history])["reason"] == "incomplete_measurements"


def test_stale_forecast_keeps_measurement_but_withholds_total():
    history = measurements()
    history.add_reading(NOON, 108, "kWh")
    result = outlook([history], fetched_at=NOON - timedelta(hours=2))
    assert result["reason"] == "stale_forecast"
    assert result["measured_kwh"] == 8
    assert result["remaining_kwh"] == 12
    assert result["total_kwh"] is None
    assert outlook([history], identity_unresolved=True)["total_kwh"] is None


def test_forecast_gap_does_not_become_zero_bridge():
    history = measurements()
    history.add_reading(NOON - timedelta(hours=1), 108, "kWh")
    data = forecast([1] * 24, START)
    data = replace(
        data, total_intervals=data.total_intervals[:11] + data.total_intervals[12:]
    )
    result = outlook([history], data=data)
    assert result["reason"] == "incomplete_forecast"
    assert result["bridge_kwh"] is None
    assert result["remaining_kwh"] == 12


@pytest.mark.parametrize("powers", [[0] * 24, [2] * 6 + [0] * 6 + [2] * 12])
def test_zero_production_and_changing_weather_never_create_a_factor(powers):
    history = measurements()
    history.add_reading(NOON, 100, "kWh")
    result = outlook([history], data=forecast(powers, START))
    assert result["total_kwh"] == sum(powers[12:])
    assert result["correction"] == "off"


@pytest.mark.parametrize("day", [datetime(2026, 3, 29), datetime(2026, 10, 25)])
def test_dst_outlook_keeps_the_actual_local_day_length(day):
    zone = ZoneInfo("Europe/Berlin")
    start = day.replace(tzinfo=zone).astimezone(UTC)
    end = (day + timedelta(days=1)).replace(tzinfo=zone).astimezone(UTC)
    count = int((end - start).total_seconds() / 3600)
    now = start + timedelta(hours=12)
    history = measurements(timezone="Europe/Berlin", start=start)
    history.add_reading(now, 108, "kWh")
    result = build_day_outlook(
        forecast([1] * count, start), [history], "Europe/Berlin", now, now, True
    )
    assert result["status"] == "available"
    assert result["total_kwh"] == 8 + count - 12


def test_estimate_uses_measurements_after_missing_morning_without_double_counting():
    """Fehlende Morgenstunden verhindern die belegte spätere Messung nicht."""
    history = measurements(start=START + timedelta(hours=8))
    history.add_reading(START + timedelta(hours=10), 104, "kWh")
    original = history.to_dict()
    result = outlook([history])
    assert result["status"] == "unavailable"
    assert result["total_kwh"] is None
    estimate = result["estimate"]
    assert estimate["status"] == "available"
    assert estimate["basis"] == "measurements_and_forecast"
    assert estimate["measured_kwh"] == 4
    assert estimate["estimated_past_kwh"] == 10
    assert estimate["remaining_kwh"] == 12
    assert estimate["total_kwh"] == 26
    assert estimate["measurement_coverage_seconds"] == 7200
    assert history.to_dict() == original


def test_estimate_fills_derived_gap_and_keeps_both_valid_sections():
    """Unbekannte Integralenergie bleibt Schätzung; beide belegten Seiten zählen."""
    history = measurements(max_interval_minutes=180)
    history.replace_source(replace(history.source, derived_energy=True))
    history.add_reading(START, 100, "kWh")
    history.add_reading(START + timedelta(hours=2), 103, "kWh")
    history.mark_gap("restart")
    history.add_reading(START + timedelta(hours=3), 110, "kWh")
    history.add_reading(START + timedelta(hours=4), 112, "kWh")
    estimate = outlook([history])["estimate"]
    assert estimate["measured_kwh"] == 5
    assert estimate["estimated_past_kwh"] == 9
    assert estimate["remaining_kwh"] == 12
    assert estimate["total_kwh"] == 26


def test_estimate_joins_different_reporting_intervals_at_exact_boundaries():
    """Einzelquellen ersetzen nur gemeinsam belegte Abschnitte der Gesamtprognose."""
    first = measurements(start=START + timedelta(hours=2))
    second = measurements("b", start=START + timedelta(hours=2))
    first.add_reading(START + timedelta(hours=3), 101, "kWh")
    first.add_reading(START + timedelta(hours=4), 103, "kWh")
    first.add_reading(START + timedelta(hours=5), 104, "kWh")
    second.add_reading(START + timedelta(hours=4), 104, "kWh")
    second.add_reading(START + timedelta(hours=6), 106, "kWh")
    estimate = outlook([first, second])["estimate"]
    assert estimate["measurement_fallback_reason"] is None
    assert estimate["measured_kwh"] == 7
    assert estimate["measurement_coverage_seconds"] == 7200
    assert estimate["total_kwh"] == 29


@pytest.mark.parametrize("second_source", [False, True])
@pytest.mark.parametrize("after_reset", [False, True])
def test_estimate_preserves_same_source_measurements_across_counter_restart(
    second_source, after_reset
):
    """Ein Zählerneustart entfernt nur seine Lücke, nicht zuvor belegte Energie."""
    history = measurements()
    history.add_reading(START + timedelta(hours=6), 104, "kWh")
    history.add_reading(START + timedelta(hours=7), 0, "kWh")
    if after_reset:
        history.add_reading(START + timedelta(hours=8), 2, "kWh")
    histories = [history]
    if second_source:
        other = measurements("b")
        other.add_reading(START + timedelta(hours=6), 103, "kWh")
        other.add_reading(START + timedelta(hours=7), 104, "kWh")
        other.add_reading(START + timedelta(hours=8), 105, "kWh")
        histories.append(other)

    now = START + timedelta(hours=8)
    result = outlook(histories, now=now, fetched_at=now)
    estimate = result["estimate"]
    measured = 4 + 3 * second_source + (2 + second_source) * after_reset
    assert estimate["status"] == "available"
    assert estimate["basis"] == "measurements_and_forecast"
    assert estimate["measured_kwh"] == measured
    assert estimate["measurement_coverage_seconds"] == (6 + after_reset) * 3600
    assert estimate["estimated_past_kwh"] == 2 - after_reset
    assert estimate["remaining_kwh"] == 16
    assert estimate["total_kwh"] == measured + 18 - after_reset
    if not after_reset:
        assert estimate["measured_kwh"] == result["measured_kwh"]
        assert estimate["total_kwh"] == result["total_kwh"] == 22 + 3 * second_source
    else:
        # Die Schätzung hebt die strengere Forderung nach einem Messpräfix nicht auf.
        assert result["status"] == "unavailable"
        assert result["reason"] == "incomplete_measurements"


@pytest.mark.parametrize(
    "case,reason",
    [
        ("none", "no_energy_sources"),
        ("power_only", "no_energy_sources"),
        ("no_readings", "no_usable_measurements"),
        ("empty_source", "no_usable_measurements"),
        ("unresolved", "unresolved_measurement_identity"),
        ("offset", "no_common_measurement_boundary"),
    ],
)
def test_estimate_without_usable_measurements_is_full_daily_forecast(case, reason):
    """Fehlende gemeinsame Messgrenzen sperren die vorhandene Tagesprognose nicht."""
    first = measurements()
    first.add_reading(NOON, 108, "kWh")
    second = measurements("b", start=START + timedelta(minutes=1))
    second.add_reading(NOON - timedelta(minutes=1), 109, "kWh")
    histories = [] if case == "none" else [first, second]
    if case == "power_only":
        histories = [SourceHistory(replace(first.source, kind="power"), "UTC", 20)]
    if case == "no_readings":
        histories = [SourceHistory(first.source, "UTC", 20)]
    if case == "empty_source":
        histories = [first, measurements("b")]
    estimate = outlook(histories, identity_unresolved=case == "unresolved")["estimate"]
    assert estimate["status"] == "available"
    assert estimate["basis"] == "forecast_only"
    assert estimate["measurement_fallback_reason"] == reason
    assert estimate["reason"] is None
    assert estimate["measured_kwh"] is None
    assert estimate["total_kwh"] == 24


@pytest.mark.parametrize("night_plateau", [False, True])
def test_offset_sources_keep_observed_energy_and_explain_forecast_only(night_plateau):
    """Belegte Einzelmengen bleiben erhalten, ohne Gesamtprognose doppelt zu zählen."""
    zone = "Europe/Berlin"
    start = datetime(2026, 9, 9, tzinfo=ZoneInfo(zone)).astimezone(UTC)
    now = start + timedelta(hours=12, minutes=45)
    histories = []
    for source_id, offset, step in [("a", 0, 0.5), ("b", 30, 0.25)]:
        history = SourceHistory(
            SourceConfig(
                source_id,
                f"sensor.{source_id}",
                "total",
                "AC-PV",
                max_interval_minutes=65,
            ),
            zone,
            20,
        )
        for hour in range(-1 if night_plateau else 0, 13):
            produced_hours = max(0, hour - 6) if night_plateau else hour
            history.add_reading(
                start + timedelta(hours=hour, minutes=offset),
                100 + produced_hours * step,
                "kWh",
            )
        histories.append(history)
    original = [history.to_dict() for history in histories]
    total = aggregate_energy(histories, start, now, now)
    result = build_day_outlook(
        forecast([1] * 24, start), histories, zone, now, now, True
    )
    estimate = result["estimate"]
    assert total["energy_kwh"] == (4.5 if night_plateau else 9)
    assert total["source_count"] == 2
    assert "stale" not in total["quality_flags"]
    assert result["status"] == "unavailable"
    assert result["reason"] == "no_common_measurement_boundary"
    assert result["measured_kwh"] is None
    assert result["total_kwh"] is None
    assert estimate["status"] == "available"
    assert estimate["reason"] is None
    assert estimate["basis"] == "forecast_only"
    assert estimate["measurement_fallback_reason"] == "no_common_measurement_boundary"
    assert estimate["measured_kwh"] is None
    assert estimate["measurement_coverage_seconds"] == 0
    assert estimate["estimated_past_kwh"] == 12.75
    assert estimate["remaining_kwh"] == 11.25
    assert estimate["total_kwh"] == 24
    assert [history.to_dict() for history in histories] == original
    assert aggregate_energy(histories, start, now, now) == total


@pytest.mark.parametrize(
    "case", ["daily_correction", "derived_gap", "former_identity", "positive_boundary"]
)
def test_discarded_differences_do_not_claim_missing_common_boundaries(case):
    """Nicht nutzbare Rohdifferenzen werden nicht als zeitversetzte Messung erklärt."""
    first = measurements()
    first.add_reading(NOON, 108, "kWh")
    source = replace(first.source, source_id="b", entity_id="sensor.b")
    if case == "daily_correction":
        source = replace(source, kind="daily")
    if case == "derived_gap":
        source = replace(source, derived_energy=True)
    second = SourceHistory(source, "UTC", 20)
    second.add_reading(
        START - timedelta(hours=1) if case == "positive_boundary" else START,
        100,
        "kWh",
    )
    second.add_reading(NOON - timedelta(hours=1), 108, "kWh")
    if case == "daily_correction":
        second.add_reading(NOON, 104, "kWh")
    if case == "former_identity":
        second.replace_source(replace(source, entity_id="sensor.replacement"))
    original = [history.to_dict() for history in [first, second]]
    assert second.deltas
    estimate = outlook([first, second])["estimate"]
    assert estimate["status"] == "available"
    assert estimate["measurement_fallback_reason"] == "no_usable_measurements"
    assert estimate["basis"] == "forecast_only"
    assert estimate["measured_kwh"] is None
    assert estimate["total_kwh"] == 24
    assert [history.to_dict() for history in [first, second]] == original


@pytest.mark.parametrize("has_source", [False, True])
def test_estimate_at_midnight_has_no_measurement_period(has_source):
    """Ein noch leerer Tag behauptet weder Meldeversatz noch eine gemessene Null."""
    estimate = outlook([measurements()] if has_source else [], now=START)["estimate"]
    assert estimate["measurement_fallback_reason"] == (
        "no_usable_measurements" if has_source else "no_energy_sources"
    )
    assert estimate["measured_kwh"] is None
    assert estimate["estimated_past_kwh"] == 0
    assert estimate["total_kwh"] == 24


def test_estimate_uses_current_location_only_and_never_splits_positive_delta():
    history = measurements()
    history.bind_location("old", "UTC", START)
    history.add_reading(START + timedelta(hours=6), 108, "kWh")
    history.bind_location("new", "UTC", START + timedelta(hours=7))
    history.add_reading(START + timedelta(hours=7), 108, "kWh")
    history.add_reading(NOON, 110, "kWh")
    estimate = outlook([history.current_location_view()])["estimate"]
    assert estimate["measured_kwh"] == 2
    assert estimate["total_kwh"] == 21

    crossing = measurements(start=START - timedelta(hours=1))
    crossing.add_reading(START + timedelta(hours=1), 104, "kWh")
    crossing.add_reading(START + timedelta(hours=2), 106, "kWh")
    estimate = outlook([crossing])["estimate"]
    assert estimate["measured_kwh"] == 2
    assert estimate["total_kwh"] == 25


def test_estimate_discards_former_source_and_daily_corrections():
    """Frühere Messgrenzen und zurückgenommene Tageswerte bleiben ausgeschlossen."""
    history = measurements()
    history.add_reading(START + timedelta(hours=4), 109, "kWh")
    history.replace_source(replace(history.source, entity_id="sensor.replacement"))
    history.add_reading(START + timedelta(hours=5), 200, "kWh")
    history.add_reading(START + timedelta(hours=7), 202, "kWh")
    estimate = outlook([history])["estimate"]
    assert estimate["measured_kwh"] == 2
    assert estimate["total_kwh"] == 24

    daily = SourceHistory(replace(history.source, kind="daily"), "UTC", 20)
    daily.add_reading(START, 0, "kWh")
    daily.add_reading(START + timedelta(hours=5), 8, "kWh")
    daily.add_reading(START + timedelta(hours=6), 4, "kWh")
    estimate = outlook([daily])["estimate"]
    assert estimate["basis"] == "forecast_only"
    assert estimate["measurement_fallback_reason"] == "no_usable_measurements"
    assert estimate["total_kwh"] == 24


def test_estimate_preserves_registry_identity_after_rename_and_counter_restart():
    """Eine umbenannte Registry-Entity behält auch ihre früheren Zählersegmente."""
    source = replace(measurements().source, registry_id="stable-registry")
    history = SourceHistory(source, "UTC", 20)
    history.add_reading(START, 100, "kWh")
    history.add_reading(START + timedelta(hours=6), 104, "kWh")
    history.add_reading(START + timedelta(hours=7), 0, "kWh")
    history.replace_source(replace(source, entity_id="sensor.renamed"))

    estimate = outlook([history])["estimate"]
    assert estimate["measured_kwh"] == 4
    assert estimate["total_kwh"] == 22


@pytest.mark.parametrize("hours", [0, 2])
def test_estimate_keeps_forecast_age_and_input_fallbacks_visible(hours):
    data = forecast([1] * 24, START)
    data = replace(
        data,
        total_intervals=tuple(
            replace(interval, quality_flags=("missing_temperature",))
            for interval in data.total_intervals
        ),
    )
    estimate = outlook([], data=data, fetched_at=NOON - timedelta(hours=hours))[
        "estimate"
    ]
    assert estimate["status"] == "available"
    assert estimate["total_kwh"] == 24
    assert estimate["forecast_stale"] is bool(hours)
    assert estimate["forecast_quality_flags"] == ["missing_temperature"]


def test_estimate_needs_forecast_only_outside_measured_sections():
    history = measurements(start=START + timedelta(hours=8))
    history.add_reading(START + timedelta(hours=10), 104, "kWh")
    data = forecast([1] * 24, START)
    data = replace(
        data, total_intervals=data.total_intervals[:8] + data.total_intervals[10:]
    )
    estimate = outlook([history], data=data)["estimate"]
    assert estimate["status"] == "available"
    assert estimate["total_kwh"] == 26
    data = replace(data, total_intervals=data.total_intervals[1:])
    estimate = outlook([history], data=data)["estimate"]
    assert estimate["status"] == "unavailable"
    assert estimate["reason"] == "incomplete_forecast"
    assert estimate["total_kwh"] is None
    assert estimate["measured_kwh"] == 4


@pytest.mark.parametrize(
    "zone_name,day,hours",
    [
        ("Europe/Berlin", datetime(2026, 3, 29), 23),
        ("Europe/Berlin", datetime(2026, 10, 25), 25),
        ("Asia/Kolkata", datetime(2026, 9, 11), 24),
    ],
)
@pytest.mark.parametrize("power", [0, 1])
def test_estimate_without_measurements_respects_local_day_and_zero(
    zone_name, day, hours, power
):
    zone = ZoneInfo(zone_name)
    start = day.replace(tzinfo=zone).astimezone(UTC)
    now = start + timedelta(hours=10, minutes=49)
    result = build_day_outlook(
        forecast([power] * hours, start), [], zone_name, now, now, True
    )
    estimate = result["estimate"]
    assert estimate["status"] == "available"
    assert estimate["total_kwh"] == pytest.approx(power * hours)
    assert estimate["estimated_past_kwh"] == pytest.approx(power * (10 + 49 / 60))
