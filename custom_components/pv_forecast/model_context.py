"""Version und stabile physische Kennung des aktuellen Prognosemodells."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

from .configuration import inverter_groups_from_options, roofs_from_options
from .const import (
    CONF_AZIMUTH,
    CONF_INSTALLED_POWER_KWP,
    CONF_INVERTER_MAX_POWER_KW,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_LOSS_FACTOR,
    CONF_ROOF_ID,
    CONF_ROOFS,
    CONF_TILT,
    CONF_TIME_ZONE,
)
from .measurements import SourceConfig
from .shading import CONF_HORIZON_PROFILES, HORIZON_RULE_VERSION

MODEL_VERSION = "1"


def _configured_measurements(entry: ConfigEntry) -> tuple[SourceConfig, ...]:
    return tuple(
        SourceConfig.from_dict(source)
        for source in entry.options.get("measurement_sources", [])
    )


def configuration_id(entry: ConfigEntry) -> str:
    """Physische Parameter und Messgrenzen ohne Anzeigenamen kanonisch markieren."""

    roof_keys = (CONF_INSTALLED_POWER_KWP, CONF_AZIMUTH, CONF_TILT, CONF_LOSS_FACTOR)
    roofs = [
        {
            CONF_ROOF_ID: roof[CONF_ROOF_ID],
            **{key: float(roof[key]) for key in roof_keys},
        }
        for roof in entry.options[CONF_ROOFS]
    ]
    physical = {
        CONF_LATITUDE: float(entry.data[CONF_LATITUDE]),
        CONF_LONGITUDE: float(entry.data[CONF_LONGITUDE]),
        CONF_TIME_ZONE: str(entry.data[CONF_TIME_ZONE]),
        CONF_ROOFS: sorted(roofs, key=lambda roof: roof[CONF_ROOF_ID]),
        CONF_INVERTER_MAX_POWER_KW: (
            float(value)
            if (value := entry.options.get(CONF_INVERTER_MAX_POWER_KW)) is not None
            else None
        ),
        "measurements": sorted(
            [
                (source.source_id, source.measurement_identity)
                for source in _configured_measurements(entry)
                if source.kind != "power"
            ],
            key=lambda value: value[0],
        ),
    }
    groups = inverter_groups_from_options(entry.options)
    if groups:
        physical["inverter_groups"] = sorted(
            [
                {
                    "id": group.id,
                    "max_power_kw": float(group.max_power_kw),
                    "roof_ids": sorted(group.roof_ids),
                }
                for group in groups
            ],
            key=lambda group: group["id"],
        )
    profiles = (
        {
            roof.id: list(roof.horizon_profile)
            for roof in roofs_from_options(entry.options)
            if roof.horizon_profile
        }
        if entry.options.get(CONF_HORIZON_PROFILES)
        else {}
    )
    if profiles:
        physical["horizon_shading"] = {
            "rule_version": HORIZON_RULE_VERSION,
            "profiles": profiles,
        }
    encoded = json.dumps(
        physical, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode()).hexdigest()
