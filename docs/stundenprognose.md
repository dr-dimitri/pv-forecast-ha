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

## Optionale Darstellungsansicht für die Karte

`get_forecast` kann mit `include_view: true`, `day: today|tomorrow` und einer
optionalen stabilen `roof_id` aufgerufen werden. Die vorhandene Gesamtantwort
bleibt erhalten; `view` Version 1 ergänzt ausgewählte Tagesintervalle, fertige
Tages-/Restwerte, Dachnamen, Serverzeit, Anlagenzeitzone und absolute lokale
Tagesgrenzen. Die Dachwerte stammen aus denselben bereits geclippten Beiträgen.
Alte Forecast-Tage werden nicht unter dem aktuellen Datum ausgegeben. Diese
Ansicht löst keinen weiteren Wetterabruf aus und wird nicht in Sensorattributen
vervielfacht. Details zur Darstellung stehen in der [Kartenanleitung](karte.md).

## Frei gewähltes Energie- und Leistungsfenster

`get_forecast` kann mit `window` zusätzlich ein halb offenes Fenster `[start,end)`
auswerten. Beide Grenzen brauchen einen UTC-Offset oder `Z`. Der Start bleibt
unverändert, auch mit Sekundenanteilen. Ohne `step_minutes` werden nur Summe und
Abdeckung geliefert; mit 5, 15, 30 oder 60 ganzen Minuten entsteht ein am Start
verankertes Raster. Die Dauer muss exakt teilbar sein, maximal 336 Schritte und
höchstens der konfigurierte lokale Prognosehorizont sind erlaubt.

```yaml
# HA-Scriptsequenz: drei Stunden ab einem gemeinsamen aktuellen Zeitpunkt lesen.
sequence:
  - variables:
      pv_start: "{{ utcnow().isoformat() }}"
  - action: pv_forecast.get_forecast
    data:
      config_entry_id: DEINE_ANLAGEN_ID
      window:
        start: "{{ pv_start }}"
        end: "{{ (as_datetime(pv_start) + timedelta(hours=3)).isoformat() }}"
    response_variable: pv
  - condition: template
    value_template: >-
      {{ pv.window.schema_version == 1 and pv.window.status == 'available'
         and pv.window.energy_kwh is not none }}
  # Eigene Folgeaktionen erst nach dieser Prüfung bewusst ergänzen.
```

Für sechs Stunden in 30-Minuten-Schritten dieselbe Anfrage mit `hours=6` und
`step_minutes: 30` im `window` verwenden. Das erzeugt zwölf Intervalle mit
`start`, `end`, `energy_kwh` und `mean_ac_power_kw`. Ein einstündiger Wert von
2 kWh liefert in jeder halben Stunde 1 kWh bei 2 kW mittlerer Leistung.

Die Antwort `window` hat eine eigene `schema_version: 1`, `scope: total`,
`start`, `end`, `as_of`, den echten `fetched_at`, `timezone`, `status`, `reason`,
`coverage`, `quality_flags`, `energy_kwh` und `mean_ac_power_kw`. `intervals`
erscheint nur bei angefragtem Raster. Die Energieerhaltung gegenüber der direkten
Fenstersumme wird mit relativer Toleranz 1e-12 und absoluter Toleranz 1e-9 kWh geprüft.

`available` mit **0 kWh** ist eine vollständige Nullprognose. `unavailable` liefert
`null` für Energie/Leistung und keine numerischen Rasterwerte. Gründe sind
`outside_forecast`, `incomplete_forecast`, `stale_forecast` und `input_fallbacks`.
Fehlgeschlagene Abrufe, fehlende/zukünftige Abrufzeit oder ein Alter über 60 Minuten
sperren die Verwendung. Nur Lücken/Qualitätsmängel im angefragten Fenster blockieren
dieses. Niemals fehlende Werte durch `float(0)` ersetzen.

Die Annahme `constant_interval_mean_power` bezeichnet die konstante mittlere
Leistung innerhalb der vorhandenen Wetterintervalle, keine feinere Wetterauflösung.
`uncertainty` bleibt `unavailable` mit `unsupported_horizon`; PV-Ertrag allein ist
kein verfügbarer Überschuss. `window` und `planning` können gemeinsam gelesen werden.
`roof_id` gilt weiterhin nur für die Kartenansicht: das Fenster ist ausdrücklich
die Gesamtanlage. Die normale Prognoseantwort und der bisherige
[EMHASS-Adapter](solarzeitfenster.md) bleiben kompatibel. Keine Abfrage fordert
neue Wetterdaten an oder installiert Schaltaktionen.

Der Aufruf verwendet den nativen Vertrag für
[HA-Aktionen mit Antwortdaten](https://developers.home-assistant.io/docs/dev_101_services/).

## Optionale Erklärung der aktuellen Gesamtprognose

```yaml
action: pv_forecast.get_forecast
data:
  config_entry_id: DEINE_ANLAGE
  day: today
  include_explanation: true
response_variable: prognose
```

`explanation` hat eine eigene `schema_version: 1`, `scope: total`, Datum,
Anlagenzeitzone und UTC-Grenzen des ausgewählten heutigen oder morgigen Tages.
Auch bei `roof_id` bleibt diese Bilanz ausdrücklich auf die Gesamtanlage bezogen.
Ohne Flag bleibt die bisherige Antwort unverändert.

`totals` und `intervals` führen Energie vor Kalibrierung (`before_calibration_kwh`),
Faktorbeitrag (`calibration_delta_kwh`), Gruppenkürzung (`group_clipping_kwh`),
zusätzliche Gesamtkürzung (`total_clipping_kwh`) und `effective_kwh`. Ihre Bilanz
wird gegen die bestehende wirksame Zeitreihe und Tageskennzahl geprüft.
`raw_intervals` liefert direkt die passende Grundmodellkurve mit Faktor 1 nach
realen AC-Grenzen. `raw_model_kwh`, `effective_minus_raw_kwh` und Prozent bei
positiver Basis stehen separat in `totals`; beim Nulltag ist Prozent `null`.

Die Antwort nennt angewendeten Faktor, Herkunft `live/restored`, echte Abrufzeit,
Abruffehler, Alter (`stale`), Vollständigkeit und Qualitätsmerkmale. Ein
wiederhergestellter Stand kann rechnerisch erklärt werden, bleibt aber als solcher
gekennzeichnet und erhält dadurch keine operative Freigabe. Fehlende oder
inkompatible Rohbasis, Abdeckung oder Bilanz liefert `status: unavailable` und
einen Grund ohne numerische Erklärung. Keine alte Basis wird aus der AC-Kurve
rekonstruiert. Die Zwischenstufen entstehen höchstens einmal je Roh-/Faktorgeneration;
Leseaktionen projizieren nur UTC-Überlappungen. Es gibt keinen HTTP-, Mess-,
Archiv- oder Lernzugriff durch die Erklärung.
