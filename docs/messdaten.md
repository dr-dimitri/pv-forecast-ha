# Lokale Messdaten und ihre Aussagegrenzen

`pv_forecast.get_measurements` liest gespeicherte Messungen, ohne Geräte,
Wetterdienste oder Recorder-Historie abzufragen. Die Auswahl der Anlage sowie
`start` und `end` sind verpflichtend. Zeitpunkte müssen einen UTC-Offset tragen;
die Auswertung normalisiert sie nach UTC. Lokale Tage werden mit der gespeicherten
IANA-Anlagenzeitzone begrenzt und können bei Zeitumstellungen 23 oder 25 Stunden
haben.

## Antwortversion 1

Die Antwort enthält `schema_version`, `timezone`, die UTC-Grenzen `start` und
`end`, `sources`, `total_energy` sowie die Aufbewahrungsgrenzen `retention_days`
und `max_readings_per_source`. Sie ist ein Messdatenvertrag; die getrennte
Prognoseaktion `get_forecast` wird dadurch nicht geändert.

Jede Quelle liefert ihre bestätigte `source_id`, Entity- und Registry-Identität,
Messart (`total`, `daily`, `power`), Messgrenze `scope`, Kennzeichnung
`derived_energy` und Datensegmente. Der letzte gemeldete Zustand ist vom letzten
gültigen Messwert getrennt. Energie wird in kWh, Leistung in kW ausgegeben;
Messzeitpunkte bezeichnen die lokale HA-Zustandsmeldung, keinen zusätzlich
verifizierten Gerätezeitpunkt. Verspätete HA-Ereignisse werden gekennzeichnet.
Ein Sensor, der selbst alte Gerätedaten als neuen Zustand meldet, kann ohne
einen verlässlichen Gerätezeitvertrag nicht als solcher erkannt werden.

| Feld | Bedeutung |
| --- | --- |
| `energy_kwh` | Summe der vollständig im Fenster enthaltenen gültigen Quelldifferenzen; bei fehlenden Differenzen `null`, bei belegtem Nullertrag `0` |
| `energy_complete` | Die Energiemenge deckt das ganze Fenster ohne wechselnde Datensegmente exakt ab |
| `complete` | Zusätzlich ist die zeitliche Abdeckung ohne markierte Erfassungslücken belegt |
| `coverage_seconds` | Durch ausreichend zeitnahe, gültige Differenzen belegte Sekunden je Quelle |
| `quality_flags` | Ursachen und Einschränkungen; keine gemessene Prognosegüte |
| `deltas` | Die Grenzen der verwendeten Zählerdifferenzen mit Segment-ID und Qualitätsmarkierungen; gesunde Abschnitte können innerhalb derselben UTC-Minute verlustfrei zusammengefasst sein |
| `readings` | Die im Fenster aufbewahrten Messpunkte, insbesondere für einen Leistungsverlauf ohne Energieintegration; überflüssige Zwischenpunkte verdichteter Energiedifferenzen entfallen |
| `identity_unresolved` | Die bestätigte Registry-Identität ist momentan nicht eindeutig auflösbar |

Eine unvollständige beobachtete Energiemenge darf nicht als vollständiger
Tagesertrag angezeigt oder als vollständige Lerngrundlage benutzt werden.
`total_energy` summiert erst nach der Auswertung jeder einzelnen Energiequelle.
Leistungssensoren tragen nichts zu dieser Summe bei. Die Quellanzahl und die
Vollständigkeit der Einzelquellen bleiben ausgewiesen.

Die ergänzende Summe `current_location_total_energy` verwendet dieselben
Felder und dasselbe angefragte UTC-Fenster, berücksichtigt aber ausschließlich
das aktuelle Standortsegment. `total_energy` und die Einzelquellen behalten
auch historische Messanteile. Die Karte verwendet den aktuellen Teilwert für
„Ist heute“; fehlende oder unvollständige Messung nach einem Standortwechsel
wird nicht durch früheren Ertrag ersetzt. Mit einem älteren Backend ohne das
Zusatzfeld verwendet die Karte weiterhin dessen `total_energy`.

## Lücken und Auflösung

Beispiel: Ein fortlaufender Zähler meldet um 10:00 Uhr 100 kWh und nach einem
Ausfall um 12:00 Uhr 104 kWh. Wenn die Differenz plausibel ist, belegt sie
4 kWh für genau dieses zweistündige Fenster. Sie liefert keine belegte Aufteilung
auf 10–11 und 11–12 Uhr. Die Abfrage eines solchen Unterfensters schneidet die
Differenz deshalb nicht proportional zu; `boundary_gap` markiert die Grenze.
Ein Abfragefenster mit grob belegter Gesamtmenge kann `energy_complete=true`,
aber wegen der Erfassungslücke `complete=false` melden.

`unknown`, `unavailable`, ungültige Einheiten und ungültige Zahlen bleiben
unbekannte Messungen. Eine aktuelle erneute Meldung desselben gültigen
Zählerstands belegt dagegen Nullertrag. Ausbleibende Meldungen werden gegenüber
dem je Quelle gewählten maximalen Meldeintervall als `stale` kenntlich gemacht.
Ein Neustart markiert seine unbeobachtete Strecke. Ein vor dem Neustart
wiederhergestellter HA-Zustand ist kein neuer gültiger Messwert.

Bei einem Integral-Helfer ergänzt `max_sub_interval` zeitgesteuerte
Zwischenstände, wenn der Leistungssensor zwischenzeitlich keinen neuen Zustand
meldet. Es drosselt nicht die von diesem Sensor ausgelösten Aktualisierungen.
Ein auf fünf Minuten gestellter Helfer kann deshalb weiterhin sekündlich neue
Energiewerte liefern. Ein vorhandener fortlaufender AC-Energiezähler vermeidet
die zusätzliche Näherung aus Leistungswerten; ein Helfer bleibt an deren
Verfügbarkeit und Meldeverhalten gebunden.

## Rückgänge, Resets und Austausch

Ein fallender fortlaufender Zähler ohne belegten `last_reset` beginnt ein neues
Segment und erzeugt keinen positiven Ertrag aus der Rückwärtsdifferenz. Bei
Tageszählern beginnt ein neuer lokaler Tag eine neue Basis; der unbekannte Rest
des Vortags wird dabei nicht ergänzt. Ein ausdrücklich zeitlich belegter Reset
kann die Energiemenge nach dem Reset erklären, aber keinen fehlenden Stand davor.
Eine nachträgliche Abwärtskorrektur eines Tageszählers macht die betroffene
Tagesauswertung unvollständig. Vorherige Teilmengen werden nicht unbeobachtet
als weiterhin vollständiger Tageswert ausgegeben.

Ein bewusst geänderter Sensor, eine andere Messart oder eine geänderte
Messgrenze beginnt ein neues Datensegment. Die Quelle behält ihre Zuordnungs-ID;
ihre früheren Segmente behalten ihre damaligen Metadaten. Eine bloße
Entity-Umbenennung folgt der unveränderten Registry-ID. Aus historischen
Segmenten dürfen spätere Lernfunktionen keine stillen Überträge zwischen
unterschiedlichen Anlagen ableiten.

## Speicherung und Berechtigungen

Der private lokale HA-Store `pv_forecast.measurements.<entry_id>` hat eine
eigene Version 3. Versionen 1 und 2 werden verlustfrei übernommen; seit #23
behalten die Messsegmente ihren ursprünglichen Standort- und Zeitzonenbezug.
Alte Stände erhalten keine erfundenen Kürzungsereignisse. Version 3 ergänzt bei
tatsächlichem Verlust durch die Mengenbegrenzung optionale `retention_losses`
mit ihrem ursprünglichen Segmentbezug.

Die gespeicherten Messungen und Differenzen bleiben begrenzt auf sieben Tage,
20.000 Messpunkte und höchstens 20.000 zugehörige Zählerdifferenzen je Quelle.
Gesunde zusammenhängende Energieabschnitte innerhalb derselben UTC-Minute,
desselben lokalen Tages und desselben Segments werden verlustfrei verdichtet.
Überflüssige Rohzwischenpunkte entfallen, die belegte Energiemenge und die
äußeren UTC-Grenzen bleiben erhalten. Lücken, Resets, Korrekturen und Übergänge
zwischen Nullertrag und positiver Energie werden nicht zusammengezogen.

Wenn sich häufige Sonderfälle nicht verdichten lassen, kann weiterhin die harte
Mengenbegrenzung greifen. Eine solche Kürzung bleibt sichtbar und belegt keine
vollständige Erfassung. Der Betriebscheck warnt vor verlorenen Messabschnitten
des laufenden lokalen Tages am aktuellen Standort, auch wenn die Quelle wieder
frische Werte liefert. Ein neuer vollständig erfasster Tag kann wieder ohne
diese Tageswarnung beginnen. Reguläre Alterslöschung nach sieben Tagen ist kein
solches Kürzungsereignis.

Reguläre Schreibungen erfolgen zu festen Fünfminutenterminen, höchstens 288 pro
Tag. Neue Meldungen verschieben den nächsten Termin nicht. Der zu schreibende
Stand wird von der laufenden Erfassung entkoppelt; JSON-Kodierung und Dateiarbeit
laufen im Executor, damit sie die HA-Ereignisverarbeitung nicht blockieren.
Bei einem harten Prozessabbruch kann das letzte noch nicht geschriebene Zeitstück fehlen.
Ein Neustart macht diese Lücke sichtbar. Beim Entladen werden Listener beendet
und ausstehende Daten gespeichert.

Die Leseaktion verlangt neben den Rechten der Anlage auch Leserechte auf alle
betroffenen externen Quellen, einschließlich aufbewahrter früherer Identitäten.
Ist eine solche Identität nicht mehr auflösbar, ist der Zugriff auf ihre
Historie Administratoren vorbehalten. Interne HA-Automationen ohne Benutzer-
Kontext folgen dem üblichen internen Aktionsweg.

Gezieltes Löschen in den Quellenoptionen entfernt nur die eigenen Messkopien.
Die nächste Meldung startet neu. Das Entfernen der gesamten Integration löscht
ihren Store. Originalsensoren und Recorder-Historie werden dabei nicht verändert.
Eine unbekannte Speicherversion oder ein Lesefehler deaktiviert ausschließlich
den Messpfad und erscheint als `storage_error` in der Antwort; der Store wird
dabei nicht überschrieben. Die Prognose bleibt weiter verwendbar.

## Technische und menschliche Abnahme

Offline-Tests prüfen Zeitgrenzen, Einheiten, Nullwerte, Lücken, Resets,
Korrekturen, Austausch, Umbenennung, Neustart, Löschung und Berechtigungen.
Die zusätzliche Erprobung aus #26 mit fünf realen PV-Anwendern ist weiterhin
offen. Für sie gilt: mindestens vier müssen ihre richtige Messquelle ohne
YAML und ohne Hilfe zuordnen. Beobachtete Fehlzuordnungen, benötigte Hilfe und
benötigte Zeit sind zu dokumentieren; ein automatisierter Flow-Test ersetzt
diese Erprobung nicht.

Grundlagen: [HA-Sensorvertrag](https://developers.home-assistant.io/docs/core/entity/sensor/),
[HA-Ereignis-Listener](https://developers.home-assistant.io/docs/integration_listen_events/)
und [PV-Messung im Energy-Dashboard](https://www.home-assistant.io/docs/energy/solar-panels/).

## Explizite Intervalle für die Karte

Die Leseaktion akzeptiert zusätzlich `interval_windows` mit höchstens 50
nicht überlappenden absoluten `start`-/`end`-Fenstern über insgesamt höchstens
48 Stunden. `total_intervals` liefert dafür die genau belegte Gesamtenergie,
mittlere AC-Leistung und Qualitätsmarkierungen. Ein künftiges oder nicht
vollständig belegtes Intervall hat `energy_kwh: null`. Ganze Zählerdifferenzen
werden weder an Fenstergrenzen geteilt noch proportional auf Stunden verteilt.
Die je Quelle gültigen Deltas und die gemeinsame Aggregation bleiben die
Berechnungsgrundlage; ein großer Rohdatensatz wird dafür nicht je Intervall
vollständig erneut durchsucht.

Zusätzlich enthält jedes Intervall `observed_energy_kwh`: die Summe der bereits
belegten, vollständig innerhalb des Fensters liegenden Zählerdifferenzen nach
allen bestehenden Quellen-, Lücken- und Korrekturregeln. Bei unvollständigen
Stunden ist dies nur eine Teilmenge; `energy_kwh` und `ac_power_kw` bleiben null.
Ohne belegte Differenzen sowie für noch nicht abgeschlossene Fenster bleibt
auch `observed_energy_kwh` null. Gesunde Nullplateaus behalten die bestehende
exakte Randzuordnung. Es gibt keine Hochrechnung auf eine vollständige Stunde.

Die Karte zeigt diese Mengen gemeinsam mit den vollständigen Intervallwerten
als gestrichelten Ist-Verlauf. Legende, Intervallauswahl und Tabelle verwenden
keinen zusätzlichen Erfassungshinweis.
So bleiben positive Messwerte bei üblichen, zur vollen Stunde versetzten
Zählermeldungen sichtbar. Die vollständige Stundenproduktion ist dabei weiterhin
unbekannt. Archiv, Lernen und vollständige Prognosevergleiche verwenden weiterhin
nur ihre bisherigen zulässigen Messwerte.
