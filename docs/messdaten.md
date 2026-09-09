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
| `deltas` | Die ursprünglichen Grenzen der verwendeten Zählerdifferenzen mit Segment-ID und Qualitätsmarkierungen |
| `readings` | Die im Fenster enthaltenen Messpunkte, insbesondere für einen Leistungsverlauf ohne Energieintegration |
| `identity_unresolved` | Die bestätigte Registry-Identität ist momentan nicht eindeutig auflösbar |

Eine unvollständige beobachtete Energiemenge darf nicht als vollständiger
Tagesertrag angezeigt oder als vollständige Lerngrundlage benutzt werden.
`total_energy` summiert erst nach der Auswertung jeder einzelnen Energiequelle.
Leistungssensoren tragen nichts zu dieser Summe bei. Die Quellanzahl und die
Vollständigkeit der Einzelquellen bleiben ausgewiesen.

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
eigene Version 1. Die gespeicherten Messungen und Differenzen sind begrenzt auf
sieben Tage, 20.000 Messpunkte und höchstens 20.000 zugehörige
Zählerdifferenzen je Quelle. Bei hoher Meldefrequenz kann zuerst
das Mengenlimit greifen. Die Speicherung bündelt Schreibvorgänge auf einen feststehenden Termin
spätestens 60 Sekunden nach der ersten ungeschriebenen Änderung. Weitere
Meldungen verschieben ihn nicht; geschrieben wird der dann aktuelle Stand.
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
