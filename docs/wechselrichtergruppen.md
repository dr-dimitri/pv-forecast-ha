# Reale AC-Wechselrichtergruppen

In den Optionen lassen sich mehrere Dachflächen einem gemeinsamen Wechselrichter
beziehungsweise einer gemeinsam begrenzten AC-Gruppe zuordnen. Jede Gruppe hat
einen Namen, eine stabile ID und eine positive maximale AC-Leistung in kW.
Ein Dach gehört höchstens einer Gruppe an. Unzugeordnete Dächer behalten die
bisherige Modellierung mit der optionalen gemeinsamen AC-Anlagengrenze.

Die Zuordnung beschreibt reale AC-Geräte. Zwei unterschiedlich ausgerichtete
Dächer an demselben Wechselrichter teilen sich eine Gruppe. Zwei eigenständige
Wechselrichter erhalten getrennte Gruppen. DC-MPPT-Eingangsgrenzen und eine
Netzeinspeisebegrenzung nach Eigenverbrauch werden damit nicht modelliert. Ein
Hybridwechselrichter mit mehreren DC-Eingängen wird deshalb nicht durch erfundene
separate AC-Limits für seine einzelnen Dachflächen ersetzt. Auch die
[PVSystem-Dokumentation von pvlib](https://github.com/pvlib/pvlib-python/blob/main/pvlib/pvsystem.py)
unterscheidet die Leistungen mehrerer MPPT-Eingänge von der gemeinsamen
AC-Ausgabe des Wechselrichters. Diese Integration behält ihr eigenes einfaches
kWp-/GTI-Modell; pvlib ist keine neue Abhängigkeit.

## Reihenfolge der Berechnung

Zuerst entstehen unverändert die temperatur- und verlustkorrigierten
Dachleistungen. Ein freigegebener Anlagenfaktor multipliziert diese Leistungen
vor jeder Begrenzung. Danach werden die Beiträge jeder Gruppe auf deren
gemeinsame AC-Leistung begrenzt. Überschreitet die Gruppe ihre Grenze, werden
nur ihre Mitgliedsdächer proportional gekürzt. Andere Gruppen behalten ihre
eigenen Beiträge.

Anschließend begrenzt die bisherige optionale Gesamtgrenze die Summe aller schon
gruppenbegrenzten sowie unzugeordneten Beiträge. Auch diese Kürzung ist
proportional. Die Gesamtgrenze bleibt eine maximale AC-Erzeugungsleistung der
Gesamtanlage; sie ist keine automatische Simulation eines Einspeisezählers,
Speichers oder Verbrauchs. Erst anschließend werden die Leistungen mit der
tatsächlichen Intervalllänge in kWh umgerechnet.

| Beispiel eines Intervalls | Ergebnis vor optionaler Gesamtgrenze |
| --- | --- |
| Dach A 8 kW und Dach B 4 kW teilen einen 6-kW-Wechselrichter | A 4 kW, B 2 kW |
| A 8 kW am 3-kW-Gerät, B 4 kW am eigenen 5-kW-Gerät | A 3 kW, B 4 kW |
| Gemeinsame 6-kW-Gruppe A/B plus unzugeordnetes Dach C mit 2 kW | A 4 kW, B 2 kW, C 2 kW |

Wird im letzten Beispiel zusätzlich eine Gesamtgrenze von 6 kW gesetzt,
ergeben sich A 3 kW, B 1,5 kW und C 1,5 kW. Ein Faktor von 0,5 ohne Gesamtgrenze
ergibt dagegen A 4 kW, B 2 kW und C 1 kW: Der Faktor wirkt vor der 6-kW-Gruppengrenze
auf die ursprünglichen Leistungen 8/4/2 kW. Eine Multiplikation der bereits
begrenzten Prognose wäre fachlich falsch.

## Kompatibilität und Archiv

Ohne Gruppen wird unmittelbar derselbe bisherige Berechnungspfad benutzt.
Explizit leere Gruppen ändern weder Float-Ergebnisse noch den bestehenden
physischen Konfigurationsfingerprint. Die Regression prüft gespeicherte
Float-Bitmuster und den Hash des vorherigen `main`-Stands. Vorhandene Dach-IDs,
Anwenderwerte, Anlagenlimits und Config-Entry-Schema 1.1 bleiben erhalten.

Gruppen-IDs, Grenzen und Zuordnungen gehören zum physischen Fingerprint;
Anzeigenamen gehören nicht dazu. Eine neue Grenze oder Zuordnung verwirft damit
die Freigabe eines auf der alten physischen Anlage geprüften Lernfaktors. Alte
Archivdaten bleiben als getrennte Vergleichsgruppe erhalten.

Für gruppierte Anlagen erhält die unveränderliche Kalibrierungsbasis den
eigenen Vertrag `schema_version: 2`. Jeder UTC-Abschnitt speichert neben der
bisherigen ungekürzten Gesamtleistung die ursprünglichen Leistungen je Gruppe
und der unzugeordneten Dächer. Die zugehörigen Gruppenlimits und die Existenz
unzugeordneter Dächer werden ebenfalls eingefroren. Die Prüfung verwirft
doppelte oder fehlende IDs, inkonsistente Summen, Bool-Werte an Zahlenfeldern,
nicht endliche Leistungen und unbekannte Versionen.

Damit berechnen Training, Kandidatenprüfung und spätere Archivbewertung jeden
Faktor korrekt vor den damaligen Gruppen- und Gesamtgrenzen. Die vorhandene
Basis ohne Gruppen bleibt in ihrem bisherigen Datenformat lesbar und erhält
keine erfundenen Gruppen. Archiv-Store 3 führt diese Basis gemeinsam mit der
Unterstützung ursprünglicher Standortzeitzonen ein; Stores 1 und 2 werden
verlustfrei migriert. Der getrennte Lern-Store bleibt bei Version 1.

Sind sämtliche Dächer Gruppen zugeordnet, begrenzt das Tages-Erfahrungsband
zusätzlich anhand der Summe der Gruppenlimits. Bei unzugeordneten Dächern lässt
sich daraus keine Grenze für die ganze Anlage ableiten; dann gilt nur eine
eventuell konfigurierte Gesamtgrenze. Abdeckung wird nach Anwendung dieser
physikalischen Grenzen geprüft.

Alle Änderungen an Faktoren und Gruppengrenzen bleiben lokale Berechnungen.
Der reguläre gemeinsame Wetterabruf bleibt unverändert, und die Anzahl der
Sensoren oder Gruppen erzeugt keine zusätzlichen Wetterabrufe. Die vorhandenen
Tages- und Stundendaten, Energy-Ansicht und Karte verwenden dieselbe berechnete
Zeitreihe.
