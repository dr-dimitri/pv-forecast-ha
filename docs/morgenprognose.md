# Morgenverlauf prüfen und vorsichtig verbessern

Unter **Konfigurieren → Erweiterte Funktionen → Morgenprognose prüfen und
verbessern** stehen Aus, Beobachten und nach erfolgreicher Prüfung automatisch
anwenden zur Verfügung. Standard ist **Aus**. Beobachten verändert keine
Prognose. Archiv und bestätigte AC-Erzeugungszähler müssen bereits eingerichtet
sein; die Auswahl schaltet sie nicht still ein. Eine optionale Messung der
Batterieleistung oder Netzeinspeisung genügt nicht.

Eine passende Tagesmenge beweist keinen zeitlich richtigen Morgenverlauf.
Die Morgenprüfung verwendet deshalb einen eigenen Nachweis. Die gewählte
AC-Schwelle (Standard 0,5 kW, 0,05–100 kW) muss über mindestens 60 Minuten
aufeinanderfolgende Intervallmittel erreicht werden. Das gesamte Nachweisfenster
liegt zwischen dem astronomischen Sonnenaufgang der Anlagenkoordinaten und vier
Stunden danach. Polartage ohne Sonnenaufgang bleiben nicht bewertbar. Zeitzone
und tatsächliche UTC-Dauern berücksichtigen die Zeitumstellung.

Das ist eine Aussage über PV-Erzeugung, keine Zusage zur Hausversorgung. Hauslast,
Tarif und Batterie sind hier unbekannt. Intervallmittel beweisen außerdem keine
kontinuierliche Mindestleistung zwischen den Meldungen.

## Ein begrenzter Kandidat

Regel `morning_redistribution_v1` untersucht ausschließlich elf Koeffizienten
von −0,5 bis +0,5 in Schritten von 0,1. Für die erste und zweite Zweistundenhälfte
des Morgens werden die jeweiligen ursprünglichen DC-Energien bestimmt. Verschoben
wird `Koeffizient × Minimum(beider Energiemengen)`: ein positiver Wert vermindert
die erste und erhöht die zweite Hälfte. Jeder Teilfaktor bleibt zwischen 0,5 und
1,5. Eine leere Hälfte erhält keine erfundene Produktion. Außerhalb des Morgens
ändert sich nichts. Die Gesamt-DC-Energie bleibt erhalten; AC-Clipping kann die
wirksame Tagesenergie verändern und wird deshalb zusätzlich geprüft.

Ein gültiger globaler Anlagenfaktor wird genau einmal berücksichtigt. Danach
wirken die bestehenden AC-Gruppen und das Gesamtlimit in ihrer bisherigen
Reihenfolge. Dachwerte und Gesamtzeitreihe stammen aus derselben Rechnung.
Die vorhandene Rohbasis bleibt unverändert. Die freigegebene Form gilt nur für
Heute und Morgen; spätere Tage des optionalen Mehrtageshorizonts bleiben wie
bisher. Die eigene Haus- oder Speichersteuerung erhält ausschließlich die
wirksame öffentliche Zeitreihe und muss keine zweite Kalibrierung nachbauen.

## Echte Zeitbelege und fester Vorabendstand

Jeder Fall verweist auf einen tatsächlich vor 18 Uhr am Vortag beobachteten
`daily_previous_18`-Archivstand mit vollständiger ursprünglicher DC-Basis.
Die Morgenreferenz selbst wird spätestens bis zu diesem Stichtag festgehalten.
Abrufzeit, tatsächlicher Erfassungszeitpunkt, Stichtag, Zieltag, Sonnenaufgang,
Anlagen-/Quellenkontext und gegebenenfalls bereits bekannter Kandidat bleiben
getrennt. Nachträglich bekannte Wetterdaten oder zusammengesetzte
`hourly_1h`-Stände bilden keinen historischen Vorabendforecast. Ein restaurierter
Forecast erzeugt keine neuen Morgenbelege. Die unbekannte Ausgabezeit des
Wettermodells bleibt unbekannt.

Nach Tagesabschluss wird die Morgen-Zeitspur aus echten lokalen Zählerdifferenzen
gebildet. Positive Differenzen werden niemals anteilig geteilt. Mehrere bestätigte,
überschneidungsfreie AC-Quellen brauchen gemeinsame echte Grenzen. Vollständige
Differenzen werden zu ungefähr fünf Minuten langen Abschnitten zusammengefasst;
kein ausgewerteter Abschnitt darf länger als 15 Minuten sein. Lücken, Resets,
unklare Identitäten und wechselnde Segmente verhindern einen gültigen Zeitbeleg.

Nicht exakt auf eine Zählergrenze fallende äußere Ränder werden nicht erfunden:
der erste beziehungsweise letzte vollständig belegte gemeinsame Zeitpunkt darf
höchstens 15 Minuten innerhalb des Vierstundenfensters liegen. Verglichen wird
auf beiden Seiten ausschließlich diese tatsächliche gemeinsame Teilspanne;
der vollständige 60-Minuten-Nachweis muss darin liegen. Zähler mit groben
Stundenmeldungen oder ohne gemeinsame Grenzen können Tageslernen ermöglichen,
aber keinen freigegebenen Morgenkandidaten. Tageskorrekturen liefern ebenfalls
keine Zeitspur. HA-Meldezeiten sind keine unabhängig bestätigten Gerätezeitpunkte.

## Lernen und spätere Prüfung

Die jüngsten 30 gültigen Morgen innerhalb von 60 lokalen Tagen bilden das
Training. Die primäre Zielmetrik ist vorab fest **MAE der Anstiegszeit in Minuten**.
Der beste Kandidat wird eingefroren; bei Gleichstand gewinnt der kleinste
Betrag. Danach zählen ausschließlich neue, rechtzeitig unter dieser Kandidaten-ID
erfasste Morgen. Mindestens 14 spätere Morgen innerhalb von 28 Tagen sind nötig.
Viele Teilintervalle eines Tages zählen weiterhin nur als ein Fall.

Die Freigabe verlangt auf denselben späteren Fällen gleichzeitig:

- mindestens 5 % geringeren Anstiegszeit-MAE gegenüber der tatsächlich wirksamen
  bisherigen Prognose, deren Zeitfehler positiv sein muss;
- keine höhere Anzahl fälschlich vorhergesagter Anstiege und kein höheres P90
  zu früher Anstiegsfehler;
- höchstens 5 % höheren Morgenenergie- und Tagesenergie-MAE; eine fehlerfreie
  Nebenmetrik darf keinen zusätzlichen Fehler erhalten, abgesehen von einer
  Rechentoleranz von 10⁻¹⁰ kWh.

Ein vollständig beobachteter Morgen ohne Anstieg bleibt Teil der Stichprobe.
Ein nur einseitig vorhandener Anstieg erhält vorab festgelegte 240 Minuten
Zeitfehler; beide Seiten ohne Anstieg erhalten null, werden aber separat gezählt.
Eine Stichprobe ganz ohne tatsächlich beobachteten Anstieg kann nicht freigegeben
werden. Fehlende Messung ist dagegen kein beobachtetes Ausbleiben und wird als
Ausschluss ausgewiesen. Die Schwellen sind Produktregeln, kein statistisches
Konfidenzversprechen.

Nach Freigabe werden die jüngsten 14 passenden Prüftage innerhalb von 28 Tagen
fortlaufend geprüft. Veralteter oder korrigierter Nachweis, Quellenwechsel,
geänderte Anlagenparameter, AC-Schwelle oder globaler Anlagenfaktor entziehen die
Morgenwirkung. Bewusste Wartungs-/Abregelungsmarkierungen und bestehende
Minderertragspausen gelten auch hier. Nach Ablehnung oder abgebrochener Prüfung
wartet ein neuer Versuch 14 Tage. Die übrige Prognose und eine unabhängig
weiterhin gültige globale Kalibrierung bleiben beim Rückfall erhalten.

Diese Prüfung belegt den festen Vorabendhorizont der eigenen Anlage. Sie behauptet
keine gleiche Güte für jeden nachfolgenden Live-Wetterabruf oder fremde Anlagen.
Ohne aktuelle erfolgreiche Wetterbasis wird keine Morgenkorrektur angewendet.
Synthetische Tests prüfen die Mechanik; eine praktische Verbesserung benötigt
weiterhin die tatsächlich später gemessenen Fälle.

## Öffentliche Daten und Speicherung

`pv_forecast.get_forecast` bleibt Schema 1. Seine `intervals`, Dachwerte, Karte,
Sensoren und native Energy-Anbindung verwenden dieselbe wirksame AC-Serie.
Der additive Block `morning` (Version 1) nennt Modus, Zustand, Methode, festgelegte
Metrik/Schwelle, Ziel-/Vorlaufbereich, Lern-/Prüfanzahl, angewendeten Kandidaten
sowie die Auflösungsannahme. Beobachtungskandidaten verändern die Serie nicht.
Detaillierte Fehlerzahlen und Ausschlussgründe stehen in den administrativen
Optionen sowie im quellenberechtigten `get_history.morning`.

Die bisherigen Erklärungsbilanzen ohne Morgenmodell dürfen dessen Einfluss nicht
als globalen Faktor ausgeben; eine nicht passende Bilanz bleibt nicht verfügbar.
Tages-/Stunden-Erfahrungsbänder werden nicht auf die neue Methode übertragen.
Für Morgen- und gleitende 60-Minuten-Fenster bleibt die Unsicherheit ausdrücklich
`unavailable`/`unsupported_horizon`.

Der private atomare Morgenstore Version 1 hält höchstens 90 Tagesreferenzen mit
je maximal 96 echten Messabschnitten, die begrenzten Lernreferenzen und höchstens
2 MiB. Er kopiert keine Wetterantworten oder gesamte Archivdaten. Reguläre
Schreibungen folgen festen Fünfminutenterminen. Bereits gespeicherte Belege
überleben die kurze Rohmess-Aufbewahrung; innerhalb der Rohfrist wird eine
widersprechende neue Messprüfung berücksichtigt. Gelöschte Archiv- oder
Quellenbelege entziehen ihre Referenzen und löschen den Morgenzustand.

Archiv-Store 8 ergänzt für tatsächlich neu erfasste operative Morgenwerte deren
Kandidaten-ID und AC-Energie. Versionen 1–7 migrieren ohne nachträglich erfundene
Morgenwirkung. **Morgenbelege zurücksetzen** löscht ausschließlich den
Morgenvergleich, beendet seine Wirkung sofort und beginnt ab jetzt neu. Entfernen
der Integration löscht den Zusatzstore. Unbekannte oder fehlerhafte Stores werden
nicht überschrieben. Config-Entry-Schema bleibt 1.1. Es gibt keine zusätzlichen
Wetter-, Geräte- oder Recorderabrufe, Sensoren oder öffentlichen Schreibaktionen.

Die vollständige Kandidaten- und Fehlerauswertung läuft im Home-Assistant-Executor
auf einer unabhängigen Kopie des Lernstands und einem eingefrorenen Fallsatz.
Konfigurations-, Quellen- oder Messänderungen während dieser Berechnung verwerfen
das alte Ergebnis und fordern eine neue Prüfung an. Solange der Nachweis erneut
geprüft wird, bleibt die Morgenkorrektur ausgesetzt. Beobachtungszeitpunkte nach
einer asynchronen Prüfung werden neu gelesen; vergangene Stichtage werden dadurch
nicht nachträglich erreicht.
