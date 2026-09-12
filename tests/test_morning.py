"""Synthetische Mechanik des Morgenversuchs, kein Nachweis realer Prognosegüte."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_forecast import morning
from custom_components.pv_forecast.history import (
    ArchiveRecord,
    Assessment,
    SourceAssessment,
)
from custom_components.pv_forecast.measurements import (
    SourceConfig,
    SourceHistory,
)
from custom_components.pv_forecast.models import (
    AcInverterGroup,
    DailyYield,
    ForecastBasisInterval,
    ForecastCalibrationBasis,
    ForecastResult,
    PvRoof,
    RoofForecast,
    RoofForecastInterval,
    TotalForecastInterval,
)
from custom_components.pv_forecast.morning import (
    MorningState,
    apply_morning,
    evaluate_morning,
    measured_morning,
    morning_window,
)

SOURCE = SourceConfig("pv", "sensor.pv", "total", "plant", registry_id="registry")
HOUR = timedelta(hours=1)
QUARTER = timedelta(minutes=15)
START = date(2026, 1, 2)


def instant(day, hour=0):
    return datetime.combine(day, time(hour), UTC)


def fixed_window(day, timezone, latitude, longitude):
    return instant(day, 6), instant(day, 10)


@pytest.fixture
def fixed_sun(monkeypatch):
    monkeypatch.setattr(morning, "morning_window", fixed_window)


def forecast(day, powers=None, groups=()):
    start = instant(day)
    powers = powers or [1.0] * 24
    roof = PvRoof("roof", "Dach", 10, 180, 30, 0.1)
    intervals = tuple(
        RoofForecastInterval(
            start + index * HOUR, start + (index + 1) * HOUR, power, power, power
        )
        for index, power in enumerate(powers)
    )
    total = tuple(
        TotalForecastInterval(item.start, item.end, item.energy_kwh, item.ac_power_kw)
        for item in intervals
    )
    return ForecastResult(
        day - timedelta(days=1),
        {"roof": RoofForecast(roof, intervals, DailyYield(0, sum(powers)))},
        DailyYield(0, sum(powers)),
        total,
        groups,
    )


def measurement_history(day, step=QUARTER, powers=None, source=SOURCE):
    history = SourceHistory(source, "UTC", 20, "segment")
    value = 0
    start = instant(day)
    history.add_reading(start, value, "kWh")
    count = int(timedelta(days=1) / step)
    for index in range(count):
        left, right = start + index * step, start + (index + 1) * step
        power = powers[index] if powers else 1.1 if 6 <= left.hour < 10 else 1.0
        value += power * step.total_seconds() / 3600
        history.add_reading(right, value, "kWh")
    return history


def record(day, actual=24.4):
    start = instant(day)
    return ArchiveRecord(
        day.isoformat(),
        start,
        start + timedelta(days=1),
        day,
        "daily_previous_18",
        start - 6 * HOUR,
        start - 7 * HOUR,
        start - 7 * HOUR,
        "UTC",
        "plant",
        (SOURCE,),
        24,
        (),
        assessment=Assessment(
            start + timedelta(days=1),
            actual,
            True,
            (),
            (SourceAssessment("pv", actual, ("segment",), ()),),
        ),
    )


def capture(state, day):
    observed = instant(day) - 7 * HOUR
    return state.capture(
        forecast(day), observed, observed, (SOURCE,), latitude=50, longitude=10
    )


def learning_state(days=45, actual_day_energy=24.4):
    state = MorningState("plant", instant(START - timedelta(days=1)))
    records = []
    history = SourceHistory(SOURCE, "UTC", 20, "segment")
    value = 0.0
    history.add_reading(instant(START), value, "kWh")
    capture(state, START)
    for offset in range(days):
        day = START + timedelta(days=offset)
        for index in range(96):
            left = instant(day) + index * QUARTER
            power = 1.1 if 6 <= left.hour < 10 else 1.0
            value += power / 4
            history.add_reading(left + QUARTER, value, "kWh")
        records.append(record(day, actual_day_energy))
        capture(state, day + timedelta(days=1))
        now = instant(day + timedelta(days=1)) + timedelta(seconds=1)
        history.readings = [
            item
            for item in history.readings
            if item.timestamp >= now - timedelta(days=7)
        ]
        history.deltas = [
            item for item in history.deltas if item.end >= now - timedelta(days=7)
        ]
        state.reconcile((history,), records, now)
    return state, records, history, now


def test_sunrise_and_dst_are_absolute():
    zone = ZoneInfo("Europe/Berlin")
    for day in (date(2026, 3, 29), date(2026, 10, 25)):
        start, end = morning_window(day, zone, 52.5, 13.4)
        assert start.tzinfo == UTC
        assert start.astimezone(zone).date() == day
        assert end - start == 4 * HOUR
        assert morning.solar_position(start, 52.5, 13.4)[0] >= 0
    assert morning_window(date(2026, 12, 21), zone, 89, 0) is None
    assert morning_window(date(2026, 6, 21), zone, 89, 0) is None


def test_original_resolution_and_missing_values_are_not_zero():
    window = (instant(START, 6), instant(START, 10))
    fine = measurement_history(START)
    measured = measured_morning(
        (fine,), (SOURCE,), window, instant(START + timedelta(days=1))
    )
    assert measured["valid"]
    assert measured["morning_energy_kwh"] == pytest.approx(4.4)
    assert measured["rise"] == instant(START, 6).isoformat()
    assert measured["device_timestamp_verified"] is False
    coarse = measurement_history(START, HOUR)
    assert (
        measured_morning(
            (coarse,), (SOURCE,), window, instant(START + timedelta(days=1))
        )["reason"]
        == "measurement_resolution_too_coarse"
    )
    fine.deltas = [item for item in fine.deltas if item.start != instant(START, 7)]
    assert not measured_morning(
        (fine,), (SOURCE,), window, instant(START + timedelta(days=1))
    )["valid"]


def test_multiple_sources_need_common_unsplit_boundaries():
    second = replace(
        SOURCE, source_id="second", entity_id="sensor.other", registry_id="other"
    )
    first_history = measurement_history(START)
    second_history = measurement_history(START, source=second)
    window = (
        instant(START, 6) + timedelta(minutes=2),
        instant(START, 10) + timedelta(minutes=2),
    )
    measured = measured_morning(
        (first_history, second_history),
        (SOURCE, second),
        window,
        instant(START + timedelta(days=1)),
    )
    assert measured["valid"]
    assert measured["edge_unassessed_minutes"] == [13, 2]
    assert measured["morning_energy_kwh"] == pytest.approx(8.25)
    second_history.deltas = [
        replace(
            item,
            start=item.start + timedelta(minutes=1),
            end=item.end + timedelta(minutes=1),
        )
        for item in second_history.deltas
    ]
    assert not measured_morning(
        (first_history, second_history),
        (SOURCE, second),
        window,
        instant(START + timedelta(days=1)),
    )["valid"]


def test_freezing_never_backfills_or_retimes_unchanged_observations(fixed_sun):
    state = MorningState("plant", instant(START - timedelta(days=1)))
    assert capture(state, START)
    original = state.to_dict()
    before = instant(START) - 7 * HOUR
    assert not state.capture(
        forecast(START),
        before,
        before + timedelta(minutes=1),
        (SOURCE,),
        latitude=50,
        longitude=10,
    )
    assert state.to_dict() == original
    assert not state.capture(
        forecast(START),
        before,
        instant(START) - 5 * HOUR,
        (SOURCE,),
        latitude=50,
        longitude=10,
    )
    assert state.to_dict() == original
    restored = MorningState.from_dict(original)
    assert restored.to_dict() == original
    assert not restored.capture(
        forecast(START + timedelta(days=1)),
        instant(START, 15),
        instant(START, 17),
        (SOURCE,),
        latitude=50,
        longitude=10,
    )


def test_factor_before_both_clippings_and_preserved_dc(fixed_sun):
    group = AcInverterGroup("inverter", "Gerät", 1.6, ("roof",))
    original = forecast(START, groups=(group,))
    result = apply_morning(original, 1.2, 1.5, 1.7, ZoneInfo("UTC"), 50, 10)
    at_six = next(
        item for item in result.total_intervals if item.start == instant(START, 6)
    )
    assert at_six.ac_power_kw == 1.6
    at_twelve = next(
        item for item in result.total_intervals if item.start == instant(START, 12)
    )
    assert at_twelve.ac_power_kw == 1.5
    assert all(item.dc_power_kw == 1 for item in result.roofs["roof"].intervals)
    basis = ForecastCalibrationBasis(
        (ForecastBasisInterval(instant(START, 6), instant(START, 7), 1, (1,), 0),),
        1.7,
        (("inverter", 1.6),),
    )
    assert (
        morning._basis_series(basis, fixed_window(START, None, 0, 0), 1.5, 1.2)[
            0
        ].energy_kwh
        == 1.6
    )
    assert apply_morning(original, 1, 1, 1.7, ZoneInfo("UTC"), 50, 10) is original


def evaluated_case(raw_rise, baseline_rise, candidate_rise, actual_rise):
    def feature(energy, rise):
        return {
            "morning_energy_kwh": energy,
            "day_energy_kwh": 24,
            "rise": rise.isoformat() if rise else None,
        }

    return {
        "raw": feature(4, raw_rise),
        "baseline": feature(4, baseline_rise),
        "candidate": feature(4.4, candidate_rise),
        "measurement": feature(4.4, actual_rise),
    }


def test_equal_day_energy_does_not_hide_wrong_morning_shape():
    actual = instant(START, 8)
    early = instant(START, 6)
    result = evaluate_morning([evaluated_case(early, early, actual, actual)])
    assert result["approved"]
    assert result["baseline_day_mae_kwh"] == result["candidate_day_mae_kwh"] == 0
    assert result["baseline_rise_mae_minutes"] == 120
    assert result["candidate_rise_mae_minutes"] == 0
    assert result["baseline_early_p90_minutes"] == 120


def test_absent_rises_stay_in_same_sample_and_cannot_approve_alone():
    rise = instant(START, 7)
    cases = [
        evaluated_case(rise, rise, rise, rise),
        evaluated_case(None, None, rise, None),
        evaluated_case(None, None, None, None),
    ]
    result = evaluate_morning(cases)
    assert not result["approved"]
    assert result["sample_days"] == 3
    assert result["candidate_false_rise_rate"] == pytest.approx(1 / 3)
    assert result["baseline_neither_rises"] == 2
    assert "false_rise_rate_worse" in result["reasons"]
    none = evaluate_morning([evaluated_case(None, None, None, None)])
    assert "rise_metric_unavailable" in none["reasons"]
    zero = evaluated_case(rise, rise, rise, rise)
    zero["baseline"]["morning_energy_kwh"] = 4.4
    assert not evaluate_morning([zero])["approved"]


def test_full_prospective_approval_observation_and_revocation(fixed_sun):
    state, records, history, now = learning_state()
    assert state.status == "approved", state.snapshot()
    assert state.training_days == 30
    assert state.validation_days == 14
    assert state.approved_factor == pytest.approx(1.1)
    assert state.snapshot("observe")["effective_factor"] == 1
    assert state.snapshot("auto")["effective_factor"] == pytest.approx(1.1)
    assert state.snapshot("off")["applied"] is False
    restored = MorningState.from_dict(state.to_dict())
    assert restored.snapshot("auto") == state.snapshot("auto")
    changed = replace(
        records[-1],
        assessment=replace(
            records[-1].assessment, actual_energy_kwh=20, assessed_at=now + HOUR
        ),
    )
    restored.reconcile((history,), [*records[:-1], changed], now + HOUR)
    assert restored.status == "invalidated"
    assert restored.approved_factor == 1
    # Ein unbekannter späterer Tag ersetzt keine aktuelle Prospektivprüfung.
    state.reconcile((), records, now + timedelta(days=29))
    assert state.approved_factor == 1


def test_no_retrospective_learning_and_source_deletion(fixed_sun):
    state = MorningState("plant", instant(START - timedelta(days=1)))
    history = measurement_history(START)
    state.reconcile((history,), [record(START)], instant(START + timedelta(days=1)))
    assert state.training_days == 0
    assert not capture(state, START)
    assert capture(state, START + timedelta(days=2))
    assert state.sources == (SOURCE,)
    assert state.delete_measurement_source("pv")
    assert not state.cases and not state.sources


def test_changed_or_newly_missing_morning_evidence_revokes(fixed_sun):
    state, records, history, now = learning_state()
    history.deltas = [
        item
        for item in history.deltas
        if item.start != instant(records[-1].target_date, 7)
    ]
    state.reconcile((history,), records, now + HOUR)
    assert state.status == "invalidated"
    assert state.approved_factor == 1


def test_storage_is_detached_bounded_and_version_checked(fixed_sun):
    state = MorningState("plant", instant(START - timedelta(days=1)))
    capture(state, START)
    stored = state.to_dict()
    state.cases[START.isoformat()]["global_factor"] = 1.1
    assert stored["cases"][0]["global_factor"] == 1
    for mutation in (
        {"rule_version": 2},
        {"cases": stored["cases"] * 97},
        {"padding": "x" * morning.MAX_BYTES},
    ):
        with pytest.raises(ValueError):
            MorningState.from_dict({**stored, **mutation})
    assert state.prune(instant(START + timedelta(days=97)))
    assert not state.cases and state.retention_truncated
    broken = deepcopy(stored)
    broken["cases"][0]["observed_at"] = instant(START).isoformat()
    with pytest.raises(ValueError):
        MorningState.from_dict(broken)


def test_unknown_measurement_quality_is_not_accepted():
    history = measurement_history(START)
    history.deltas = [
        (
            replace(item, quality_flags=frozenset({"gap"}))
            if item.start == instant(START, 7)
            else item
        )
        for item in history.deltas
    ]
    assert not measured_morning(
        (history,),
        (SOURCE,),
        fixed_window(START, None, 0, 0),
        instant(START + timedelta(days=1)),
    )["valid"]
    assert not measured_morning(
        (history,),
        (replace(SOURCE, registry_id="changed"),),
        fixed_window(START, None, 0, 0),
        instant(START + timedelta(days=1)),
    )["valid"]


def test_multiday_forecast_preserves_global_only_after_tomorrow(fixed_sun):
    original = replace(forecast(START), local_date=START, forecast_days=7)
    intervals = tuple(
        replace(
            item,
            start=item.start + timedelta(days=offset),
            end=item.end + timedelta(days=offset),
        )
        for offset in range(7)
        for item in original.roofs["roof"].intervals
    )
    totals = tuple(
        TotalForecastInterval(item.start, item.end, item.energy_kwh, item.ac_power_kw)
        for item in intervals
    )
    original = replace(
        original,
        roofs={"roof": replace(original.roofs["roof"], intervals=intervals)},
        total_intervals=totals,
    )
    changed = apply_morning(original, 1.1, 1.2, None, ZoneInfo("UTC"), 50, 10)
    for offset in range(7):
        interval = next(
            item
            for item in changed.total_intervals
            if item.start == instant(START + timedelta(days=offset), 6)
        )
        assert interval.ac_power_kw == pytest.approx(1.32 if offset < 2 else 1.2)


def test_sustained_interval_means_need_sixty_minutes_without_gaps():
    start = instant(START, 6)
    three = [
        (start + index * QUARTER, start + (index + 1) * QUARTER, 0.5)
        for index in range(3)
    ]
    assert morning._rise(three) is None
    four = [*three, (start + 3 * QUARTER, start + 4 * QUARTER, 0.5)]
    assert morning._rise(four) == start
    four[1] = (*four[1][:2], 0.49)
    assert morning._rise(four) is None


def test_corrupt_approval_is_not_trusted(fixed_sun):
    state, _, _, _ = learning_state()
    original = state.to_dict()
    assert original["status"] == "approved"
    bad = deepcopy(original)
    bad["candidate"]["factor"] = 1.2
    with pytest.raises(ValueError):
        MorningState.from_dict(bad)
    bad = deepcopy(original)
    training = next(iter(bad["candidate"]["training"]))
    next(item for item in bad["cases"] if item["date"] == training)["measurement"][
        "observed_at"
    ] = instant(START + timedelta(days=70)).isoformat()
    with pytest.raises(ValueError):
        MorningState.from_dict(bad)
    assert state.prune(instant(START + timedelta(days=97)))
    assert state.approved_factor == 1


def test_persisted_size_includes_native_indentation(fixed_sun, monkeypatch):
    state = MorningState("plant", instant(START - timedelta(days=1)))
    capture(state, START)
    compact = len(morning.json.dumps(state.to_dict()).encode())
    pretty = morning._storage_size(state.to_dict())
    assert pretty > compact
    monkeypatch.setattr(morning, "MAX_BYTES", (compact + pretty) // 2)
    assert state.prune(instant(START))
    assert not state.cases


def test_model_version_and_assessment_knowledge_must_match(fixed_sun):
    state = MorningState("plant", instant(START - timedelta(days=1)))
    capture(state, START)
    future_record = replace(
        record(START),
        assessment=replace(
            record(START).assessment, assessed_at=instant(START + timedelta(days=2))
        ),
    )
    state.reconcile(
        (measurement_history(START),),
        [future_record],
        instant(START + timedelta(days=1)),
    )
    assert state.training_days == 0
    unknown_model = replace(record(START), model_version="future")
    state.reconcile(
        (measurement_history(START),),
        [unknown_model],
        instant(START + timedelta(days=1)),
    )
    assert state.training_days == 0
    stored = state.to_dict()
    stored["cases"][0]["model_version"] = "future"
    with pytest.raises(ValueError):
        MorningState.from_dict(stored)


def test_modified_measured_copy_cannot_turn_rejection_into_approval(fixed_sun):
    state, records, history, now = learning_state(actual_day_energy=20)
    assert state.status == "rejected"
    assert "day_mae_kwh_worse" in state.reasons
    stored = state.to_dict()
    stored["status"] = "approved"
    stored["metrics"]["approved"] = True
    for case in stored["cases"]:
        if case["measurement"] is not None:
            case["measurement"]["day_energy_kwh"] = 24.4
    with pytest.raises(ValueError):
        MorningState.from_dict(stored)
    # Auch bereits im Speicher veränderte Kopien müssen gegen die echte
    # Tagesbewertung geprüft werden, deren alter Fingerprint unverändert bleibt.
    state.status = "approved"
    state.metrics["approved"] = True
    for case in state.cases.values():
        if case["measurement"] is not None:
            case["measurement"]["day_energy_kwh"] = 24.4
    state.reconcile((history,), records, now + HOUR)
    assert state.status == "invalidated"
    assert state.approved_factor == 1


def test_complete_measurement_content_is_bound_to_training_references(fixed_sun):
    state, _, _, _ = learning_state()
    stored = state.to_dict()
    training_date = next(iter(stored["candidate"]["training"]))
    case = next(case for case in stored["cases"] if case["date"] == training_date)
    measured = case["measurement"]
    left, right, power = measured["intervals"][0]
    measured["intervals"][0][2] = power + 0.01
    measured["morning_energy_kwh"] += (
        0.01
        * (datetime.fromisoformat(right) - datetime.fromisoformat(left)).total_seconds()
        / 3600
    )
    # Summen und Anstieg sind weiterhin intern konsistent, aber die vor der
    # Prüfung verwendete Messrevision darf so nicht unbemerkt geändert werden.
    with pytest.raises(ValueError):
        MorningState.from_dict(stored)
