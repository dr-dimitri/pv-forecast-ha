"""Tests der reinen PV-Berechnung."""

from datetime import date, datetime

import pytest

from custom_components.pv_forecast.calculations import (
    InvalidConfigurationError,
    aggregate_energy_for_day,
    calculate_dc_power_kw,
    calculate_forecast,
    proportional_clipping,
    temperature_factor,
    to_open_meteo_azimuth,
    validate_coordinates,
    validate_roof,
)
from custom_components.pv_forecast.models import PvRoof, RoofForecastInterval

from .helpers import TIMEZONE, roof, weather


@pytest.mark.parametrize(
    ("compass", "expected"),
    [
        (0, -180),
        (45, -135),
        (90, -90),
        (135, -45),
        (180, 0),
        (270, 90),
        (315, 135),
        (360, -180),
    ],
)
def test_open_meteo_azimuth_mapping(compass: float, expected: float) -> None:
    """Alle Haupt- und Zwischenrichtungen werden zentral korrekt umgerechnet."""

    assert to_open_meteo_azimuth(compass) == expected


def test_non_finite_azimuth_is_rejected() -> None:
    """Nicht endliche Azimutwerte erreichen die API nicht."""

    with pytest.raises(InvalidConfigurationError):
        to_open_meteo_azimuth(float("nan"))


def test_zero_irradiance_produces_zero() -> None:
    """Ohne Einstrahlung entsteht kein Ertrag."""

    assert calculate_dc_power_kw(roof(), weather(0)) == 0


def test_reference_yield_at_1000_w_m2() -> None:
    """10 kWp ergeben bei Referenzbedingungen 10 kW."""

    assert calculate_dc_power_kw(roof(), weather()) == 10


def test_loss_and_temperature_correction() -> None:
    """Verlust und vereinfachte Temperaturkorrektur werden genau einmal angewendet."""

    result = calculate_dc_power_kw(roof(loss=0.12), weather(1000, 35))
    assert result == pytest.approx(10 * 0.965 * 0.88)
    assert temperature_factor(None) == 1


def test_temperature_factor_never_negative() -> None:
    """Extreme Temperaturwerte erzeugen keine negative Leistung."""

    assert temperature_factor(1000) == 0
    assert temperature_factor(float("nan")) == 1


def test_invalid_inverter_and_empty_system_are_rejected() -> None:
    """Leere Anlagen und nicht positive Wechselrichterlimits sind ungültig."""

    with pytest.raises(InvalidConfigurationError):
        proportional_clipping({"roof": 1}, 0)
    with pytest.raises(InvalidConfigurationError):
        calculate_forecast((), {}, None, date(2026, 8, 23), TIMEZONE)
    assert proportional_clipping({"roof": 1}, 2) == {"roof": 1}
    assert proportional_clipping({}, 2) == {}


def test_zero_length_interval_is_not_aggregated() -> None:
    """Ein kaputtes Intervall kann keine Energie zum Tag addieren."""

    instant = datetime(2026, 8, 23, 12, tzinfo=TIMEZONE)
    interval = RoofForecastInterval(instant, instant, 1, 1, 1)
    result = calculate_forecast(
        (roof(),),
        {"roof_1": (weather(),)},
        None,
        date(2026, 8, 23),
        TIMEZONE,
    )
    assert result.total.today == 10
    assert aggregate_energy_for_day((interval,), date(2026, 8, 23), TIMEZONE) == 0


def test_fractional_interval_and_missing_gti() -> None:
    """Energie nutzt die Intervalllänge; fehlendes Dachwetter ist null."""

    roofs = (roof("a", power=10), roof("b", power=5))
    result = calculate_forecast(
        roofs,
        {"a": (weather(minutes=30),), "b": ()},
        None,
        date(2026, 8, 23),
        TIMEZONE,
    )
    assert result.roofs["a"].daily.today == pytest.approx(5)
    assert result.roofs["b"].daily.today == 0
    assert result.total.today == pytest.approx(5)


def test_reference_proportional_inverter_clipping() -> None:
    """6 + 5 kW werden proportional auf ein 8-kW-Limit verteilt."""

    roofs = (roof("a", power=6), roof("b", power=5))
    point = weather()
    result = calculate_forecast(
        roofs,
        {"a": (point,), "b": (point,)},
        8,
        date(2026, 8, 23),
        TIMEZONE,
    )
    first = result.roofs["a"].intervals[0]
    second = result.roofs["b"].intervals[0]
    assert first.dc_power_kw == 6
    assert second.dc_power_kw == 5
    assert first.ac_power_kw == pytest.approx(4.363636, rel=1e-6)
    assert second.ac_power_kw == pytest.approx(3.636364, rel=1e-6)
    assert result.total.today == pytest.approx(8)


def test_preceding_hour_is_assigned_by_interval_overlap() -> None:
    """Der 00:00-GTI-Punkt gehört zur vorhergehenden lokalen Stunde."""

    midnight = datetime(2026, 8, 24, 0, tzinfo=TIMEZONE)
    result = calculate_forecast(
        (roof(),),
        {"roof_1": (weather(end=midnight),)},
        None,
        date(2026, 8, 23),
        TIMEZONE,
    )
    assert result.total.today == pytest.approx(10)
    assert result.total.tomorrow == 0


@pytest.mark.parametrize(
    "bad_roof",
    [
        PvRoof("", "Dach", 1, 180, 30, 0.1),
        PvRoof("id", "", 1, 180, 30, 0.1),
        PvRoof("id", "Dach", 0, 180, 30, 0.1),
        PvRoof("id", "Dach", 1, 360, 30, 0.1),
        PvRoof("id", "Dach", 1, 180, 91, 0.1),
        PvRoof("id", "Dach", 1, 180, 30, -0.1),
    ],
)
def test_invalid_roof_values_are_rejected(bad_roof: PvRoof) -> None:
    """Ungültige Dachparameter erreichen die Berechnung nicht."""

    with pytest.raises(InvalidConfigurationError):
        validate_roof(bad_roof)


@pytest.mark.parametrize("coordinates", [(91, 0), (-91, 0), (0, 181), (0, -181)])
def test_invalid_coordinates_are_rejected(coordinates: tuple[float, float]) -> None:
    """Standortgrenzen werden fachlich validiert."""

    with pytest.raises(InvalidConfigurationError):
        validate_coordinates(*coordinates)
