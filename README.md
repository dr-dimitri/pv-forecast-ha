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
keinen Zahlenwert. Die Sensoren erhalten vor der gemeinsamen Statistikentscheidung
mit dem geplanten Prognosearchiv keine `state_class`.

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
- automatische Kalibrierung anhand realer Erträge
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
```

Beim Mergen eines Pull Requests erstellt die Release-Automation standardmäßig
ein Patch-Release. Mit genau einem der Labels `release:major`, `release:minor`
oder `release:patch` wird der gewünschte Teil der Versionsnummer erhöht.
