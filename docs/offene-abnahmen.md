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
| #31 | Zusammenhängendes Solarzeitfenster in Leseaktion und Karte | UTC/DST, Teilstunden, Hysterese, laufende Fenster, nativer HA-Script-Blueprint und EMHASS-Datenadapter |
| #29 | Tages-Erfahrungsband für eingefrorene Stände | 60 frühere Trainings- und 30 spätere Prüffälle, gemeinsame Prüfung von Abdeckung und Breite |
| #30 | Getrennte Tagesaussicht aus Messpräfix, geschätzter Brücke und Zukunft | Keine Doppelzählung, Messlücken und Quellenrechte; keine kurzfristige Korrektur |

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
- **#30:** Ein gesondert vorab festgelegter Zukunftsvergleich für eine
  kurzfristige Korrektur ist noch umzusetzen und mit mindestens 30 gültigen
  Testtagen zu prüfen. Das Qualitätsziel von fünf Prozent geringerem MAE ist
  kein behauptetes Resultat. Die jetzige Tagesaussicht wendet keinen Faktor an.
- **#17:** Ein anderes Temperaturmodell benötigt zuerst einen dokumentierten
  Vergleich mit Parameterherkunft auf gleichen späteren Messintervallen.
  Diese Vergleichsvariante und ihr Gütenachweis stehen aus. Die bestehende
  Außentemperaturnäherung bleibt deshalb unverändert; Montage oder Wind am
  Modul werden nicht aus unbelegten Standardwerten als bekannt ausgegeben.
- **#32:** Hinweise auf wiederkehrende Mindererträge benötigen die geforderte
  belastbare Vergleichsbasis und einen Feldtest mit Treffer- und Fehlalarmrate.
  Die Funktion ist noch nicht implementiert. Ein Vergleich mit der
  Wetterprognose allein begründet keine Defektmeldung.

Die P3-Erweiterungen **#20, #21 und #24** bleiben gemäß Roadmap bis zu einem
belegten Bedarf zurückgestellt: Verschattung, weitere Prognosetage und mehrere
Standorte erweitern den aktuellen Produktumfang. Die nativen HA-Frontendfehler
**#52 und #53** liegen außerhalb dieses Integrationscodes. Ihre dokumentierten
Grenzen und die eindeutige interne UTC-Zeitreihe bleiben bestehen.

Die genannten Issues und die Roadmap bleiben offen, soweit ihre menschlichen
Abnahmen oder beschriebenen Folgeschritte noch fehlen.
