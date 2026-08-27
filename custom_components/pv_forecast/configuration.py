"""Umwandlung persistierter Config-Entry-Daten in Domänenmodelle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .calculations import InvalidConfigurationError, validate_roof
from .const import (
    CONF_AZIMUTH,
    CONF_INSTALLED_POWER_KWP,
    CONF_LOSS_FACTOR,
    CONF_NAME,
    CONF_ROOF_ID,
    CONF_ROOFS,
    CONF_TILT,
)
from .models import PvRoof


def roof_from_dict(data: Mapping[str, Any]) -> PvRoof:
    """Persistierte Dachwerte lesen und normalisieren."""

    try:
        roof = PvRoof(
            id=str(data[CONF_ROOF_ID]),
            name=str(data[CONF_NAME]).strip(),
            installed_power_kwp=float(data[CONF_INSTALLED_POWER_KWP]),
            compass_azimuth_deg=float(data[CONF_AZIMUTH]),
            tilt_deg=float(data[CONF_TILT]),
            loss_fraction=float(data[CONF_LOSS_FACTOR]) / 100,
        )
    except (KeyError, TypeError, ValueError) as err:
        raise InvalidConfigurationError("Dachkonfiguration ist unvollständig") from err
    validate_roof(roof)
    return roof


def roofs_from_options(options: Mapping[str, Any]) -> tuple[PvRoof, ...]:
    """Alle Dachflächen aus Config-Entry-Optionen lesen."""

    raw_roofs = options.get(CONF_ROOFS)
    if not isinstance(raw_roofs, Sequence) or isinstance(raw_roofs, str | bytes):
        raise InvalidConfigurationError("Dachkonfiguration fehlt")
    roofs = tuple(
        roof_from_dict(item) for item in raw_roofs if isinstance(item, Mapping)
    )
    if len(roofs) != len(raw_roofs) or not roofs:
        raise InvalidConfigurationError(
            "Mindestens eine gültige Dachfläche ist erforderlich"
        )
    if len({roof.id for roof in roofs}) != len(roofs):
        raise InvalidConfigurationError("Dach-IDs müssen eindeutig sein")
    return roofs
