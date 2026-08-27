# Verbindliche Projektvorgaben

## Geltungsbereich und Priorität

Diese Datei ist die lokale Kurzfassung von
[Issue #1](https://github.com/dr-dimitri/pv-forecast-ha/issues/1) und gilt für
das gesamte Repository.

Bei Widersprüchen gilt folgende Reihenfolge:

1. Issue #1,
2. aktuelle offizielle Dokumentation von Home Assistant und Open-Meteo für
   technische Details,
3. diese lokale Zusammenfassung,
4. bestehender Code und sonstige Dokumentation.

Änderungen am fachlichen Umfang müssen zuerst im Issue geklärt und danach hier
nachgezogen werden. Dokumentation, Kommentare, UI-Texte und Zusammenarbeit sind
auf Deutsch zu verfassen. Produkt- und Bibliotheksbegriffe dürfen ihre
gebräuchliche englische Bezeichnung behalten.

## Ziel und Umfang von V1

`pv_forecast` ist eine UI-konfigurierte Home-Assistant-Custom-Integration. Sie
prognostiziert mit Open-Meteo den PV-Energieertrag für den lokalen heutigen und
morgigen Tag. Unterstützt werden mehrere unabhängig konfigurierte Dachflächen.

V1 stellt ausschließlich Sensor-Entities bereit:

- Gesamtprognose heute und morgen,
- Prognose heute und morgen je Dachfläche.

Alle Werte werden in kWh ausgegeben. Nicht Teil von V1 sind spätere
Forecast-Tage, Wetter-, GTI-, Temperatur-, Debug- oder Statussensoren, andere
Entity-Typen, Services, weitere Wetteranbieter, Verschattung, Speicher,
Eigenverbrauch, Wallboxen, automatische Kalibrierung und komplexe
Strahlungsmodelle.

## Konfiguration

Die Einrichtung erfolgt ausschließlich über einen Config Flow; YAML ist nicht
vorgesehen. Genau ein Config Entry repräsentiert die PV-Anlage. Vor dem Anlegen
des Eintrags muss ein Open-Meteo-Testabruf erfolgreich sein. Ein zweites Setup
desselben Standorts beziehungsweise derselben logischen Anlage wird verhindert.

Grunddaten in `ConfigEntry.data`:

- Breitengrad von -90 bis +90,
- Längengrad von -180 bis +180,
- Zeitzone und verständlicher Standortname,
- die gewählte Standortquelle,
- bei manueller Adresseingabe zusätzlich Straße, Postleitzahl und Land.

Die UI fragt keine Koordinaten ab. Sie übernimmt entweder die in Home Assistant
hinterlegten Koordinaten oder löst eine vom Benutzer eingegebene Anschrift genau
einmal über Nominatim auf. Die Prognose verwendet danach ausschließlich die
gespeicherten Koordinaten; eine regelmäßige Adressabfrage oder Autovervollständigung
findet nicht statt.

Veränderbare Daten werden über einen Options Flow bearbeitet und in
`ConfigEntry.options` geführt:

- eine oder mehrere Dachflächen mit stabiler, namensunabhängiger ID,
- Name,
- installierte Leistung in kWp, größer als 0,
- Ausrichtung,
- Neigung von 0 bis 90 Grad,
- Systemwirkungsgrad von 0 bis 100 Prozent, Standard 90 Prozent; zur
  Rückwärtskompatibilität wird er intern als pauschaler Gesamtverlust gespeichert,
- optional eine maximale AC-Wechselrichterleistung der Gesamtanlage in kW,
  größer als 0.

Die UI bietet verständliche Himmelsrichtungen an. Eine zentrale Funktion
übersetzt diese in die Open-Meteo-Konvention: Süd = 0°, Ost = -90°, West =
+90°, Nord = ±180°. Die Umrechnung darf nicht dupliziert werden.

Vor dem Anlegen zeigt ein Abschlussdialog Standortquelle, Breiten- und
Längengrad, alle Dachflächen mit ihrer installierten Leistung sowie die maximale
Wechselrichterleistung. Native Menüschaltflächen ermöglichen dort das
Abschließen oder den gezielten Rücksprung zu Standort, Dachflächen und
Wechselrichterleistung, ohne bereits eingegebene unabhängige Werte zu verlieren.

## Architekturgrenzen

Die Integration liegt unter `custom_components/pv_forecast/` und verwendet
asynchronen Code, Typisierung sowie `ConfigEntry.runtime_data`.

- `api.py`: ausschließlich HTTP-Kommunikation mit Open-Meteo, Timeouts,
  Statusprüfung und Antwortvalidierung; keine PV-Berechnung.
- `geocoding.py`: ausschließlich die einmalige, benutzergesteuerte und
  strukturierte Adressauflösung über Nominatim einschließlich Antwortvalidierung.
- `models.py`: typisierte, möglichst unveränderliche Modelle für Dachflächen,
  Wetterintervalle und Prognosen.
- `calculations.py`: reine, deterministische Python-Funktionen ohne
  Home-Assistant-Abhängigkeit.
- `coordinator.py`: gemeinsamer Abruf, Aufbereitung, Berechnung und Aggregation
  für alle Sensoren über einen `DataUpdateCoordinator`.
- `config_flow.py` und `configuration.py`: UI-Ablauf, Validierung und
  persistente Konfiguration.
- `sensor.py`: ausschließlich Entity-Abbildung auf vorhandene
  Coordinator-Daten; keine eigenen Netzwerkaufrufe oder Berechnungsmodelle.

Entities werden beim Setup beziehungsweise Reload erstellt, nicht bei jedem
Update. Jede Entity hat eine stabile `unique_id`, die nicht allein vom
Anzeigenamen abhängt.

## Open-Meteo und Zeitsemantik

Open-Meteo ist die einzige Datenquelle. Ein Request lädt nur:

- `global_tilted_irradiance`,
- `temperature_2m`,
- `forecast_days=2`,
- die lokale Zeitzone von Home Assistant beziehungsweise dem Standort.

Jede unterschiedliche Kombination aus Neigung und Open-Meteo-Azimut braucht
ihren eigenen GTI-Verlauf. Dächer mit identischer Geometrie teilen sich den
Abruf. Alle nötigen Abrufe werden in einem Coordinator-Update gebündelt und
parallel ausgeführt; die Anzahl der Requests darf niemals mit der Anzahl der
Sensoren wachsen.

Open-Meteo-Zeitstempel bezeichnen bei GTI den Mittelwert der vorhergehenden
Stunde. Tageszuordnung und Energieberechnung verwenden deshalb das tatsächliche
lokale Intervall zwischen zwei Zeitstempeln. Das muss auch an DST-Tagen korrekt
sein. Fehlendes GTI ergibt für das betroffene Intervall 0 kWh; ungültige
Antworten erzeugen einen kontrollierten Update-Fehler statt eines Absturzes.

Der Coordinator aktualisiert standardmäßig alle 30 Minuten. Bestehende Daten
bleiben bei einem vorübergehenden Updatefehler über den normalen
`DataUpdateCoordinator`-Mechanismus erhalten.

## Berechnungsmodell

Für jedes Zeitintervall und jede Dachfläche gilt:

```text
Rohleistung [kW] = installierte Leistung [kWp] × GTI [W/m²] / 1000
Temperaturfaktor = 1 + (-0,0035 × (Außentemperatur [°C] - 25 °C))
Leistung [kW] = Rohleistung × Temperaturfaktor × (1 - Verlustanteil)
Energie [kWh] = Leistung × tatsächliche Intervalldauer [h]
```

Die Außentemperatur ist in V1 ausdrücklich nur eine dokumentierte Näherung für
die Zelltemperatur. Der Temperaturfaktor muss gegen physikalisch unsinnige
negative Ergebnisse begrenzt werden. Negative Einstrahlung oder Erträge werden
als 0 behandelt.

Ein optionales Wechselrichterlimit gilt für die Gesamtanlage. Überschreitet die
Summe der zeitgleichen Dachleistungen das Limit, werden die Dachbeiträge vor der
Energieintegration proportional gekürzt. Ohne gesetztes Limit findet kein
Clipping statt.

Nicht zulässig ist ein alternatives Flächen-/Modulwirkungsgradmodell. Grundlage
von Issue #1 ist ausschließlich kWp × GTI / 1000.

## Tests und Qualitätsprüfung

Tests laufen deterministisch und offline; externe Antworten werden gemockt.
Der ausgelieferte Integrationscode muss mindestens mit der Python-3.13-Syntax
kompatibel bleiben, auch wenn die Haupt-Testsuite auf einer neueren, von der
aktuellen Home-Assistant-Version verlangten Python-Version läuft.
Mindestens abzudecken sind:

- 0 und 1000 W/m²,
- Verluste und Temperaturkorrektur,
- Wechselrichterbegrenzung,
- mehrere Dachflächen und Aggregation,
- alle Eingabegrenzen und fehlende Wetterwerte,
- Azimutabbildung,
- lokale Tagesgrenzen, vorhergehende GTI-Stunde und DST,
- erfolgreicher Config Flow,
- ungültige Koordinaten und Dachparameter,
- nicht erreichbare beziehungsweise ungültig antwortende API,
- doppeltes Setup,
- Coordinator-Fehler und Sensor-Metadaten.

Verbindliche lokale CI-Befehle:

```bash
python -m pip install --requirement requirements_test.txt
ruff check custom_components tests
black --check custom_components tests
pytest -v
```

Bei Änderungen an Manifest oder Home-Assistant-Struktur zusätzlich Hassfest
ausführen. Die README nennt Installation, Konfiguration, Grenzen und die
Attribution für Open-Meteo-Daten unter CC BY 4.0.

## Arbeitsablauf

Vor einer Änderung den aktuellen `main`-Stand holen und einen eigenen Branch
anlegen. Änderungen klein und testbar halten, alle Qualitätsprüfungen ausführen,
committen, pushen und mit einer aussagekräftigen Pull-Request-Beschreibung
einreichen. Keine unnötigen Abhängigkeiten oder spekulativen Erweiterungen
einführen.
