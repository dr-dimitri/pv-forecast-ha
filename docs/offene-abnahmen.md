# Lieferumfang und freiwillige Erprobung

Der technische Lieferumfang ersetzt keine Prüfung mit echten PV-Anlagen.
Die Roadmap in [Issue #25](https://github.com/dr-dimitri/pv-forecast-ha/issues/25)
und die konkretisierten Verträge in
[Issue #1](https://github.com/dr-dimitri/pv-forecast-ha/issues/1) bleiben maßgeblich.

## Keine blockierende Nutzerabnahme

Seit der Anwenderentscheidung vom 09.09.2026 sind Nutzerbefragungen und reale
Feld-/Gütetests freiwillige Nachweise. Sie blockieren weder weitere Issues noch
die technische Weiterentwicklung. Die Hinweise unten dokumentieren weiterhin,
welche Aussagen noch nicht durch reale Erprobung belegt sind. Sie sind keine
verpflichtende Aufgabenliste. Automatische Qualitätsprüfungen
und experimentelle Kennzeichnungen der Funktionen bleiben unverändert.

## Technisch umgesetzt

| Issues | Umfang | Nachweis im Repository |
| --- | --- | --- |
| #12, #19 | Zentrale Sprachpflege und verlustfrei editierbare Kompasswinkel | Sprachabgleich in CI, echte HA-Flowtests und Erhalt genauer gespeicherter Winkel |
| #14 | Datensparsame native Diagnosedaten | Positivliste, Strukturprüfung und nativer HA-Download-Endpunkt |
| #10 | Importbefund ohne erforderlichen Umbau | [Unabhängige Modulimporte](issue-pruefung.md) |
| #22 | Reale AC-Wechselrichtergruppen mit nachgelagerter Gesamtgrenze | Rechenbeispiele, bitgleicher alter Pfad, UI |
| #23 | Standortkorrektur bei erhaltenen IDs und alten Datenkontexten | Beide Standortquellen, Fehlerversuche, Migrationen, ursprüngliche Tagesgrenzen und neue Segmente |
| #31 | Zusammenhängendes Solarzeitfenster in Leseaktion und Karte | UTC/DST, Teilstunden, Hysterese, laufende Fenster, nativer HA-Script-Blueprint und EMHASS-Datenadapter |
| #21 | Optional zwei bis sieben Prognosetage | UTC-Zeiträume, unveränderte Abrufzahl, Tageswerte und mehrtägige Planung; spätere Tage als Tendenz |
| #24 | Mehrere unabhängige logische Anlagen | Eigene IDs, Sensoren, Messwerte und Stores; explizite Bestätigung am selben Ort; getrenntes Entladen |
| #20 | Experimentelles Horizontprofil je Dach | Sonnengeometrie, diffuse Reste, Winter/Sommer, Clipping und unveränderter Standard |
| #96 | Freiwilliges PV-Dashboard direkt im Config-/Options-Flow | Native Panelregistrierung, mehrere Anlagen, Entladen, Konflikte; 360 px in Hell/Dunkel, Tastatur und Ende der Kartenabfragen |
| #101 | Änderungen am Dashboard über HA-Reparaturen übernehmen | Inhaltsfingerprint, Erststand, wiederholte Änderungen, native Reparatur, Konflikte und interner Neulade-Link |
| #30 | Aktuelle Tagesaussicht | Keine Doppelzählung, Messlücken und Quellenrechte; produktiv keine kurzfristige Korrektur |

Die neuen Darstellungen der Karte wurden bei 360 px in Hell/Dunkel und mit
Tastatur geprüft. Die Screenshots zeigen reproduzierbare Testdaten. Sie sind
kein Nachweis tatsächlicher Erträge oder einer erreichten Prognosegüte.

## Freiwillige Erprobung und weiterhin fehlende Gütenachweise

- **#26 und #28:** Die vorgesehenen moderierten Tests mit fünf echten
  PV-Anwendern bleiben offen. Zielwerte sind mindestens vier erfolgreiche
  Messquellenzuordnungen ohne Hilfe und vier Personen, die Restenergie und
  Messung/Prognose innerhalb von zehn Sekunden richtig erkennen.
- **#31:** Freiwillige Testanwender können ein Verbraucherfenster finden und eine
  freiwillige Automation einrichten. Der EMHASS-Adapter ist gegen den
  dokumentierten Datenvertrag offline geprüft; ein vollständiger realer
  Optimierungslauf mit Last, Tarifen und gegebenenfalls Speicher steht aus.
P3 wurde in der freigegebenen Reihenfolge **#21, #24, #20** technisch umgesetzt.
Reale Güte und praktischer Nutzen späterer Prognosetage sowie des experimentellen
[Horizontprofils](horizontprofil.md) bleiben gesondert zu prüfen. Ein synthetischer
Mechanismusvergleich ersetzt den späteren Vergleich mit Messdaten nicht.
Die nativen HA-Frontendfehler **#52 und #53** liegen außerhalb dieses
Integrationscodes. Ihre dokumentierten Grenzen und die eindeutige interne
UTC-Zeitreihe bleiben bestehen.

Die früher dafür offengehaltenen Issues #17, #26, #29, #30, #31 und #32 sind
auf Anwenderwunsch als nicht weiter geplante Aufgaben geschlossen. #18, #27
und #28 bleiben geschlossen. Weitere technisch umsetzbare Arbeiten benötigen
keine vorherige Rückmeldung einer Testgruppe. Nicht gelieferte weitergehende
Varianten können bei konkretem Bedarf separat beauftragt werden.

## Bekannte native Frontendgrenzen

Die Gesamtprognose darf im Energy Dashboard genau einem Solarzähler zugeordnet
werden. Mehrfachdarstellung (#52) und lokale Rundung bei DST-/Teilstundenanzeige
(#53) betreffen das native HA-Frontend. Die eigene Zeitreihe verwendet eindeutige
UTC-Grenzen; Energy- und Kartentests prüfen Zeitumstellungen und Teilstunden.
