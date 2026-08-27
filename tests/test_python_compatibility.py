"""Regressionstests für die von Home-Assistant-Installationen genutzte Syntax."""

from __future__ import annotations

import ast
from pathlib import Path


def test_integration_is_compatible_with_python_313_syntax() -> None:
    """Alle ausgelieferten Python-Dateien müssen mit Python 3.13 parsebar sein."""

    integration_root = Path(__file__).parents[1] / "custom_components" / "pv_forecast"
    for path in integration_root.rglob("*.py"):
        ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
            feature_version=(3, 13),
        )
