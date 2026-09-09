# Lokales Tages-Erfahrungsband

Die vorhandene Aktion `pv_forecast.get_history` liefert zusätzlich unter
`uncertainty` eine vorsichtige Bandansicht. Sie liest ausschließlich das aktivierte
Archiv und seine bereits bestätigten AC-Messungen. Es entstehen keine neuen
Sensoren, Wetterabrufe, Aktionen, Dateien oder Stores. Die vorhandenen Leserechte
der Anlage und aller betroffenen Messquellen gelten auch für die Bandansicht.

## Welche Prognose wird dargestellt?

Ein Band gehört immer zu einem tatsächlich eingefrorenen Tagesstand:

| Horizont | Zentralwert | Stichtag |
| --- | --- | --- |
| `daily_previous_18` | Ganzer lokaler Zieltag | 18 Uhr am Vortag |
| `daily_same_06` | Ganzer lokaler Zieltag | 06 Uhr am Zieltag |

Für heute wird der jüngste bereits feste, zur aktuellen Anlage passende dieser
Stände ausgewählt. Für morgen liegt der erste feste Stand erst nach dem
Vortagsstichtag vor. Die Anlage bestimmt die lokale Zeitzone. Der ausgegebene
Zentralwert bleibt die damalige Punktprognose; er ist weder der Mittelwert der
Bandgrenzen noch eine neu trainierte Ertragsschätzung. Bei schiefen Fehlern muss
er nicht in der Mitte oder innerhalb des Bandes liegen. Abruf-/Beobachtungszeit
und Stichtag bleiben unterscheidbar; eine Wettermodell-Ausgabezeit ist unbekannt.

Der aktuelle Sensorwert kann sich seitdem geändert haben. Dieses Tagesband darf
deshalb nicht als Band um die jüngste Live-Prognose dargestellt werden. Für
gleitenden Restertrag, nächste 60 Minuten und beliebige andere Planungsfenster
fehlen passende rechtzeitig archivierte historische Fenster. Ihre Antwort
lautet ausdrücklich `unsupported_horizon`. Stundenquantile werden weder addiert
noch auf Tages- oder Restfenster übertragen.

## Vorab festgelegte Regel 1

Die Berechnung verwendet die letzten 180 lokalen Tage und verlangt pro Horizont
und kompatibler Vergleichsgruppe 60 gültige Trainingstage sowie 30 danach liegende
Prüftage. Maßgeblich sind Anlagenkonfiguration einschließlich AC-Limit, Zeitzone,
Modellversion und bestätigte Identität der Energiequellen. Registry-Umbenennungen
erhalten die Identität. Quellen-, Messgrenzen- oder inkompatible Anlagenänderungen
vermischen die Daten nicht. Es gibt keine Zusammenlegung verschiedener Horizonte,
um die Mindeststichprobe zu erreichen.

Rohmodell und tatsächlich angewendete Kalibrierung bilden getrennte Varianten.
Für das Rohmodell sind dessen unverändert gespeicherte Originalwerte auch an
Tagen mit angewendeter Kalibrierung vorhanden. Die kalibrierte Variante benötigt
jeweils einen tatsächlich angewendeten, archivierten Wert. Damalige Faktorwerte
und Kandidaten-IDs teilen dieselbe Kalibrierungsmethode nicht täglich auf.
Regel 1 der Kalibrierung ist die einzige derzeit unterstützte Methode. Jede
künftige inkompatible Methodenänderung muss den archivierten Modellvertrag
versionieren und die Bandgruppierung ausdrücklich anpassen; eine Kandidaten-ID
ersetzt keine Methodenversion. Unbekannte Archiv-Modellverträge werden weiterhin
kontrolliert zurückgewiesen.

Die jüngsten 30 geeigneten abgeschlossenen Tage bilden die Prüfung. Für das
Training werden die jüngsten 60 früheren Fälle gewählt, deren Messbewertungen
bereits vor der Ausgabe des ersten Prüfforecasts bekannt waren. Eine erst danach
erfasste Messkorrektur darf nicht rückwirkend ins Training gelangen. Vorhandene
Bewertungsrevisionen erlauben die damalige Sicht; fehlt diese belegbare Revision,
fehlt der Fall. Die höchstens drei früheren Archivrevisionen werden dafür nicht
erweitert. Rund um den Vortagsstichtag ist zusätzlich ein zeitlicher Abstand
notwendig: Der vorhergehende Tag war um 18 Uhr noch nicht vollständig gemessen.
Deshalb garantieren auch 90 Kalendertage noch keine ausreichend große Stichprobe.

Das signierte Residuum ist gemessene Tagesenergie minus damalige Tagesprognose.
Die sortierten 60 Trainingsresiduen liefern den 6. und 54. Wert als untere und
obere Verschiebung. Das vorab festgelegte interne Ziel sind zentrale 80 Prozent.
Die breite Referenz verwendet kleinstes und größtes Trainingsresiduum. Alle vier
Verschiebungen bleiben während der Prüfung fest. Weder ein Prüfergebnis noch ein
späteres Residuum passt sie innerhalb dieser Prüfung an.

Vollständig gemessene Nulltage bleiben gültig. Fehlende Messung, fehlende
Wettereingaben und gelöschte Quellen werden nicht als Nullfehler eingesetzt.
Große Fehler werden nicht nachträglich aussortiert. Für einen bereits
ausgegebenen Tagesstand verwendet auch eine spätere Archivabfrage höchstens das
Wissen von dessen tatsächlichem Beobachtungszeitpunkt.

## Prüfung von Abdeckung und Breite

Band und Referenz werden bei jedem Prüffall zuerst auf null und, falls bekannt,
auf AC-Leistungsgrenze mal tatsächliche UTC-Tagesdauer begrenzt. Ein Tag mit
Zeitumstellung kann 23 oder 25 Stunden haben. Auch eine Messung außerhalb einer
bekannten physikalischen Grenze wird nicht allein wegen ihres großen Fehlers
aus der Prüfung entfernt.

Die Ausgabe verlangt mindestens 70 Prozent beobachtete Abdeckung auf den 30
Prüftagen. Zusätzlich darf der mittlere Winkler-Score nicht schlechter sein als
derjenige der breiten Trainingsreferenz auf denselben Fällen. Der Score bewertet
Bandbreite und die Entfernung außerhalb des Bandes gemeinsam. Bei Ziel 80 Prozent
beträgt die Fehlbetragsstrafe zehnmal die Entfernung zur verfehlten Grenze.
Damit zählt eine größere Breite nicht automatisch als bessere Prognose.
Das zugrunde liegende Bewertungskonzept ist im
[Lehrbuch der Autoren Hyndman und Athanasopoulos](https://otexts.com/fpp3/distaccuracy.html)
beschrieben. Die hiesigen Stichproben- und Freigabeschwellen sind ausdrücklich
Projektentscheidungen.

Die Antwort nennt Zielabdeckung, beobachtete Abdeckung, mittlere Breite,
Referenzbreite, beide Scores, Fallzahlen und die getrennten Zeiträume. Ein
95-Prozent-Wilson-Intervall zeigt zusätzlich die Unsicherheit des beobachteten
Trefferanteils. Die verwendete Formel beschreibt das
[NIST-Handbuch](https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm).
Dieser Bereich ist hier ausdrücklich nur indikativ: Er setzt unabhängige Tage
voraus; aufeinanderfolgende Wetterlagen können diese Annahme verletzen. Er ist
kein zweites Energieband und kein Nachweis einer garantierten 80-Prozent-Abdeckung.

Auch ein bestandenes Ergebnis heißt weiterhin **Erfahrungsband**, ohne
P10/P90-Etiketten. Wetterlagen und Jahreszeiten können die Fehlerverteilung
verändern. Die nächsten Tagesstände verschieben die zeitlich geordnete Auswahl
und prüfen die Methode erneut. Ein späterer saisonaler Wechsel kann die Freigabe
wieder verlieren. Die Prüfung mit echten späteren Messdaten bleibt eine offene
menschliche Erfolgskontrolle; die automatisierten Offline-Tests belegen nur die
Auswahl, Trennung, Berechnung und kontrollierten Leerzustände.

## Antwort und kontrollierter Rückfall

`uncertainty.schema_version` ist 1. `days.today` und `days.tomorrow` enthalten:

- `status`: `available` oder `unavailable`, einen deutschen `label` und
  maschinenlesbare `reasons`;
- `lower_kwh`, `central_kwh`, `upper_kwh`; Grenzen sind bei fehlender oder
  durchgefallener Prüfung immer `null`;
- bei vorhandenem festen Stand `target_date`, `horizon`, `cutoff`,
  `forecast_observed_at` und `variant`;
- Fallzahlen, festgelegte Mindestwerte und nach vollständiger Prüfung
  `training_period`, `validation_period` sowie `evaluation`;
- `quality_flags`, Ausschlusszahlen und das bekannte physikalische Maximum.

`evaluation.coverage_wilson95` kennzeichnet die Unabhängigkeitsannahme mit
`assumption: independent_days` und `indicative_only: true`.
`retention_truncated` bewahrt sichtbar, ob Archivgrenzen alte Daten entfernt
haben. Aufbewahrungsgrenzen werden nicht zugunsten einer Freigabe vergrößert.
Fehlt eine tragfähige Basis, lautet die Darstellung „Bandbreite noch nicht
belastbar“. Es gibt keine ersatzweise erfundene Prozentspanne. Karte und
Automationen lesen dieselben backendseitig berechneten Grenzen.
