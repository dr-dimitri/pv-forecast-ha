# Lieferumfang und offene Abnahmen

Der technische Lieferumfang ersetzt keine Prüfung mit echten PV-Anlagen.
Die Roadmap in [Issue #25](https://github.com/dr-dimitri/pv-forecast-ha/issues/25)
und die konkretisierten Verträge in
[Issue #1](https://github.com/dr-dimitri/pv-forecast-ha/issues/1) bleiben maßgeblich.

## Technisch umgesetzt

| Issues | Umfang | Nachweis im Repository |
| --- | --- | --- |
| #12, #19 | Zentrale Sprachpflege und verlustfrei editierbare Kompasswinkel | Sprachabgleich in CI, echte HA-Flowtests und Erhalt genauer gespeicherter Winkel |
| #14 | Datensparsame native Diagnosedaten | Positivliste, Strukturprüfung und nativer HA-Download-Endpunkt |
| #10 | Importbefund ohne erforderlichen Umbau | [Unabhängige Modulimporte](issue-pruefung.md) |
| #22 | Reale AC-Wechselrichtergruppen mit nachgelagerter Gesamtgrenze | Rechenbeispiele, bitgleicher alter Pfad, UI, unveränderliche Kalibrierungsbasis |
| #23 | Standortkorrektur bei erhaltenen IDs und alten Datenkontexten | Beide Standortquellen, Fehlerversuche, Migrationen, ursprüngliche Tagesgrenzen und neue Segmente |
| #32 | Experimentelle Hinweise für wiederkehrenden Gesamtminderertrag | Geprüfte feste Tagesbasis, sieben gültige Tage, Lernstopp, Quittierung und ruhige Opt-in-Mitteilung |
| #17 | Ross-Zelltemperatur als reine Beobachtungsvariante | Explizite Montageannahme je Dach, Referenzgleichung, gleichzeitige Rohmodellpaare und unveränderter Standard |
| #31 | Zusammenhängendes Solarzeitfenster in Leseaktion und Karte | UTC/DST, Teilstunden, Hysterese, laufende Fenster, nativer HA-Script-Blueprint und EMHASS-Datenadapter |
| #29 | Tages- und Stunden-Erfahrungsband für eingefrorene Stände | Je Horizont 60 frühere Trainingstage und 30 spätere Prüftage, gemeinsame Prüfung von Abdeckung und Breite; Stunden nach lokaler Startzeit getrennt |
| #21 | Optional zwei bis sieben Prognosetage | UTC-Zeiträume, unveränderte Abrufzahl, Tageswerte und mehrtägige Planung; spätere Tage als Tendenz |
| #24 | Mehrere unabhängige logische Anlagen | Eigene IDs, Sensoren, Messwerte und Stores; explizite Bestätigung am selben Ort; getrenntes Entladen |
| #20 | Experimentelles Horizontprofil je Dach | Sonnengeometrie, diffuse Reste, Winter/Sommer, Clipping und unveränderter Standard |
| #96 | Freiwilliges PV-Dashboard direkt im Config-/Options-Flow | Native Panelregistrierung, mehrere Anlagen, Entladen, Konflikte; 360 px in Hell/Dunkel, Tastatur und Ende der Kartenabfragen |
| #101 | Änderungen am Dashboard über HA-Reparaturen übernehmen | Inhaltsfingerprint, Erststand, wiederholte Änderungen, native Reparatur, Konflikte und interner Neulade-Link |
| #30 | Tagesaussicht und optional eingefrorene Zukunftskandidaten | Keine Doppelzählung, Messlücken und Quellenrechte; produktiv keine kurzfristige Korrektur |

Die neuen Darstellungen der Karte wurden bei 360 px in Hell/Dunkel und mit
Tastatur geprüft. Die Screenshots zeigen reproduzierbare Testdaten. Sie sind
kein Nachweis tatsächlicher Erträge oder einer erreichten Prognosegüte.

## Vor einer vollständigen Abnahme noch erforderlich

- **#26 und #28:** Die vorgesehenen moderierten Tests mit fünf echten
  PV-Anwendern bleiben offen. Zielwerte sind mindestens vier erfolgreiche
  Messquellenzuordnungen ohne Hilfe und vier Personen, die Restenergie und
  Messung/Prognose innerhalb von zehn Sekunden richtig erkennen.
- **#31:** Reale Testanwender müssen ein Verbraucherfenster finden und eine
  freiwillige Automation einrichten. Der EMHASS-Adapter ist gegen den
  dokumentierten Datenvertrag offline geprüft; ein vollständiger realer
  Optimierungslauf mit Last, Tarifen und gegebenenfalls Speicher steht aus.
- **#29:** Reale zeitlich spätere Daten müssen Abdeckung und Breite bestätigen.
  Für gleitende Restfenster und beliebige Laufdauern fehlen passende rechtzeitig
  archivierte historische Fenster. Diese Bandbreiten bleiben ausdrücklich
  nicht verfügbar; Tagesgrenzen werden nicht auf Stunden übertragen.
- **#30:** Der abschaltbare [Beobachtungsversuch](kurzfristiger-vergleich.md) friert
  Zukunftskandidaten rechtzeitig ein. Sein Nutzen ist noch mit mindestens 30
  gültigen späteren Testtagen zu prüfen. Das Qualitätsziel von fünf Prozent geringerem MAE ist
  kein behauptetes Resultat. Die jetzige Tagesaussicht wendet keinen Faktor an.
- **#17:** Ein anderes Temperaturmodell benötigt zuerst einen dokumentierten
  Vergleich mit Parameterherkunft auf gleichen späteren Messintervallen.
  Der [separate Ross-Vergleich](temperaturvergleich.md) friert alternative
  Rohmodellstände rechtzeitig ein. Sein realer Gütenachweis steht aus. Die bestehende
  Außentemperaturnäherung bleibt deshalb unverändert; Montage oder Wind am
  Modul werden nicht aus unbelegten Standardwerten als bekannt ausgegeben.
- **#32:** Hinweise auf wiederkehrende Mindererträge benötigen die geforderte
  belastbare Vergleichsbasis und einen Feldtest mit Treffer- und Fehlalarmrate.
  Die [experimentelle Beobachtung](minderertragshinweise.md) ist technisch
  umgesetzt und hinter einer historischen Qualitätsprüfung gesperrt. Ein
  Vergleich mit der Wetterprognose allein begründet keine Defektmeldung.

P3 wurde in der freigegebenen Reihenfolge **#21, #24, #20** technisch umgesetzt.
Reale Güte und praktischer Nutzen späterer Prognosetage sowie des experimentellen
[Horizontprofils](horizontprofil.md) bleiben gesondert zu prüfen. Ein synthetischer
Mechanismusvergleich ersetzt den späteren Vergleich mit Messdaten nicht.
Die nativen HA-Frontendfehler **#52 und #53** liegen außerhalb dieses
Integrationscodes. Ihre dokumentierten Grenzen und die eindeutige interne
UTC-Zeitreihe bleiben bestehen.

Die genannten Issues und die Roadmap bleiben offen, soweit ihre menschlichen
Abnahmen oder beschriebenen Folgeschritte noch fehlen.
