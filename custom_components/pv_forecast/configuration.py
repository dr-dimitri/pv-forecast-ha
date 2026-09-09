"""Umwandlung persistierter Config-Entry-Daten in Domänenmodelle."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from .calculations import (
    InvalidConfigurationError,
    validate_inverter_groups,
    validate_roof,
)
from .const import (
    CONF_AZIMUTH,
    CONF_GROUP_ID,
    CONF_GROUP_MAX_POWER_KW,
    CONF_GROUP_ROOF_IDS,
    CONF_INSTALLED_POWER_KWP,
    CONF_INVERTER_GROUPS,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_LOSS_FACTOR,
    CONF_NAME,
    CONF_ROOF_ID,
    CONF_ROOFS,
    CONF_TILT,
    CONF_TIME_ZONE,
)
from .models import AcInverterGroup, PvRoof
from .shading import CONF_HORIZON_PROFILES, validate_profile


def location_fingerprint(data: Mapping[str, Any]) -> str:
    """Physische Standortgrenzen ohne Adresse oder Anzeigenamen kennzeichnen."""

    location: dict[str, float | str | None] = {
        key: float(data[key]) if data.get(key) is not None else None
        for key in (CONF_LATITUDE, CONF_LONGITUDE)
    }
    location[CONF_TIME_ZONE] = str(data[CONF_TIME_ZONE])
    encoded = json.dumps(location, sort_keys=True, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


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
    profiles = options.get(CONF_HORIZON_PROFILES, {})
    if not isinstance(profiles, Mapping) or set(profiles) - {roof.id for roof in roofs}:
        raise InvalidConfigurationError("Horizontprofile benötigen vorhandene Dach-IDs")
    try:
        return tuple(
            replace(roof, horizon_profile=validate_profile(profiles.get(roof.id, ())))
            for roof in roofs
        )
    except ValueError as err:
        raise InvalidConfigurationError(str(err)) from err


def inverter_groups_from_options(
    options: Mapping[str, Any], roofs: tuple[PvRoof, ...] | None = None
) -> tuple[AcInverterGroup, ...]:
    """Optionale reale AC-Gruppen ohne Änderung bestehender Dachwerte lesen."""

    raw_groups = options.get(CONF_INVERTER_GROUPS, ())
    if not isinstance(raw_groups, list | tuple):
        raise InvalidConfigurationError("Die AC-Gruppen müssen eine Liste sein")
    groups = []
    try:
        for data in raw_groups:
            if not isinstance(data, Mapping) or not isinstance(
                data.get(CONF_GROUP_ROOF_IDS), list | tuple
            ):
                raise InvalidConfigurationError("Die AC-Gruppenkonfiguration fehlt")
            groups.append(
                AcInverterGroup(
                    id=data[CONF_GROUP_ID],
                    name=data[CONF_NAME],
                    max_power_kw=data[CONF_GROUP_MAX_POWER_KW],
                    roof_ids=tuple(data[CONF_GROUP_ROOF_IDS]),
                )
            )
        if groups:
            configured_roofs = (
                roofs if roofs is not None else roofs_from_options(options)
            )
            validate_inverter_groups(
                groups, tuple(roof.id for roof in configured_roofs)
            )
    except (KeyError, TypeError, OverflowError) as err:
        raise InvalidConfigurationError(
            "Die AC-Gruppenkonfiguration ist ungültig"
        ) from err
    return tuple(groups)
