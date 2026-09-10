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
Energy und Archiv verwenden bei ihrer Umsetzung dieselbe Datenbasis.

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
Ohne einen erfolgreichen, vollständigen Stand für die beiden aktuellen lokalen
Tage gibt der Adapter keine Prognose aus, da der native Vertrag weder Alter noch
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
Langzeitarchivierung gehört zu #27. Es gibt keinen Recorder-Import und keine
zusätzlichen Geräte- oder Wetterabrufe. Listener enden beim Entladen.
Daten sind gezielt über die Optionen löschbar und werden bei Entfernung der
Integration gelöscht. Das Config-Entry-Schema bleibt 1.1.

Die rein lesende Aktion `pv_forecast.get_measurements` liefert für eine explizite
Anlage und ein UTC-Fenster Einzelquellen, beobachtete Energie, Abdeckung und
Qualitätsmarkierungen. Leserechte der zugeordneten Sensoren werden mitgeprüft.
Es entstehen keine zusätzlichen Sensoren oder Messlisten in Sensorattributen.
Der Nutzertest mit fünf realen PV-Anwendern ist eine separate, noch offene
menschliche Abnahme und wird durch Offline-Tests nicht ersetzt.

## Optionales Prognosearchiv aus #27 und Statistikentscheidung #4

Das Archiv ist per UI opt-in und speichert tatsächlich rechtzeitig beobachtete
Prognosestände in einem getrennten HA-Store (seit #32 Version 6). Festgelegte Stichtage:
18 Uhr am Vortag und 06 Uhr am Zieltag für lokale Tageswerte (maximal zwei
Stunden alte Prognose), Vorlauf eine beziehungsweise drei Stunden für
UTC-Intervalle (maximal eine Stunde alte Prognose). Nach dem Stichtag werden
diese Prognosewerte nicht geändert. Fehlende rechtzeitige Daten bleiben fehlend.
Abruf-/Beobachtungszeit, Zielintervall, Zeitzone, Konfigurations-, Messgrenzen-
und Modellversion werden gespeichert; die Wettermodell-Ausgabezeit bleibt null.

`history.py` enthält die reine Auswahl und Bewertung, `history_runtime.py` die
HA-Anbindung und Speicherung. Sie verwenden die gemeinsame Gesamtzeitreihe und
die Messsegmente aus #26. Messkorrekturen werden revisioniert; Quellenwechsel
übertragen keine Messung still auf andere Grenzen. MAE und Bias in kWh sowie
Stichprobe und Abdeckung werden für 7/30/90 abgeschlossene lokale Tage nach
Horizont getrennt ausgewiesen. Große Prognosefehler sind kein Ausschlussgrund.
Vergleichsvarianten verwenden dieselben gültigen Testintervalle; ohne reale
Kalibrierung werden keine korrigierten Ergebnisse behauptet.

Optional werden vorhandene HA-Tagesprognosesensoren für heute/morgen rein lokal
mitgelesen, nach bestätigter gleicher AC-Messgrenze und Tageszeitzone. Es gibt
keinen zusätzlichen Anbieter- oder Recorder-Zugriff. Reguläre Archivschreibungen
haben feste Fünfminutentermine, höchstens 288 pro Tag. Aufbewahrung:
Stundenstände 90 Tage, Tagesbewertungen 365 Tage, maximal 6.000 Zieldatensätze,
je drei frühere Bewertungsrevisionen und zusätzlich eine Grenze von 32 MiB.
Kürzungen bleiben sichtbar. Unbekannte Speicherversionen werden nicht überschrieben.

`get_history` und `export_history` lesen mit expliziter Anlagenauswahl und
Quellen-Leserechten, ohne HTTP oder automatisch veröffentlichte Dateien.
Löschung erfolgt bewusst in den Optionen; Quellenlöschung entfernt auch deren
Archiv-Messkopien. Entladen beendet Listener. Config-Entry-Schema bleibt 1.1.
Prognosesensoren behalten dauerhaft keine `state_class`; die Langzeitauswertung
verwendet dieses Archiv statt einer irreführenden Erzeugungszählerstatistik.

## Freiwillige Lovelace-Karte aus #28

Ein kleines, mit der Integration gebündeltes JavaScript-Modul stellt eine
native Karte mit visuellem Editor bereit. Die Ressource wird bewusst über die
HA-Oberfläche hinzugefügt; das Backend funktioniert auch ohne Karte. Die erste
Lieferung zeigt Energie in kWh je tatsächlichem Intervall, vier Tageskennzahlen,
Heute/Morgen und Gesamt-/Dachauswahl sowie optionale 7-/30-Tage-Berichte.

Die vorhandenen Leseaktionen erhalten kompatible Darstellungsdaten: `get_forecast`
eine versionierte `view` mit fertigen Tages-/Dachwerten, `get_measurements` genaue
Gesamtwerte für höchstens 50 explizite UTC-Intervalle und `get_history` bereits
eingefrorene aktuelle Stundenstände. Die Archivlinie heißt „Jeweils 1 Stunde
vorher“; sie behauptet keine gemeinsame ursprüngliche Tagesausgabe. Fehlende
Messwerte, unvollständige Erfassung und nicht zugeordnete Dachmessungen bleiben
erkennbar. Es entstehen keine neue öffentliche Stundenaktion, Wetter- oder
Recorderabrufe und keine PV-/Lernberechnung im Browser.

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

## Optionale Selbstkalibrierung aus #18

Ein begrenzter Anlagenfaktor wird ausschließlich in den Optionen aktiviert:
aus (Standard), beobachten oder nach erfolgreicher Prüfung automatisch anwenden.
Beobachtung und Automatik benötigen ein aktiviertes Archiv und bestätigte
AC-Energiequellen. Der Faktor von 0,5 bis 1,5 gilt vor dem bestehenden
proportionalen AC-Clipping. Anwenderwerte bleiben unverändert. Rohmodell und
wirksame gemeinsame Zeitreihe bleiben getrennt; lokale Faktorwechsel lösen
weder HTTP aus noch verändern sie Abrufzeit oder Fehlerstatus.

Regelversion 1 verwendet ausschließlich `daily_previous_18`: die jüngsten
30 gültigen Lerntage innerhalb von 60 lokalen Tagen, Faktor in Schritten von
0,01 mit minimalem Tages-MAE, bei Gleichstand näher an 1. Training benötigt
vollständige Messung, brauchbare Eingaben, mindestens 0,1 kWh ungekürzte Energie
und höchstens 10 Prozent regulären Clippingverlust. Fehlende Daten und bekannte
Abregelung/Wartung sind ungeeignet; große Fehler allein bleiben erhalten.

Ein fester Kandidat benötigt anschließend mindestens 14 erst danach rechtzeitig
eingefrorene Prüftage innerhalb von 28 lokalen Kalendertagen. Freigabe verlangt
mindestens 5 Prozent geringeren MAE bei positivem Roh-MAE und höchstens 5 Prozent
höheres 90%-Quantil absoluter Fehler, auf denselben Prüftagen. Die Prüfung lässt
reguläres Clipping und große Fehler zu. Nach Freigabe wird sie mit den jüngsten
14 Tagen innerhalb von 28 Tagen fortgeführt; fehlender Nachweis bedeutet Faktor 1.
Nach Ablehnung oder Abbruch beginnt frühestens nach 14 Tagen ein neuer Versuch.

Neue Archivstände erhalten eine unveränderliche UTC-Basis vor Clipping und
gegebenenfalls angewendete sowie geprüfte Kandidatenwerte. Die verlustfreie
Archivmigration 1 auf 2 ergänzt für alte Daten keine erfundene Lernbasis. Ein
getrennter Lern-Store Version 1 hält Segment, Kandidat, 30 Trainingsreferenzen
und höchstens 28 Prüfbelege mit Inhaltsfingerprints, auf 1 MiB begrenzt.
Reguläre Schreibungen folgen festen Fünfminutenterminen. Config Entries bleiben
bei Schema 1.1; unbekannte Speicherversionen werden nicht überschrieben.

Messkorrektur, Quellenlöschung oder physische Anlagenänderung entzieht betroffenen
Faktoren ihre Freigabe. Reine Registry-Umbenennung bewahrt den Bezug. Rücksetzen
beginnt ab jetzt ein neues Lernsegment. Ausschalten verwendet Faktor 1;
Quellen-/Archivlöschung entfernt abhängige Lernbelege. Entladen beendet Listener,
Entfernen der Integration löscht den Lern-Store.

Die Optionen zeigen Lernstatus und Vergleich und erlauben maximal 90 bewusste
Markierungen bekannter Abregelung/Wartung für lokale Tage des letzten Jahres.
Die bestehende Archiv-Leseaktion ergänzt den Status unter denselben Quellenrechten.
Es entstehen keine neuen Sensoren oder Aktionen. Die reale Güteerprobung bleibt
offen; Kalibrierung ist weder Defektmeldung noch garantierte Verbesserung.

## Bedienung und Diagnosedaten aus #12, #19 und #14

Deutsche Formular- und Menütexte stammen aus `strings.json`. Native Menüs
verwenden übersetzbare Schlüssel; dynamische Zusammenfassungen lesen die
HA-Übersetzungen. `scripts/sync_translations.py` aktualisiert die deutschen
Ressourcen und die derzeit deutsche englische Rückfallsprache. Sein
schreibfreier Prüfmodus gehört zur CI; eine spätere freigegebene Übersetzung
muss nicht dauerhaft mit den deutschen Texten identisch bleiben.

Der native Diagnostics-Download stellt ausschließlich ausgewählte lokale
Metadaten bereit: Versionen, Anzahlen, Datenalter, Abdeckung sowie grobe
Fehler-, Mess-, Archiv- und Lernzustände. Die Ausgabe entsteht aus einer festen
Allowlist und enthält keine Standorte, Koordinaten, Namen, IDs, URLs,
Fehlermeldungen, Haushalts- oder Ertragshistorien. Sie löst keine Wetterabrufe
oder Speicheränderungen aus und benötigt keine zusätzlichen Entities.
Home Assistant ergänzt seinen üblichen äußeren Diagnoserahmen.

## Solarzeitfenster aus #31

Für die erste Stufe von #31 erweitert ausschließlich die vorhandene Leseaktion `get_forecast` ihren Vertrag optional um eine versionierte Planung für die Gesamtanlage. Angefragt werden Laufdauer und eindeutige früheste/späteste UTC-Grenzen innerhalb der zwei lokalen Prognosetage. Reine Python-Funktionen maximieren die Energie eines zusammenhängenden Fensters anhand der bereits geclippten Intervalle. Gleichstände wählen den frühesten absoluten Start. Fehlende Zeitabdeckung, veraltete Daten, unbrauchbare Eingaben, Nullertrag und unerfüllbare Grenzen liefern begründete Leerzustände. Die Annahme konstanter mittlerer Leistung innerhalb eines Intervalls wird ausgegeben. Ein explizit mitgegebener bisheriger Start wird nur bei einer Verbesserung um mehr als 5 Prozent und 0,1 kWh verschoben, solange er noch zulässig ist; bereits gestartete Verbraucher werden nicht neu geplant. Die Karte nutzt dieselbe Antwort, ohne PV-Berechnung im Browser. Ein bewusst eingerichtetes HA-Automationsbeispiel dient als geprüfter Nutzungspfad; keine automatische Geräteaktion und keine Behauptung verfügbaren Überschusses. Bandbreiten für beliebige Laufzeitfenster werden ohne passende empirische Basis nicht erfunden.

P3-Ausweitungen (#21/#24/#20) sind ausdrücklich zur sequentiellen Umsetzung freigegeben. Die offenen realen Nutzer-/Güteprüfungen und Fehler des fremden nativen HA-Frontends bleiben ausdrücklich von technisch lieferbaren Repositoryänderungen getrennt.

## Erfahrungsband: erste prüfbare Stufe zu #29

Die erste Stufe bewertet ausschließlich tatsächlich eingefrorene Tagesprognosen (`daily_previous_18` und `daily_same_06`) anhand passender Tagesresiduen. Historische gleitende Restfenster fehlen bislang; für diese und beliebige Planungsfenster bleibt die Bandbreite ausdrücklich nicht verfügbar. Es werden keine Stundenquantile zu Tagesquantilen addiert.

Regelversion 1 nutzt je kompatiblem Modell, Anlagenkonfiguration und bestätigter Quellenidentität 60 chronologisch frühere gültige Trainingstage und 30 strikt spätere Prüftage innerhalb der letzten 180 lokalen Tage. Rohmodell und damals angewendete Kalibrierungsmethode werden getrennt bewertet; laufende Faktorwerte oder Kandidaten-IDs teilen dieselbe Methode nicht täglich auf. Frühere Messkorrekturen werden nur benutzt, wenn ihre Bewertung bereits vor dem ersten Prüfforecast bekannt war. Fehlt diese belegbare Revision, fehlt die Stichprobe.

Das signierte empirische Residuenband hat als vorab festgelegtes Ziel zentrale 80 Prozent. Die spätere Prüfung verlangt mindestens 70 Prozent beobachtete Abdeckung und bewertet zugleich Breite und Winkler-Score gegen ein ausschließlich aus dem Training bestimmtes breites Referenzband. Energiegrenzen werden auf null und das bekannte AC-Limit begrenzt, auch während der Prüfung. Ziel, tatsächliche Abdeckung, mittlere Breite, Stichprobengröße und ein ausdrücklich nur unter Unabhängigkeitsannahme indikatives Wilson-Intervall bleiben sichtbar. Ohne bestandene Prüfung erscheinen keine Grenzen, sondern ein begründeter Leerzustand. Keine Genauigkeits- oder Sicherheitsgarantie, keine weiteren Wetterdaten, keine neue Speicherung oder Aktion. Die vorhandene Archivabfrage stellt die rein lokal berechneten Ergebnisse für Karte und Automationen unter denselben Quellenrechten bereit.

Die Prüfung mit echten späteren Messdaten bleibt offen; deterministische Tests belegen die Auswahl- und Ausgaberegeln, keine erreichte reale Güte.

## Stunden-Erfahrungsband: Folgeschritt zu #29

Die vorhandene Archiv-Leseaktion ergänzt die bereits eingefrorenen UTC-Stunden mit Vorlauf einer beziehungsweise drei Stunden um eigene Erfahrungsbänder. Regelversion 2 für diese Stunden verwendet je Horizont und gleicher lokaler Startzeit einschließlich Fold 60 chronologisch frühere Lerntage und 30 strikt spätere Prüftage. Pro lokalem Tag zählt höchstens ein Fall. Konfiguration, Zeitzone, Quellenidentität, damalige Roh-/Kalibrierungsmethode, rechtzeitige Bewertungsrevisionen, zentrale 80-Prozent-Zielabdeckung und die gemeinsame Prüfung von Abdeckung und Winkler-Score bleiben wie beim Tagesband getrennt. Die bestehenden Archivgrenzen werden nicht vergrößert; fehlende Fälle liefern keine Grenzen. Tages-Regelversion 1 bleibt unverändert.

Die Karte zeigt die backendseitigen Grenzen nur für diese expliziten eingefrorenen zukünftigen Stunden, mit UTC-Grenzen, lokalem Offset, Stichtag, Zentralwert und Prüfkennzahlen. Eine Stunde mit drei Stunden Vorlauf ist keine Dreistunden-Energiesumme. Es entstehen keine neuen Stores, Aktionen, Sensoren oder Wetterabrufe. Gleitende Rest-/60-Minuten-Fenster und beliebige Planungsfenster bleiben ohne passende historische Fenster ausdrücklich nicht verfügbar. Die reale Güteprüfung bleibt offen.

## Tagesaussicht: erste Stufe zu #30

Die bestehende berechtigungsgeprüfte Messdatenaktion liefert optional die aktuelle Tagesaussicht aus einem vollständig belegten Messpräfix seit lokaler Mitternacht, einer sichtbar geschätzten Brücke vom letzten gemeinsam gesicherten Messzeitpunkt bis jetzt und der Prognose ab jetzt bis Tagesende. Alle drei Abschnitte sind disjunkt. Mehrere Quellen benötigen einen gemeinsamen exakten Zählergrenzzeitpunkt; fehlende Abdeckung, unklare Identität, Quellenwechsel oder Korrektur erzeugen keine künstliche vollständige Messung. Es werden keine Zählerdifferenzen anteilig zerlegt. Fehlende beziehungsweise veraltete Prognose verhindert eine vollständige Tagesaussicht, während vorhandene Messwerte und Restprognose weiterhin getrennt lesbar bleiben.

Diese rein lesende erste Stufe verwendet unverändert die wirksame gemeinsame Prognose und benötigt keine Einstellung, Persistenz oder zusätzlichen Abruf. Eine automatische kurzfristige Korrektur wird erst nach einem gesondert vorab festgelegten Versuch mit tatsächlich eingefrorenen Zukunftsständen umgesetzt; die Addition bekannter Messwerte behauptet keine verbesserte Vorhersage. Die Karte zeigt Messung, geschätzte Brücke und Zukunft getrennt. #30 bleibt für den Korrekturversuch und die echte Güteprüfung offen.

## Standortänderung mit erhaltenen Daten zu #23

Der native Reconfigure-Flow erlaubt nachträgliche Standortkorrekturen über dieselben validierten Standortquellen wie das Setup. Ein Entwurf wird nach erfolgreichem Open-Meteo-Test und ausdrücklichem Abschluss gespeichert. Entry-ID, Unique-ID und Dach-/sonstige Optionen bleiben erhalten. Reine Namensänderungen ohne neue Koordinaten oder Zeitzone beginnen keine neue Vergleichsgrundlage.

Ein physischer Standort- oder Zeitzonenwechsel beginnt neue Mess-/Archiv-/Lernsegmente. Mess-Store Version 2 ergänzt je Segment den ursprünglichen Standortkontext, die ursprüngliche Zeitzone und den Beginn. Die Migration des bisherigen Stores erfolgt verlustfrei im noch gültigen alten Standortkontext. Alte Zeitpunkte, Zählerdifferenzen und Korrekturen werden nicht in die neue Zone umgedeutet; vor dem Wechsel datierte HA-Zustände liefern keine neue Zählerbasis. Archiv-Store Version 3 erhält gemischte, individuell validierte Record-Zeitzonen. Neue Erfassung verwendet die aktive Zone; Berichte trennen Konfigurationskennung und Zeitzone. Die üblichen aktuellen Kennzahlen beziehen sich auf die aktuelle Vergleichsgrundlage. Alte Archivstände bleiben vorhanden.

Vor einer physischen Änderung werden bekannte Stores im alten Kontext sicher vorbereitet. Unbekannte oder unlesbare Speicherversionen verhindern die Änderung, um keine noch nicht interpretierbare historische Standortinformation zu verlieren. Aktive Manager werden vor dem eigentlichen Config-Update beendet; dadurch dürfen alte laufende Abrufe keine Daten unter einer neuen Konfigurationskennung archivieren. Danach folgt genau ein Reload. Eine alte Kalibrierungsfreigabe wird nicht auf den neuen Standort übertragen. Config Entries bleiben bei Schema 1.1. Speicher-, Mess-, Rechte- und Aufbewahrungsgrenzen bleiben bestehen.

## Echte AC-Wechselrichtergruppen zu #22

Optional werden reale AC-Wechselrichtergruppen in den Optionen als stabile ID, Name, positive maximale AC-kW und zugeordnete stabile Dach-IDs geführt. Ein Dach gehört höchstens einer Gruppe an; mehrere Dächer dürfen ein gemeinsames Gerät teilen. Nicht zugeordnete Dächer unterliegen weiterhin dem bestehenden Anlagenlimit. Die UI erklärt AC-Gruppen ausdrücklich und deutet weder DC-MPPT-Grenzen noch Netzeinspeiselimits als zusätzliche Wechselrichter um.

Eine zentrale reine Funktion begrenzt zuerst jede Gruppe proportional innerhalb ihrer Dachbeiträge und anschließend genau einmal die resultierende Gesamtleistung nach dem bisherigen Anlagenlimit. Gruppen beeinflussen keine fremden Dachbeiträge. Ohne Gruppen bleibt der bisherige Rechenweg identisch. Der optionale Anlagenkalibrierungsfaktor wird vor beiden Begrenzungsstufen angewendet.

Neue archivierte Kalibrierungsbasen mit Gruppen erhalten einen eigenen Basisvertrag Version 2: je UTC-Intervall unveränderte DC-Leistung je Gruppe und unzugeordnete Leistung, dazu die damals geltenden Gruppen- und Gesamtlimits. Ihre Summe muss zur vorhandenen Gesamt-DC-Leistung passen. Basen ohne Gruppen behalten den bisherigen Vertrag und werden nicht nachträglich ergänzt oder umgedeutet. Der unabhängig versionierte Archiv-Store 3 enthält diese Erweiterung zusammen mit den Standortkontexten; Lern-Store 1 und Config-Entry-Schema 1.1 bleiben erhalten. Nichtleere Gruppen mit Zuordnungen und Limits erweitern den physischen Fingerprint und entziehen inkompatiblen Lernfaktoren die Freigabe; reine Gruppennamen tun dies nicht. Ohne Gruppen ändert sich der bestehende Fingerprint nicht.

Die Leseaktionen, Energy und Karte bleiben auf derselben wirksamen Gesamtzeitreihe. Es gibt keine zusätzlichen Sensoren oder Wetterabrufe. Physische Bandgrenzen berücksichtigen bei vollständig zugeordneten Dächern auch die Summe der Gruppenlimits. Tests sichern getrennte Geräte, mehrere Dächer an einem Gerät, kombiniertes Anlagenlimit, Kalibrierung vor beiden Clippings, verlustfreie Basisübergänge und bitgleiche Ergebnisse ohne Gruppen ab.

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
Lern- und Archivdaten werden bei ihrer Einführung unabhängig versioniert.

Vor einer Änderung den aktuellen `main`-Stand holen und einen eigenen Branch
anlegen. Änderungen klein und testbar halten, alle Qualitätsprüfungen ausführen,
committen, pushen und mit einer aussagekräftigen Pull-Request-Beschreibung
einreichen. Keine unnötigen Abhängigkeiten oder spekulativen Erweiterungen
einführen.

## Prospektiver Beobachtungsversuch zu #30

Die Archivoption „Kurzfristige Korrektur beobachten“ ist standardmäßig aus. Sie verändert keine produktiven Prognosewerte. Nur bei aktiviertem Archiv werden mit neuen Prognoseständen begrenzte Vergleichskandidaten rechtzeitig eingefroren: volle UTC-Stunden mit einer/drei Stunden Vorlauf und der lokale Resttag von 12 Uhr bis Mitternacht. Für den Resttag gilt maximal eine Stunde Datenalter. Alte Stände erhalten keine nachträglichen Kandidaten. Archiv-Store 4 migriert Versionen 1–3 verlustfrei ohne erfundene Belege; Config Entries bleiben 1.1.

Versuchsregel 1 verwendet drei aufeinanderfolgende vollständig bewertete Stunden derselben Anlagen-/Quellenbasis, deren Ende höchstens 90 Minuten zurückliegt und deren Bewertung zum Beobachtungszeitpunkt bekannt war. Mindestens 0,3 kWh Basisenergie, keine Eingabefallbacks oder bekannten Abregelungs-/Wartungstage. Ein Verhältnis außerhalb 0,5–1,5 liefert einen Leerzustand. Innerhalb dieses Bereichs wird der Faktor auf 0,8–1,2 begrenzt und seine Wirkung linear innerhalb sechs Stunden bis null reduziert. Er betrifft nur zukünftige Intervalle des laufenden lokalen Tages. Die eingefrorene DC-Basis wird vor Gruppen- und Gesamtclipping skaliert; der langfristige Anlagenfaktor bleibt unverändert. Ohne Basis entsteht kein Kandidat.

Kandidat, Beobachtungszeit und Inhaltsfingerprints der drei damals bekannten Mess-/Forecastbelege werden im bestehenden Archivdatensatz bewahrt. Änderungen oder fehlende Belege verhindern seine Verwendung im Vergleich. Die vorhandene berechtigungsgeprüfte Archivaktion liefert getrennt nach Vorlauf MAE, Bias, Stichprobe, Abdeckung und Ausschlüsse auf denselben späteren Messintervallen. Mindestens 30 abgeschlossene lokale Tage und mindestens fünf Prozent geringerer MAE bei positivem Basis-MAE sind das vorab festgelegte Prüfziel; große Fehler bleiben enthalten. Die jüngsten 60 Tage werden geprüft. Selbst bei erreichtem Ziel bleibt die Korrektur in dieser Stufe ausschließlich beobachtend. Freigabe produktiver Anwendung benötigt einen gesonderten belegten Auswertungsschritt. Löschung und Aufbewahrung folgen dem bestehenden Archiv; keine zusätzlichen Wetterabrufe, Stores, Entities oder Aktionen.

## Prospektiver Temperaturvergleich zu #17

Eine standardmäßig ausgeschaltete Archivoption vergleicht das unveränderte Rohmodell mit einer Ross-Zelltemperatur-Näherung. Sie verwendet ausschließlich bereits geladene GTI- und Außentemperaturintervalle. Die reine Berechnung ist `T_Zelle = T_Luft + k × GTI`; der kWp×GTI-Ansatz, Verlustfaktor und beide AC-Clippingstufen bleiben erhalten. Referenzparameter aus der offiziellen pvlib-Dokumentation: gut belüftetes Schrägdach 0,02, freistehend 0,0208, gut belüftetes Flachdach 0,026 und schwach belüftetes Schrägdach 0,0342 K m²/W. Das Modell setzt vereinfachend 1 m/s Wind und stationäre beziehungsweise langsam veränderliche Einstrahlung voraus; der tatsächliche Wind am Modul wird nicht behauptet.

Je Dach wird eine Vergleichsannahme bewusst ausgewählt; Standard ist „nicht festgelegt“. Ohne vollständige Dachzuordnung entsteht kein Anlagenvergleich. Keine Pflichtangaben oder vorsorgliche Änderung bestehender Dachmodelle. Die Auswahl gilt nur für die Beobachtung; Sensoren, gemeinsame wirksame Zeitreihe, Kalibrierungsfreigabe und Anwenderverluste bleiben unverändert. Parameteränderungen beginnen durch eine eigene Kennung eine neue Vergleichsgruppe, reine Dachnamenänderungen nicht.

Neue Archivstände speichern ausschließlich rechtzeitig mit derselben Wetterantwort berechnete Alternativenergie mit eigener Modell-/Parameterkennung. Archiv-Store 5 migriert Versionen 1–4 ohne erfundene alte Vergleichsstände. Konfigurationsschema 1.1, Mess- und Lern-Store sowie Schreib-/Aufbewahrungsgrenzen bleiben erhalten. Fehlende Eingaben bleiben Qualitätsmängel und gehen nicht als fehlerfreie Vergleichsfälle ein. Die bestehende Archivaktion berichtet MAE/Bias und Stichprobe auf identischen späteren gültigen Messintervallen, getrennt nach Horizont und Parameterkennung. Ein belegter Nutzen und die Prüfung mit echten Anlagen stehen aus; diese Stufe ändert kein Standardmodell und übernimmt keine bisherige Lernfreigabe in ein anderes Modell. Keine zusätzliche Bibliothek, Wettervariable, HTTP-Abfrage, Entity, Aktion oder Speicherung außerhalb des Archivs.

Referenz: https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.temperature.ross.html

## Experimentelle Minderertragshinweise zu #32

Eine standardmäßig ausgeschaltete Beobachtung bewertet ausschließlich die Gesamtanlage und rechtzeitig eingefrorene Rohmodell-Tagesstände `daily_previous_18`. Eine Auswertung benötigt sieben unmittelbar aufeinanderfolgende abgeschlossene lokale Tage mit vollständigen bestätigten Messungen, brauchbaren Eingaben, gleicher Konfiguration und Quellenidentität sowie mindestens 0,1 kWh Rohprognose je Tag. Bewusst markierte Abregelungs-/Wartungstage unterbrechen diese Folge. Ein einziges vor dem ersten Beobachtungstag bekanntes Tages-Erfahrungsband muss seine bestehende Prüfung mit 60 früheren Trainings- und 30 späteren Prüftagen bestanden haben. Es wird während der sieben Tage nicht nachtrainiert. Mindestens fünf Tage müssen unter dessen physisch begrenzter Untergrenze liegen und zugleich mehr als 20 Prozent sowie 0,1 kWh unter ihrer Rohprognose; die gesamte Folge benötigt ebenfalls mehr als 20 Prozent Minderertrag. Diese erste Regel erkennt nachhaltige Gesamtänderungen, keine Dach- oder Ursachendiagnose.

Ein Hinweis hält seine ursprüngliche Vergleichsgruppe und höchstens 97 Referenzen mit Inhaltsfingerprints im Archiv fest. Spätere Lernfaktoren können ihn nicht verschwinden lassen. Messkorrekturen, fehlende Referenzen oder neue bekannte Ausschlusstage machen die Grundlage sichtbar ungültig; sie bestätigen keine Erholung. Standort-/Quellenwechsel und gezielte Quellenlöschung entfernen abhängige Hinweise. Der Anwender quittiert den Hinweis oder löscht ihn bewusst in den Optionen. Quittieren beendet nur Benachrichtigungen. Solange der Hinweis besteht, pausieren Training und Kandidatenprüfung; die wirksame Kalibrierung kehrt sichtbar zum Faktor 1 zurück. Der zuletzt verwendete Faktor bleibt als Kontext nachvollziehbar. Nach bewusstem Löschen beginnt frühestens mit sieben neu abgeschlossenen Tagen eine weitere Erkennung. Ein automatischer Erholungsnachweis gehört nicht zu dieser Stufe.

Karte und bestehende berechtigungsgeprüfte Archivaktion zeigen Zeitraum, Messung, Rohprognose, Abdeckung, Vergleichsprüfung und konkrete Prüfhilfen. Die Funktion heißt ausdrücklich experimentell. Eine zusätzlich aktivierte lokale HA-Benachrichtigung enthält ausschließlich einen allgemeinen Verweis zur geschützten Karte, keine Erträge, Namen oder Messdaten. Pro zusammengefasstem Hinweis entsteht höchstens eine Benachrichtigung, auch nach regulärem Neustart. Der Versandvermerk folgt den bestehenden Schreibterminen; ein harter Ausfall vor dem Speichern kann die allgemeine Mitteilung wiederholen. Ruhezeit ist 22 bis 08 Uhr in der Anlagenzeitzone. Quittieren/Löschen entfernt die Benachrichtigung. Es gibt keine automatische Geräteaktion, neue öffentliche Aktion, Entity oder Wetterabfrage.

Archiv-Store 6 übernimmt Versionen 1–5 verlustfrei ohne alte Hinweise zu erfinden. Maximal ein Hinweis, seine Referenzen und Bedienzustand sind zusätzlich auf 64 KiB begrenzt und zählen zur bisherigen Archivgrenze; reguläre Schreibungen bleiben auf den bestehenden Fünfminutenterminen. Config Entry 1.1, Mess-Store 2 und Lern-Store 1 bleiben unverändert. Reale Trefferquote, Fehlalarmziel höchstens ein unbegründeter Hinweis je Monat und Feldfreigabe bleiben in #32 offen. Synthetische Tests ersetzen diese Freigabe nicht.

## Optionaler Prognosehorizont aus #21

Die Optionen erlauben zwei bis sieben lokale Tage, Standard zwei. Die gemeinsame UTC-Zeitreihe, Forecast-Abfrage und Solarzeitfensterplanung verwenden diesen Horizont. Die Karte zeigt kompakte datierte Gesamt-/Dachwerte; spätere Tage heißen Tendenz, ihre gemessene Güte bleibt bis zu einem passenden Nachweis nicht verfügbar. Sensorzahl, IDs und Schema 1.1 bleiben erhalten. Energy gibt weiterhin ausschließlich heute/morgen aus. Archivstichtage, Kalibrierung und Erfahrungsbänder behalten ihre bisherigen Vorläufe und Speichergrenzen. Pro Geometrie bleibt es bei einem HTTP-Request; Antwortumfang und anbieterabhängig gezähltes Kontingent werden getrennt ausgewiesen. Diese Ausnahme ersetzt nur die bisherigen Zweitagesgrenzen für Abruf, gemeinsame Zeitreihe und Planung.

## Mehrere unabhängige Anlagen aus #24

Je logischer Anlage wird ein eigener Config Entry unterstützt. Neue Einträge erhalten eine stabile zufällige Unique-ID; bestehende Domain-Unique-IDs, Entry-/Entity-/Dach-IDs und Stores bleiben unverändert. Schema 1.1 bleibt erhalten. Weitere Einträge benötigen einen unterscheidbaren Namen, an ähnlichen Koordinaten zusätzlich eine ausdrückliche Bestätigung der unabhängigen Anlage. Gerundete Koordinaten sind nur Duplikatheuristik, keine Identität. Gleichörtliche laufende Setups werden gesperrt; der Abschluss prüft neue Nachbarn erneut. Standortänderungen verwerfen alte Bestätigungen. Coordinator, Sensoren, Limits, Mess-/Archiv-/Lernzustände und Karten bleiben je Entry getrennt. HA-weit geteilt sind ausschließlich die flüchtige Anbieterpause und HTTP-Slots. Entfernen einer Anlage berührt andere Anlagen nicht. Diese Ausnahme ersetzt die frühere Beschränkung auf genau einen Entry.

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
`shading.py` bleibt rein und wird vor Temperatur, Verlusten, Kalibrierung sowie
Gruppen-/Gesamtclipping in `calculations.py` eingebunden.

Effektive Profile samt Regelversion erweitern ausschließlich bei Aktivierung die
physische Konfigurationskennung und entziehen inkompatiblen Lernfaktoren ihre
Freigabe. Alte archivierte DC-Basen bleiben unverändert. Speicherverträge und
Config-Entry-Schema 1.1 bleiben bestehen. Forecast-Antwort und Karte kennzeichnen
die experimentelle Anwendung. Ein synthetischer Winter-/Sommervergleich belegt
nur den zeitabhängigen Mechanismus; der reale Zusatznutzen gegenüber konstanten
Verlusten oder Kalibrierung bleibt offen. Nahverschattung, elektrische Stringeffekte,
diffuse Himmelsverdeckung und 3D-Modelle sind ausdrücklich nicht enthalten.

## Geführte Messquellenauswahl für vorhandene Geräte

Auf ausdrücklichen Anwenderwunsch ergänzt der Config-/Options-Flow eine einfache Auswahl bereits eingerichteter Geräte. Der erste Adapter erkennt KOSTAL KSEM aus der Integration `ksem` anhand der Registry und des AC-Summenregisters 40974. Bestätigung: keine Batterie an den erfassten Wechselrichtern, vollständige AC-PV-Messgrenze und keine doppelte Energiezuordnung. Weitere Adapter beschreiben Erkennung und Messart zentral; Netz-/DC-/Verbrauchssensoren werden nicht als PV-Ertrag vorgeschlagen. Die manuelle Zuordnung bleibt erhalten.

Für bestätigte Leistungsmessungen erstellt der Assistent erst beim Speichern einen nativen HA-Integral-Helfer oder verwendet einen passenden vorhandenen. kWh, Trapezregel und ereignisbasierte Integration ohne Fortschreibung stehengebliebener Messwerte sind festgelegt. Ein solcher Helfer bleibt als eigenständiger, auch anderweitig nutzbarer HA-Helfer beim Entfernen der PV-Zuordnung erhalten; dies wird vorab erklärt. Abbruch vor dem Speichern erzeugt keinen Helfer, Fehler beim Anlegen rollen neu angelegte Helfer zurück. Abgeleitete Energie bleibt gekennzeichnet. Quellenidentität, Leserechte und Datenlücken berücksichtigen auch den zugrunde liegenden Leistungssensor. Keine eigenen Geräteabrufe, keine eigene numerische Integration, keine neuen Prognosesensoren oder öffentlichen Aktionen. Config-Entry-Schema bleibt 1.1. Die Ausnahme erweitert ausschließlich die Einrichtung nativer Messhelfer und ersetzt die entgegenstehende Beschränkung auf bereits vorhandene Helfer. Offline-Tests ersetzen keine reale KSEM-Erprobung.

## Planungsentscheidung vom 09.09.2026

Auf ausdrücklichen Anwenderwunsch werden ausstehende Benutzerrückmeldungen, moderierte Nutzerabnahmen und reale Feld-/Güteerprobungen nicht mehr als blockierende Issues oder Voraussetzungen für weitere technisch umsetzbare Arbeiten geführt. #17, #26, #29, #30, #31 und #32 werden deshalb aus der offenen Planung genommen. Bereits geschlossene #18, #27 und #28 werden dafür nicht wieder geöffnet.

Nicht durchgeführte Erprobungen gelten weiterhin nicht als bestanden. Bestehende Laufzeitprüfungen, Mindeststichproben, experimentelle Kennzeichnungen und unveränderte Standardmodelle bleiben erhalten. Noch nicht gelieferte weitergehende Varianten werden nicht als umgesetzt gewertet; sie können bei einem konkreten Auftrag separat geplant werden. Die technischen Folgearbeiten benötigen keine vorangehende Rückmeldung einer Testgruppe.

## Korrektur der Messauswertung aus #105 und #109

Auf ausdrücklichen Anwenderauftrag werden abgeleitete Energiedifferenzen über bekannte Berichts-, Neustart- oder Ausgangssensorlücken nicht als beobachtete Energie übernommen. Ein Integral-Helfer kann den unbekannten Leistungsverlauf nicht durch seinen fortlaufenden Zählerstand belegen. Echte fortlaufende Energiezähler behalten die bisherige Trennung zwischen belegter Gesamtmenge und unbekannter Stundenverteilung. Bereits gespeicherte irrtümlich gültige Archivbewertungen mit nachgewiesenen Lücken einer abgeleiteten Quelle werden als Messkorrektur revisioniert und verlieren ihre Lernfreigabe; eingefrorene Prognosen und Rohbelege bleiben erhalten.

Für die Fensterauswahl dürfen gültige, lückenlos beobachtete Zählerdifferenzen mit exakt null kWh innerhalb derselben Quellen-/Standortidentität an UTC-Grenzen zugeschnitten werden. Die Energie jedes Teilintervalls ist exakt null; dies ist keine proportionale Schätzung. Positive Zählerdifferenzen, Lücken, Resets, Korrekturen und Quellenwechsel werden weiterhin nicht auf Teilfenster verteilt. So können gewöhnliche versetzte Meldezeitpunkte bei beobachteten nächtlichen Nullplateaus vollständige Tageswerte liefern. Die zentrale Lesesicht gilt gleichermaßen für Gesamtmessung, Karte, Tagesaussicht, Archiv und Lernen; Rohmessungen bleiben unverändert gespeichert.

Ein täglicher Reset allein belegt keine endgültige Vortagsmenge. Solche Quellen bleiben für belegte Messabschnitte nutzbar; die UI erklärt bei Auswahl und Kalibrierung ihre Grenze und verweist für vollständige Tagesberichte auf einen fortlaufenden AC-Ertragszähler beziehungsweise den KSEM-Assistenten. Es werden keine fehlenden Abschlusswerte erfunden. Keine zusätzlichen Sensoren, Aktionen, Wetter-/Geräteabrufe oder Speicherfelder; Config-Entry-Schema 1.1 bleibt bestehen.
## Frei gewählte Prognosefenster aus #130

Die vorhandene Leseaktion `get_forecast` ergänzt optional `window` mit eindeutigen Start-/Endzeitpunkten und optional 5/15/30/60 Minuten Raster. Der Start verankert das exakt teilbare Raster mit höchstens 336 Schritten innerhalb des konfigurierten Horizonts. Version 1 liefert ausschließlich die Gesamtanlage: UTC-Grenzen, Abrufzeit, Abdeckung, Qualitätsmerkmale, Energie in kWh und mittlere AC-kW. Exakte UTC-Überlappungen verwenden konstante Intervallmittel; Rasterenergien erhalten die direkte Fenstersumme. Nullertrag bei vollständiger Abdeckung ist verfügbar. Fehlende Abdeckung, Bereichsüberschreitung, Eingabefallbacks im Fenster oder fehlgeschlagene/fehlende/zukünftige/über 60 Minuten alte Abrufe liefern begründete Leerzustände ohne numerische Rasterwerte. Nicht angefragte Datenlücken blockieren vollständige Teilfenster nicht. Planung und Fenster dürfen gemeinsam gelesen werden; Dachauswahl betrifft weiterhin nur die Kartenansicht. Rechteprüfung, Rohantwort, Schema 1.1 und EMHASS-Kompatibilität bleiben erhalten. Keine neuen Aktionen, Optionen, Sensoren, Speicherung, Wetterdaten oder Geräteaktionen.
