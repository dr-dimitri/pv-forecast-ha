"""Die Pflegequelle und gezielt erzeugten deutschen Sprachressourcen absichern."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.sync_translations import INTEGRATION_DIR, main, synchronize


def test_generated_translation_resources_are_current() -> None:
    """Der reguläre Testlauf erkennt abweichende ausgelieferte Sprachdateien."""

    assert synchronize(INTEGRATION_DIR, check=True)


@pytest.mark.parametrize("language", ["de", "en"])
def test_ci_check_detects_translation_drift_without_changing_files(
    tmp_path: Path, language: str
) -> None:
    """Ein abweichender Text stoppt die CI; der Pflegebefehl stellt ihn wieder her."""

    (tmp_path / "strings.json").write_text('{"title": "PV-Prognose"}', encoding="utf-8")
    assert synchronize(tmp_path, check=False)
    target = tmp_path / "translations" / f"{language}.json"
    target.write_text('{"title": "Veraltete Beschriftung"}', encoding="utf-8")
    with (
        patch("scripts.sync_translations.INTEGRATION_DIR", tmp_path),
        patch("sys.argv", ["sync_translations.py", "--check"]),
        pytest.raises(SystemExit) as result,
    ):
        main()
    assert result.value.code == 1
    assert (
        json.loads(target.read_text(encoding="utf-8"))["title"]
        == "Veraltete Beschriftung"
    )
    assert synchronize(tmp_path, check=False)
    assert synchronize(tmp_path, check=True)


def test_generated_resources_do_not_overwrite_other_translations(
    tmp_path: Path,
) -> None:
    """Die aktuelle Sprachzuordnung schreibt keine Gleichheit aller Sprachen vor."""

    (tmp_path / "strings.json").write_text('{"title": "PV-Prognose"}', encoding="utf-8")
    (tmp_path / "translations").mkdir()
    translated = tmp_path / "translations" / "fr.json"
    translated.write_text('{"title": "Prévision photovoltaïque"}', encoding="utf-8")
    original = translated.read_bytes()
    assert synchronize(tmp_path, check=False)
    assert synchronize(tmp_path, check=True)
    assert translated.read_bytes() == original
