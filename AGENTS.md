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

## Entfernung des Prognosearchivs vom 25.09.2026

Auf ausdrücklichen Anwenderwunsch entfällt die gesamte Archivfunktion mit allen
abhängigen Auswertungen und Einstellungen: Archivansichten/-berichte,
`get_history`, `export_history`, Selbstkalibrierung, Morgenanpassung,
Erfahrungsbänder, manuelle Tageskorrekturen, Fremdprognose-, Kurzfrist- und
Temperaturvergleiche sowie experimentelle Minderertragshinweise.
`retirement.py` entfernt beim Laden einer Anlage ausschließlich ihre alten
history-, calibration- und morning-Stores samt zugehörigen Optionen und
Beobachtungshinweis. Aktuelle Messwerterfassung, Prognose, Tagesaussicht, Planung,
Energy/SAX und der separate Neustartcache bleiben bestehen. Config Entries
bleiben bei Schema 1.1. Diese Entscheidung ersetzt alle früheren Archiv- und
Lernanforderungen.

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
Fehlende Zeitabdeckung ist nicht null Ertrag. Die Sensoren erhalten gemäß der
Statistikentscheidung in #4/#27 dauerhaft keine `state_class`.

Eine gemeinsame Gesamtzeitreihe entsteht einmal je Berechnung nach Clipping,
begrenzt auf die zwei lokalen Prognosetage. Der versionierte Lesevertrag
enthält Intervallgrenzen, kWh, mittlere AC-kW, Abrufzeit, Abdeckung und
Eingabefallbacks. Der Modell-Ausgabezeitpunkt ist unbekannt. Qualitätsmarkierungen
sind keine gemessene Prognosegüte. Die rein lesende HA-Aktion
`pv_forecast.get_forecast` mit verpflichtender Anlagenauswahl ist die einzige
öffentliche Stunden-Schnittstelle für Automationen und die spätere Karte.
Sie löst kein HTTP aus und dupliziert keine Stundenlisten in Sensorattributen.
Energy verwendet dieselbe Datenbasis.

Ein gemeinsamer lokaler Minutentakt führt die Planungswerte nach; er verändert
weder Pollingfrist noch Abrufzeit oder Fehlerstatus und wird beim Entladen
beendet. Es entstehen keine zusätzlichen Wetterabrufe. PV-Erzeugung allein
ersetzt keine Verbrauchs-, Speicher- oder Tarifdaten. Diese abgegrenzte
Erweiterung ist in Issue #1 konkretisiert und ersetzt nur die entgegenstehenden
V1-Grenzen; die übrigen Architektur- und Modellvorgaben gelten weiter.

## Erweiterung nach V1: Energy-Adapter aus Issue #15

Der native, rein lesende Integrationsadapter `energy.py` stellt über
`async_get_solar_forecast` die gemeinsame Gesamtzeitreihe als `wh_hours` bereit.
UTC-Intervallbeginne bleiben eindeutig; kWh werden genau einmal in Wh umgerechnet.
Innere lokale Tagesgrenzen innerhalb eines Intervalls werden proportional geteilt.
Ohne einen erfolgreichen, höchstens 60 Minuten alten Abruf mit vorhandenem,
nicht zukünftigem Abrufzeitpunkt und vollständiger Abdeckung der beiden aktuellen
lokalen Tage gibt der Adapter keine Prognose aus (#171), da der native Vertrag weder Alter noch
Fehler oder Abdeckung kenntlich machen kann. Es entstehen keine zusätzlichen
Entities, Abrufe, Konfigurationsfelder oder Statistikklassen.

Die Einrichtung setzt echte Solarproduktionszähler im Energy Dashboard voraus.
Die Gesamtprognose wird genau einem solchen Zähler zugeordnet. Bekannte Grenzen
des nativen Frontends bei Mehrfachzuordnung (#52) und DST-/Teilstundenanzeige (#53)
werden dokumentiert; die interne UTC-Zeitreihe bleibt maßgeblich. Diese in #1
konkretisierte Ausnahme erweitert ausschließlich den nativen Plattformumfang.

## Optionale Messquellen aus Issue #26

Bestehende HA-Sensoren für bestätigte AC-PV-Erzeugung können optional über die UI
zugeordnet werden: Energie in Wh/kWh als fortlaufender oder täglicher Zähler,
optional Leistung in W/kW ohne eigene Integration zu Energie. Messgrenze und
Überschneidungsfreiheit werden ausdrücklich bestätigt. Netzexport, Verbrauch,
Batterieentladung und ungeklärte Hybrid-Messgrenzen sind keine PV-Erzeugung.
Abgeleitete Energie aus bestehenden Integral-Helfern wird gekennzeichnet.

Ein gemeinsamer lokaler Messpfad normalisiert UTC-Zeitpunkte und bildet gültige
Differenzen je Quelle vor der Aggregation. Lücken sind keine Nullwerte;
Zählerdifferenzen über Lücken werden nicht auf Stunden verteilt. Fehlender
Tagesabschluss bleibt unvollständig. Quellenwechsel beginnt neue Segmente,
Entity-Umbenennung mit gleicher Registry-Identität bewahrt den Bezug.
`measurements.py` bleibt unabhängig von HA; `measurement_runtime.py` erfasst
lokale HA-Ereignisse und verwaltet einen getrennt versionierten Store.
Dieser hält zunächst maximal sieben Tage, 20.000 Messpunkte und höchstens
20.000 daraus abgeleitete Zählerdifferenzen je Quelle;
Ein langfristiges Prognosearchiv gibt es nicht. Es gibt keinen Recorder-Import und keine
zusätzlichen Geräte- oder Wetterabrufe. Listener enden beim Entladen.
Daten sind gezielt über die Optionen löschbar und werden bei Entfernung der
Integration gelöscht. Das Config-Entry-Schema bleibt 1.1.

Die rein lesende Aktion `pv_forecast.get_measurements` liefert für eine explizite
Anlage und ein UTC-Fenster Einzelquellen, beobachtete Energie, Abdeckung und
Qualitätsmarkierungen. Leserechte der zugeordneten Sensoren werden mitgeprüft.
Es entstehen keine zusätzlichen Sensoren oder Messlisten in Sensorattributen.
Der Nutzertest mit fünf realen PV-Anwendern ist eine separate, noch offene
menschliche Abnahme und wird durch Offline-Tests nicht ersetzt.

## Freiwillige Lovelace-Karte aus #28

Ein kleines, mit der Integration gebündeltes JavaScript-Modul stellt eine
native Karte mit visuellem Editor bereit. Die Ressource wird bewusst über die
HA-Oberfläche hinzugefügt; das Backend funktioniert auch ohne Karte. Die erste
Lieferung zeigt Energie in kWh je tatsächlichem Intervall, vier Tageskennzahlen,
Heute/Morgen und Gesamt-/Dachauswahl.

Die vorhandenen Leseaktionen erhalten kompatible Darstellungsdaten: `get_forecast`
eine versionierte `view` mit fertigen Tages-/Dachwerten, `get_measurements` genaue
Gesamtwerte für höchstens 50 explizite UTC-Intervalle. Fehlende Messwerte,
unvollständige Erfassung und nicht zugeordnete Dachmessungen bleiben erkennbar.
Es entstehen keine neue öffentliche Stundenaktion, Wetter- oder Recorderabrufe
und keine PV-Berechnung im Browser.

Die Grafik positioniert nach absoluten UTC-Grenzen und beschriftet in der
gespeicherten Anlagenzeitzone. Wiederholte DST-Stunden und Teilstunden bleiben
unterscheidbar. Mehrere sichtbare Karten teilen Leseaufrufe; entfernte oder
ausgeblendete Karten halten keine Abrufschleifen aktiv. Tests und Screenshots
prüfen 360 px, Hell/Dunkel, Tastatur, Datenlücken und Fehlerzustände. Die reale
Nutzererprobung wird davon getrennt ausgewiesen.

## Freiwillige Dashboard-Einrichtung aus #96

Config-Flow-Abschluss und Options Flow bieten einen standardmäßig ausgeschalteten
Schalter für eine eigene PV-Seite in der HA-Seitenleiste sowie deren Titel.
Ein nativer HA-Custom-Panelvertrag lädt dafür das gebündelte Kartenmodul ohne
manuelle Ressourcenregistrierung oder YAML. Die Seite hat ein festes Layout;
die einzelne Lovelace-Karte bleibt für frei gestaltete Dashboards verfügbar.
Bestehende Dashboards, Ressourcen und Benutzer-Standardansichten bleiben erhalten.

Je Config Entry wird ausschließlich das eigene Panel mit einer stabilen,
namensunabhängigen URL verwaltet. Reload und Neustart erzeugen keine Duplikate;
Abschalten und Entladen entfernen nur das eigene Panel. Fremde URL-Belegungen
werden als Konflikt gemeldet. Ein später startendes Frontend wird abgewartet;
Entladen und Abbruch beenden auch wartende Listener. Das Backend bleibt ohne
Frontend oder Dashboard nutzbar.

Reine Dashboardänderungen werden ohne Config-Entry-Reload oder Wetterabruf
übernommen. Es gibt keine weiteren Stores, Sensoren, Aktionen oder Berechnungen;
Schema 1.1 und alle Daten-/Quellenrechte bleiben bestehen. Die eingebettete Karte
teilt weiter ihre Leseaufrufe und beendet sie beim Ausblenden oder Entfernen.
Tests prüfen beide Flows und den Panel-Lebenszyklus; Browserprüfungen sichern
360 px, Hell/Dunkel, Tastatur und die mobile HA-Menünavigation ab.

## Dashboard-Aktualisierung über HA-Reparaturen aus #101

Die automatisch verwaltete PV-Seite vergleicht beim Laden den SHA-256-Fingerprint
des gebündelten JavaScript-Moduls mit der zuletzt übernommenen Fassung in der
internen Entry-Option `dashboard_revision`. Ein erstmaliger Vergleichsstand
erzeugt keine Änderungsmeldung. Spätere Inhaltsänderungen erzeugen je Anlage eine
native behebbare Reparaturmeldung; unveränderte oder bestätigte Fassungen nicht.
Die native Issue Registry bewahrt Meldung und Ignorierung derselben Fassung über
Neustarts. Eine weitere Inhaltsänderung wird erneut angeboten.

Der native Reparaturdialog mit seinen Adminrechten übernimmt eine Fassung erst
nach erfolgreicher Registrierung des eigenen Panels. Ein interner Link lädt
danach die PV-Seite als neues Dokument, damit bereits registrierte Custom Elements
ersetzt werden. Der Modul-URL-Parameter verwendet den Inhaltsfingerprint. Fehler,
fremde URL-Belegung oder eine inzwischen neue Fassung werden nicht als erfolgreich
bestätigt. Abschalten oder Entfernen löscht nur die zugehörige Reparaturmeldung.

Schema 1.1 bleibt erhalten; es gibt keinen zusätzlichen Integrationsstore,
Wetterabruf, fachlichen Reload, Sensor oder Service. Manuelle Lovelace-Ressourcen
und andere Dashboards werden nicht verändert. Reparaturtexte stammen aus
`strings.json`. Diese Ausnahme erweitert ausschließlich den nativen
Plattformumfang um `repairs.py` und den bestehenden Dashboard-Lebenszyklus.

## Bedienung und Diagnosedaten aus #12, #19 und #14

Deutsche Formular- und Menütexte stammen aus `strings.json`. Native Menüs
verwenden übersetzbare Schlüssel; dynamische Zusammenfassungen lesen die
HA-Übersetzungen. `scripts/sync_translations.py` aktualisiert die deutschen
Ressourcen und die derzeit deutsche englische Rückfallsprache. Sein
schreibfreier Prüfmodus gehört zur CI; eine spätere freigegebene Übersetzung
muss nicht dauerhaft mit den deutschen Texten identisch bleiben.

Der native Diagnostics-Download stellt ausschließlich ausgewählte lokale
Metadaten bereit: Versionen, Anzahlen, Datenalter, Abdeckung sowie grobe
Fehler-, Mess- und Speicherzustände. Die Ausgabe entsteht aus einer festen
Allowlist und enthält keine Standorte, Koordinaten, Namen, IDs, URLs,
Fehlermeldungen, Haushalts- oder Ertragshistorien. Sie löst keine Wetterabrufe
oder Speicheränderungen aus und benötigt keine zusätzlichen Entities.
Home Assistant ergänzt seinen üblichen äußeren Diagnoserahmen.

## Solarzeitfenster aus #31

Für die erste Stufe von #31 erweitert ausschließlich die vorhandene Leseaktion `get_forecast` ihren Vertrag optional um eine versionierte Planung für die Gesamtanlage. Angefragt werden Laufdauer und eindeutige früheste/späteste UTC-Grenzen innerhalb der zwei lokalen Prognosetage. Reine Python-Funktionen maximieren die Energie eines zusammenhängenden Fensters anhand der bereits geclippten Intervalle. Gleichstände wählen den frühesten absoluten Start. Fehlende Zeitabdeckung, veraltete Daten, unbrauchbare Eingaben, Nullertrag und unerfüllbare Grenzen liefern begründete Leerzustände. Die Annahme konstanter mittlerer Leistung innerhalb eines Intervalls wird ausgegeben. Ein explizit mitgegebener bisheriger Start wird nur bei einer Verbesserung um mehr als 5 Prozent und 0,1 kWh verschoben, solange er noch zulässig ist; bereits gestartete Verbraucher werden nicht neu geplant. Die Karte nutzt dieselbe Antwort, ohne PV-Berechnung im Browser. Ein bewusst eingerichtetes HA-Automationsbeispiel dient als geprüfter Nutzungspfad; keine automatische Geräteaktion und keine Behauptung verfügbaren Überschusses. Bandbreiten für beliebige Laufzeitfenster werden ohne passende empirische Basis nicht erfunden.

P3-Ausweitungen (#21/#24/#20) sind ausdrücklich zur sequentiellen Umsetzung freigegeben. Die offenen realen Nutzer-/Güteprüfungen und Fehler des fremden nativen HA-Frontends bleiben ausdrücklich von technisch lieferbaren Repositoryänderungen getrennt.

## Tagesaussicht: erste Stufe zu #30

Die bestehende berechtigungsgeprüfte Messdatenaktion liefert optional die aktuelle Tagesaussicht aus einem vollständig belegten Messpräfix seit lokaler Mitternacht, einer sichtbar geschätzten Brücke vom letzten gemeinsam gesicherten Messzeitpunkt bis jetzt und der Prognose ab jetzt bis Tagesende. Alle drei Abschnitte sind disjunkt. Mehrere Quellen benötigen einen gemeinsamen exakten Zählergrenzzeitpunkt; fehlende Abdeckung, unklare Identität, Quellenwechsel oder Korrektur erzeugen keine künstliche vollständige Messung. Es werden keine Zählerdifferenzen anteilig zerlegt. Fehlende beziehungsweise veraltete Prognose verhindert eine vollständige Tagesaussicht, während vorhandene Messwerte und Restprognose weiterhin getrennt lesbar bleiben.

Diese rein lesende erste Stufe verwendet unverändert die wirksame gemeinsame Prognose und benötigt keine Einstellung, Persistenz oder zusätzlichen Abruf. Die Addition bekannter Messwerte behauptet keine verbesserte Vorhersage. Die Karte zeigt Messung, geschätzte Brücke und Zukunft getrennt.

## Standortänderung mit erhaltenen Daten zu #23

Der native Reconfigure-Flow erlaubt nachträgliche Standortkorrekturen über dieselben validierten Standortquellen wie das Setup. Ein Entwurf wird nach erfolgreichem Open-Meteo-Test und ausdrücklichem Abschluss gespeichert. Entry-ID, Unique-ID und Dach-/sonstige Optionen bleiben erhalten. Reine Namensänderungen ohne neue Koordinaten oder Zeitzone beginnen keine neue Vergleichsgrundlage.

Ein physischer Standort- oder Zeitzonenwechsel beginnt neue Messsegmente.
Mess-Store Version 2 ergänzt je Segment den ursprünglichen Standortkontext,
die ursprüngliche Zeitzone und den Beginn. Alte Zeitpunkte und Zählerdifferenzen
werden nicht in die neue Zone umgedeutet; vor dem Wechsel datierte HA-Zustände
liefern keine neue Zählerbasis. Vor der Änderung wird der Mess-Store im alten
Kontext sicher vorbereitet. Unbekannte oder unlesbare Messspeicherversionen
verhindern die Änderung. Aktive Manager werden vor dem Config-Update beendet;
danach folgt genau ein Reload. Config Entries bleiben bei Schema 1.1.

## Echte AC-Wechselrichtergruppen zu #22

Optional werden reale AC-Wechselrichtergruppen in den Optionen als stabile ID, Name, positive maximale AC-kW und zugeordnete stabile Dach-IDs geführt. Ein Dach gehört höchstens einer Gruppe an; mehrere Dächer dürfen ein gemeinsames Gerät teilen. Nicht zugeordnete Dächer unterliegen weiterhin dem bestehenden Anlagenlimit. Die UI erklärt AC-Gruppen ausdrücklich und deutet weder DC-MPPT-Grenzen noch Netzeinspeiselimits als zusätzliche Wechselrichter um.

Eine zentrale reine Funktion begrenzt zuerst jede Gruppe proportional innerhalb ihrer Dachbeiträge und anschließend genau einmal die resultierende Gesamtleistung nach dem bisherigen Anlagenlimit. Gruppen beeinflussen keine fremden Dachbeiträge. Ohne Gruppen bleibt der bisherige Rechenweg identisch.

Die Leseaktionen, Energy und Karte verwenden dieselbe Gesamtzeitreihe.
Es gibt keine zusätzlichen Sensoren oder Wetterabrufe. Physische Gruppenparameter
erweitern die Konfigurationskennung; reine Gruppennamen tun dies nicht.
Ohne Gruppen ändern sich der bisherige Rechenweg und die Kennung nicht.
Tests sichern getrennte Geräte, mehrere Dächer an einem Gerät und das
kombinierte Anlagenlimit ab.

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

Die UI bietet acht verständliche Himmelsrichtungen und einen frei wählbaren
Kompasswinkel von 0° bis unter 360° an. Exakte Winkel und Nachkommastellen
bleiben auch beim Bearbeiten ohne Rundung erhalten; IDs und das bestehende
Config-Entry-Schema 1.1 bleiben unverändert. Eine zentrale Funktion übersetzt
den Kompasswinkel in die Open-Meteo-Konvention: Süd = 0°, Ost = -90°, West =
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
python scripts/sync_translations.py --check
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
Messdaten und Prognosecache sind unabhängig versioniert.

Vor einer Änderung den aktuellen `main`-Stand holen und einen eigenen Branch
anlegen. Änderungen klein und testbar halten, alle Qualitätsprüfungen ausführen,
committen, pushen und mit einer aussagekräftigen Pull-Request-Beschreibung
einreichen. Keine unnötigen Abhängigkeiten oder spekulativen Erweiterungen
einführen.

Jede Umsetzung benötigt vor dem Merge ein Review des endgültigen Änderungsstands.
Das gilt auch für reine Dokumentationsänderungen und Änderungen dieser
Projektanweisungen. Review-Ergebnis und geprüften Commit im Pull Request
nachvollziehbar festhalten; offene Review-Befunde vor dem Merge klären und
erforderliche Korrekturen erneut prüfen lassen. Sofern nicht explizit anders
gewünscht kann das LLM das Review ohne weitere Nachfrage selbst durchführen.

Nach erfolgreich abgeschlossenem Review und erfolgreichen verbindlichen
Qualitätsprüfungen einschließlich der GitHub-CI darf der Pull Request
selbstständig ohne erneute Merge-Bestätigung gemergt werden. Repositoryseitige
Schutzregeln und erforderliche Freigaben bleiben verbindlich und werden nicht
umgangen.

## Optionaler Prognosehorizont aus #21

Die Optionen erlauben zwei bis sieben lokale Tage, Standard zwei. Die gemeinsame UTC-Zeitreihe, Forecast-Abfrage und Solarzeitfensterplanung verwenden diesen Horizont. Die Karte zeigt kompakte datierte Gesamt-/Dachwerte; spätere Tage heißen Tendenz, ihre gemessene Güte bleibt bis zu einem passenden Nachweis nicht verfügbar. Sensorzahl, IDs und Schema 1.1 bleiben erhalten. Energy gibt weiterhin ausschließlich heute/morgen aus. Pro Geometrie bleibt es bei einem HTTP-Request; Antwortumfang und anbieterabhängig gezähltes Kontingent werden getrennt ausgewiesen. Diese Ausnahme ersetzt nur die bisherigen Zweitagesgrenzen für Abruf, gemeinsame Zeitreihe und Planung.

## Mehrere unabhängige Anlagen aus #24

Je logischer Anlage wird ein eigener Config Entry unterstützt. Neue Einträge erhalten eine stabile zufällige Unique-ID; bestehende Domain-Unique-IDs, Entry-/Entity-/Dach-IDs und Stores bleiben unverändert. Schema 1.1 bleibt erhalten. Weitere Einträge benötigen einen unterscheidbaren Namen, an ähnlichen Koordinaten zusätzlich eine ausdrückliche Bestätigung der unabhängigen Anlage. Gerundete Koordinaten sind nur Duplikatheuristik, keine Identität. Gleichörtliche laufende Setups werden gesperrt; der Abschluss prüft neue Nachbarn erneut. Standortänderungen verwerfen alte Bestätigungen. Coordinator, Sensoren, Limits, Messzustände und Karten bleiben je Entry getrennt. HA-weit geteilt sind ausschließlich die flüchtige Anbieterpause und HTTP-Slots. Entfernen einer Anlage berührt andere Anlagen nicht. Diese Ausnahme ersetzt die frühere Beschränkung auf genau einen Entry.

## Experimentelles Horizontprofil aus #20

Die ausdrücklich freigegebene P3-Erweiterung ergänzt optional je stabiler Dach-ID
12 oder 24 gleichmäßig verteilte Höhenwinkel von 0 bis 90 Grad, Nord zuerst im
Uhrzeigersinn und zyklisch linear interpoliert. Die Options-UI verlangt eine
Bestätigung für einen entfernten Horizont der gesamten Dachfläche. Leere und
Nullprofile sind deaktiviert. Dachlöschung entfernt nur das eigene Profil.
Beliebige PVGIS-Exporte sind kein unterstütztes Importformat; nur die geprüfte
Richtungs-/Höhenkonvention der manuellen Uploadlisten stimmt überein.

Ausschließlich betroffene Geometrieabrufe ergänzen DNI und DHI im vorhandenen
Open-Meteo-Request. Regelversion 1 berechnet mit der veröffentlichten geometrischen
NOAA-Näherung ohne Refraktion zwölf absolute UTC-Mittelpunktproben je Wetterintervall.
Konstantes stündliches DNI wird auf die Dachfläche projiziert. Der abziehbare
Direktanteil ist durch seinen Probenmittelwert und GTI oberhalb des konservativ
erhaltenen isotropen diffusen Anteils begrenzt. Nur sein blockierter Anteil wird
abgezogen; das Ergebnis bleibt nichtnegativ. Fehlende einzelne Zusatzwerte erhalten
GTI mit `horizon_input_fallback`, fehlerhafte Zusatzreihen erzeugen Updatefehler.
`shading.py` bleibt rein und wird vor Temperatur, Verlusten sowie
Gruppen-/Gesamtclipping in `calculations.py` eingebunden.

Effektive Profile samt Regelversion erweitern ausschließlich bei Aktivierung die
physische Konfigurationskennung und invalidieren dadurch inkompatible Neustartcaches. Speicherverträge und
Config-Entry-Schema 1.1 bleiben bestehen. Forecast-Antwort und Karte kennzeichnen
die experimentelle Anwendung. Ein synthetischer Winter-/Sommervergleich belegt
nur den zeitabhängigen Mechanismus; der reale Zusatznutzen gegenüber konstanten
Verlusten bleibt offen. Nahverschattung, elektrische Stringeffekte,
diffuse Himmelsverdeckung und 3D-Modelle sind ausdrücklich nicht enthalten.

## Geführte Messquellenauswahl für vorhandene Geräte

Auf ausdrücklichen Anwenderwunsch ergänzt der Config-/Options-Flow eine einfache Auswahl bereits eingerichteter Geräte. Der erste Adapter erkennt KOSTAL KSEM aus der Integration `ksem` anhand der Registry und des AC-Summenregisters 40974. Bestätigung: keine Batterie an den erfassten Wechselrichtern, vollständige AC-PV-Messgrenze und keine doppelte Energiezuordnung. Weitere Adapter beschreiben Erkennung und Messart zentral; Netz-/DC-/Verbrauchssensoren werden nicht als PV-Ertrag vorgeschlagen. Die manuelle Zuordnung bleibt erhalten.

Für bestätigte Leistungsmessungen erstellt der Assistent erst beim Speichern einen nativen HA-Integral-Helfer oder verwendet einen passenden vorhandenen. kWh, Trapezregel und ereignisbasierte Integration ohne Fortschreibung stehengebliebener Messwerte sind festgelegt. Ein solcher Helfer bleibt als eigenständiger, auch anderweitig nutzbarer HA-Helfer beim Entfernen der PV-Zuordnung erhalten; dies wird vorab erklärt. Abbruch vor dem Speichern erzeugt keinen Helfer, Fehler beim Anlegen rollen neu angelegte Helfer zurück. Abgeleitete Energie bleibt gekennzeichnet. Quellenidentität, Leserechte und Datenlücken berücksichtigen auch den zugrunde liegenden Leistungssensor. Keine eigenen Geräteabrufe, keine eigene numerische Integration, keine neuen Prognosesensoren oder öffentlichen Aktionen. Config-Entry-Schema bleibt 1.1. Die Ausnahme erweitert ausschließlich die Einrichtung nativer Messhelfer und ersetzt die entgegenstehende Beschränkung auf bereits vorhandene Helfer. Offline-Tests ersetzen keine reale KSEM-Erprobung.

## Korrektur der Messauswertung aus #105 und #109

Auf ausdrücklichen Anwenderauftrag werden abgeleitete Energiedifferenzen über bekannte Berichts-, Neustart- oder Ausgangssensorlücken nicht als beobachtete Energie übernommen. Ein Integral-Helfer kann den unbekannten Leistungsverlauf nicht durch seinen fortlaufenden Zählerstand belegen. Echte fortlaufende Energiezähler behalten die bisherige Trennung zwischen belegter Gesamtmenge und unbekannter Stundenverteilung.

Für die Fensterauswahl dürfen gültige, lückenlos beobachtete Zählerdifferenzen mit exakt null kWh innerhalb derselben Quellen-/Standortidentität an UTC-Grenzen zugeschnitten werden. Die Energie jedes Teilintervalls ist exakt null; dies ist keine proportionale Schätzung. Positive Zählerdifferenzen, Lücken, Resets, Korrekturen und Quellenwechsel werden weiterhin nicht auf Teilfenster verteilt. So können gewöhnliche versetzte Meldezeitpunkte bei beobachteten nächtlichen Nullplateaus vollständige Tageswerte liefern. Die zentrale Lesesicht gilt gleichermaßen für Gesamtmessung, Karte und Tagesaussicht; Rohmessungen bleiben unverändert gespeichert.

Ein täglicher Reset allein belegt keine endgültige Vortagsmenge. Solche Quellen bleiben für belegte Messabschnitte nutzbar; die UI erklärt bei der Auswahl ihre Grenze und verweist für vollständige Tagesberichte auf einen fortlaufenden AC-Ertragszähler beziehungsweise den KSEM-Assistenten. Es werden keine fehlenden Abschlusswerte erfunden. Keine zusätzlichen Sensoren, Aktionen, Wetter-/Geräteabrufe oder Speicherfelder; Config-Entry-Schema 1.1 bleibt bestehen.
## Frei gewählte Prognosefenster aus #130

Die vorhandene Leseaktion `get_forecast` ergänzt optional `window` mit eindeutigen Start-/Endzeitpunkten und optional 5/15/30/60 Minuten Raster. Der Start verankert das exakt teilbare Raster mit höchstens 336 Schritten innerhalb des konfigurierten Horizonts. Version 1 liefert ausschließlich die Gesamtanlage: UTC-Grenzen, Abrufzeit, Abdeckung, Qualitätsmerkmale, Energie in kWh und mittlere AC-kW. Exakte UTC-Überlappungen verwenden konstante Intervallmittel; Rasterenergien erhalten die direkte Fenstersumme. Nullertrag bei vollständiger Abdeckung ist verfügbar. Fehlende Abdeckung, Bereichsüberschreitung, Eingabefallbacks im Fenster oder fehlgeschlagene/fehlende/zukünftige/über 60 Minuten alte Abrufe liefern begründete Leerzustände ohne numerische Rasterwerte. Nicht angefragte Datenlücken blockieren vollständige Teilfenster nicht. Planung und Fenster dürfen gemeinsam gelesen werden; Dachauswahl betrifft weiterhin nur die Kartenansicht. Rechteprüfung, Rohantwort, Schema 1.1 und EMHASS-Kompatibilität bleiben erhalten. Keine neuen Aktionen, Optionen, Sensoren, Speicherung, Wetterdaten oder Geräteaktionen.

## Optionaler Prognosecache aus #129

Die standardmäßig ausgeschaltete Option „Letzte Prognose für Neustarts speichern“ hält genau einen vollständig erfolgreichen Rohmodellstand je Anlage in einem atomaren HA-Store Version 1 mit höchstens 8 MiB. Er enthält Modell-/Konfigurationskennung, Horizont, Zone, stabile Dachzuordnungen, ursprüngliche DC-/AC-Beiträge und Grenzen, Qualitätsmerkmale sowie echte Abrufzeit, keine Messhistorie oder HTTP-Antwort. Namen werden aus der aktuellen Konfiguration gelesen. Ungültige, fremde, zukünftige, unvollständige oder inkompatible Stände werden abgelehnt; unbekannte Storeversionen nicht überschrieben, Übergröße kürzt keine Intervalle.

Ein gültiger, noch überlappender Cache ermöglicht das Laden einer bestehenden Anlage nach fehlgeschlagenem erstem Wetterabruf. Herkunft `restored|live`, ursprünglicher `fetched_at` und separates `restored_at` bleiben sichtbar. Sensoren behalten ihren Fehlervertrag; Energy, operative Planung, Tagesaussicht und Zeitfenster erhalten bei Abruffehler keine verwendbare Prognose. Die neue Einrichtung benötigt weiter den erfolgreichen Open-Meteo-Test. Messlistener starten einmal und bewahren Neustartlücken; vor dem Wiederherstellungsstart gemeldete HA-Werte sind keine frische Messbasis.

Nur echte erfolgreiche Wetterupdates schreiben den Cache. Lokale Takte schreiben ihn nicht. Schreibgenerationen und Entladen/Abschalten/Löschen verhindern nachlaufende Wiederherstellung entfernter Dateien. Abschalten und Entry-Entfernung löschen nur diesen Cache. Config Entry 1.1 sowie Mess-Stores bleiben unverändert; keine neuen Sensoren, Aktionen oder Abrufschleifen. Deutsche UI-Texte und grobe, datensparsame Cachediagnose ergänzen die bestehenden Anzeigen.

## Rein lokaler Betriebscheck aus #131

Die Integrationsoptionen bieten „Betrieb prüfen“ mit erneutem rein lokalem Lesen. Feste Ergebniskennungen, Schweregrade info/warning/error, übersetzte Erklärungen und nächste menschliche Schritte ordnen Wetter/Prognose, Messquellen und optionalen Cache ein. Ausgeschaltete Funktionen sind keine Fehler; unbekannte Zustände bleiben nicht prüfbar. Ohne Runtime bleibt der Bericht eingeschränkt nutzbar.

Der Check verwendet vorhandene Entry-/Coordinator-/Registry-/Managerzustände und explizite Zeit. Er löst weder HTTP, Storezugriffe noch Konfigurationsänderungen aus. Anbieterpausen bedeuten frühestens mögliche Wiederholung, keine garantierte Abrufzeit. Gespeicherter gesamter Horizont und aktuelle Heute-/Morgen-Abdeckung werden nach UTC getrennt geprüft. Die Diagnose-Allowlist bleibt unabhängig vom ausführlicheren UI-Bericht und enthält keine Identitäten, Erträge oder Exceptiontexte. Native Rechte, Schema 1.1 und bestehende Stores/Aktionen bleiben erhalten.

## Erklärung der aktuellen Prognose aus #133

Die Prognoseaktion ergänzt optional `include_explanation` mit einem eigenen
Erklärungsblock (Schema 2) für die Gesamtanlage und heute/morgen. Je UTC-Intervall
gilt: Energie vor Clipping minus Gruppenkürzung minus zusätzliche Gesamtkürzung
gleich AC-Energie. Die Bilanz wird gegen Zeitreihe und Tageskennzahl geprüft.
Die Zwischenstufen entstehen einmal je Wettergeneration; Leseaktionen
projizieren nur UTC-Überlappungen. Fehlende oder inkompatible Daten liefern
keine erfundenen Werte. Herkunft, Abrufzeit, Fehler und Qualitätsmerkmale bleiben
sichtbar. Die Karte zeigt die Bilanz unter „Prognose erklärt“.
Modellierte Begrenzungsverluste sind keine gemessenen Geräteverluste.
Keine neuen Sensoren, Stores, Aktionen oder Wetter-/Messabrufe.

## Vereinfachte Energieanzeige vom 10.09.2026

Auf ausdrücklichen Anwenderwunsch entfallen in der Karte einschließlich Kennzahlen,
Legende, Intervallwerten und Meldungen die Hinweise auf teilweise/unvollständig
erfasste oder aus Leistung berechnete Ist-Energie. Das Diagramm zeigt ausschließlich
die Prognose durchgezogen und die vorhandenen tatsächlichen Energiemengen als eine
gestrichelte Linie. Messbalken, separate Teilmengen- und Grundmodellkurven entfallen.
Fehlende Werte bleiben leer. Backendwerte, Qualitätsmerkmale, Abdeckungsprüfung
und Messregeln bleiben unverändert. Diese in Issue #1 festgehaltene
Darstellungsentscheidung ersetzt die entgegenstehenden Kennzeichnungs- und
Kurvenvorgaben der Karte.

## Robuste lokale Erfassung und Speicherung aus #152–#155

Schnell meldende Energiezähler dürfen nicht allein wegen redundanter Zwischenstände den vollständigen Tagesnachweis verlieren. Vor der bisherigen harten Anzahlgrenze werden zusammenhängende, einwandfreie Differenzen derselben Quelle und desselben Segments innerhalb einer UTC-Minute und desselben ursprünglichen lokalen Tages verlustfrei verdichtet. Nur exakt darstellbare Energiesummen werden zusammengezogen; positive Energie wird nicht anteilig zerlegt. Lücken, Resets, Korrekturen, Identitätswechsel sowie Übergänge zwischen Null- und positiver Energie bleiben erhalten. Entbehrliche rohe Zwischenpunkte dürfen entfallen. Sieben Tage, 20.000 Punkte und 20.000 Differenzen je Quelle bleiben harte Obergrenzen. Unvermeidbarer Verlust belegter Energie durch die Anzahlgrenze wird im lokalen Betriebscheck für den betroffenen laufenden Tag sichtbar. Mess-Store 3 migriert Versionen 1/2 verlustfrei und ergänzt nur die dafür erforderliche begrenzte Verlustmarkierung.

Reguläre Messschreibungen verwenden feste Fünfminutentermine, höchstens 288 pro Tag. Die Messspeicherung serialisiert unveränderte Inhalte nicht bei jedem Ereignis erneut vollständig. Kohärente, entkoppelte Snapshots werden im Event Loop vorbereitet; die eigentliche JSON-Kodierung und Dateioperation laufen im Executor. Neue Generationen, Fehler, verzögertes Schreiben, Entladen und Löschen bewahren die bestehenden Bestätigungs- und Wiederholungsregeln.

Die Tagesaussicht ergänzt im bestehenden Schema 1 das Alter der gemeinsamen Messgrenze, ein eigenes Veraltungssignal gemäß den bereits konfigurierten individuellen Quellenfristen und getrennte Mess-/Prognosequalitätsmerkmale. Eine alte gemeinsame Grenze bei noch frischen versetzt meldenden Quellen ist allein kein Veraltungsbeleg. Die Karte zeigt diese Information nur innerhalb der geöffneten Tagesaussicht. Die aktuelle Berechnung und die vereinfachte übrige Energieanzeige bleiben erhalten. Keine neuen Optionen, Entities, öffentlichen Aktionen, Stores, Wetter-/Geräteabrufe oder Änderungen am Config-Entry-Schema 1.1.

## Verfügbare Tagesaussicht bei Messlücken vom 11.09.2026

Auf ausdrücklichen Anwenderwunsch bleibt die Tagesaussicht bei fehlenden oder unvollständigen Messdaten sichtbar. Ein kompatibler, eigenständig versionierter `estimate`-Block in der bestehenden Messdatenantwort ergänzt belegte gemeinsame Messabschnitte der aktuellen Quellen-/Standortidentität mit der vorhandenen wirksamen Prognose für die übrigen Abschnitte. Messung, geschätzte Vergangenheit und Restprognose sind disjunkt; positive Zählerdifferenzen werden nicht aufgeteilt. Fehlende Quellen oder gemeinsame exakte Grenzen führen zur reinen Tagesprognose. Ohne Messleserechte verwendet die Karte ausschließlich die bereits berechtigte Tageskennzahl aus `get_forecast`.

Die Karte zeigt die verfügbare Tagessumme und erklärt knapp, welche Anteile geschätzt sind. Ein älterer oder mit Eingabefallbacks berechneter vorhandener Prognosestand bleibt mit entsprechendem Hinweis sichtbar. Wenn auch die benötigte Prognoseabdeckung fehlt, werden keine Zahlen erfunden. Die bisherigen strengen `outlook`-Felder, Mess-, Energy- und operativen Planungsregeln bleiben erhalten. Keine neuen Aktionen, Stores, Einstellungen, Wetter-/Geräteabrufe oder Browserberechnungen; Config-Entry-Schema 1.1 bleibt bestehen. Diese Entscheidung ersetzt die entgegenstehenden Anzeigevorgaben der bisherigen Tagesaussicht.


## Direkte PV-Prognosequelle für SAX Power aus #175

Die bestehenden Gesamtsensoren „Prognose heute“, „Prognose morgen“ und
„Restertrag heute“ bilden den SAX-Vertrag als numerischen Energiezustand in kWh
mit `device_class: energy` und ohne `state_class` ab. Es entstehen keine doppelten
Sensoren, Solcast-Attribute, Optionen oder Abrufe. SAX verwendet eine gemeinsame
Quelle und wechselt nicht automatisch zwischen den dokumentierten Zeiträumen.

Die drei Zustände benötigen einen erfolgreichen, bekannten, nicht zukünftigen,
höchstens 60 Minuten alten Abruf und einen vollständig gültigen Zeitraum ohne
Eingabefallbacks gemäß dem vorhandenen operativen Fenstervertrag. Ungültige Werte
werden als `unavailable` ausgegeben; echte vollständige Null bleibt 0. Der
bestehende Minutentakt führt die Verfügbarkeit spätestens zur nächsten Minute
nach, ohne Abrufzeit, Pollingfrist oder Fehlerstatus zu ändern. Offline restaurierte
Caches geben die Sensoren nicht frei. Anlagenzeitzone, UTC-Tagesgrenzen und stabile
IDs bleiben maßgeblich. Dach-, übrige Planungssensoren und rohe Leseaktionen
behalten ihre bisherigen Alters-/Qualitätsregeln. Der Sensor liefert erwartete
AC-PV-Erzeugung; der SAX-Nutzungsanteil wird ausschließlich in SAX angewendet.
