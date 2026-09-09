"""Tests der reinen PV-Berechnung."""

from datetime import UTC, date, datetime, timedelta

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
from custom_components.pv_forecast.models import (
    PvRoof,
    RoofForecastInterval,
    WeatherInterval,
)

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
    ("local_day", "hours"),
    [
        (date(2026, 8, 23), 24),
        (date(2026, 3, 29), 23),
        (date(2026, 10, 25), 25),
    ],
    ids=["normaler-tag", "sommerzeitbeginn", "sommerzeitende"],
)
def test_forecast_preserves_every_hour_of_local_day(
    local_day: date, hours: int
) -> None:
    """Ein vollständiger lokaler Tag behält auch beim Zeitwechsel jede Stunde."""

    start = datetime.combine(local_day, datetime.min.time(), TIMEZONE).astimezone(UTC)
    points = tuple(
        WeatherInterval(
            start=(start + timedelta(hours=hour)).astimezone(TIMEZONE),
            end=(start + timedelta(hours=hour + 1)).astimezone(TIMEZONE),
            gti_w_m2=1000,
            ambient_temperature_c=25,
        )
        for hour in range(hours)
    )
    result = calculate_forecast(
        (roof(power=1),),
        {"roof_1": points},
        None,
        local_day,
        TIMEZONE,
    )

    intervals = result.roofs["roof_1"].intervals
    assert len(intervals) == hours
    assert [interval.end.astimezone(UTC) for interval in intervals] == [
        start + timedelta(hours=hour + 1) for hour in range(hours)
    ]
    assert result.roofs["roof_1"].daily.today == pytest.approx(hours)
    assert result.total.today == pytest.approx(hours)
    assert result.total.tomorrow == 0


def test_repeated_autumn_hour_keeps_distinct_weather_and_clipping() -> None:
    """Beide 02-Uhr-Stunden behalten ihre Dachwerte und ihr gemeinsames AC-Limit."""

    ends = (
        datetime(2026, 10, 25, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 1, tzinfo=UTC),
    )
    points_by_roof = {
        roof_id: tuple(
            WeatherInterval(
                start=(end - timedelta(hours=1)).astimezone(TIMEZONE),
                end=end.astimezone(TIMEZONE),
                gti_w_m2=gti,
                ambient_temperature_c=25,
            )
            for end, gti in zip(ends, gti_values, strict=True)
        )
        for roof_id, gti_values in {"a": (1000, 250), "b": (500, 1000)}.items()
    }
    # Abweichende Eingabereihenfolgen dürfen die zeitgleichen Dächer nicht mischen.
    points_by_roof["a"] = tuple(reversed(points_by_roof["a"]))
    result = calculate_forecast(
        (roof("a", power=6, azimuth=90), roof("b", power=4, azimuth=270)),
        points_by_roof,
        6,
        date(2026, 10, 25),
        TIMEZONE,
    )

    for roof_result in result.roofs.values():
        assert len(roof_result.intervals) == 2
        assert [
            interval.end.astimezone(UTC) for interval in roof_result.intervals
        ] == list(ends)
        assert [interval.end.hour for interval in roof_result.intervals] == [2, 2]
        assert [interval.end.fold for interval in roof_result.intervals] == [0, 1]

    first_a, second_a = result.roofs["a"].intervals
    first_b, second_b = result.roofs["b"].intervals
    assert (first_a.dc_power_kw, first_b.dc_power_kw) == pytest.approx((6, 2))
    assert (first_a.ac_power_kw, first_b.ac_power_kw) == pytest.approx((4.5, 1.5))
    assert (second_a.dc_power_kw, second_b.dc_power_kw) == pytest.approx((1.5, 4))
    assert (second_a.ac_power_kw, second_b.ac_power_kw) == pytest.approx((1.5, 4))
    assert result.roofs["a"].daily.today == pytest.approx(6)
    assert result.roofs["b"].daily.today == pytest.approx(5.5)
    assert result.total.today == pytest.approx(11.5)
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


@pytest.mark.parametrize("inverter_limit", [None, 15])
def test_finite_weather_overflow_is_controlled(inverter_limit: float | None) -> None:
    """Extremes endliches GTI erzeugt weder unendliche Energie noch NaN."""

    with pytest.raises(InvalidConfigurationError, match="Rohleistung"):
        calculate_forecast(
            (roof(),),
            {"roof_1": (weather(gti=1e308),)},
            inverter_limit,
            date(2026, 8, 23),
            TIMEZONE,
        )


@pytest.mark.parametrize(
    ("ambient", "coefficient", "reference"),
    [(1e308, -1e308, 25), (-1e308, -1e308, 25), (1e308, -0.0035, -1e308)],
)
def test_temperature_factor_overflow_is_controlled(
    ambient: float, coefficient: float, reference: float
) -> None:
    """Auch ein negativer Rechenüberlauf wird vor der Begrenzung als Fehler erkannt."""

    with pytest.raises(InvalidConfigurationError, match="Temperatur"):
        temperature_factor(ambient, coefficient, reference)


def test_corrected_dc_power_overflow_is_controlled() -> None:
    """Endliche Faktoren dürfen beim Produkt keine unendliche DC-Leistung erzeugen."""

    with pytest.raises(
        InvalidConfigurationError, match="temperaturkorrigierte Leistung"
    ):
        calculate_dc_power_kw(roof(power=1000), weather(1000, -1e308))


def test_total_losses_do_not_hide_raw_power_overflow() -> None:
    """Hundert Prozent Verluste dürfen einen ungültigen Rechengang nicht heilen."""

    with pytest.raises(InvalidConfigurationError, match="Rohleistung"):
        calculate_dc_power_kw(roof(loss=1), weather(gti=1e308))


@pytest.mark.parametrize("inverter_limit", [None, 15])
def test_clipping_sum_overflow_is_controlled(inverter_limit: float | None) -> None:
    """Eine überlaufende Dachsumme wird auch ohne eingestelltes AC-Limit verworfen."""

    with pytest.raises(InvalidConfigurationError, match="Gesamtleistung"):
        proportional_clipping({"a": 1e308, "b": 1e308}, inverter_limit)


@pytest.mark.parametrize("invalid_power", [float("inf"), float("-inf"), float("nan")])
def test_clipping_rejects_non_finite_power(invalid_power: float) -> None:
    """Nicht endliche berechnete Leistungen bekommen keinen Wetter-Fallback."""

    with pytest.raises(InvalidConfigurationError, match="Dachleistung"):
        proportional_clipping({"a": invalid_power}, None)


def test_interval_energy_overflow_is_controlled() -> None:
    """Eine endliche Leistung darf bei längerer Integration nicht unendlich werden."""

    with pytest.raises(InvalidConfigurationError, match="Intervallenergie"):
        calculate_forecast(
            (roof(power=1e308),),
            {"roof_1": (weather(0.1, -1e6, minutes=360),)},
            None,
            date(2026, 8, 23),
            TIMEZONE,
        )


@pytest.mark.parametrize("day_offset", [0, 1])
def test_roof_daily_sum_overflow_is_controlled(day_offset: int) -> None:
    """Endliche Stunden dürfen zusammen keine unendliche Dachenergie ergeben."""

    points = tuple(
        weather(
            0.1, -1e6, end=datetime(2026, 8, 23 + day_offset, hour, tzinfo=TIMEZONE)
        )
        for hour in range(1, 7)
    )
    with pytest.raises(InvalidConfigurationError, match="Tagesenergie"):
        calculate_forecast(
            (roof(power=1e308),),
            {"roof_1": points},
            None,
            date(2026, 8, 23),
            TIMEZONE,
        )


@pytest.mark.parametrize("day_offset", [0, 1])
def test_system_daily_sum_overflow_is_controlled(day_offset: int) -> None:
    """Endliche Dach-Tageswerte können ohne zeitgleiche Leistungsspitze überlaufen."""

    roofs = (roof("a", power=1e308), roof("b", power=1e308))
    points = {
        item.id: tuple(
            weather(
                0.1 if (hour <= 3) == (item.id == "a") else 0,
                -1e6,
                end=datetime(2026, 8, 23 + day_offset, hour, tzinfo=TIMEZONE),
            )
            for hour in range(1, 7)
        )
        for item in roofs
    }
    with pytest.raises(InvalidConfigurationError, match="Tagesenergie"):
        calculate_forecast(roofs, points, None, date(2026, 8, 23), TIMEZONE)


def test_large_finite_results_remain_allowed() -> None:
    """Die Endlichkeitsgrenze erzwingt keine willkürlichen fachlichen Höchstwerte."""

    assert calculate_dc_power_kw(roof(power=1), weather(gti=1e308)) == 1e305
    assert proportional_clipping({"a": 8e307, "b": 8e307}, None) == {
        "a": 8e307,
        "b": 8e307,
    }
