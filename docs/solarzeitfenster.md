# Solarzeitfenster und Tagesaussicht

In der Karte öffnest du **Bestes Solarzeitfenster**, wählst die Laufdauer,
den frühesten Start und das späteste Ende und klickst auf **Zeitfenster berechnen**.
Die Auswahl gilt für die Gesamtanlage. Datum und UTC-Offset unterscheiden
auch wiederholte Ortsstunden. Freie, minutengenaue Grenzen sind über die
vorhandene Aktion möglich.

Die Integration sucht die größte Energie eines zusammenhängenden Fensters.
Beispielsweise schlagen zwei benachbarte Stunden mit je 6 kWh eine einzelne
8-kWh-Spitze, deren Nachbarstunden nichts liefern. Innerhalb eines vorhandenen
Wetterintervalls gilt dessen mittlere Leistung; zusätzliche Minutenwerte sind
keine feinere Wettervorhersage. Heute und morgen richten sich nach der
gespeicherten Anlagenzeitzone, die Laufdauer nach verstrichener UTC-Zeit.

## Leseaktion für eigene Automationen

```yaml
action: pv_forecast.get_forecast
data:
  config_entry_id: DEINE_ANLAGEN_ID
  planning:
    duration_minutes: 120
    earliest_start: "2026-09-09T10:00:00+02:00"
    latest_end: "2026-09-09T18:00:00+02:00"
response_variable: solar
```

Die unveränderte Prognoseantwort erhält zusätzlich `planning` mit eigener
`schema_version: 1`, `status`, `reason`, `start`, `end`, `energy_kwh`,
`as_of`, `fetched_at`, `timezone` und `quality_flags`. Erst `status: available`
ist eine nutzbare Empfehlung. `unavailable` bedeutet beispielsweise
`stale_forecast`, `incomplete_forecast`, `input_fallbacks`,
`infeasible_window`, `outside_forecast` oder `no_solar_energy`.
Ein fehlendes Ergebnis enthält keine Ersatzenergie.

Die Daten dürfen höchstens 60 Minuten alt sein und der letzte Abruf muss
erfolgreich sein. Der vollständige Suchbereich benötigt brauchbare Abdeckung;
sonst ist ein behauptetes Maximum nicht belegt. Grenzen brauchen einen
UTC-Offset, die Dauer 1 bis 2880 ganze Minuten. Ein 49-Stunden-Fenster am
Herbstwechsel ist als Suchbereich möglich, die maximale Laufdauer bleibt
2880 Minuten. Es entstehen keine zusätzlichen Wetterabrufe.

Ein Folgeaufruf kann `previous_start` aus der vorigen Antwort mitgeben.
Ein noch zulässiges Fenster wird erst bei **mehr als 5 % und mehr als
0,1 kWh** Vorteil ersetzt; andernfalls steht `hysteresis_applied: true` in
der Antwort, wenn das bisherige Fenster gegenüber dem neuen Maximum erhalten
bleibt. Die absolute 0,1-kWh-Schwelle gilt auch bei einem bisherigen
Nullfenster. Bleibt dadurch ein Fenster ohne prognostizierte Energie erhalten,
lautet die Antwort `unavailable` mit `no_solar_energy`; sie enthält keine neue
Startempfehlung. Ist das bisherige Fenster gestartet oder beendet, liefert die Aktion
`started` beziehungsweise `completed` mit den ursprünglichen Grenzen und
ohne neue Energieempfehlung. Die Karte führt diese Auswahl lokal weiter;
eigene Automationen müssen den bisherigen Start bei Bedarf selbst bewahren.
Ein Neustart der Karte begründet keinen Gerätestatus.

## Bewusst eingerichteter HA-Hinweis

Der [Script-Blueprint](../blueprints/script/pv_forecast/solarzeitfenster.yaml)
kann in Home Assistant importiert werden. Wähle Anlage, Laufdauer und absolute
Zeitgrenzen. Ein bewusster Script-Aufruf erstellt eine HA-Benachrichtigung mit
dem gefundenen Zeitraum oder einem verständlichen Leerzustand. Du kannst den
Aufruf anschließend selbst in einer Automation verwenden; für andere Tage
müssen auch die eingegebenen Datumsgrenzen angepasst werden.

Das Beispiel wird in den Offline-Tests mit dem nativen HA-Script-Runner,
dem echten Integrations-Lesevertrag und abgefangener Benachrichtigung
ausgeführt. Es schaltet keine Geräte. Reale Bedienung durch Testanwender
bleibt eine getrennte Abnahme. Grundlagen:
[HA-Script-Blueprints](https://www.home-assistant.io/docs/blueprint/schema/) und
[HA-Aktionen mit Antwortdaten](https://developers.home-assistant.io/docs/dev_101_services/).

## Geprüfter Datenadapter für EMHASS

[scripts/emhass_forecast.py](../scripts/emhass_forecast.py) wandelt eine gespeicherte
`get_forecast`-JSON-Antwort in die dokumentierte EMHASS-Eingabe
`pv_power_forecast` mit absoluten UTC-Zeitstempeln und mittleren **Watt** um.
Ein explizites Raster wird anhand der Intervallüberlappungen energieerhaltend
gebildet; fehlende, überlappende, markierte oder alte Daten werden abgelehnt.
Das Script liest nur die übergebene Datei und schreibt nach stdout.

```bash
python scripts/emhass_forecast.py \
  --start '2026-09-09T10:00:00+00:00' \
  --end '2026-09-10T10:00:00+00:00' \
  --step-minutes 30 < forecast.json > emhass-pv.json
```

Passe Datumsgrenzen an den aktuellen Forecast und EMHASS-Lauf an. Der
EMHASS-Zeitschritt muss dem gewählten Raster entsprechen; `prediction_horizon`
begrenzt die Zahl der ausgegebenen Schritte. Die Ausgabe enthält ausschließlich
die PV-Eingabe und diesen Horizont. Last, Tarife und gegebenenfalls Speicher
müssen in EMHASS separat korrekt eingerichtet sein. Das Script sendet keine
Anfrage an den Optimierer und löst keine Geräteaktion aus.

Offline geprüft sind Einheiten, Teilintervalle, DST-Fold, Energieerhaltung und
Fehlerfälle gegen den dokumentierten Datenvertrag. Ein vollständiger realer
EMHASS-Betrieb ist damit nicht abgenommen. Referenz:
[EMHASS: eigene Forecast-Daten](https://emhass.readthedocs.io/en/latest/forecasts.html#passing-your-own-forecast-data).

## Heute voraussichtlich insgesamt

Die optionale `include_outlook: true`-Ergänzung von `get_measurements` liefert
unter `outlook` eine getrennte Tagesaussicht für die Gesamtanlage:

- `measured_kwh`: vollständig belegte Energie seit lokaler Mitternacht,
- `measured_until`: letzter gemeinsamer exakter Messzeitpunkt,
- `measurement_age_minutes`: Alter dieses gemeinsamen Zeitpunkts in Minuten,
- `measurement_stale`: mindestens eine beteiligte Quelle hat seit ihrer letzten
  gültigen Meldung die bestätigte `max_interval_minutes`-Meldefrist überschritten,
- `bridge_kwh`: geschätzte Energie von diesem Zeitpunkt bis jetzt,
- `remaining_kwh`: Prognose ausschließlich ab jetzt bis Tagesende,
- `total_kwh`: die überschneidungsfreie Summe, sofern alle Abschnitte vorliegen.

`measurement_quality_flags` und `forecast_quality_flags` trennen die
Qualitätsmarkierungen nach Herkunft. Die bisherige gemeinsame Liste
`quality_flags` bleibt für ältere Karten unverändert bestehen; die Ergänzungen
verwenden weiterhin `schema_version: 1`. Ohne vollständig belegtes Messpräfix
bleiben gemeinsamer Messzeitpunkt und dessen Alter `null`.

Die Messantwort liefert daneben `current_location_total_energy` für das
angefragte Zeitfenster: bereits beobachtete Energie am aktuellen Standort mit
Abdeckung und Qualitätsmarkierungen, auch wenn der Tag noch unvollständig ist.

Beispiel: 8 kWh bis 11:30 Uhr, 0,5 kWh geschätzte Brücke bis 12 Uhr und
12 kWh Restprognose ergeben 20,5 kWh. Die Brücke ist keine Messung. Bei
mehreren Zählern wird eine gemeinsame belegte Grenze benötigt; unterschiedliche
Meldezeiten rechtfertigen keine anteilige Verteilung von Zählerdifferenzen.
Fehlt das Messpräfix oder eine Prognoseabdeckung, bleibt die Tagesaussicht
unvollständig. Die verbleibende Basisprognose bleibt getrennt zugänglich.

Eine halbstündige Brücke ist dabei keine feste Altersgrenze. Meldet eine Quelle
beispielsweise sechs Stunden nichts mehr, bleibt eine ansonsten vollständige
Tagesaussicht verfügbar; diese sechs Stunden stammen vollständig aus der
geschätzten Brücke. In der geöffneten Tagesaussicht erscheinen das Alter des
gemeinsamen Messzeitpunkts und der eigene Hinweis „Der letzte gesicherte Messwert
ist zu alt.“ Die bereits bestätigte Meldefrist jeder Quelle entscheidet über
diesen Hinweis. Individuell frische, versetzt meldende Quellen können eine ältere
gemeinsame Grenze haben, ohne als veraltet zu gelten. Ihr gemeinsames Alter bleibt
trotzdem sichtbar. Diagramm, Tageskennzahlen und Archivbewertung ändern sich nicht.

Es wird kein kurzfristiger Korrekturfaktor angewendet (`correction: off`).
Die Addition bereits bekannter Messenergie belegt keine bessere Vorhersage.
Der [Beobachtungsversuch zu #30](kurzfristiger-vergleich.md) speichert
rechtzeitige Zukunftskandidaten; seine reale Güteprüfung bleibt offen. PV-Erzeugung ist kein verfügbarer Überschuss und kein Nachweis einer
Einsparung ohne passende Verbrauchs-, Speicher- und Tarifdaten.
