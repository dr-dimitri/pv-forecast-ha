"""Offline-Prüfungen der datierten Kartenprojektion und vorhandenen Dachbeiträge."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_forecast.calculations import calculate_forecast
from custom_components.pv_forecast.card_data import (
    UnknownRoofError,
    build_forecast_view,
)

from .helpers import roof, weather

DAY = date(2026, 9, 9)
HOUR = timedelta(hours=1)


def forecast(day=DAY, timezone="UTC", gti=1000):
    """Das reale gemeinsame Berechnungsmodell liefert die Daten für die Darstellung."""
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=2), time.min, zone).astimezone(UTC)
    cursor = start.replace(minute=0, second=0, microsecond=0) + HOUR
    points = []
    while cursor - HOUR < end:
        points.append(weather(gti=gti, end=cursor))
        cursor += HOUR
    roofs = (roof("south", name="Süddach"), roof("garage", name="Garage"))
    return calculate_forecast(
        roofs, {item.id: tuple(points) for item in roofs}, 15, day, zone
    )


def view(data, now=None, timezone="UTC", **kwargs):
    now = now or datetime.combine(data.local_date, time(12), ZoneInfo(timezone))
    return build_forecast_view(
        data, timezone, "Meine PV-Anlage", now, now, True, **kwargs
    )


def test_total_and_roof_views_use_existing_clipped_contributions() -> None:
    """Die Karte übernimmt das bereits berechnete gemeinsame Clipping."""
    data = forecast()
    original = deepcopy(data)
    total = view(data)
    selected = view(data, roof_id="garage")
    assert total["summary"] == {
        "today_kwh": 360,
        "tomorrow_kwh": 360,
        "remaining_today_kwh": 180,
    }
    assert selected["summary"] == {
        "today_kwh": 180,
        "tomorrow_kwh": 180,
        "remaining_today_kwh": 90,
    }
    assert total["intervals"][0]["energy_kwh"] == 15
    assert selected["intervals"][0]["energy_kwh"] == 7.5
    assert selected["intervals"][0]["ac_power_kw"] == 7.5
    assert selected["roof_id"] == "garage"
    assert selected["roofs"] == [
        {"id": "south", "name": "Süddach"},
        {"id": "garage", "name": "Garage"},
    ]
    assert data == original
    assert json.loads(json.dumps(selected, allow_nan=False)) == selected


def test_tomorrow_selection_preserves_today_summary_and_server_boundaries() -> None:
    """Die Auswahl der Kurve verändert die Bedeutung der vier Kennzahlen nicht."""
    data = forecast()
    selected = view(data, day="tomorrow")
    assert selected["date"] == "2026-09-10"
    assert selected["start"] == "2026-09-10T00:00:00+00:00"
    assert selected["today_start"] == "2026-09-09T00:00:00+00:00"
    assert selected["summary"] == view(data)["summary"]


@pytest.mark.parametrize(
    ("day", "hours"), [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)]
)
def test_dst_uses_actual_utc_day_length(day, hours) -> None:
    """23 und 25 Ortsstunden behalten ihre tatsächlichen absoluten Grenzen."""
    result = view(forecast(day, "Europe/Berlin"), timezone="Europe/Berlin")
    assert len(result["intervals"]) == hours
    assert result["summary"]["today_kwh"] == hours * 15
    assert result["complete"] is True
    if hours == 25:
        starts = {item["start"] for item in result["intervals"]}
        assert "2026-10-25T00:00:00+00:00" in starts
        assert "2026-10-25T01:00:00+00:00" in starts


@pytest.mark.parametrize("selected_roof", [None, "garage"])
def test_fractional_timezone_splits_forecast_edges_exactly_once(
    selected_roof, monkeypatch
) -> None:
    """Teilstundengrenzen folgen der Anlage unabhängig von der Browser-/Systemzone."""
    monkeypatch.setenv("TZ", "America/New_York")
    data = forecast(timezone="Asia/Kolkata")
    result = view(data, timezone="Asia/Kolkata", roof_id=selected_roof)
    power = 15 if selected_roof is None else 7.5
    assert result["start"] == "2026-09-08T18:30:00+00:00"
    assert result["end"] == "2026-09-09T18:30:00+00:00"
    assert len(result["intervals"]) == 25
    assert result["intervals"][0]["energy_kwh"] == power / 2
    assert result["intervals"][-1]["energy_kwh"] == power / 2
    assert sum(item["energy_kwh"] for item in result["intervals"]) == power * 24
    assert result["summary"]["today_kwh"] == power * 24


def test_cached_tomorrow_becomes_today_without_relabeling_old_today() -> None:
    """Nach Mitternacht zählt der tatsächliche Zeitraum des erhaltenen Snapshots."""
    data = forecast()
    data = replace(
        data,
        total_intervals=tuple(
            (
                replace(
                    item,
                    energy_kwh=item.energy_kwh * 2,
                    ac_power_kw=item.ac_power_kw * 2,
                )
                if item.start.date() > DAY
                else item
            )
            for item in data.total_intervals
        ),
    )
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    result = build_forecast_view(
        data, "UTC", "Anlage", now, now - timedelta(days=1), False
    )
    assert result["date"] == "2026-09-10"
    assert result["summary"] == {
        "today_kwh": 720,
        "tomorrow_kwh": None,
        "remaining_today_kwh": 360,
    }
    assert result["stale"] is True


def test_fully_expired_snapshot_is_not_presented_as_current_zero_day() -> None:
    """Ein vollständig vergangener Forecast ist keine heutige Nullprognose."""
    data = forecast()
    result = view(data, datetime(2026, 9, 11, 12, tzinfo=UTC))
    assert result["date"] == "2026-09-11"
    assert result["intervals"] == []
    assert result["summary"] == {
        "today_kwh": None,
        "tomorrow_kwh": None,
        "remaining_today_kwh": None,
    }
    assert result["complete"] is False
    assert result["stale"] is True


def test_zero_and_missing_forecast_are_distinct() -> None:
    """Nulltage bleiben gültig; fehlende Intervalle bleiben unbekannt."""
    zero = forecast(gti=0)
    result = view(zero)
    assert result["summary"]["today_kwh"] == 0
    assert result["complete"] is True
    assert result["stale"] is False
    missing = replace(
        zero, total_intervals=zero.total_intervals[:8] + zero.total_intervals[9:]
    )
    result = view(missing)
    assert result["summary"]["today_kwh"] is None
    assert result["complete"] is False
    assert len(result["intervals"]) == 23


def test_roof_quality_conservatively_follows_common_coverage() -> None:
    """Die Dachansicht zeigt gemeinsame Datenlücken und Eingabefallbacks."""
    data = forecast()
    marked = replace(
        data.total_intervals[0],
        is_complete=False,
        quality_flags=("missing_roof_data", "gti_fallback"),
    )
    data = replace(data, total_intervals=(marked, *data.total_intervals[1:]))
    result = view(data, roof_id="garage")
    assert result["summary"]["today_kwh"] is None
    assert result["intervals"][0]["quality_flags"] == [
        "missing_roof_data",
        "gti_fallback",
    ]
    assert result["intervals"][0]["is_complete"] is False


@pytest.mark.parametrize(
    ("age", "success", "stale"),
    [
        (timedelta(minutes=59), True, False),
        (timedelta(minutes=60), True, False),
        (timedelta(minutes=60, seconds=1), True, True),
        (timedelta(0), False, True),
        (None, True, True),
        (timedelta(minutes=-1), True, True),
    ],
)
def test_stale_state_preserves_known_values(age, success, stale) -> None:
    """Alter und Fehler sind sichtbar, ohne bekannte Prognosewerte umzudeuten."""
    data = forecast()
    now = datetime(2026, 9, 9, 12, tzinfo=UTC)
    result = build_forecast_view(
        data, "UTC", "Anlage", now, now - age if age is not None else None, success
    )
    assert result["stale"] is stale
    assert result["summary"]["today_kwh"] == 360


@pytest.mark.parametrize("bad_value", [float("inf"), float("nan"), -1, True])
def test_invalid_numeric_interval_is_a_gap_not_invalid_json(bad_value) -> None:
    """Ungültige Intervalle erreichen die Karte nicht als NaN oder falsche Null."""
    data = forecast()
    broken = replace(data.total_intervals[0], energy_kwh=bad_value)
    data = replace(data, total_intervals=(broken, *data.total_intervals[1:]))
    result = view(data)
    assert result["complete"] is False
    assert result["summary"]["today_kwh"] is None
    json.dumps(result, allow_nan=False)


def test_extreme_finite_daily_sum_is_not_serialized_as_infinity() -> None:
    """Auch die reine Darstellung begrenzt nicht darstellbare Summen kontrolliert."""
    data = forecast()
    data = replace(
        data,
        total_intervals=tuple(
            replace(item, energy_kwh=1e308) for item in data.total_intervals
        ),
    )
    result = view(data)
    assert result["summary"]["today_kwh"] is None
    assert result["complete"] is False
    json.dumps(result, allow_nan=False)


def test_roof_lookup_uses_stable_id_and_rejects_unknown_roof() -> None:
    """Dachnamen sind keine Identitäten für die Auswahl."""
    data = forecast()
    with pytest.raises(UnknownRoofError, match="Dachfläche"):
        view(data, roof_id="Garage")
    assert view(data, roof_id="garage")["roof_id"] == "garage"


def test_naive_now_is_rejected() -> None:
    """Eine fehlende Zeitzone darf nicht still zur Maschinenzone werden."""
    with pytest.raises(ValueError, match="eindeutige Zeitpunkte"):
        view(forecast(), datetime(2026, 9, 9, 12))
