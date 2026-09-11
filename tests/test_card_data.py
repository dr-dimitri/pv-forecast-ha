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


def forecast(day=DAY, timezone="UTC", gti=1000, forecast_days=2):
    """Das reale gemeinsame Berechnungsmodell liefert die Daten für die Darstellung."""
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, zone).astimezone(UTC)
    end = datetime.combine(
        day + timedelta(days=forecast_days), time.min, zone
    ).astimezone(UTC)
    cursor = start.replace(minute=0, second=0, microsecond=0) + HOUR
    points = []
    while cursor - HOUR < end:
        points.append(weather(gti=gti, end=cursor))
        cursor += HOUR
    roofs = (roof("south", name="Süddach"), roof("garage", name="Garage"))
    return calculate_forecast(
        roofs,
        {item.id: tuple(points) for item in roofs},
        15,
        day,
        zone,
        forecast_days=forecast_days,
    )


def view(data, now=None, timezone="UTC", **kwargs):
    now = now or datetime.combine(data.local_date, time(12), ZoneInfo(timezone))
    return build_forecast_view(
        data, timezone, "Meine PV-Anlage", now, now, True, **kwargs
    )


@pytest.mark.parametrize("selected_roof", [None, "garage"])
@pytest.mark.parametrize("elapsed_days", [1, 3])
@pytest.mark.parametrize(
    ("timezone", "current_day"),
    [
        ("UTC", DAY),
        ("Europe/Berlin", date(2026, 3, 29)),
        ("Europe/Berlin", date(2026, 10, 25)),
        ("Asia/Kathmandu", DAY),
    ],
)
def test_multiday_view_starts_with_current_local_day(
    selected_roof, elapsed_days, timezone, current_day
) -> None:
    """Gesamt- und Dachaussicht ordnen alte Stände nach dem heutigen lokalen Tag ein."""

    data = forecast(
        current_day - timedelta(days=elapsed_days), timezone, forecast_days=7
    )
    original = deepcopy(data)
    zone = ZoneInfo(timezone)
    now = datetime.combine(current_day, time(0, 1), zone).astimezone(UTC)
    result = view(data, now, timezone, roof_id=selected_roof)
    days = result["daily_forecasts"]
    assert [item["date"] for item in days] == [
        (current_day + timedelta(days=offset)).isoformat()
        for offset in range(7 - elapsed_days)
    ]
    assert [item["tendency"] for item in days] == [False, False] + [True] * (
        5 - elapsed_days
    )
    power = 15 if selected_roof is None else 7.5
    for item in days:
        day = date.fromisoformat(item["date"])
        start = datetime.combine(day, time.min, zone).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
        assert item["energy_kwh"] == pytest.approx(
            power * (end - start).total_seconds() / 3600
        )
    assert days[0]["energy_kwh"] == result["summary"]["today_kwh"]
    assert days[1]["energy_kwh"] == result["summary"]["tomorrow_kwh"]
    assert result["forecast_days"] == 7
    assert data == original


@pytest.mark.parametrize("forecast_days", [3, 7])
def test_multiday_view_does_not_extend_expired_horizon(forecast_days) -> None:
    """Der letzte gespeicherte Tag bleibt lesbar; danach ist die Aussicht leer."""

    data = forecast(forecast_days=forecast_days)
    for elapsed in (forecast_days - 1, forecast_days, forecast_days + 1):
        now = datetime.combine(DAY + timedelta(days=elapsed), time(12), UTC)
        result = view(data, now)
        if elapsed == forecast_days - 1:
            assert len(result["daily_forecasts"]) == 1
            assert result["daily_forecasts"][0]["tendency"] is False
            assert result["daily_forecasts"][0]["energy_kwh"] == 360
            assert result["summary"]["tomorrow_kwh"] is None
        else:
            assert result["daily_forecasts"] == []
            assert result["summary"]["today_kwh"] is None
        assert result["forecast_days"] == forecast_days


@pytest.mark.parametrize("selected_roof", [None, "garage"])
def test_multiday_view_keeps_missing_days_distinct_from_zero(selected_roof) -> None:
    """Ein fehlender Tag bleibt unbekannt und korrekt datiert."""

    data = forecast(gti=0, forecast_days=7)
    missing_day = DAY + timedelta(days=2)
    data = replace(
        data,
        total_intervals=tuple(
            item for item in data.total_intervals if item.start.date() != missing_day
        ),
    )
    now = datetime.combine(DAY + timedelta(days=1), time(1), UTC)
    result = view(data, now, roof_id=selected_roof)
    days = result["daily_forecasts"]
    assert [item["energy_kwh"] for item in days] == [0, None, 0, 0, 0, 0]
    assert days[1]["date"] == missing_day.isoformat()
    assert days[1]["tendency"] is False
    assert result["summary"]["tomorrow_kwh"] is None


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


@pytest.mark.parametrize("selected_roof", [None, "garage"])
@pytest.mark.parametrize(
    ("day", "timezone"),
    [
        (DAY, "UTC"),
        (date(2026, 3, 29), "Europe/Berlin"),
        (date(2026, 10, 25), "Europe/Berlin"),
        (DAY, "Asia/Kolkata"),
    ],
)
def test_both_day_views_share_one_summary_and_preserve_backend_day_projection(
    selected_roof, day, timezone
):
    """Tageswechsel wählt nur vorbereitete Felder desselben Serverzeitpunkts aus."""
    data = forecast(day, timezone)
    current = view(data, timezone=timezone, roof_id=selected_roof)
    tomorrow = view(data, timezone=timezone, day="tomorrow", roof_id=selected_roof)
    assert current["day_views"] == tomorrow["day_views"]
    assert current["summary"] == tomorrow["summary"]
    for name, expected in (("today", current), ("tomorrow", tomorrow)):
        assert current["day_views"][name] == {
            key: expected[key]
            for key in ("day", "date", "start", "end", "intervals", "complete", "stale")
        }
