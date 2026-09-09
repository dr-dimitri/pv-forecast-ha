"""Der externe Beispielpfad erhält Energie, absolute Zeiten und Datenlücken."""

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from io import StringIO

import pytest

from custom_components.pv_forecast.models import (
    DailyYield,
    ForecastResult,
    TotalForecastInterval,
)
from custom_components.pv_forecast.services import _serialize_forecast
from scripts.emhass_forecast import convert, main

NOW = datetime(2026, 10, 25, tzinfo=UTC)


def response():
    forecast = ForecastResult(
        date(2026, 10, 25),
        {},
        DailyYield(3, 0),
        (
            TotalForecastInterval(NOW, NOW + timedelta(hours=1), 1, 1),
            TotalForecastInterval(
                NOW + timedelta(hours=1), NOW + timedelta(hours=2), 2, 2
            ),
        ),
    )
    return _serialize_forecast(forecast, "Europe/Berlin", NOW, True)


def test_timestamped_emhass_watts_preserve_fold_and_energy():
    result = convert(response(), NOW, NOW + timedelta(hours=2), 30, NOW)
    values = result["pv_power_forecast"]
    assert list(values.values()) == [1000, 1000, 2000, 2000]
    assert result["prediction_horizon"] == 4
    assert list(values) == [
        "2026-10-25 00:00:00+00:00",
        "2026-10-25 00:30:00+00:00",
        "2026-10-25 01:00:00+00:00",
        "2026-10-25 01:30:00+00:00",
    ]
    assert sum(values.values()) * 0.5 / 1000 == 3


def test_partial_interval_is_energy_weighted():
    result = convert(
        response(), NOW + timedelta(minutes=45), NOW + timedelta(minutes=75), 30, NOW
    )
    assert list(result["pv_power_forecast"].values()) == [1500]


@pytest.mark.parametrize("fault", ["gap", "overlap", "fallback", "stale", "version"])
def test_unsafe_external_input_is_rejected(fault):
    data = deepcopy(response())
    if fault == "gap":
        data["intervals"].pop()
    elif fault == "overlap":
        data["intervals"].append(data["intervals"][0])
    elif fault == "fallback":
        data["intervals"][0]["quality_flags"] = ["missing_gti"]
    elif fault == "stale":
        data["last_update_success"] = False
    else:
        data["schema_version"] = 999
    with pytest.raises(ValueError):
        convert(data, NOW, NOW + timedelta(hours=2), 30, NOW)


@pytest.mark.parametrize("value", [True, False, 1.0, "1", None])
def test_schema_version_requires_an_actual_supported_integer(value):
    """Insbesondere True bezeichnet keinen unterstützten Prognosevertrag."""

    data = response()
    data["schema_version"] = value
    with pytest.raises(ValueError, match="Vertrag unbekannt"):
        convert(data, NOW, NOW + timedelta(hours=2), 30, NOW)


@pytest.mark.parametrize("value", [None, [], True, "Prognose"])
def test_malformed_json_root_gets_an_explained_validation_error(value):
    with pytest.raises(ValueError, match="Prognoseantwort muss ein JSON-Objekt"):
        convert(value, NOW, NOW + timedelta(hours=2), 30, NOW)


@pytest.mark.parametrize("value", [None, {}, "Intervalle"])
def test_intervals_require_a_json_array(value):
    data = response()
    data["intervals"] = value
    with pytest.raises(ValueError, match="Prognoseintervalle müssen eine JSON-Liste"):
        convert(data, NOW, NOW + timedelta(hours=2), 30, NOW)


@pytest.mark.parametrize("value", [None, [], True, "Intervall"])
def test_each_interval_requires_a_json_object(value):
    data = response()
    data["intervals"][0] = value
    with pytest.raises(ValueError, match="Prognoseintervall muss ein JSON-Objekt"):
        convert(data, NOW, NOW + timedelta(hours=2), 30, NOW)


def test_cli_reports_malformed_json_without_an_unhandled_traceback(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        [
            "emhass_forecast.py",
            "--start",
            NOW.isoformat(),
            "--end",
            (NOW + timedelta(hours=2)).isoformat(),
        ],
    )
    monkeypatch.setattr("sys.stdin", StringIO("[]"))
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "Die Prognoseantwort muss ein JSON-Objekt sein" in output.err
    assert "Traceback" not in output.err
    assert output.out == ""
