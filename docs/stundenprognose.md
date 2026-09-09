# Stundenprognose: Lesevertrag Version 1

`pv_forecast.get_forecast` liest den letzten berechneten Stand der ausgewählten
Anlage. Die Aktion benötigt `config_entry_id` und unterstützt ausschließlich
Aufrufe mit Antwortdaten. Im visuellen HA-Aktionseditor wird die Anlage über
einen Selektor gewählt; ihre interne ID muss dort nicht von Hand ermittelt werden.

```yaml
action: pv_forecast.get_forecast
data:
  config_entry_id: "ID_DER_AUSGEWAEHLTEN_ANLAGE"
response_variable: pv_prognose
```

In einem nachfolgenden Template stehen beispielsweise
`pv_prognose.last_update_success`, `pv_prognose.fetched_at` und
`pv_prognose.intervals` zur Verfügung. Die Aktion ist die gemeinsame öffentliche
Stundenschnittstelle für Automationen und eine spätere Karte. Sie ist gemäß den
[HA-Vorgaben für Aktionen mit Antwortdaten](https://developers.home-assistant.io/docs/dev_101_services/#response-data)
im Integrations-Setup registriert und bleibt nach dem Entladen aufrufbar, um einen
verständlichen Fehler zurückzugeben.

## Antwortformat

| Feld | Bedeutung |
| --- | --- |
| `schema_version` | Ganzzahl `1`; unabhängig von Config-Entry- und Release-Version |
| `timezone` | Gespeicherte IANA-Zeitzone der Anlage |
| `forecast_start_date` | Lokales Startdatum des berechneten Zweitagesstands als `YYYY-MM-DD` |
| `fetched_at` | Tatsächlicher Zeitpunkt des letzten erfolgreichen Abrufs, ISO 8601 in UTC |
| `model_issued_at` | Immer `null`, da die Ausgabezeit des Wettermodells unbekannt ist |
| `last_update_success` | Ob der letzte Aktualisierungsversuch erfolgreich war |
| `coverage.start`, `coverage.end` | Äußere Grenzen der vorhandenen Gesamtintervalle, ISO 8601 in UTC; bei leerer Reihe `null` |
| `coverage.complete` | Ob beide Tage dieses datierten Stands vollständig und lückenlos abgedeckt sind |
| `intervals` | Nach absolutem Beginn geordnete Liste der Gesamtintervalle |

Jedes Intervall enthält `start`, `end`, `energy_kwh`, `ac_power_kw`,
`quality_flags` und `is_complete`. Zeitpunkte sind ISO-8601-Zeichenketten in UTC;
Intervalle sind halb offen: Der Beginn gehört dazu, das Ende zum Folgeintervall.
`energy_kwh` ist die Energie der Gesamtanlage nach dem gemeinsamen
Wechselrichterlimit, `ac_power_kw` ihr mittleres Leistungsniveau im Intervall.
Die Zeitreihe wird einmal je Berechnung aus den geclippten Dachbeiträgen erzeugt.

Die äußeren Intervalle sind auf die zwei lokalen Prognosetage begrenzt. Bei
Zeitzonen mit Teilstundenoffset haben diese Randintervalle eine kürzere Dauer
und entsprechend anteilige Energie. Die mittlere Leistung bleibt gleich.
Zeitumstellungen können 23 oder 25 tatsächliche Stunden in einem lokalen Tag
erzeugen; wiederholte Ortsstunden behalten unterschiedliche UTC-Zeitpunkte.

## Datenqualität und Aktualität

`quality_flags` ist eine Liste ohne doppelte Einträge. Eine leere Liste bedeutet,
dass für dieses Intervall kein Eingabefallback nötig war. Sie belegt keine
gemessene Prognosegenauigkeit.

| Markierung | Bedeutung |
| --- | --- |
| `gti_fallback` | Mindestens ein GTI-Eingabewert wurde durch 0 ersetzt |
| `temperature_fallback` | Für mindestens einen Dachbeitrag entfiel die Temperaturkorrektur wegen fehlender oder ungültiger Eingabe |
| `missing_roof_data` | Mindestens ein Dachbeitrag fehlt; das Gesamtintervall ist nicht vollständig |

Vorhandene Wetterzeitpunkte mit ersetztem GTI bleiben gemäß Berechnungsmodell
abgedeckt und liefern 0 für den betroffenen Beitrag. Fehlende Zeitabdeckung ist
etwas anderes: `is_complete: false` kennzeichnet ein unvollständiges Intervall.
Der normale API-Client weist unvollständige Zeitraster bereits vor der
Berechnung ab. Die Angabe im Lesevertrag schützt zusätzlich gegen die Verwendung
unvollständiger interner Daten.

Nach einem fehlgeschlagenen Update bleibt der letzte Snapshot lesbar und
`last_update_success` ist `false`. Ein Minutentick, eine gelesene Antwort oder ein
Tageswechsel setzt diesen Fehler nicht zurück und erneuert `fetched_at` nicht.
Auch `coverage.complete: true` bezieht sich auf `forecast_start_date`, nicht
automatisch auf den heutigen Tag. Verbraucher müssen deshalb Datum, Zeitabdeckung,
Abrufzeit und letzten Update-Erfolg gemeinsam prüfen.

Unbekannte, fremde oder ungeladene Config Entries erhalten keine Prognoseantwort.
Nutzer benötigen Leserechte für die gesamte ausgewählte Anlage. Interne
Automationen ohne Nutzerkontext können den Vertrag lesen. Die Aktion löst keinen
HTTP-Abruf aus, verändert keine Geräte und speichert keine zusätzliche Historie.

## Planungssensoren

Die vier Planungssensoren verwenden denselben Gesamtstand. Restertrag heute
integriert von jetzt bis zur nächsten lokalen Mitternacht; nächste 60 Minuten
integriert exakt 3.600 Sekunden in UTC. Bei 2 kW von 10–11 Uhr und 4 kW von
11–12 Uhr ergeben sich um 10:30 Uhr beispielsweise 3 kWh für die nächste Stunde.
Das ist eine anteilige Integration der Stundenmittel, keine zusätzliche
Wetterauflösung.

Die geschätzte Leistung jetzt entspricht dem mittleren AC-Leistungsniveau im
laufenden Intervall. Der Spitzenzeitpunkt bezeichnet das Intervall mit dem
höchsten mittleren AC-Leistungsniveau des ganzen heutigen Tages. Bei Gleichstand
gilt der früheste absolute Beginn. Ein auf den Tagesbeginn beschnittenes
Randintervall wird nicht allein wegen seiner kürzeren Dauer abgewertet.
Ein vollständig ertragloser Tag hat keinen Spitzenzeitpunkt; ein lückenhafter
Tag erlaubt keine verlässliche Spitzenbestimmung.

Vollständig abgedeckte Nullfenster ergeben 0. Fehlende Abdeckung liefert keinen
Zahlenwert. Die Sensorverfügbarkeit berücksichtigt zusätzlich den normalen
Coordinator-Fehlerstatus. Ein gemeinsamer Minutentakt führt diese Werte aus dem
gespeicherten Stand nach und wird beim Entladen beendet. Abruffrist, Fehlerstatus,
Erfolgszeitpunkt und Anbieterpause bleiben dabei erhalten.
