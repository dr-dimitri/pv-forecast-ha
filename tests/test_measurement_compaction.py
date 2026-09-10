"""Verlustfreie Tagesnachweise trotz schnell meldender AC-Zähler."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import fsum
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_forecast.measurements import (
    EnergyDelta,
    SourceConfig,
    SourceHistory,
)

START = datetime(2026, 9, 1, tzinfo=UTC)


def history(timezone="UTC", derived=True):
    return SourceHistory(
        SourceConfig(
            "pv", "sensor.pv", "total", "AC-PV", "registry", derived_energy=derived
        ),
        timezone,
        50,
    )


def feed(values, start, seconds, step=1, maximum=20000):
    """Auch im Ereignispfad die feste Obergrenze zu jedem Zeitpunkt prüfen."""
    energies = []
    previous = None
    for offset in range(0, seconds + 1, step):
        stamp = start + timedelta(seconds=offset)
        energy = 100 + offset / 5000
        values.add_reading(stamp, energy, "kWh")
        values.prune_if_needed(stamp - timedelta(days=7), maximum)
        assert len(values.readings) <= maximum
        assert len(values.deltas) <= maximum
        if previous is not None:
            energies.append(energy - previous)
        previous = energy
    return fsum(energies)


def test_second_reports_preserve_complete_day_and_exact_energy():
    """86.401 Zustände belegen weiterhin den gesamten Tag statt nur 5,6 Stunden."""
    values = history()
    expected = feed(values, START, 86400)
    end = START + timedelta(days=1)
    result = values.snapshot(START, end, end)
    assert result["complete"] is True
    assert result["energy_complete"] is True
    assert result["coverage_seconds"] == 86400
    assert result["energy_kwh"] == expected
    assert not values.has_retention_loss(START, end)
    restored = SourceHistory.from_dict(values.source, values.to_dict(), "UTC", 50)
    assert restored.snapshot(START, end, end) == result
    restored.add_reading(end + timedelta(seconds=1), 100 + 86401 / 5000, "kWh")
    assert restored.deltas[-1].start == end


@pytest.mark.parametrize(
    ("timezone", "day", "hours"),
    [
        ("Europe/Berlin", "2026-03-29", 23),
        ("Europe/Berlin", "2026-10-25", 25),
        ("Asia/Kolkata", "2026-09-01", 24),
    ],
)
def test_local_days_and_hour_boundaries_survive(timezone, day, hours):
    """UTC-Minuten bewahren auch DST-Folds und halbstündige Tagesgrenzen."""
    start_local = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(timezone))
    start = start_local.astimezone(UTC)
    end = (start_local + timedelta(days=1)).astimezone(UTC)
    values = history(timezone)
    expected = feed(values, start, hours * 3600, step=10, maximum=2000)
    result = values.snapshot(start, end, end)
    assert result["complete"] is True
    assert result["energy_kwh"] == expected
    for hour in range(hours):
        left = start + timedelta(hours=hour)
        assert values.snapshot(left, left + timedelta(hours=1), end)["complete"]


def test_seven_days_fit_without_losing_coverage():
    """Die gesamte Siebentagefrist bleibt bei häufigen gesunden Meldungen nutzbar."""
    values = history()
    expected = feed(values, START, 7 * 86400, step=6)
    end = START + timedelta(days=7)
    result = values.snapshot(START, end, end)
    assert result["complete"] is True
    assert result["energy_kwh"] == expected
    assert not values._retention_losses


@pytest.mark.parametrize(
    "flags",
    [
        {"gap"},
        {"stale_gap"},
        {"counter_reset"},
        {"daily_reset"},
        {"daily_correction"},
        {"source_changed"},
        {"implausible_jump"},
    ],
)
def test_quality_events_are_never_compacted(flags):
    """Bekannte Ausnahmen bleiben mit ihren ursprünglichen Intervallen erhalten."""
    values = history()
    feed(values, START, 3)
    values.deltas = [
        replace(delta, quality_flags=frozenset(flags)) for delta in values.deltas
    ]
    before = values.to_dict()
    values._compact()
    assert values.to_dict() == before


def test_zero_plateau_and_unrepresentable_sums_keep_boundaries():
    """Weder belegte Nullfenster noch exakte float-Summen werden verändert."""
    values = history()
    for offset, energy in enumerate([100, 100, 100, 101, 102]):
        values.add_reading(START + timedelta(seconds=offset), energy, "kWh")
    # Für den isolierten Rundungstest liegen die Differenzen bewusst außerhalb
    # der realen Leistungsgrenze; die Verdichtung darf daran nichts ändern.
    values.deltas = [
        EnergyDelta(
            START + timedelta(seconds=i),
            START + timedelta(seconds=i + 1),
            energy,
            frozenset(),
            values.segment_id,
        )
        for i, energy in enumerate([0.0, 0.0, 1.0, 2**-54])
    ]
    values._compact()
    assert [delta.energy_kwh for delta in values.deltas] == [0, 1, 2**-54]
    assert values.deltas[0].end == START + timedelta(seconds=2)
    assert (
        values.snapshot(
            START + timedelta(microseconds=1), START + timedelta(seconds=1), START
        )["energy_kwh"]
        == 0
    )


def test_local_midnight_with_historical_second_offset_is_preserved():
    """Auch eine lokale Tagesgrenze innerhalb einer UTC-Minute bleibt ein Anker."""
    values = history("Africa/Monrovia")
    midnight = datetime(1970, 1, 2, tzinfo=ZoneInfo("Africa/Monrovia")).astimezone(UTC)
    for offset in range(-2, 3):
        values.add_reading(
            midnight + timedelta(seconds=offset), 100 + offset / 5000, "kWh"
        )
    values._compact()
    assert any(delta.end == midnight for delta in values.deltas)
    assert any(delta.start == midnight for delta in values.deltas)


def test_count_loss_is_visible_persistent_and_separate_from_age_retention():
    """Nicht verdichtbare Nachweise werden begrenzt und ihr Verlust bleibt sichtbar."""
    values = history()
    for minute in range(5):
        values.add_reading(
            START + timedelta(minutes=minute), 100 + minute / 5000, "kWh"
        )
    values.prune(START - timedelta(days=7), max_readings=2)
    end = START + timedelta(minutes=5)
    assert values.has_retention_loss(START, end)
    restored = SourceHistory.from_dict(values.source, values.to_dict(), "UTC", 50)
    assert restored.has_retention_loss(START, end)
    assert not restored.has_retention_loss(
        START + timedelta(days=1), START + timedelta(days=2)
    )
    restored.bind_location("new", "UTC", end)
    # Erstmaliges Binden ergänzt denselben Kontext, ein echter Wechsel trennt ihn.
    restored.bind_location("other", "Europe/Berlin", end + timedelta(minutes=1))
    assert not restored.has_retention_loss(START, end)
    aged = history()
    feed(aged, START, 10)
    aged.prune(START + timedelta(seconds=5))
    assert not aged._retention_losses


def test_late_readings_preserve_ordering_after_compaction_and_cache():
    """Späte Zustände ersetzen weder Zählerbasis noch jüngsten angenommenen Wert."""
    values = history()
    feed(values, START, 60)
    values._compact()
    late = values.add_reading(START + timedelta(seconds=30), 100, "kWh")
    assert "late_reading" in late.quality_flags
    values.add_reading(START + timedelta(seconds=61), 100 + 61 / 5000, "kWh")
    assert values.deltas[-1].start == START + timedelta(seconds=60)
    assert (
        SourceHistory.from_dict(values.source, values.to_dict(), "UTC", 50).to_dict()
        == values.to_dict()
    )


def test_invalid_retention_metadata_is_rejected():
    """Neue Metadaten dürfen keine unbekannten Segmente still übernehmen."""
    values = history()
    data = values.to_dict()
    data["retention_losses"] = {"missing": START.isoformat()}
    with pytest.raises(ValueError):
        SourceHistory.from_dict(values.source, data, "UTC", 50)


def test_loss_of_last_record_in_previous_counter_segment_stays_visible():
    """Ein Zählerneustart darf den Verlust vorheriger Tagesenergie nicht verstecken."""
    values = history()
    for minute, energy in enumerate([100, 100.1, 100.2, 0, 0.1, 0.2, 0.3]):
        values.add_reading(START + timedelta(minutes=minute), energy, "kWh")
    previous_segment = values.readings[0].segment_id
    values.prune(START - timedelta(days=7), max_readings=4)
    assert all(reading.segment_id != previous_segment for reading in values.readings)
    assert values.has_retention_loss(START, START + timedelta(hours=1))
    assert previous_segment in values._retention_losses
    restored = SourceHistory.from_dict(values.source, values.to_dict(), "UTC", 50)
    assert restored.has_retention_loss(START, START + timedelta(hours=1))
    for minute in range(7, 100):
        restored.add_reading(
            START + timedelta(minutes=minute), (minute % 3) / 10, "kWh"
        )
        restored.prune_if_needed(START - timedelta(days=7), 4)
    assert len(restored._retention_losses) == 1
    assert len(restored._segments) <= 5
    assert restored.has_retention_loss(START, START + timedelta(hours=2))
    restored.prune(START + timedelta(days=8))
    assert not restored._retention_losses


def test_event_path_keeps_hard_age_limit_below_count_cap():
    """Auch unterhalb der Anzahlgrenze gelangt kein überalterter Punkt in den Store."""
    values = history()
    values.add_reading(START, 100, "kWh")
    end = START + timedelta(days=7, minutes=1)
    values.add_reading(end, 101, "kWh")
    values.prune_if_needed(end - timedelta(days=7))
    assert [reading.timestamp for reading in values.readings] == [end]
    assert values.deltas == []
    assert values._retention_losses == {}
    # Ein noch später zugestellter alter Punkt umgeht auch den Minimumcache nicht.
    values.add_reading(START, 100.1, "kWh")
    values.prune_if_needed(end - timedelta(days=7))
    assert [reading.timestamp for reading in values.readings] == [end]
