"""Issue #172: Zeitform, echte Messgrenzen und kausale Morgenfreigabe."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from math import fsum

import pytest

from custom_components.pv_forecast.calculations import forecast_basis
from custom_components.pv_forecast.models import (
    ForecastBasisInterval,
    ForecastCalibrationBasis,
    TotalForecastInterval,
)
from custom_components.pv_forecast.morning import (
    COEFFICIENTS,
    MorningCase,
    MorningLearning,
    _error,
    apply_morning,
    approved,
    measured_trace,
    metrics,
    morning_start,
    projected_basis,
    rise,
)

from .test_history import forecast

DAWN = datetime(2026, 3, 1, 6, tzinfo=UTC)


def case(offset=0, *, actual_coefficient=0.5, trial_id=None):
    dawn = DAWN + timedelta(days=offset)
    start = dawn.replace(hour=0)
    powers = [0] * 6 + [0.4, 0.7, 1.2, 1.5] + [2] * 5 + [0] * 9
    basis = ForecastCalibrationBasis(
        tuple(
            ForecastBasisInterval(
                start + timedelta(hours=i), start + timedelta(hours=i + 1), value
            )
            for i, value in enumerate(powers)
        ),
        None,
    )
    actual = projected_basis(basis, dawn, actual_coefficient, 1.0)
    measured = tuple(
        TotalForecastInterval(
            dawn + timedelta(minutes=i * 5),
            dawn + timedelta(minutes=(i + 1) * 5),
            next(
                x.ac_power_kw
                for x in actual
                if x.start <= dawn + timedelta(minutes=i * 5) < x.end
            )
            / 12,
            next(
                x.ac_power_kw
                for x in actual
                if x.start <= dawn + timedelta(minutes=i * 5) < x.end
            ),
        )
        for i in range(48)
    )
    return MorningCase(
        str(offset),
        dawn.date(),
        start - timedelta(hours=6),
        f"evidence-{offset}",
        dawn,
        basis,
        1,
        measured,
        fsum(x.energy_kwh for x in actual),
        trial_id,
    )


def test_same_daily_energy_different_morning_timing():
    """Eine reine Tagesbilanz erkennt den zwei Stunden früheren Anstieg nicht."""
    item = case()
    base, candidate = metrics([item], 0, 0.5), metrics([item], 0.5, 0.5)
    assert base["day_mae_kwh"] == pytest.approx(0)
    assert candidate["day_mae_kwh"] == pytest.approx(0)
    assert base["time_mae_minutes"] > 0
    assert candidate["time_mae_minutes"] == 0
    assert candidate["morning_mae_kwh"] == pytest.approx(0)


@pytest.mark.parametrize("coefficient", COEFFICIENTS)
def test_profile_moves_energy_within_morning_and_preserves_daily_dc(coefficient):
    item = case()
    values = projected_basis(item.basis, item.dawn, coefficient, 1.0)
    assert fsum(i.energy_kwh for i in values) == pytest.approx(item.day_actual_kwh)
    for original, changed in zip(item.basis.intervals, values, strict=True):
        if original.start < item.dawn or original.start >= item.dawn + timedelta(
            hours=4
        ):
            assert changed.ac_power_kw == original.dc_power_kw
        elif original.dc_power_kw:
            assert 0.5 <= changed.ac_power_kw / original.dc_power_kw <= 1.5 + 1e-12


def test_group_limits_and_global_factor_are_applied_once():
    item = case()
    basis = ForecastCalibrationBasis(
        tuple(
            replace(
                i,
                group_dc_power_kw=(i.dc_power_kw * 0.8,),
                ungrouped_dc_power_kw=i.dc_power_kw * 0.2,
            )
            for i in item.basis.intervals
        ),
        1.0,
        (("a", 0.5),),
        True,
    )
    result = projected_basis(basis, item.dawn, 0.5, 1.2)
    assert all(i.ac_power_kw <= 1.0 for i in result)
    noon = next(i for i in result if i.start.hour == 12)
    assert noon.ac_power_kw == pytest.approx(0.98)


def test_live_series_matches_pure_candidate_for_all_roofs(monkeypatch):
    """Operative Kurve, Lernkurve und Dachsumme bleiben dieselbe AC-Rechnung."""
    raw = forecast(DAWN.date(), timezone="UTC", dc_power=1)
    monkeypatch.setattr(
        "custom_components.pv_forecast.morning.morning_start",
        lambda day, *_: datetime.combine(day, DAWN.timetz()),
    )
    result = apply_morning(
        raw,
        raw,
        coefficient=0.5,
        global_factor=1,
        limit=1.0,
        timezone="UTC",
        latitude=48,
        longitude=11,
    )
    start = DAWN.replace(hour=0)
    basis = forecast_basis(raw, start, start + timedelta(days=1), 1.0)
    assert basis is not None
    expected = projected_basis(basis, DAWN, 0.5, 1)
    observed = [i for i in result.total_intervals if i.start.date() == DAWN.date()]
    assert [i.energy_kwh for i in observed] == pytest.approx(
        [i.energy_kwh for i in expected]
    )
    for interval in result.total_intervals:
        roofs = [
            i
            for r in result.roofs.values()
            for i in r.intervals
            if i.start == interval.start and i.end == interval.end
        ]
        assert interval.energy_kwh == pytest.approx(fsum(i.energy_kwh for i in roofs))
    assert raw is apply_morning(
        raw,
        raw,
        coefficient=0,
        global_factor=1,
        limit=None,
        timezone="UTC",
        latitude=48,
        longitude=11,
    )


@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
def test_sunrise_uses_actual_utc_on_dst_days(day):
    dawn = morning_start(day, "Europe/Berlin", 48.1, 11.5)
    assert dawn is not None and dawn.tzinfo is UTC
    assert 3 <= dawn.hour <= 7


def test_polar_day_has_no_invented_sunrise():
    assert morning_start(date(2026, 6, 21), "Europe/Oslo", 89, 15) is None


def evidence(*, step=5, energy=0.05, source="s"):
    return {
        "sources": [
            {
                "source_id": source,
                "kind": "total",
                "identity_unresolved": False,
                "deltas": [
                    {
                        "start": (DAWN + timedelta(minutes=i)).isoformat(),
                        "end": (DAWN + timedelta(minutes=i + step)).isoformat(),
                        "energy_kwh": energy,
                        "quality_flags": [],
                        "segment_id": "one",
                    }
                    for i in range(0, 240, step)
                ],
            }
        ]
    }


def test_true_ac_deltas_sum_without_interpolation():
    trace = measured_trace(evidence(), {"s"}, DAWN)
    assert len(trace) == 48
    assert fsum(i.energy_kwh for i in trace) == pytest.approx(2.4)
    assert rise(trace, 0.5) == DAWN


def test_two_confirmed_sources_aggregate_on_common_real_boundaries():
    data = evidence()
    data["sources"] += evidence(step=10, energy=0.1, source="s2")["sources"]
    trace = measured_trace(data, {"s", "s2"}, DAWN)
    assert len(trace) == 24
    assert all(i.ac_power_kw == pytest.approx(1.2) for i in trace)


@pytest.mark.parametrize(
    "bad", ["gap", "coarse", "negative", "duplicate", "source", "identity", "segment"]
)
def test_missing_or_bad_measurements_are_never_zero(bad):
    data = evidence()
    values = data["sources"][0]["deltas"]
    if bad == "gap":
        values[4]["quality_flags"] = ["gap"]
    if bad == "coarse":
        data = evidence(step=60)
    if bad == "negative":
        values[4]["energy_kwh"] = -1
    if bad == "duplicate":
        values.insert(4, dict(values[4]))
    if bad == "source":
        data["sources"][0]["source_id"] = "wrong"
    if bad == "identity":
        data["sources"][0]["identity_unresolved"] = True
    if bad == "segment":
        values[4]["segment_id"] = "two"
    with pytest.raises(ValueError):
        measured_trace(data, {"s"}, DAWN)


def test_small_boundary_offsets_use_only_actual_complete_deltas():
    data = evidence()
    data["sources"][0]["deltas"] = data["sources"][0]["deltas"][1:-1]
    trace = measured_trace(data, {"s"}, DAWN)
    assert trace[0].start == DAWN + timedelta(minutes=5)
    assert trace[-1].end == DAWN + timedelta(hours=4, minutes=-5)
    assert fsum(i.energy_kwh for i in trace) == pytest.approx(2.3)


def test_no_rise_is_observed_absence_and_false_predictions_are_counted():
    item = case()
    measured = tuple(replace(i, ac_power_kw=0, energy_kwh=0) for i in item.measured)
    errors = _error(replace(item, measured=measured), 0, 0.5)
    assert errors["false_rise"] == 1 and errors["time"] == 240
    assert errors["early"] == 240
    errors = _error(replace(item, measured=measured), 0, 100)
    assert errors["both_absent"] == 1 and errors["time"] == 0


def learned():
    cases = [case(i) for i in range(30)]
    now = DAWN + timedelta(days=30, hours=7)
    state = MorningLearning(DAWN - timedelta(days=2))
    state.update(cases, now, now.date())
    assert state.candidate is not None
    return state, cases, now


def test_30_training_and_14_later_mornings_are_causal_and_disjoint():
    state, training, now = learned()
    assert state.effective_coefficient == 0
    trial_id = state.candidate["id"]
    validation = [case(i, trial_id=trial_id) for i in range(31, 45)]
    before = now + timedelta(days=15)
    state.update(training + validation[:-1], before, before.date())
    assert state.effective_coefficient == 0
    state.update(training + validation, before, before.date())
    assert state.report["status"] == "approved"
    assert state.effective_coefficient > 0
    assert state.report["validation_days"] == 14
    assert set(state.candidate["training"]).isdisjoint(state.candidate["validation"])


def test_retroactive_candidate_values_do_not_validate():
    state, training, now = learned()
    state.update(
        training + [case(i) for i in range(31, 45)],
        now + timedelta(days=15),
        (now + timedelta(days=15)).date(),
    )
    assert state.report["validation_days"] == 0
    assert state.effective_coefficient == 0


def test_corrected_evidence_revokes_candidate_and_sets_cooldown():
    state, training, now = learned()
    changed = [replace(training[0], fingerprint="corrected"), *training[1:]]
    state.update(changed, now, now.date())
    assert state.candidate is None and state.report["status"] == "evidence_changed"
    assert state.retry_after == now + timedelta(days=14)


def test_missing_recent_validation_expires_without_reusing_ancient_days():
    state, training, now = learned()
    later = now + timedelta(days=29)
    state.update(training, later, later.date())
    assert state.candidate is None and state.report["status"] == "expired"


def test_restart_requires_revalidation_of_preserved_evidence():
    state, _training, now = learned()
    restored = MorningLearning.from_dict(state.to_dict())
    assert restored.effective_coefficient == 0
    assert restored.began == state.began
    restored.update([], now, now.date())
    assert restored.candidate is None


@pytest.mark.parametrize(
    "field", ["false_rise", "early_p90_minutes", "morning_mae_kwh", "day_mae_kwh"]
)
def test_secondary_error_gates_block_better_time_with_worse_other_errors(field):
    base = {
        "days": 14,
        "actual_rise": 14,
        "time_mae_minutes": 60,
        "false_rise": 0,
        "early_p90_minutes": 30,
        "morning_mae_kwh": 0,
        "day_mae_kwh": 0,
    }
    candidate = {**base, "time_mae_minutes": 10, field: base[field] + 1}
    assert not approved(base, candidate)


def test_perfect_time_baseline_or_no_actual_rise_cannot_claim_benefit():
    item = case()
    base = metrics([item] * 14, 0.5, 0.5)
    assert not approved(base, base)
    base = metrics([item] * 14, 0, 100)
    assert not approved(base, base)


def test_existing_archive_records_actual_morning_effect_without_changing_raw_basis():
    from custom_components.pv_forecast.history import HistoryArchive

    from .test_history import SOURCE

    raw = forecast(DAWN.date(), dc_power=1)
    effective = replace(
        raw,
        total_intervals=tuple(
            replace(i, energy_kwh=i.energy_kwh / 2, ac_power_kw=i.ac_power_kw / 2)
            for i in raw.total_intervals
        ),
    )
    archive = HistoryArchive("UTC")
    now = DAWN.replace(hour=3)
    archive.capture(
        raw,
        now,
        now,
        "config",
        [SOURCE],
        morning_forecast=effective,
        morning_candidate_id="morning-id",
    )
    records = [r for r in archive.records.values() if r.morning is not None]
    assert records and all(
        r.effective_energy_kwh == pytest.approx(r.raw_energy_kwh / 2) for r in records
    )
    restored = HistoryArchive.from_dict(archive.to_dict(), "UTC")
    assert restored.to_dict() == archive.to_dict()
    assert all(r.basis is not None for r in records)


def test_morning_adjustment_has_no_transferred_daily_or_hourly_uncertainty():
    from custom_components.pv_forecast.history import HistoryArchive
    from custom_components.pv_forecast.uncertainty import evaluate_experience_band

    from .test_history import SOURCE

    raw = forecast(DAWN.date(), dc_power=1)
    archive = HistoryArchive("UTC")
    now = DAWN.replace(hour=3)
    archive.capture(
        raw,
        now,
        now,
        "config",
        [SOURCE],
        morning_forecast=raw,
        morning_candidate_id="morning-id",
    )
    record = next(iter(archive.records.values()))
    result = evaluate_experience_band(
        tuple(archive.records.values()), record, as_of=now
    )
    assert result["status"] == "unavailable"
    assert result["variant"] == "morning_redistribution_v1"
