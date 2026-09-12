# Optionaler Morgenvergleich

Der Morgenvergleich aus [Issue #172](https://github.com/dr-dimitri/pv-forecast-ha/issues/172)
prüft die vorhergesagte AC-PV-Erzeugung rund um den Sonnenaufgang gegen später
beobachtete Erzeugung. Eine passende Tagesmenge belegt keinen richtigen
Morgenverlauf: Zwei Prognosen mit je 20 kWh können dieselbe Leistungsschwelle
zwei Stunden auseinander überschreiten. Die vorhandene globale
[Selbstkalibrierung](kalibrierung.md) bleibt ein eigenständiges Verfahren.

Die technische Umsetzung ist ein begrenzter Versuch. Deterministische Tests
prüfen Auswahl, Zeitrechnung, Vergleich, Freigabe und Entzug. Ein realer
prospektiver Nutzen ist damit nicht belegt und bleibt offen.

## Freiwillige Einrichtung

Unter **Konfigurieren → Erweiterte Funktionen → Morgenvergleich (optional)**
stehen drei Modi zur Auswahl:

- **Aus** ist der Standard. Es wird keine Morgenkorrektur angewendet.
- **Beobachten** sammelt geeignete künftige Profile und prüft einen Kandidaten.
  Die produktive Prognose wird dadurch nicht verändert.
- **Nach eigenständiger Morgenprüfung automatisch anwenden** erlaubt eine
  Anwendung ausschließlich, solange ein gültiger eigener Nachweis vorliegt.
  Die Auswahl allein gibt keinen Faktor frei. Ohne Nachweis gilt Morgenfaktor 1.

Beobachten und Automatik setzen ein bewusst aktiviertes
[Prognosearchiv](prognosearchiv.md) sowie bestätigte, überschneidungsfreie
AC-PV-Energiequellen der Gesamtanlage voraus. Die Auswahl aktiviert keine dieser
Grundlagen automatisch. Ein Leistungssensor allein genügt nicht. Netzexport,
Speicherentladung und Hausverbrauch sind keine AC-PV-Messung.

Ein täglich zurückgesetzter Zähler ohne belegten Tagesabschluss liefert keinen
vollständigen Tagesnachweis. Eine nachträgliche Tageskorrektur erzeugt keine
Morgen-Zeitform. Die tatsächlichen Messintervalle und bestehenden
[Messqualitätsregeln](messdaten.md) bleiben verbindlich.

Der Statusdialog zeigt den laufenden Modus, Morgenfaktor, festen Kandidaten,
Lern-/Prüftage, Vergleichsmetriken und Gründe für fehlende Freigabe. Die
Optionsänderung wird mit **Fertig** gespeichert. **Morgenversuch zurücksetzen**
verlangt eine eigene bewusste Bestätigung und löscht sofort nur die eigenen
Morgenprofile und Lern-/Prüfbelege. Der Versuch beginnt ab diesem Zeitpunkt neu;
globale Kalibrierung, übrige Archivdaten und Optionen bleiben erhalten.

## Vorab festgelegter Messvertrag

Regelversion 1 verwendet das Fenster vom lokalen Sonnenaufgang bis vier
absolute Stunden danach. Der Sonnenaufgang wird aus dem gespeicherten Standort
lokal berechnet; fehlender Sonnenaufgang liefert einen Leerzustand. Alle
Intervallvergleiche verwenden eindeutige UTC-Grenzen, die Anzeige die gespeicherte
Anlagenzeitzone. Eine Zeitumstellung erzeugt keine doppeldeutigen Intervalle.

Der PV-Anstieg verwendet die feste AC-Schwelle **0,5 kW** und ein vollständiges
**60-Minuten-Fenster** innerhalb dieses Morgenfensters. Beide Seiten verwenden
dieselbe dokumentierte Intervallmittelregel. Eine mittlere AC-Leistung beweist
keine kontinuierliche Mindestleistung zwischen Messmeldungen. Die Regel wird
nicht nach Betrachtung der Ergebnisse auf günstigere Schwellen umgestellt.

Positive Energiedifferenzen werden weder über Lücken verteilt noch an einer
unbekannten Zwischengrenze aufgespalten. Verglichen wird deshalb der exakt
gemeinsam belegte Morgenkern innerhalb des Sonnenaufgangsfensters. Seine äußeren
Ränder dürfen höchstens 15 Minuten von den vorgesehenen Grenzen abweichen.
Mehrere Quellen benötigen gemeinsame exakte Grenzen; ihre gültigen Differenzen
werden erst danach aggregiert. Die Auflösung für einen Anstiegsnachweis beträgt
höchstens 15 Minuten je Messintervall. Grobe Zählerdifferenzen, fehlende Abdeckung
oder ungeklärte Quellenidentität liefern keinen behaupteten Schwellenverlauf.

Ein auf 15 Minuten ausgewerteter Stundenforecast erhält dadurch keine neue
Wetterauflösung. Abrufzeit, tatsächliche Beobachtungszeit, Vorhersagestichtag,
Zielgrenzen und Messmeldezeit bleiben getrennt. Eine HA-Meldung belegt keinen
verifizierten Gerätezeitpunkt; die Wettermodell-Ausgabezeit bleibt `null`.

Für Nachtentscheidungen wird ausschließlich ein tatsächlich rechtzeitig
beobachtetes, vollständiges Tagesprofil mit unveränderter DC-Basis verwendet:
**18 Uhr am lokalen Vortag**, höchstens **zwei Stunden** alte Prognose. Aus diesem
damals bekannten Profil werden Morgen- und Tagesvergleiche abgeleitet. Spätere
Abrufe ersetzen den eingefrorenen Stand nicht. Laufende `hourly_1h`-Stände werden
nicht zu einer angeblichen Vorabendprognose zusammengesetzt; ältere Archivtage
erhalten keine nachträglich rekonstruierten Morgenprofile.

## Kandidat und eigenständige Prüfung

Auf denselben gültigen Morgenfällen werden Rohmodell, bisher wirksame Prognose
einschließlich eines damals gültigen globalen Anlagenfaktors und optionalen
Horizontprofils sowie Morgenkandidat verglichen. Die bisher wirksame Prognose
ohne Morgenkorrektur ist die maßgebliche Baseline für den zusätzlichen Nutzen.

Der einzige neue Parameter ist ein Morgenfaktor von **0,8 bis 1,2** in Schritten
von **0,01**. Er skaliert die DC-Basis im Morgenfenster. Danach folgen genau einmal
der unabhängige globale Anlagenfaktor, das proportionale AC-Gruppenclipping und
das bestehende Anlagenlimit. Rohmodell und Anwenderwerte bleiben erhalten.
Es gibt keine zusätzlichen Dach-, Saison- oder Wetterfaktoren und keinen
zweiten Kalibrierungspfad für SAX.

Mindestens **30 geeignete Morgenfälle innerhalb von 60 lokalen Tagen** bilden
das Training. Anschließend bleibt der Kandidat fest und benötigt mindestens
**14 neue gültige Morgenfälle innerhalb von 28 lokalen Tagen**. Die Prüfprofile
müssen erst nach Festlegung des Kandidaten rechtzeitig eingefroren worden sein.
Viele Teilintervalle desselben Morgens zählen weiterhin als ein Tag. Vorhandene
globale Lerntage oder eine globale Freigabe ersetzen diese Prüfung nicht.

Die vorab gewählte primäre Zielmetrik ist der **Morgenenergie-MAE in kWh**.
Die Prüfung verlangt gleichzeitig:

- mindestens 5 Prozent geringeren Morgenenergie-MAE bei positivem Baseline-MAE;
- keine höhere Rate vorhergesagter, tatsächlich nicht belegter Anstiege;
- keinen höheren P90 der zu frühen Anstiegsfehler;
- höchstens 5 Prozent höheren MAE der belegten Anstiegszeit;
- höchstens 5 Prozent höheren Tagesenergie-MAE auf denselben Vergleichstagen.

Bei einer Nebenmetrik mit Baseline-Fehler null darf kein zusätzlicher Fehler
entstehen. Nicht bewertbare Zeitmetriken oder eine Nullfehler-Baseline
rechtfertigen keine Freigabe. Beobachtetes Ausbleiben, fehlende Messung und
Fälle ohne Anstieg auf beiden Seiten werden getrennt gezählt. Bei beobachtetem
Ausbleiben auf beiden Seiten beträgt der Zeitfehler null; steigt nur eine Seite
an, gilt die vorab festgelegte Strafe von 240 Minuten. Ohne mindestens einen Tag
mit belegtem Anstieg in Messung und allen drei Prognosevarianten gibt es keine
Freigabe. Große Fehler allein sind kein Ausschlussgrund. Vorab markierte
Abregelung oder Wartung nutzt dieselben Tagesmarkierungen wie die globale
Kalibrierung.

Auch nach einer Freigabe bleiben spätere Belege erforderlich. Veralteter oder
unzureichender Nachweis, geänderte Messungen, Quellen-/Standort-/physische
Konfigurationswechsel und gelöschte Belege können die Morgenfreigabe entziehen.
Die wirksame Prognose fällt dann auf den übrigen Modellstand zurück. Eine
unabhängig gültige globale Kalibrierung bleibt dabei bestehen. Lokale
Faktorwechsel lösen keinen Wetterabruf aus und verändern weder die echte
Abrufzeit noch den letzten Aktualisierungserfolg. Nach Ablehnung oder Entzug
beginnt frühestens nach 14 Tagen ein neuer Kandidatenversuch.

## Lesevertrag und Grenzen

`pv_forecast.get_forecast` liefert weiterhin dieselbe gemeinsame wirksame
AC-Zeitreihe. Ein additiver versionierter Block `morning` nennt Methode
`morning_factor_v1`, Modus, Anwendungsstatus, wirksamen Faktor, Kandidatenbezug,
Morgenfenster und Vorabendstichtag. Ein ungeprüfter Kandidat wird dadurch nicht
als wirksame Prognose ausgegeben.

Die optionale Prognoseerklärung bleibt auch mit angewendetem Morgenfaktor
verfügbar. Ihr additiver Beitrag `morning_delta_kwh` steht vor dem globalen
`calibration_delta_kwh` und den beiden AC-Kürzungen. Die Karte zeigt diese
Bilanz in den Details; Morgenfaktor und globaler Anlagenfaktor werden nicht
zusammen als ein pauschaler Tagesfaktor ausgegeben.

Mit `include_morning: true` ergänzt dieselbe Aktion den vollständigen Vergleich
mit Stichproben und Prüfstatus. Dabei werden zusätzlich die Leserechte der
aktuellen und verwendeten historischen Messquellen geprüft. Die bestehende
Aktion `pv_forecast.get_history` enthält den vollständigen Morgenbericht unter
ihren bisherigen Quellenrechten. Beide Antworten entstehen lokal ohne Abruf.

Beispiel für die bewusst ausgewählte Anlage:

```yaml
action: pv_forecast.get_forecast
data:
  config_entry_id: "ID_der_PV_Anlage"
  include_morning: true
response_variable: pv_prognose
```

Unsicherheit für das konkrete Morgen- oder gleitende 60-Minuten-Fenster bleibt
ohne eigene passende empirische Basis ausdrücklich nicht verfügbar. Es werden
weder Tagesbänder skaliert noch Stundenquantile zu einer Fenstersicherheit
addiert. Es gibt keine neuen Sensoren, öffentlichen Aktionen, Wettervariablen
oder Geräteabrufe. Config Entries bleiben bei Schema 1.1.

Die zusätzlichen Profile und Nachweise liegen begrenzt im bestehenden
Archiv-Store Version 8; es entsteht kein separater Store. Die Migration
übernimmt vorhandene Archive ohne erfundene historische Morgenprofile.
Der Morgenbereich hält höchstens 96 Tagesprofile aus den letzten 96 Tagen,
höchstens 50 DC-Intervalle und 500 originale Messaggregate je Fall und insgesamt
höchstens 2 MiB. Kürzungen bleiben im Bericht sichtbar. Die vorhandene
Gesamtgrenze von 32 MiB, regulären Fünfminutenschreibtermine und Schutzregeln für
unbekannte Speicherversionen gelten weiter. Archiv-/Quellen- und
Integrationslöschung entfernen abhängige Morgenbelege. Entladen beendet den
bestehenden Erfassungsweg.

Ein belegter PV-Anstieg ist kein Nachweis verfügbarer Haushaltsversorgung oder
eines Überschusses. Hauslast, Nachtreserve, Speicher, Netzbezug und Tarife
bleiben außerhalb dieses Vergleichs. SAX muss diese Größen weiterhin über
seine eigenen Bedarfs- und Versorgungsverträge prüfen; fehlende Hauslast ist
keine Nullannahme.
