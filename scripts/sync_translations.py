"""Die derzeit deutsch ausgelieferten Sprachressourcen erzeugen oder prüfen.

strings.json ist die Pflegequelle für de.json und den aktuellen deutschen
en.json-Fallback. Bewusst freigegebene weitere Übersetzungen gehören nicht zu
diesen erzeugten Ressourcen; bei ihrer Einführung wird diese Zuordnung angepasst.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

GENERATED_LANGUAGES = ("de", "en")
INTEGRATION_DIR = (
    Path(__file__).resolve().parents[1] / "custom_components" / "pv_forecast"
)


def synchronize(integration_dir: Path, *, check: bool) -> bool:
    """Nur die ausdrücklich erzeugten Sprachdateien mit der Pflegequelle abgleichen."""

    source = json.loads((integration_dir / "strings.json").read_text(encoding="utf-8"))
    serialized = json.dumps(source, ensure_ascii=False, indent=2) + "\n"
    consistent = True
    for language in GENERATED_LANGUAGES:
        target = integration_dir / "translations" / f"{language}.json"
        if check:
            if (
                not target.exists()
                or json.loads(target.read_text(encoding="utf-8")) != source
            ):
                print(
                    f"{target}: weicht von strings.json ab; "
                    "bitte Sprachdateien erzeugen."
                )
                consistent = False
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(serialized, encoding="utf-8")
    return consistent


def main() -> None:
    """Pflegebefehl und rein lesenden CI-Abgleich anbieten."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Nur prüfen, nichts ändern."
    )
    args = parser.parse_args()
    raise SystemExit(0 if synchronize(INTEGRATION_DIR, check=args.check) else 1)


if __name__ == "__main__":
    main()
