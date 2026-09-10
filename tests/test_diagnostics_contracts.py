"""Unabhängige Diagnose-Positivlisten gegen Statusdrift der Erzeuger absichern."""

import ast
import inspect
from datetime import UTC, datetime

from custom_components.pv_forecast import (
    calibration,
    calibration_configuration,
    calibration_runtime,
    diagnostics,
    forecast_cache_runtime,
    history_runtime,
    measurement_runtime,
    storage,
)
from custom_components.pv_forecast.health import HealthState, check_health


def _literal_strings(node):
    """Nur feste Zuweisungswerte lesen, keine Texte aus deren Bedingungen."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _literal_strings(node.body) | _literal_strings(node.orelse)
    return set()


def _assigned_strings(module, names):
    """Direkte und bedingte Statuszuweisungen ausschließlich im Test erfassen."""
    result = set()
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Attribute):
                name = target.attr
            elif isinstance(target, ast.Name):
                name = target.id
            elif isinstance(target, ast.Subscript) and isinstance(
                target.slice, ast.Constant
            ):
                name = target.slice.value
            else:
                continue
            if name in names:
                result.update(_literal_strings(node.value))
    return result


def _health_statuses(group, field):
    """Die vom Betriebscheck erkannten Kennungen anhand seiner Ausgabe bestimmen."""
    candidates = {
        node.value
        for node in ast.walk(ast.parse(inspect.getsource(check_health)))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    recognized = set()
    for value in candidates:
        state = HealthState(
            loaded=True,
            forecast=None,
            timezone="UTC",
            calibration_mode="off" if value == "off" else "observe",
            **{field: value},
        )
        findings = check_health(state, datetime(2026, 8, 23, tzinfo=UTC))
        if any(
            finding.group == group and not finding.code.endswith("_unknown")
            for finding in findings
        ):
            recognized.add(value)
    return recognized


def test_calibration_statuses_match_producers_and_health():
    """Erzeugte und im Betriebscheck bekannte Lernstatus müssen exportierbar sein."""
    produced = _assigned_strings(calibration, {"status"}) | _assigned_strings(
        calibration_runtime, {"status"}
    )
    assert produced == diagnostics._CALIBRATION_STATUSES
    assert _health_statuses("calibration", "calibration_status") == produced


def test_forecast_cache_statuses_match_producers_and_health():
    """Auch Speicherfehler des Cache-Schreibpfads gehören zum Statusvertrag."""
    produced = _assigned_strings(forecast_cache_runtime, {"status", "write_status"})
    assert produced == diagnostics._FORECAST_CACHE_STATUSES
    assert _health_statuses("cache", "cache_status") == produced


def test_storage_errors_match_all_optional_managers():
    """Mess-, Archiv- und Lernmanager samt gemeinsamem Schreibpfad bleiben abgedeckt."""
    produced = set()
    for module in (measurement_runtime, history_runtime, calibration_runtime, storage):
        produced.update(_assigned_strings(module, {"_storage_error", "write_error"}))
    assert produced == diagnostics._STORAGE_ERRORS


def test_calibration_modes_match_configuration():
    """Neue angebotene Lernmodi benötigen eine bewusste Aufnahme in die Diagnose."""
    assert diagnostics._CALIBRATION_MODES == calibration_configuration._MODES.keys()
