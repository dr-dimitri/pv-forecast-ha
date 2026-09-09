"""Offline-Prüfungen der Messgrenzen, Zählerdifferenzen und Zeitabdeckung."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_forecast.measurements import (
    EnergyDelta,
    Reading,
    SourceConfig,
    SourceHistory,
    aggregate_energy,
    normalize_reading_value,
)

START = datetime(2026, 9, 9, 10, tzinfo=UTC)
HOUR = timedelta(hours=1)


def source(**changes) -> SourceConfig:
    """Explizit bestätigte Quelle für eine getrennte AC-PV-Anlage."""
    return replace(
        SourceConfig("a", "sensor.pv", "total", "AC-PV ohne Speicher", "registry-a"),
        **changes,
    )


def history(**changes) -> SourceHistory:
    return SourceHistory(source(**changes), "Europe/Berlin", 20)


@pytest.mark.parametrize(
    ("raw", "unit", "kind", "value", "flags"),
    [
        ("0", "kWh", "total", 0, set()),
        ("1000", "Wh", "daily", 1, set()),
        ("2000", "W", "power", 2, set()),
        ("2.5", "kW", "power", 2.5, set()),
        ("unknown", "kWh", "total", None, {"unknown"}),
        (None, "kWh", "total", None, {"unknown"}),
        ("unavailable", "kWh", "total", None, {"unavailable"}),
        (True, "kWh", "total", None, {"invalid_value"}),
        ([], "kWh", "total", None, {"invalid_value"}),
        ("-1", "kWh", "total", None, {"invalid_value"}),
        ("NaN", "kWh", "total", None, {"invalid_value"}),
        ("inf", "kWh", "total", None, {"invalid_value"}),
        ("1e9999", "Wh", "total", None, {"invalid_value"}),
        (10**1000, "Wh", "total", None, {"invalid_value"}),
        ("1", "W", "total", None, {"unsupported_unit"}),
        ("1", "kWh", "power", None, {"unsupported_unit"}),
    ],
)
def test_units_and_invalid_values(raw, unit, kind, value, flags) -> None:
    """Null, fehlende Daten, falsche Einheiten und Überläufe bleiben unterscheidbar."""
    assert normalize_reading_value(raw, unit, kind) == (value, frozenset(flags))


@pytest.mark.parametrize(
    "changes",
    [
        {"source_id": ""},
        {"entity_id": "switch.pv"},
        {"kind": "export"},
        {"scope": " "},
        {"registry_id": ""},
        {"confirmed_pv": False},
        {"confirmed_disjoint": False},
        {"confirmed_pv": 1},
        {"derived_energy": 1},
        {"max_interval_minutes": True},
        {"max_interval_minutes": 0},
        {"max_interval_minutes": float("inf")},
        {"max_interval_minutes": 10**1000},
    ],
)
def test_source_configuration_rejects_unconfirmed_or_invalid_input(changes) -> None:
    """Eine passende Einheit ersetzt die ausdrückliche Messgrenzenbestätigung nicht."""
    with pytest.raises(ValueError):
        source(**changes)


def test_source_serialization_requires_confirmations() -> None:
    """Gespeicherte Metadaten werden unverändert übernommen, Zusagen nicht ergänzt."""
    config = source(derived_energy=True, max_interval_minutes=5)
    assert SourceConfig.from_dict(config.to_dict()) == config
    missing = config.to_dict()
    missing.pop("confirmed_pv")
    with pytest.raises(ValueError):
        SourceConfig.from_dict(missing)


def test_stable_zero_is_complete_energy_measurement() -> None:
    """Zwei gültige Nullstände sind eine belegte Nullenergiemenge."""
    values = history()
    values.add_reading(START, "0", "kWh")
    values.add_reading(START + HOUR, "0", "kWh")
    result = values.snapshot(START, START + HOUR, START + HOUR)
    assert result["energy_kwh"] == 0
    assert result["energy_complete"] is True
    assert result["complete"] is True
    assert result["coverage_seconds"] == 3600


def test_unit_change_preserves_normalized_counter() -> None:
    """Eine Wh-/kWh-Umstellung verändert weder Segment noch Differenz."""
    values = history()
    segment = values.segment_id
    values.add_reading(START, "1000", "Wh")
    values.add_reading(START + HOUR, "2", "kWh")
    assert values.deltas[0].energy_kwh == 1
    assert values.segment_id == segment


def test_unknown_gap_keeps_total_delta_but_never_invents_hourly_values() -> None:
    """Die grobe Gesamtdifferenz belegt keine Stundenverteilung der Messlücke."""
    values = history()
    values.add_reading(START, 10, "kWh")
    values.add_reading(START + HOUR, "unknown", "kWh")
    values.add_reading(START + 2 * HOUR, 14, "kWh")
    total = values.snapshot(START, START + 2 * HOUR, START + 2 * HOUR)
    assert total["energy_kwh"] == 4
    assert total["energy_complete"] is True
    assert total["complete"] is False
    assert total["coverage_seconds"] == 0
    assert {"unknown", "gap", "stale_gap"} <= set(total["quality_flags"])
    first_hour = values.snapshot(START, START + HOUR, START + 2 * HOUR)
    assert first_hour["energy_kwh"] is None
    assert first_hour["energy_complete"] is False
    assert "boundary_gap" in first_hour["quality_flags"]


def test_valid_short_delta_is_not_interpolated_at_window_boundary() -> None:
    """Auch regelmäßige Zählerdifferenzen erfinden keine Teilstundenverteilung."""
    values = history()
    values.add_reading(START, 10, "kWh")
    values.add_reading(START + HOUR, 12, "kWh")
    half = values.snapshot(START, START + HOUR / 2, START + HOUR)
    assert half["energy_kwh"] is None
    assert half["deltas"] == []


def test_stale_flag_is_independent_of_historical_coverage() -> None:
    """Ein mittlerweile alter Sensorstand löscht keine belegte vergangene Stunde."""
    values = history()
    values.add_reading(START, 0, "kWh")
    values.add_reading(START + HOUR, 1, "kWh")
    result = values.snapshot(START, START + HOUR, START + 3 * HOUR)
    assert result["complete"] is True
    assert "stale" in result["quality_flags"]


def test_late_measurement_does_not_rewrite_accepted_counter_baseline() -> None:
    """Ein verspäteter Messpunkt verändert die akzeptierte Zählerbasis nicht."""
    values = history()
    values.add_reading(START, 10, "kWh")
    values.add_reading(START + HOUR, 11, "kWh")
    late = values.add_reading(START + HOUR / 2, 5, "kWh")
    values.add_reading(START + 2 * HOUR, 12, "kWh")
    assert "late_reading" in late.quality_flags
    assert [delta.energy_kwh for delta in values.deltas] == [1, 1]
    assert values.last_valid_reading.value == 12


def test_duplicate_report_is_ignored_even_after_gap_annotation() -> None:
    """state_changed und state_reported dürfen denselben Stand nicht doppelt zählen."""
    values = history()
    values.add_reading(START, 10, "kWh")
    values.mark_gap()
    first = values.add_reading(START + HOUR, 11, "kWh")
    repeated = values.add_reading(START + HOUR, 11, "kWh")
    assert repeated == first
    assert len(values.readings) == 2
    assert len(values.deltas) == 1


def test_unconfirmed_total_decrease_begins_segment_without_reset_energy() -> None:
    """Ein fallender Gesamtzähler ist kein pauschal erlaubter Reset."""
    values = history()
    values.add_reading(START, 100, "kWh")
    before = values.segment_id
    falling = values.add_reading(START + HOUR, 20, "kWh")
    values.add_reading(START + 2 * HOUR, 21, "kWh")
    assert "counter_decrease" in falling.quality_flags
    assert values.segment_id != before
    assert [delta.energy_kwh for delta in values.deltas] == [1]
    assert (
        values.snapshot(START, START + 2 * HOUR, START + 2 * HOUR)["complete"] is False
    )


def test_explicit_reset_only_counts_energy_since_reported_reset() -> None:
    """Ein belegter Reset erlaubt keine fiktive Schlussmenge des vorherigen Zählers."""
    values = history()
    values.add_reading(START, 100, "kWh")
    reset = START + HOUR / 2
    values.add_reading(START + HOUR, 2, "kWh", last_reset=reset)
    assert len(values.deltas) == 1
    assert values.deltas[0].start == reset
    assert values.deltas[0].energy_kwh == 2
    assert (
        values.snapshot(START, START + HOUR, START + HOUR)["energy_complete"] is False
    )


def test_future_reset_metadata_never_creates_a_future_delta() -> None:
    """Fehlerhafte Reset-Metadaten gelten nicht als Resetbeleg."""
    values = history()
    values.add_reading(START, 100, "kWh")
    reading = values.add_reading(START + HOUR, 2, "kWh", START + 2 * HOUR)
    assert "invalid_last_reset" in reading.quality_flags
    assert "counter_decrease" in reading.quality_flags
    assert values.deltas == []


def test_daily_midnight_reset_does_not_invent_missing_closing_value() -> None:
    """Eine beobachtete Null um Mitternacht belegt den Vortagsabschluss nicht."""
    values = history(kind="daily")
    midnight = datetime(2026, 9, 9, 22, tzinfo=UTC)
    values.add_reading(midnight - 2 * HOUR, 8, "kWh")
    values.add_reading(midnight - HOUR, 9, "kWh")
    values.add_reading(midnight, 0, "kWh")
    values.add_reading(midnight + HOUR, 1, "kWh")
    yesterday = values.snapshot(midnight - 2 * HOUR, midnight, midnight + HOUR)
    today = values.snapshot(midnight, midnight + HOUR, midnight + HOUR)
    assert yesterday["energy_kwh"] == 1
    assert yesterday["complete"] is False
    assert today["energy_kwh"] == 1
    assert today["complete"] is True


def test_new_daily_date_without_reset_only_sets_new_baseline() -> None:
    """Der Tageswechsel allein erlaubt keine Differenz über die Tagesgrenze."""
    values = history(kind="daily")
    midnight = datetime(2026, 9, 9, 22, tzinfo=UTC)
    values.add_reading(midnight - HOUR, 9, "kWh")
    reading = values.add_reading(midnight + HOUR, 10, "kWh")
    assert values.deltas == []
    assert "daily_reset_unconfirmed" in reading.quality_flags


def test_daily_explicit_reset_allows_current_day_prefix() -> None:
    """Ein bestätigter Resetzeitpunkt belegt genau die danach aufgelaufene Energie."""
    values = history(kind="daily")
    midnight = datetime(2026, 9, 9, 22, tzinfo=UTC)
    values.add_reading(midnight - HOUR, 9, "kWh")
    values.add_reading(midnight + HOUR, 1, "kWh", midnight)
    result = values.snapshot(midnight, midnight + HOUR, midnight + HOUR)
    assert result["energy_kwh"] == 1
    assert result["complete"] is True


def test_daily_retrospective_decrease_invalidates_already_accumulated_day() -> None:
    """Eine Tageskorrektur darf beim erneuten Anstieg keine Energie doppelt zählen."""
    values = history(kind="daily")
    for hour, value in enumerate((0, 5, 4, 5)):
        values.add_reading(START + hour * HOUR, value, "kWh")
    result = values.snapshot(START, START + 3 * HOUR, START + 3 * HOUR)
    assert result["energy_kwh"] is None
    assert result["complete"] is False
    assert "daily_correction" in result["quality_flags"]
    restored = SourceHistory.from_dict(
        values.source, values.to_dict(), "Europe/Berlin", 20
    )
    assert restored.snapshot(START, START + 3 * HOUR, START + 3 * HOUR) == result


def test_independent_daily_resets_are_differenced_before_total() -> None:
    """Das Zurücksetzen eines Wechselrichters verfälscht den anderen Zähler nicht."""
    first = history(kind="daily")
    second = history(source_id="b", entity_id="sensor.second", registry_id="b")
    midnight = datetime(2026, 9, 9, 22, tzinfo=UTC)
    first.add_reading(midnight - HOUR, 9, "kWh")
    first.add_reading(midnight, 0, "kWh")
    first.add_reading(midnight + HOUR, 1, "kWh")
    second.add_reading(midnight, 100, "kWh")
    second.add_reading(midnight + HOUR, 102, "kWh")
    result = aggregate_energy(
        [first, second], midnight, midnight + HOUR, midnight + HOUR
    )
    assert result["energy_kwh"] == 3
    assert result["complete"] is True


@pytest.mark.parametrize(
    ("day", "hours"), [(datetime(2026, 3, 29), 23), (datetime(2026, 10, 25), 25)]
)
def test_dst_day_uses_absolute_utc_intervals(day, hours) -> None:
    """23 und 25 lokale Stunden bleiben jeweils eindeutige absolute Messfenster."""
    zone = ZoneInfo("Europe/Berlin")
    start = day.replace(tzinfo=zone).astimezone(UTC)
    end = (day + timedelta(days=1)).replace(tzinfo=zone).astimezone(UTC)
    values = history()
    for hour in range(hours + 1):
        values.add_reading(start + hour * HOUR, hour, "kWh")
    result = values.snapshot(start, end, end)
    assert result["energy_kwh"] == hours
    assert result["complete"] is True
    assert result["coverage_seconds"] == hours * 3600


def test_restart_preserves_amount_but_marks_unobserved_time() -> None:
    """Persistente Zählerstände erlauben nach Neustart keine erfundene Feinabdeckung."""
    values = history()
    values.add_reading(START, 10, "kWh")
    restored = SourceHistory.from_dict(
        values.source, values.to_dict(), "Europe/Berlin", 20
    )
    restored.mark_gap("restart")
    restored.add_reading(START + HOUR, 11, "kWh")
    result = restored.snapshot(START, START + HOUR, START + HOUR)
    assert result["energy_kwh"] == 1
    assert result["energy_complete"] is True
    assert result["complete"] is False
    assert "restart" in result["quality_flags"]


def test_registry_rename_preserves_segment_and_counter() -> None:
    """Der Anzeigename ist keine Messidentität; Registry-Renames erhalten Deltas."""
    values = history()
    values.add_reading(START, 10, "kWh")
    segment = values.segment_id
    renamed = replace(values.source, entity_id="sensor.renamed")
    restored = SourceHistory.from_dict(renamed, values.to_dict(), "Europe/Berlin", 20)
    restored.add_reading(START + HOUR, 11, "kWh")
    assert restored.segment_id == segment
    assert restored.deltas[0].energy_kwh == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"registry_id": "replacement"},
        {"registry_id": None},
        {"scope": "Andere getrennte Anlage"},
        {"kind": "daily"},
        {"derived_energy": True},
    ],
)
def test_measurement_identity_change_starts_new_segment(changes) -> None:
    """Neue Sensoren oder Messgrenzen übernehmen keine alte Lernkontinuität."""
    values = history()
    values.add_reading(START, 10, "kWh")
    segment = values.segment_id
    replacement = replace(values.source, **changes)
    restored = SourceHistory.from_dict(
        replacement, values.to_dict(), "Europe/Berlin", 20
    )
    assert restored.last_valid_reading is None
    restored.add_reading(START + HOUR, 100, "kWh")
    assert restored.segment_id != segment
    assert restored.deltas == []
    assert len(restored.segment_sources) == 2


def test_multiple_segments_are_not_a_continuous_learning_sample() -> None:
    """Ein historisches Fenster zeigt Segmentwechsel statt stiller Zusammenführung."""
    values = history()
    values.add_reading(START, 10, "kWh")
    values.add_reading(START + HOUR, 11, "kWh")
    values.replace_source(replace(values.source, registry_id="replacement"))
    values.add_reading(START + HOUR, 100, "kWh")
    values.add_reading(START + 2 * HOUR, 101, "kWh")
    result = values.snapshot(START, START + 2 * HOUR, START + 2 * HOUR)
    assert result["energy_kwh"] == 2
    assert result["energy_complete"] is False
    assert "multiple_segments" in result["quality_flags"]


def test_implausible_jump_is_flagged_and_not_integrated() -> None:
    """Die Leistungsheuristik erklärt Sprünge, ohne Energie zu erfinden."""
    values = history()
    values.add_reading(START, 0, "kWh")
    reading = values.add_reading(START + HOUR, 1000, "kWh")
    assert "implausible_jump" in reading.quality_flags
    assert values.deltas == []
    assert values.latest_reading.value == 1000
    assert values.last_valid_reading.value == 0


def test_extreme_finite_delta_and_sum_do_not_emit_infinity() -> None:
    """Endliche Einzelstände können eine nicht darstellbare Summe ergeben."""
    values = SourceHistory(source(), "UTC", 1e308)
    values.add_reading(START, 0, "kWh")
    reading = values.add_reading(START + timedelta(microseconds=1), 1e308, "kWh")
    assert "arithmetic_overflow" in reading.quality_flags
    assert values.deltas == []
    first = SourceHistory(source(), "UTC", 1e308)
    second = SourceHistory(
        source(source_id="b", registry_id="b", entity_id="sensor.b"), "UTC", 1e308
    )
    for item in (first, second):
        item.add_reading(START, 0, "kWh")
        item.add_reading(START + HOUR, 1e308, "kWh")
    total = aggregate_energy([first, second], START, START + HOUR, START + HOUR)
    assert total["energy_kwh"] is None
    assert "arithmetic_overflow" in total["quality_flags"]
    assert total["complete"] is False


def test_power_samples_are_never_integrated() -> None:
    """Eine optionale W-/kW-Quelle ist eine Livekurve und kein Energiezähler."""
    values = history(kind="power")
    values.add_reading(START, 1000, "W")
    values.add_reading(START + HOUR, 2, "kW")
    assert values.deltas == []
    assert values.last_valid_reading.value == 2
    result = values.snapshot(START, START + 2 * HOUR, START + HOUR)
    assert [reading["value"] for reading in result["readings"]] == [1, 2]
    assert (
        aggregate_energy([values], START, START + HOUR, START + HOUR)["energy_kwh"]
        is None
    )


def test_duplicate_source_identity_is_rejected_by_aggregation() -> None:
    """Auch unterhalb des UI schützt die Auswertung vor doppelten Quellen."""
    first = history()
    with pytest.raises(ValueError):
        aggregate_energy([first, first], START, START + HOUR, START)
    second = history(source_id="b")
    with pytest.raises(ValueError):
        aggregate_energy([first, second], START, START + HOUR, START)


def test_prune_bounds_points_deltas_and_obsolete_baseline() -> None:
    """Die Aufbewahrungsgrenze beschränkt auch Deltas und beginnt danach sauber neu."""
    values = history()
    for hour in range(5):
        values.add_reading(START + hour * HOUR, hour, "kWh")
    values.prune(START, max_readings=2)
    assert len(values.readings) == 2
    assert len(values.deltas) == 1
    values.prune(START + 7 * HOUR)
    assert values.readings == []
    assert values.deltas == []
    values.add_reading(START + 8 * HOUR, 8, "kWh")
    assert values.deltas == []


def test_serialized_models_reject_nonfinite_and_naive_values() -> None:
    """Kaputte Datensätze erzeugen weder NaN noch eine lokale Zeitannahme."""
    values = history()
    with pytest.raises(ValueError):
        values.add_reading(datetime(2026, 1, 1), 1, "kWh")
    values.add_reading(START, 1, "kWh")
    values.add_reading(START + HOUR, 2, "kWh")
    reading = values.readings[0].to_dict()
    reading["value"] = float("nan")
    with pytest.raises(ValueError):
        Reading.from_dict(reading)
    delta = values.deltas[0].to_dict()
    delta["energy_kwh"] = float("inf")
    with pytest.raises(ValueError):
        EnergyDelta.from_dict(delta)


def test_runtime_metadata_flags_survive_invalid_capture() -> None:
    """Ungültige HA-Metadaten sind schon vor dem nächsten gültigen Wert sichtbar."""
    values = history()
    values.add_reading(START, 1, "kWh")
    values.add_reading(
        START + HOUR, "unavailable", "kWh", quality_flags={"invalid_metadata"}
    )
    result = values.snapshot(START, START + HOUR, START + HOUR)
    assert "invalid_metadata" in result["latest_reading"]["quality_flags"]
    assert result["last_valid_reading"]["value"] == 1


@pytest.mark.parametrize(
    "corruption",
    [
        "duplicate_delta",
        "unknown_segment",
        "wrong_source",
        "old_baseline",
        "out_of_order",
        "bad_flags",
        "bad_day",
        "missing_key",
    ],
)
def test_corrupt_store_is_rejected_before_future_accumulation(corruption) -> None:
    """Inkonsistente lokale Daten dürfen nach Neustart keine Doppelzählung auslösen."""
    values = history()
    for hour in range(3):
        values.add_reading(START + hour * HOUR, hour, "kWh")
    stored = deepcopy(values.to_dict())
    if corruption == "duplicate_delta":
        stored["deltas"].append(stored["deltas"][-1])
    elif corruption == "unknown_segment":
        stored["readings"][0]["segment_id"] = "unknown"
    elif corruption == "wrong_source":
        stored["segments"][values.segment_id]["source_id"] = "other"
    elif corruption == "old_baseline":
        stored["baseline"] = stored["readings"][0]
    elif corruption == "out_of_order":
        stored["readings"].reverse()
    elif corruption == "bad_flags":
        stored["readings"][0]["quality_flags"] = "gap"
    elif corruption == "bad_day":
        stored["invalid_days"] = [[values.segment_id, "bad-date"]]
    else:
        del stored["segments"]
    with pytest.raises(ValueError):
        SourceHistory.from_dict(values.source, stored, "Europe/Berlin", 20)


def test_read_response_cannot_mutate_internal_segment_metadata() -> None:
    """Auch verschachtelte Antwortfelder sind vom internen Speicher getrennt."""
    values = history()
    result = values.snapshot(START, START + HOUR, START)
    result["segments"][values.segment_id]["scope"] = "Verändert"
    assert values.segment_sources[values.segment_id].scope == values.source.scope


def test_replacement_cannot_introduce_readings_older_than_retained_history() -> None:
    """Ein alter Gerätestand darf beim Austausch keine Überlappung erzeugen."""
    values = history()
    values.add_reading(START, 1, "kWh")
    values.add_reading(START + HOUR, 2, "kWh")
    values.replace_source(replace(values.source, registry_id="replacement"))
    late = values.add_reading(START, 10, "kWh")
    values.add_reading(START + 2 * HOUR, 20, "kWh")
    assert "late_reading" in late.quality_flags
    assert len(values.deltas) == 1
    restored = SourceHistory.from_dict(
        values.source, values.to_dict(), "Europe/Berlin", 20
    )
    assert restored.to_dict() == values.to_dict()


def test_historical_energy_survives_change_to_power_source() -> None:
    """Die aktuelle Messart darf historische Energie nicht rückwirkend entfernen."""
    values = history()
    values.add_reading(START, 100, "kWh")
    values.add_reading(START + HOUR, 101, "kWh")
    before = aggregate_energy([values], START, START + HOUR, START + HOUR)
    values.replace_source(
        replace(
            values.source, kind="power", entity_id="sensor.power", registry_id="power"
        )
    )
    values.add_reading(START + HOUR, 2, "kW")
    values.add_reading(START + 2 * HOUR, 3, "kW")
    after = aggregate_energy([values], START, START + HOUR, START + 2 * HOUR)
    assert after["energy_kwh"] == before["energy_kwh"] == 1
    assert after["source_count"] == 1
    assert after["energy_complete"] is True
    assert len(values.deltas) == 1
    power_only = aggregate_energy(
        [values], START + HOUR, START + 2 * HOUR, START + 2 * HOUR
    )
    assert power_only["energy_kwh"] is None
    assert power_only["source_count"] == 0


@pytest.mark.parametrize("new_start_hour", [1, 2])
def test_previous_daily_correction_does_not_contaminate_new_source(
    new_start_hour,
) -> None:
    """Die Tageskorrektur einer ersetzten Quelle entwertet kein neues Segment."""
    values = history(kind="daily")
    values.add_reading(START, 10, "kWh")
    values.add_reading(START + HOUR, 9, "kWh")
    values.replace_source(replace(values.source, registry_id="new-daily"))
    new_start = START + new_start_hour * HOUR
    values.add_reading(new_start, 0, "kWh")
    values.add_reading(new_start + HOUR, 1, "kWh")
    result = values.snapshot(new_start, new_start + HOUR, new_start + HOUR)
    assert result["energy_kwh"] == 1
    assert result["coverage_seconds"] == 3600
    assert result["energy_complete"] is True
    assert result["complete"] is True
    assert "daily_correction" not in result["quality_flags"]


def test_historical_missing_energy_is_not_hidden_by_current_power_kind() -> None:
    """Ein historisches Energiesegment bleibt auch ohne gültige Differenz sichtbar."""
    values = history()
    values.add_reading(START, "unknown", "kWh")
    values.add_reading(START + HOUR, "unavailable", "kWh")
    values.replace_source(replace(values.source, kind="power"))
    result = aggregate_energy([values], START, START + HOUR, START + HOUR)
    assert result["source_count"] == 1
    assert result["energy_kwh"] is None
    assert result["complete"] is False


def test_current_empty_energy_source_remains_required_for_total_coverage() -> None:
    """Eine noch leere Energiequelle darf Gesamtvollständigkeit nicht vortäuschen."""
    measured = history()
    measured.add_reading(START, 1, "kWh")
    measured.add_reading(START + HOUR, 2, "kWh")
    empty = history(source_id="empty", entity_id="sensor.empty", registry_id="empty")
    result = aggregate_energy([measured, empty], START, START + HOUR, START + HOUR)
    assert result["source_count"] == 2
    assert result["energy_kwh"] == 1
    assert result["complete"] is False
