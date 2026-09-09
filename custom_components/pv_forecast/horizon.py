"""Begrenzter optionaler Prognosehorizont ohne Home-Assistant-Abhängigkeit."""

from collections.abc import Mapping
from typing import Any

CONF_FORECAST_DAYS = "forecast_days"
DEFAULT_FORECAST_DAYS = 2
MAX_FORECAST_DAYS = 7


def validate_forecast_days(value: object) -> int:
    """Nur ganze Tage von zwei bis sieben akzeptieren, auch vom Zahlenselektor."""
    if (
        type(value) not in (int, float)
        or not DEFAULT_FORECAST_DAYS <= value <= MAX_FORECAST_DAYS
        or value != int(value)
    ):
        raise ValueError("Der Prognosehorizont benötigt zwei bis sieben ganze Tage")
    return int(value)


def forecast_days_from_options(options: Mapping[str, Any]) -> int:
    """Bestehende Einträge ohne neue Option unverändert mit zwei Tagen behandeln."""
    return validate_forecast_days(
        options.get(CONF_FORECAST_DAYS, DEFAULT_FORECAST_DAYS)
    )
