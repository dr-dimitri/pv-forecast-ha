<p align="center">
  <img src="custom_components/pv_forecast/brand/icon@2x.png" width="128" alt="Icon der PV-Ertragsprognose">
</p>

# PV-Ertragsprognose für Home Assistant

`pv_forecast` ist eine Custom Integration für Home Assistant. Sie berechnet den
erwarteten Energieertrag einer PV-Anlage für den lokalen heutigen und morgigen
Tag. Als Wetter- und Strahlungsdatenquelle dient
[Open-Meteo](https://open-meteo.com/).

Die Einrichtung und spätere Konfiguration erfolgen vollständig über die
Home-Assistant-Oberfläche. YAML wird nicht unterstützt.

## Funktionen

- Prognose für heute und morgen in kWh
- Gesamtwerte für die Anlage und Einzelwerte je Dachfläche
- Restertrag heute, Ertrag der nächsten 60 Minuten, geschätzte Leistung jetzt
  und Beginn der stärksten Prognosestunde heute
- lesende Aktion für die gemeinsame Stundenprognose in Automationen
- native Solarprognose im Home-Assistant-Energie-Dashboard
- optionale lokale Erfassung bestehender PV-Ertragszähler mit Messdatenprüfung
- optionales Prognosearchiv mit Soll-Ist-Berichten und bewusstem JSON-/CSV-Export
- freiwillige Lovelace-Karte mit visuellem Editor, Tageskurven und Dachauswahl
- optionale Selbstkalibrierung mit getrennten Lern- und späteren Prüftagen
- mehrere Dachflächen mit eigener Leistung, Ausrichtung, Neigung und eigenem
  Systemwirkungsgrad
- Übernahme des in Home Assistant hinterlegten Standorts oder einmalige
  Adressauflösung über Nominatim
- optionales AC-Leistungslimit für einen gemeinsam genutzten Wechselrichter
- stabile Sensor-IDs, auch wenn eine Dachfläche umbenannt wird
- automatische Aktualisierung standardmäßig alle 30 Minuten

Die Integration legt ausschließlich Prognosesensoren an. Wetter-,
Einstrahlungs-, Temperatur-, Status- und Debug-Sensoren gehören nicht zum
Funktionsumfang.

## Installation

Voraussetzung ist **Home Assistant 2025.12.0 oder neuer**. Das gilt auch bei
manueller Installation; HACS berücksichtigt diese Mindestversion beim Update.

### HACS

1. Öffne in HACS die benutzerdefinierten Repositorys.
2. Füge `https://github.com/dr-dimitri/pv-forecast-ha` als Repository der
   Kategorie **Integration** hinzu.
3. Lade **PV-Ertragsprognose** herunter.
4. Starte Home Assistant neu.
5. Öffne **Einstellungen → Geräte & Dienste → Integration hinzufügen** und
   suche nach **PV-Ertragsprognose**.

Nach einem Update kann ein vollständiges Neuladen der Home-Assistant-Seite
erforderlich sein (`Strg`/`Cmd` + `Umschalt` + `R`), damit der Browser keine
älteren Formulartexte aus dem Cache verwendet.

### Manuelle Installation

Kopiere den Ordner `custom_components/pv_forecast` in das Verzeichnis
`custom_components` deiner Home-Assistant-Konfiguration. Starte Home Assistant
anschließend neu und füge die Integration über **Einstellungen → Geräte &
Dienste** hinzu.

## Einrichtung

### 1. Standort wählen

Die Integration bietet zwei Standortquellen:

- **Home-Assistant-Standort:** Verwendet Standortname, Koordinaten, Land und
  Zeitzone aus den allgemeinen Einstellungen von Home Assistant.
- **Andere Anschrift:** Wandelt Postleitzahl, Straße mit Hausnummer und Land
  einmalig über Nominatim in Koordinaten um. Open-Meteo ermittelt bei der
  Einrichtung die zugehörige Zeitzone; sie erscheint vor dem Speichern in der
  Zusammenfassung und bestimmt die lokalen Prognosetage.

Für spätere Prognosen werden die gespeicherten Koordinaten und die gespeicherte
Anlagenzeitzone verwendet. Es findet keine regelmäßige Adressauflösung statt.
Bereits eingerichtete Anlagen behalten ihre gespeicherte Zeitzone auch nach
einem Update.

### 2. Dachflächen konfigurieren

Mindestens eine Dachfläche ist erforderlich.

| Einstellung | Beschreibung |
| --- | --- |
| Name | Eindeutige Bezeichnung, beispielsweise „Süddach“ |
| Installierte Leistung | Nennleistung der Module in kWp, größer als 0 |
| Ausrichtung | Himmelsrichtung der Dachfläche |
| Neigung | Dachneigung von 0° bis 90° |
| Systemwirkungsgrad | Verbleibender Anteil nach pauschalen Verlusten, standardmäßig 90 % |

Ein Systemwirkungsgrad von 90 % entspricht einem pauschalen Gesamtverlust von
10 %. Darin können beispielsweise Wechselrichter-, Leitungs- und
Verschmutzungsverluste zusammengefasst werden. Der Wert ersetzt keine
detaillierte elektrische Simulation.

### 3. Wechselrichterlimit festlegen

Optional kann die maximale AC-Leistung des gemeinsamen Wechselrichters in kW
angegeben werden. Überschreitet die berechnete Gesamtleistung dieses Limit,
werden die Beiträge der Dachflächen proportional reduziert.

Vor dem Speichern prüft die Integration den Zugriff auf Open-Meteo und zeigt
eine Zusammenfassung der Konfiguration. Standort, Dachflächen und
Wechselrichterlimit lassen sich von dort gezielt korrigieren.

Die Konfiguration kann später unter **Einstellungen → Geräte & Dienste →
PV-Ertragsprognose → Konfigurieren** geändert werden. Ein Menü bietet dort
gezielt einzelne Aktionen an: eine Dachfläche hinzufügen, eine bestehende
bearbeiten (ihre technische ID bleibt dabei erhalten), eine Dachfläche nach
ausdrücklicher Bestätigung entfernen oder das Wechselrichterlimit ändern.
Jede Aktion wirkt für sich allein, ohne die übrigen Dachflächen anzufassen.

## Echte PV-Erzeugung zuordnen (optional)

Im Abschlussdialog der Einrichtung oder unter **Konfigurieren → Messquellen**
kannst du bereits vorhandene HA-Sensoren auswählen. Die Prognose funktioniert
auch ohne Messquelle. Pro Quelle wählst du die Messart, beschreibst die gemessene
Anlage beziehungsweise ihren Teil und prüfst die Vorschau mit Einheit und
letztem gültigem Messwert.

Unterstützt werden fortlaufende oder täglich zurückgesetzte **Energiezähler in
Wh/kWh** sowie optional **Leistung in W/kW**. Die Quelle muss tatsächlich
AC-PV-Erzeugung messen: Netzexport, Hausverbrauch und Batterieentladung sind
andere Größen. Bei Hybridwechselrichtern muss die Messgrenze eindeutig sein.
Namen und Einheiten allein können das nicht belegen. Mehrere Energiequellen
dürfen nur voneinander getrennte Wechselrichter erfassen; einen Gesamtzähler
und seine Teilzähler darfst du nicht gemeinsam auswählen. Diese Angaben werden
vor dem Speichern ausdrücklich bestätigt.

Ein bestehender, passend eingerichteter HA-Integral-Helfer kann Energie liefern;
kennzeichne ihn als abgeleitet. Seine Abtastrate und Lückenbehandlung bleiben
relevant. Die Integration wandelt selbst keine Leistung in Energie um und
verteilt einen Anlagenzähler nicht künstlich auf Dachflächen.

Die Erfassung beginnt ab der Zuordnung und liest ausschließlich lokale
HA-Ereignisse. Sie normalisiert nach kWh beziehungsweise kW und UTC. Es gibt
keine Geräteabfragen, keinen Recorder-Import und keine zusätzlichen
Wetterabrufe. Eine reguläre Wiederholung desselben Zählerstands ist ein gültiger
Nullertrag; `unknown`, `unavailable` und ausbleibende Meldungen sind keine Null.
Das maximal erwartete Meldeintervall wird je Quelle festgelegt (Standard
60 Minuten). Eine großzügige Sprungprüfung mit dem Doppelten der installierten
Anlagenleistung markiert auffällige Änderungen, ohne eine Messgenauigkeit zu
behaupten.

Jede Quelle wird vor der Summierung einzeln ausgewertet. Ein Zählerrückgang
ist nicht automatisch ein Reset. Bei Tageszählern ohne belegten Abschluss bleibt
der Vortag unvollständig. Über eine Lücke kann ein fortlaufender Zähler eine
Gesamtmenge belegen, aber keine beliebige stündliche Verteilung. Abdeckung und
Qualitätsmarkierungen bleiben deshalb getrennt von der beobachteten Menge.

Eine Entity-Umbenennung bleibt über ihre Registry-ID verbunden. Der Austausch
einer Quelle oder ihrer Messgrenze beginnt ein neues Datensegment. Bei Entities
ohne Registry-Identität ist ein Gerätewechsel unter identischem Entitynamen
nicht automatisch erkennbar; bestätige einen solchen Wechsel über die Optionen.

Messkopien bleiben lokal, unabhängig versioniert und auf sieben Tage sowie
20.000 Messpunkte mit höchstens 20.000 zugehörigen Zählerdifferenzen je Quelle
begrenzt; häufige Meldungen können das verfügbare
Zeitfenster verkürzen. Über die Quellenoptionen lassen sich ihre Daten gezielt
löschen. Das entfernt nur die Kopie dieser Integration, weder den Originalsensor
noch dessen HA-Historie. Beim Entfernen der Anlage wird ihr Messdatenspeicher
mit entfernt. Das unten beschriebene Prognosearchiv ist optional; Lernen folgt
separat.

Die lesende Aktion `pv_forecast.get_measurements` liefert die vorhandenen Daten
für eine explizite Anlage und ein Zeitfenster. `start` und `end` verlangen
ISO-8601-Zeitpunkte mit Offset oder `Z`, beispielsweise
`2026-09-09T00:00:00Z` und `2026-09-10T00:00:00Z`. Die Antwort enthält
Einzelquellen, beobachtete Energiemengen, Abdeckung und Qualitätsmarkierungen;
die Leserechte der ursprünglichen Sensoren gelten auch hier. Der genaue
[Datenvertrag](docs/messdaten.md) erläutert die Grenzen für spätere Vergleiche.

## Prognosearchiv und Soll-Ist-Vergleich (optional)

Aktiviere **Prognosearchiv** im Abschlussdialog oder unter **Konfigurieren**.
Die Erfassung beginnt ab diesem Zeitpunkt; fehlende Vergangenheit wird nicht
nachgebaut. Pausieren erhält die vorhandenen Daten für Bericht und Export.
Ohne zugeordneten Energiezähler bleiben Bewertungen fehlend.

Der Bericht zeigt für 7, 30 oder 90 abgeschlossene lokale Tage die Anzahl
rechtzeitig erfasster Prognosen, gültige Messpaare, Abdeckung, MAE und Bias in
kWh. Positiver Bias bedeutet Überschätzung. Die Horizonte sind getrennt:
Tagesprognose bis 18 Uhr am Vortag, Tagesprognose bis 06 Uhr am Zieltag sowie
Stundenprognosen mit einer und drei Stunden Vorlauf. Spätere Verbesserungen
überschreiben die gewählten Prognosen nicht; Messkorrekturen bleiben als
Bewertungsrevisionen nachvollziehbar. Ohne Stichprobe wird keine Genauigkeit
behauptet.

Optional kannst du vorhandene fremde Tagesprognosen für heute/morgen zuordnen,
wenn Anlagenzeitzone, Tagesbezug und AC-Messgrenze übereinstimmen. Verglichen
werden ausschließlich dieselben gültigen Messpaare; das Datenalter wird
getrennt ausgewiesen. Die Integration ruft dafür keinen weiteren Wetteranbieter
ab. Die optionale Selbstkalibrierung verwendet einen getrennten, späteren
Prüfzeitraum und bewahrt diese Rohprognosen.

Das Archiv ist lokal begrenzt: Stundenstände 90 Tage, Tagesbewertungen 365 Tage,
maximal 6.000 Zieldatensätze und 32 MiB, mit je drei früheren Bewertungen.
**Prognosearchiv löschen** entfernt es nach Bestätigung. Beim Löschen einer
Messquelle werden auch ihre Messkopien im Archiv entfernt.
`pv_forecast.get_history` liest den Bericht; `pv_forecast.export_history`
liefert auf ausdrücklichen Aufruf JSON-/CSV-Inhalt mit Dateiname, ohne eine
Datei automatisch zu veröffentlichen. Der
[Archivvertrag](docs/prognosearchiv.md) erklärt Stichtage, Formeln, Grenzen und
Leserechte. Die reale Nutzer- und Güteerprobung aus der Roadmap bleibt offen.

## Selbstkalibrierung (optional)

Unter **Konfigurieren → Selbstkalibrierung → Modus auswählen** kannst du
zunächst **Beobachten** wählen. Voraussetzung sind ein aktiviertes Prognosearchiv
und bestätigte PV-Energiequellen. Standardmäßig ist die Funktion aus. Sie
verändert weder die installierte Leistung noch deinen Systemwirkungsgrad.

Nach mindestens 30 vollständigen Lerntagen wird ein begrenzter Anlagenfaktor
an mindestens 14 späteren Tagen geprüft. **Automatisch anwenden** verwendet ihn
erst bei bestandenem Nutzenkriterium; andernfalls bleibt die Rohprognose wirksam.
Alle Dächer und das Energy Dashboard verwenden denselben Faktor vor dem
Wechselrichterlimit. Es entstehen keine zusätzlichen Wetterabrufe.

Der **Lern- und Prüfstatus ansehen** zeigt die verfügbare Stichprobe und den Vergleich. Bekannte
Abregelung oder Wartung lässt sich für einen lokalen Tag markieren. Abschalten
verwendet wieder das Grundmodell; **Lernzustand zurücksetzen** beginnt nach
Bestätigung von vorn. [Regeln, Bedienung und Grenzen](docs/kalibrierung.md)
erklären insbesondere, warum ältere Archivtage nicht nachträglich als Lerntage
verwendet werden und warum eine Verbesserung nicht garantiert ist.

## Eigene Dashboard-Karte (optional)

Das Integrationspaket enthält **PV Forecast** als eigene Lovelace-Karte. Füge
unter **Einstellungen → Dashboards → Ressourcen** die URL
`/pv_forecast/pv-forecast-card.js?v=1` als **JavaScript-Modul** hinzu. Danach
kannst du die Karte über **Dashboard bearbeiten → Karte hinzufügen** und ihren
visuellen Editor ohne YAML einrichten.

Sie zeigt vier Tageskennzahlen, Heute/Morgen und Gesamt-/Dachauswahl, eine
Energiekurve sowie freiwillig erfasste Messungen und feste Archivprognosen.
Messlücken werden nicht aufgefüllt; ohne zugeordnete Dachmessung bleibt diese
in der Dachansicht fehlend. Die historische Linie **„Jeweils 1 Stunde vorher“**
verwendet pro Intervall einen eigenen festen Stichtag. Die Karte verwendet die
Anlagenzeitzone und unterscheidet wiederholte Stunden bei Zeitumstellungen.

[Installation, Bedienung und Grenzen der Karte](docs/karte.md) beschreiben auch
Versionen, Datenzugriff und die noch ausstehende Nutzererprobung.

## Sensoren

Die Integration erstellt folgende Sensoren:

- `Prognose heute` und `Prognose morgen` für die Gesamtanlage
- `<Dachfläche> Prognose heute` und `<Dachfläche> Prognose morgen` für jede
  konfigurierte Dachfläche

Die Tageswerte werden in kWh ausgegeben. Zusätzlich erhält die Gesamtanlage:

| Sensor | Bedeutung |
| --- | --- |
| Restertrag heute | Prognostizierte kWh von jetzt bis zur nächsten Anlagenmitternacht |
| Ertrag nächste 60 Minuten | Prognostizierte kWh über die nächsten exakt 3.600 Sekunden, auch über Mitternacht |
| Geschätzte Leistung jetzt | Mittlere AC-Leistung des laufenden Wetterintervalls in kW; kein Live-Messwert |
| Beginn der stärksten Prognosestunde heute | Beginn des Intervalls mit der höchsten mittleren AC-Leistung im ganzen heutigen Tag |

Die Planungssensoren werden jede Minute aus den gespeicherten Intervallen
nachgeführt. Dadurch entstehen keine zusätzlichen Wetterabrufe und keine feinere
Wetterauflösung. Bei gleicher Spitzenleistung zählt der früheste absolute
Zeitpunkt; auf einem vollständig ertraglosen Tag bleibt der Zeitpunkt unbekannt.
Bei Teilstundenzeitzonen wird der Beginn auf den heutigen Tagesanfang begrenzt.
Ein vollständig abgedecktes Nullfenster ergibt 0, fehlende Zeitabdeckung dagegen
keinen Zahlenwert. Alle Prognosesensoren behalten dauerhaft keine `state_class`.
Revidierte Tages-, Rest- und Stundenprognosen sind keine Erzeugungszähler:
`total` würde Prognoseänderungen akkumulieren, `total_increasing` könnte
Abwärtskorrekturen als Zählerrücksetzung interpretieren. Auch die geschätzte
Leistung ist kein aktueller Erzeugungsmesswert. Die normale Recorder-Historie
richtet sich nach deiner HA-Aufbewahrung; langfristige Soll-Ist-Vergleiche
verwenden das datierte Prognosearchiv. Die native Energy-Anbindung benötigt
keine Statistikklasse dieser Sensoren.

Die Zuordnung zu heute und morgen wechselt zur Mitternacht am Anlagenstandort.
Bis neue Wetterdaten vorliegen, kann der bisherige Morgenwert als heutige
Prognose dienen. Der neue morgige Tag bleibt ohne passende Daten nicht
verfügbar; er wird weder als null noch als Wert eines falschen Tages angezeigt.
Bei einem fehlgeschlagenen Update greift weiterhin die normale
Home-Assistant-Nichtverfügbarkeit, während der letzte Datenstand intern erhalten
bleibt.

Bei einer Abrufbegrenzung oder einem vorübergehenden API-Ausfall berücksichtigt
die Integration die von Open-Meteo angegebene Wartefrist. Fehlt eine verwendbare
Frist, steigt die Pause bei weiteren Fehlschlägen auf 60, 120 und höchstens
240 Minuten. Eine längere gültige Anbieterfrist bleibt maßgeblich. Manuelle
Aktualisierungen, Tageswechsel und erneute Einrichtungsversuche umgehen diese
Pause nicht. Nach einem erfolgreichen Abruf gilt wieder der normale
30-Minuten-Takt. Dauerhaft fehlerhafte Antworten werden getrennt behandelt.

## Stundenprognose in Automationen

Unter **Entwicklerwerkzeuge → Aktionen → PV-Prognose lesen** liefert
`pv_forecast.get_forecast` die gespeicherte Gesamtzeitreihe der ausgewählten
PV-Anlage. Die Aktion fragt keine Wetterdaten ab. Sie gibt Intervallgrenzen,
kWh und mittlere AC-kW sowie Abrufzeit, Abdeckung und Eingabefallbacks zurück.
Ältere Daten bleiben nach einem Abruffehler mit `last_update_success: false`
lesbar; die Abdeckungsgrenzen und der Abrufzeitpunkt müssen zusätzlich beachtet
werden. Die Ausgabezeit des Wettermodells ist unbekannt.

Die [Beschreibung des Stundenvertrags](docs/stundenprognose.md) enthält das
Antwortformat und ein Beispiel für eine HA-Aktion mit Antwortvariable. Die
Zeitreihe wird nicht in Sensorattributen wiederholt und damit nicht pro Entity
im Recorder vervielfacht.

PV-Erzeugung allein beschreibt keinen verfügbaren Überschuss. Verbrauch,
Speicherzustand und Tarife sind zusätzliche Daten für entsprechende Entscheidungen.

## Prognose im Energie-Dashboard

1. Öffne die Konfiguration des **Energie-Dashboards**.
2. Füge unter **Solarproduktion** einen vorhandenen Sensor hinzu, der die
   tatsächlich erzeugte PV-Energie misst, oder bearbeite eine bestehende Quelle.
3. Wähle bei der Solarproduktionsprognose deine **PV-Ertragsprognose**-Anlage aus
   und speichere die Änderung. Die native Solarproduktionsgrafik kann nun die
   erwartete Erzeugung zusammen mit den echten Erträgen anzeigen.

Die Prognosesensoren sind keine gemessenen Energiezähler und werden hier nicht
als Erzeugungsquelle ausgewählt. Bei mehreren realen Solarzählern wird die
Gesamtprognose **genau einem Zähler** zugeordnet. Das untersuchte HA-Frontend
addiert eine mehrfach zugeordnete Anlage sonst mehrfach in der Grafik, obwohl
das Backend sie nur einmal abruft ([bekannte Grenze #52](https://github.com/dr-dimitri/pv-forecast-ha/issues/52)).

Die Energy-Anbindung liest dieselben geclippten Gesamtintervalle wie die
Tagessensoren. Sie rechnet kWh in Wh um und erhält lokale Tagesanteile sowie
eindeutige UTC-Zeitpunkte. Es entstehen keine zusätzlichen Wetterabrufe oder
Entities. Nach einem Abruffehler, während des Entladens oder solange die beiden
aktuellen lokalen Tage nicht vollständig abgedeckt sind, wird keine Kurve
geliefert. Das native Format kann ältere oder unvollständige Daten nicht als
solche kennzeichnen. Die Leseaktion bietet weiterhin die beschriebenen Metadaten.

Bei der Herbst-Zeitumstellung und in Teilstundenzeitzonen kann das native
HA-Frontend verschiedene Prognoseintervalle in einem Stundenpunkt zusammenfassen.
Die im Adapter erhaltenen UTC-Zeitpunkte und Energiesummen ändern diese
Darstellungsgrenze nicht ([bekannte Grenze #53](https://github.com/dr-dimitri/pv-forecast-ha/issues/53)).
Die Grenzen wurden mit HA 2026.8.3 und Frontend 20260729.7 nachvollzogen.
Weitere Hinweise zu den nativen Karten stehen in der
[HA-Dokumentation zum Energie-Dashboard](https://www.home-assistant.io/dashboards/energy/).

## Berechnungsmodell

Die Energie wird für jedes Wetterintervall nach folgendem Modell berechnet:

```text
Rohleistung [kW] = installierte Leistung [kWp] × GTI [W/m²] / 1000
Temperaturfaktor = 1 + (-0,0035 × (Außentemperatur [°C] - 25 °C))
Leistung [kW] = Rohleistung × Temperaturfaktor × Systemwirkungsgrad
Energie [kWh] = Leistung × tatsächliche Intervalldauer [h]
```

Die Außentemperatur dient dabei nur als Näherung für die Zelltemperatur. Die
Berechnung berücksichtigt lokale Tagesgrenzen, Zeitumstellungen und die
Open-Meteo-Semantik, nach der ein GTI-Zeitstempel den Mittelwert der
vorhergehenden Stunde bezeichnet.

Der Abruf umfasst alle Stundenintervalle, die den heutigen und morgigen lokalen
Tag überlappen, einschließlich der letzten Stunde morgen. Die Integration
bestimmt diese Tagesgrenzen in der gespeicherten Anlagenzeitzone und fragt das
benötigte Fenster mit eindeutigen UTC-Zeitpunkten ab. Dadurch bleiben auch
23-/25-Stunden-Tage und Zeitzonen mit halbstündigem oder viertelstündigem Versatz
korrekt zugeordnet; Randintervalle werden bei Bedarf anteilig berücksichtigt.

## Grenzen

Die Prognose ist ein vereinfachtes Modell und keine vollständige
Anlagensimulation. Nicht berücksichtigt werden insbesondere:

- Verschattung und detaillierte Modul- oder Stringeigenschaften
- dach- oder jahreszeitenspezifische Lernprofile
- Batteriespeicher, Eigenverbrauch, Wallboxen und Ladeplanung
- Prognosetage nach morgen
- weitere Wetteranbieter

## Datenschutz und Datenquellen

- Wetter- und Strahlungsdaten stammen von
  [Open-Meteo](https://open-meteo.com/) und werden unter
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) bereitgestellt.
- Die Abrufe teilen sich nach Dachgeometrie auf; höchstens vier HTTP-Requests
  laufen gleichzeitig. Vier Geometrien benötigen im normalen Halb-Stunden-Takt
  ungefähr 192 HTTP-Requests pro Tag, zuzüglich Einrichtung und besonderer
  Aktualisierungen. Open-Meteo unterscheidet diese Requests von gewichteten
  API-Calls; die aktuellen [Kontingente und Nutzungsbedingungen](https://open-meteo.com/en/pricing)
  gelten unabhängig davon.
- Bei manueller Adresseingabe wird einmalig eine strukturierte Suchanfrage an
  [Nominatim](https://nominatim.org/) gesendet. Die zugrunde liegenden
  Kartendaten sind © [OpenStreetMap-Mitwirkende](https://www.openstreetmap.org/copyright)
  und stehen unter ODbL.
- Bei Übernahme des Home-Assistant-Standorts wird keine Anschrift an Nominatim
  übertragen.

## Entwicklung

Die Regeln für bestehende Config Entries und spätere Datenmigrationen stehen
unter [Konfigurationsversionen](docs/konfigurationsversionen.md). Das aktuelle
Schema bleibt bei 1.1; die Release-Version ist davon unabhängig.

```bash
python3 -m venv .venv
.venv/bin/pip install --requirement requirements_test.txt
.venv/bin/ruff check custom_components tests
.venv/bin/black --check custom_components tests
.venv/bin/pytest -v
node --test tests/frontend/*.test.mjs
```

Beim Mergen eines Pull Requests erstellt die Release-Automation standardmäßig
ein Patch-Release. Mit genau einem der Labels `release:major`, `release:minor`
oder `release:patch` wird der gewünschte Teil der Versionsnummer erhöht.
