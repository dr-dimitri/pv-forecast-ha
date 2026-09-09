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

## Erweiterung nach V1: Stundenvertrag aus Issue #16

Nach Abschluss der Grundlagen in #25 ergänzt #16 die Gesamtanlage um vier
Sensoren: Restertrag heute, Ertrag der nächsten gleitenden 60 Minuten,
geschätzte Leistung jetzt aus dem laufenden Intervallmittel und Beginn der
stärksten Prognosestunde des ganzen heutigen Tages. Bei gleich hohen Spitzen
gilt der früheste absolute Zeitpunkt; ein Nulltag hat kein Maximum. Alle
Fenster verwenden UTC-Überlappungen und die gespeicherte Anlagenzeitzone.
Fehlende Zeitabdeckung ist nicht null Ertrag. Die Sensoren erhalten vor der
Statistikentscheidung in #4/#27 keine `state_class`.

Eine gemeinsame Gesamtzeitreihe entsteht einmal je Berechnung nach Clipping,
begrenzt auf die zwei lokalen Prognosetage. Der versionierte Lesevertrag
enthält Intervallgrenzen, kWh, mittlere AC-kW, Abrufzeit, Abdeckung und
Eingabefallbacks. Der Modell-Ausgabezeitpunkt ist unbekannt. Qualitätsmarkierungen
sind keine gemessene Prognosegüte. Die rein lesende HA-Aktion
`pv_forecast.get_forecast` mit verpflichtender Anlagenauswahl ist die einzige
öffentliche Stunden-Schnittstelle für Automationen und die spätere Karte.
Sie löst kein HTTP aus und dupliziert keine Stundenlisten in Sensorattributen.
Energy und Archiv verwenden bei ihrer Umsetzung dieselbe Datenbasis.

Ein gemeinsamer lokaler Minutentakt führt die Planungswerte nach; er verändert
weder Pollingfrist noch Abrufzeit oder Fehlerstatus und wird beim Entladen
beendet. Es entstehen keine zusätzlichen Wetterabrufe. PV-Erzeugung allein
ersetzt keine Verbrauchs-, Speicher- oder Tarifdaten. Diese abgegrenzte
Erweiterung ist in Issue #1 konkretisiert und ersetzt nur die entgegenstehenden
V1-Grenzen; die übrigen Architektur- und Modellvorgaben gelten weiter.

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

Bei einer neuen Adresskonfiguration wird vor dem Forecast-Test die
IANA-Zeitzone anhand der Koordinaten einmalig über Open-Meteo mit
`timezone=auto` ermittelt. Der reine Metadatenabruf enthält keine Wettervariablen.
Die validierte Zeitzone wird im Einrichtungsablauf wiederverwendet und in
`ConfigEntry.data` gespeichert. Eine neue Anschrift verwirft die alte Zeitzone;
fehlende oder ungültige Antworten erlauben keinen stillen Ersatz durch UTC oder
die HA-Zeitzone. Bereits gespeicherte Anlagenzeitzonen werden beim Start oder
Update nicht automatisch umgedeutet.

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
Längengrad, Anlagenzeitzone, alle Dachflächen mit ihrer installierten Leistung
sowie die maximale Wechselrichterleistung. Native Menüschaltflächen ermöglichen
dort das Abschließen oder den gezielten Rücksprung zu Standort, Dachflächen und
Wechselrichterleistung, ohne bereits eingegebene unabhängige Werte zu verlieren.

## Architekturgrenzen

Die Integration liegt unter `custom_components/pv_forecast/` und verwendet
asynchronen Code, Typisierung sowie `ConfigEntry.runtime_data`.

- `api.py`: ausschließlich HTTP-Kommunikation mit Open-Meteo, Timeouts,
  Statusprüfung, gemeinsame Abrufpausen und Antwortvalidierung; keine
  PV-Berechnung.
- `runtime.py`: verbindet den Open-Meteo-Client mit der HA-Session und teilt
  dessen flüchtigen Abrufzustand über Einrichtung und Reload hinweg. Dieser
  Zustand gehört nicht in die gespeicherte Anlagenkonfiguration.
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

Open-Meteo ist die einzige Wetterdatenquelle. Ein Forecast-Request lädt nur:

- `global_tilted_irradiance`,
- `temperature_2m`,
- ein absolutes stündliches UTC-Fenster über `start_hour` und `end_hour`
  (einschließlich des letzten GTI-Endzeitpunkts),
- `timezone=UTC` und `timeformat=unixtime` für eindeutige Transportzeitpunkte.

Diese in Issue #1 für die Korrektur von Issue #5 festgelegte Ausnahme ersetzt
`forecast_days=2` und die lokale Request-Zeitzone. Die beiden Zielgrenzen werden
weiterhin zuerst in der gespeicherten Anlagenzeitzone bestimmt. Geladen werden
nur die UTC-Stundenintervalle, die heute und morgen lokal überlappen; bei
Teilstunden-Zeitzonen werden Randintervalle anteilig ausgewertet. Das vermeidet
die festen Request-Offsets von Open-Meteo über Zeitumstellungen hinweg. Es
werden weiterhin ausschließlich die zwei lokalen Zieltage prognostiziert.

Jede unterschiedliche Kombination aus Neigung und Open-Meteo-Azimut braucht
ihren eigenen GTI-Verlauf. Dächer mit identischer Geometrie teilen sich den
Abruf. Alle nötigen Abrufe werden in einem Coordinator-Update gebündelt und
mit höchstens vier gleichzeitigen HTTP-Requests ausgeführt; die Anzahl der
Requests darf niemals mit der Anzahl der Sensoren wachsen.

Open-Meteo-Zeitstempel bezeichnen bei GTI den Mittelwert der vorhergehenden
Stunde. Tageszuordnung und Energieberechnung verwenden deshalb das tatsächliche
lokale Intervall zwischen zwei Zeitstempeln. Das muss auch an DST-Tagen korrekt
sein. Fehlendes GTI ergibt für das betroffene Intervall 0 kWh; ungültige
Antworten erzeugen einen kontrollierten Update-Fehler statt eines Absturzes.

Der Coordinator aktualisiert standardmäßig alle 30 Minuten. Bestehende Daten
bleiben bei einem vorübergehenden Updatefehler über den normalen
`DataUpdateCoordinator`-Mechanismus erhalten.

Bei HTTP 429 und vorübergehenden Transport-/Serverfehlern gilt die gemeinsame
Abrufpause aus #13: gültiges `Retry-After` berücksichtigen, sonst begrenzter
Backoff mit 60, 120 und höchstens 240 Minuten. Längere gültige Anbieterfristen
werden nicht gekürzt. Der API-Client verhindert verfrühte Abrufe auch bei
manuellen Aktualisierungen, Setup-Wiederholungen und Reloads. Erst vollständig
validierter Erfolg setzt die Fehlerfolge zurück. Der native
`TimestampDataUpdateCoordinator` hält den letzten erfolgreichen Abrufzeitpunkt;
dieser ist ausdrücklich keine Ausgabezeit des Wettermodells.

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

Bestehende Config Entries verwenden Schema 1.1. Migrationen werden gemeinsam
mit tatsächlichen Schemaänderungen umgesetzt; vorsorgliche Versionssprünge oder
verlustbehaftete Normalisierungen sind nicht vorgesehen. IDs, Anwenderwerte und
gespeicherte Anlagenzeitzonen bleiben erhalten. Die verbindliche Strategie aus
#11 ist in [Konfigurationsversionen](docs/konfigurationsversionen.md) beschrieben.
Lern- und Archivdaten werden bei ihrer Einführung unabhängig versioniert.

Vor einer Änderung den aktuellen `main`-Stand holen und einen eigenen Branch
anlegen. Änderungen klein und testbar halten, alle Qualitätsprüfungen ausführen,
committen, pushen und mit einer aussagekräftigen Pull-Request-Beschreibung
einreichen. Keine unnötigen Abhängigkeiten oder spekulativen Erweiterungen
einführen.
