"""Tests der persistenten Konfigurationsgrenze."""

import pytest

from custom_components.pv_forecast.calculations import InvalidConfigurationError
from custom_components.pv_forecast.configuration import (
    roof_from_dict,
    roofs_from_options,
)
from custom_components.pv_forecast.const import CONF_ROOFS

from .helpers import persisted_roof


def test_incomplete_roof_is_rejected() -> None:
    """Unvollständige Config-Entry-Daten werden verständlich abgewiesen."""

    with pytest.raises(InvalidConfigurationError):
        roof_from_dict({})


@pytest.mark.parametrize(
    "options",
    [{}, {CONF_ROOFS: "kein Array"}, {CONF_ROOFS: []}, {CONF_ROOFS: [object()]}],
)
def test_missing_or_malformed_roof_list_is_rejected(options: dict) -> None:
    """Nur eine nicht leere Liste gültiger Dachobjekte ist zulässig."""

    with pytest.raises(InvalidConfigurationError):
        roofs_from_options(options)


def test_duplicate_roof_ids_are_rejected() -> None:
    """Stabile Dach-IDs müssen innerhalb eines Config Entries eindeutig sein."""

    with pytest.raises(InvalidConfigurationError):
        roofs_from_options(
            {CONF_ROOFS: [persisted_roof("duplicate"), persisted_roof("duplicate")]}
        )
