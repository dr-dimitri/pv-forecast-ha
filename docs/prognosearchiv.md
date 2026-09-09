# Prognosestände und ehrlicher Soll-Ist-Vergleich

Das Prognosearchiv wird bewusst in der Einrichtung oder unter **Konfigurieren →
Prognosearchiv** aktiviert. Es beobachtet die vorhandenen gemeinsamen Prognosen
und lokalen Messwerte; es fragt keine zusätzlichen Geräte, Wetteranbieter oder
Recorder-Daten ab. Ohne Energiequelle werden Prognosen erfasst, Bewertungen
bleiben fehlend. Pausieren beendet die neue Erfassung und erhält das vorhandene
Archiv für Ansicht und Export.

## Auswahlregeln vor der Erfassung

| Horizont | Ziel | Letzter zulässiger Zeitpunkt | Höchstalter am Stichtag |
| --- | --- | --- | --- |
| `daily_previous_18` | Ganzer lokaler Zieltag | 18 Uhr am Vortag | 2 Stunden |
| `daily_same_06` | Ganzer lokaler Zieltag | 06 Uhr am Zieltag | 2 Stunden |
| `hourly_1h` | Eine volle UTC-Stunde | 1 Stunde vor ihrem Beginn | 1 Stunde |
| `hourly_3h` | Eine volle UTC-Stunde | 3 Stunden vor ihrem Beginn | 1 Stunde |

Sowohl Abruf als auch tatsächliche Beobachtung müssen im zulässigen
Auswahlfenster liegen. Bis zum Stichtag kann ein neuerer rechtzeitiger Kandidat
den bisherigen Kandidaten ersetzen. Danach bleibt die gewählte Prognose
unverändert. Eine spätere Verbesserung und ein rückdatierter Replay nach einem
bereits beobachteten Stichtag dürfen den alten Wert nicht ersetzen. Der
Beobachtungsstand wird mit gespeichert. Nicht rechtzeitig erfasste Prognosen
bleiben fehlend; es wird keine Vergangenheit aus neuen Wetterdaten aufgebaut.

Die Tagesgrenzen und Stichtage beziehen sich auf die gespeicherte IANA-Zeitzone
der Anlage, nicht auf die Anzeigezeitzone des Browsers. Tagesziele können 23
oder 25 Stunden haben. Stundenstichproben verwenden ausschließlich volle,
eindeutige UTC-Stunden. Ihr Zieltag ist das lokale Datum ihres Beginns. Die an
den äußeren Prognosegrenzen gekürzten Intervalle in Teilstundenzonen werden
nicht als zusätzliche Stundenstichproben gezählt. Dadurch entsteht bei einem
Tageswechsel keine zweite, überlappende Version derselben Stunde. Eine Stunde,
die über lokale Mitternacht hinausreicht, wird erst nach ihrem absoluten Ende
bewertet. Tagesprognosen verwenden weiterhin ihren gesamten lokalen Zieltag;
Messwerte werden niemals proportional an gewünschte Grenzen angepasst.

Eine Tagesprognose bis 06 Uhr kann bereits vergangene Stunden desselben Tages
enthalten. Deshalb wird dieser Horizont getrennt von der Vortagsprognose
berichtet und nicht als einheitlicher Vorhersageabstand ausgegeben.

## Unveränderliche Prognose und korrigierbare Messung

Jeder Datensatz enthält UTC-Zielgrenzen, Zieltag, Horizont, Stichtag, Abruf- und
Beobachtungszeit, Anlagenzeitzone, Modellversion und einen Fingerabdruck der
physischen Konfiguration sowie der Energie-Messgrenzen. Anzeigenamen ändern
diesen Bezug nicht. Optionale Leistungssensoren gehören nicht zur
Energieauswertung. Die Ausgabezeit des Wettermodells ist unbekannt und bleibt
`null`. Eingabefallbacks werden markiert; sie sind keine gemessene Prognosegüte.

Die Bewertung verwendet die tatsächlich erfassten Einzelquellen und Segmente
aus dem [Messdatenvertrag](messdaten.md). Gültige Zählerdifferenzen werden erst
je Quelle gebildet und dann addiert. Eine Messquelle darf nicht doppelt
zugeordnet werden; Gesamt- und Teilzähler müssen in der Einrichtung als
überschneidungsfrei bestätigt sein. Es findet keine nochmalige Summierung
aufgrund mehrerer Energy-Dashboard-Zuordnungen statt.

Eine genau begrenzte Energiemenge kann trotz einer Erfassungslücke für ihr
gesamtes Fenster belegt sein. Sie belegt aber keine frei erfundene Verteilung
auf Unterstunden. Deshalb entscheidet die Energieabdeckung des konkreten
Zielintervalls; zeitliche Lücken und Qualitätsmarkierungen bleiben zusätzlich
sichtbar. Eine aktuell nicht erreichbare Quelle macht einen früher vollständig
belegten Zeitraum nicht rückwirkend unbekannt.

Echte nachträgliche Korrekturen erzeugen eine neue datierte Bewertung. Bis zu
drei frühere Bewertungen bleiben zusätzlich zum aktuellen Stand erhalten.
Eine Tageskorrektur kann eine vorherige Bewertung als ungültig ausweisen.
Ein regulärer Ablauf oder Mengen-Purge der kurzlebigen Messrohkopien ist
hingegen keine Korrektur und entfernt keine bereits belegte Archivbewertung.
Bei anderem Sensor, anderer Energie-Messgrenze oder einer Konfigurationsänderung
im Zielintervall werden unpassende Daten ausgeschlossen. Zahlen einer fremden
Quellidentität werden nicht in die Messkopie eines alten Ziels übernommen.

## Kennzahlen für 7, 30 und 90 Tage

Die Berichte verwenden abgeschlossene lokale Kalendertage. Jeder der vier
Horizonte hat seine eigene Stichprobe und zeigt:

- erwartete Zielintervalle, tatsächlich rechtzeitig gespeicherte Prognosen und
  gültige Messpaare;
- Datenabdeckung als gültige Paare geteilt durch erwartete Zielintervalle;
- mittleren absoluten Fehler (MAE) in kWh;
- mittleren vorzeichenbehafteten Fehler (Bias) in kWh;
- konkrete Ausschlussgründe, etwa fehlende Prognose, unvollständige Messung,
  andere Messgrenze oder bewusste Löschung.

Für jedes gültige Paar gilt `Fehler = Prognose − tatsächlicher Ertrag`.
`MAE = Summe(|Fehler|) / Anzahl`, `Bias = Summe(Fehler) / Anzahl`.
Positiver Bias bedeutet Überschätzung, negativer Bias Unterschätzung. Große
Fehler werden nicht zum Verbessern der Kennzahl entfernt. Auch gültig gemessene
Nulltage gehören zur Auswertung. Es gibt keine MAPE an Nulltagen und keine
umgerechnete „Genauigkeit in Prozent“. Ohne Stichprobe sind Fehlerwerte `null`,
nicht null Fehler. Mehrere Ausschlussgründe können dasselbe Ziel betreffen;
ihre Summe ist deshalb keine zusätzliche Stichprobenzahl.

Die erwartete Anzahl umfasst auch Tage vor Aktivierung oder während einer
Pause. Wenige vorhandene Datensätze werden dadurch nicht als vollständige
90-Tage-Erprobung dargestellt. Aufbewahrungskürzungen werden zusätzlich als
`retention_truncated` ausgewiesen.

## Freiwilliger Vergleich

Optional lassen sich bereits vorhandene fremde Tagesprognosesensoren für heute
und morgen in Wh/kWh auswählen. Die Zuordnung bestätigt ausdrücklich denselben
lokalen Tagesbezug, dieselbe Anlagenzeitzone und dieselbe gesamte AC-PV-Messgrenze.
Eine bloß ähnliche Entity-Bezeichnung belegt das nicht. Eine Statistikklasse
wird für Prognosesensoren nicht verlangt.

Die Integration kopiert den vorhandenen Zustand am eigenen Erfassungszeitpunkt.
Ein unbekannter, veralteter oder lediglich durch HA wiederhergestellter Wert
bleibt fehlend. Die Quelle und ihr HA-Berichtszeitpunkt werden zusammen mit
der eigenen Prognose eingefroren. Das mittlere Datenalter **am Stichtag** wird
für beide Seiten getrennt gezeigt; der HA-Berichtszeitpunkt ist kein behaupteter
Ausgabezeitpunkt des fremden Wettermodells.

Der Vergleich verwendet ausschließlich identische gültige Zielintervalle für
beide Prognosen und weist diese gemeinsame Paaranzahl aus. Er ist kein Beleg
für generell bessere Leistung auf anderen Anlagen. `calibrated_comparison`
vergleicht nur tatsächlich am Stichtag angewendete Korrekturen aus der optionalen
[Selbstkalibrierung](kalibrierung.md) mit dem Rohmodell auf identischen gültigen
Zielen. Reine Testkandidaten zählen dort nicht als angewendete Korrektur.
Ohne solche Werte bleibt die korrigierte Variante `null`.

## Lokaler Speicher, Löschen und Lebenszyklus

Der private HA-Store `pv_forecast.history.<entry_id>` hat seit #23 Version 3, unabhängig
von Config-Entry-Schema 1.1 und Messstore-Version. Stundenstände werden maximal
90 Tage und Tagesbewertungen maximal 365 Tage aufbewahrt. Zusätzlich gelten
höchstens 6.000 Zieldatensätze, drei frühere Bewertungsrevisionen pro Datensatz
und insgesamt 32 MiB. Älteste Datensätze werden bei Bedarf zuerst entfernt;
ein gekürztes Archiv wird kenntlich gemacht.

Normale Änderungen werden zu festen Fünfminutenterminen geschrieben. Weitere
Ereignisse verschieben einen bereits geplanten Termin nicht. Damit gibt es
höchstens 288 reguläre Schreibvorgänge pro Tag; Entladen und ausdrücklich
bestätigte Löschungen können zusätzlich schreiben. Bei hartem Prozessabbruch
kann das letzte noch ungeschriebene Stück fehlen. Unbekannte Speicherversionen
werden nicht überschrieben; ein Archivfehler wird getrennt angezeigt.

Die Migration aus Version 1 erhält vorhandene Daten unverändert. Neue Stände
speichern zusätzlich `basis` (UTC-Intervalle mit Gesamtleistung vor Clipping
und damaligem AC-Limit), gegebenenfalls `applied_factor`, `applied_candidate_id`
und `calibrated_energy_kwh` sowie den rechtzeitig bekannten Testkandidaten
(`candidate_factor`, `candidate_id`, `candidate_energy_kwh`). Alte Stände erhalten
keine rückwirkend erfundene Basis. Die ursprüngliche `raw_energy_kwh` bleibt
auch bei aktiver Kalibrierung unkorrigiert. Die eigene historische Kartenlinie
verwendet die tatsächlich wirksame eingefrorene Energie.

Die Bewertung gibt den HA-Eventloop zwischen Arbeitsschritten frei und erzeugt
keine unbegrenzten parallelen Auswertungen. Beim Entladen enden Listener und
laufende Bewertungen. Eine Löschung im entladenen Zustand startet sie nicht
neu. Das gesamte Archiv kann in den Optionen nach ausdrücklicher Bestätigung
gelöscht werden; erst ein neuer rechtzeitig beobachteter Abruf beginnt wieder.

Das gezielte Löschen einer Messquelle entfernt auch ihre Archiv-Messkopien und
Bewertungsrevisionen. Geschlossene gelöschte Ziele werden nicht beim nächsten
Minutentakt wieder aufgebaut. Ihre ursprünglichen Prognosestände können als
Prognosen erhalten bleiben. Originalsensoren und Recorder-Historie werden
nicht verändert. Beim Entfernen der Anlage wird ihr Archiv mit gelöscht.
Quellen- und Archivlöschung verwerfen außerdem die davon abhängigen Lernbelege
und kehren zum Grundmodell zurück. Die vorhandene berechtigungsgeprüfte
Archivantwort enthält unter `calibration` den aktuellen Lernstatus.

## Leseaktionen und Export

`pv_forecast.get_history` verlangt `config_entry_id`, bietet `days` als
7/30/90 (Standard 30) und optional `include_records`. Die Antwortversion 1
enthält Zeitzone, Fenster, Zustand der Erfassung, Aufbewahrungsmarkierung und
`horizons` mit den vier getrennten Kennzahlengruppen. Auf ausdrücklichen Wunsch
enthält `records` die datierten Prognosen und Bewertungen einschließlich
begrenzter Revisionen.

`pv_forecast.export_history` verlangt zusätzlich `format` (`json` oder `csv`).
Die Antwort enthält `filename`, `mime_type` und den Dateiinhalt in `content`.
Sie schreibt oder veröffentlicht keine Datei automatisch. JSON enthält auch
Metadaten und Kennzahlen; CSV führt die Zieldatensätze mit JSON-kodierten
verschachtelten Metadaten und Revisionen auf. Die Dateien bleiben erst dann an
einem anderen Ort, wenn du den Inhalt dort bewusst speicherst oder weitergibst.

Beide Aktionen prüfen neben der Anlage alle aufbewahrten externen
Quellidentitäten einschließlich früherer Mess- und Vergleichsquellen. Nicht
mehr eindeutig auflösbare Identitäten sind Administratoren vorbehalten.
Leseaktionen lösen weder neue Abrufe noch eine Umschreibung alter Daten aus.

## Statistikentscheidung und offene Erprobung

Die vorhandenen Prognosesensoren behalten `state_class=None`. Revidierbare
Vorhersagen sind keine fortlaufenden Erzeugungszähler; ihre Änderungen sollen
nicht in Zählerstatistiken akkumuliert werden. Die native Energy-Anbindung
bleibt unabhängig davon nutzbar. Grundlage ist der
[Home-Assistant-Sensorvertrag](https://developers.home-assistant.io/docs/core/entity/sensor/).

Die Offline-Tests ersetzen weder die fünf realen Nutzer aus #26 noch die
geplanten mindestens 30 gültigen Erprobungstage und mehrere freiwillige Anlagen
für allgemeine Güteaussagen. Solche Ergebnisse werden erst nach tatsächlicher
Erfassung ausgewiesen, einschließlich Misserfolgen, Stichprobe und Abdeckung.

## Aktuelle feste Stundenstände für die Karte

Mit `current_targets: true` ergänzt `get_history` die separate Struktur
`current_targets` (Darstellungsversion 1). Sie enthält nur bereits eingefrorene
`hourly_1h`-Prognosen, deren UTC-Ziele heute oder morgen in der Anlagenzeitzone
überlappen. Noch ersetzbare Kandidaten vor ihrem Stichtag bleiben verborgen.
Die Linie heißt **„Jeweils 1 Stunde vorher“**, weil sie verschiedene
Erfassungszeitpunkte enthält. Die regulären 7-/30-/90-Tage-Metriken und
`records` bleiben auf abgeschlossene Tage begrenzt. Das Lesen ändert weder
Prognosen noch Bewertungen und gibt hier keine Messkopien aus.

An lokalen Teilstunden-Tagesgrenzen werden ausschließlich die zusätzlichen
Darstellungsintervalle anteilig geteilt. `source_start`, `source_end` und
`raw_energy_kwh` erhalten den vollständigen ursprünglichen Stundenbezug; die
Archivdatensätze bleiben identisch. So zeigt konstante Leistung am Tagesrand
keinen künstlichen Unterschied zwischen aktueller und historischer Energie.

## Getrennte Anlagenkonfigurationen

Ein [Standortwechsel](standortwechsel.md) erhält die ursprüngliche Zeitzone
jedes Archivstands. Die aktuelle Berichtssumme zählt nur die aktive Kombination
aus Konfigurationskennung und Zeitzone. `configuration_groups` enthält die
getrennten früheren Vergleichsgrundlagen; deren Werte bleiben exportierbar.
Die Migration zu Store 3 deutet keine alten Tagesgrenzen um.

[Wechselrichtergruppen](wechselrichtergruppen.md) erweitern die unveränderliche
Kalibrierungsbasis bei gruppierten Anlagen um die ursprüngliche DC-Aufteilung
und die damaligen AC-Grenzen. Der eigene Basisvertrag hat dann Version 2.
Damit gilt jeder geprüfte Faktor weiterhin vor beiden Begrenzungsstufen.
Bestehende Basen ohne Gruppen bleiben unverändert lesbar.


### Archivversion 4: optionaler kurzfristiger Beobachtungsversuch

Seit dem Folgeschritt zu #30 ergänzt der Archiv-Store rechtzeitig eingefrorene
Korrekturkandidaten und optionale Resttagsstände ab 12 Uhr. Die Versionen 1 bis 3
werden verlustfrei gelesen; ältere Datensätze erhalten keine erfundenen
Versuchsbelege. Config-Entry-Schema 1.1 und Mess-/Lern-Store bleiben unverändert.
Details: [Kurzfristiger Vergleich](kurzfristiger-vergleich.md).
