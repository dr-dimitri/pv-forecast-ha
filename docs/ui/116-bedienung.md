# Stabile Bedienung und Barrierearmut (#116)

Reguläre Aktualisierungen gleichen die vorhandenen DOM-Knoten ab. Eingabefelder,
Cursor und native Auswahllisten bleiben verbunden. Eine fokussierte Auswahl
bekommt neue Optionen erst nach dem Verlassen; die übrigen Werte aktualisieren
sich weiter. Auch ein Update unmittelbar vor dem verzögerten `toggle`-Ereignis
schließt keine gerade geöffneten Details. Bestehende Details und lokale
Tabellen-, Panel- und Dokument-Scrollpositionen bleiben erhalten.

Bei entfernter Dachansicht führt der Fokus zur angebotenen Rückkehr zur
Gesamtanlage, anschließend zur Flächenauswahl. Intervallwechsel und
Planungs-/Berichtszustände verwenden eine beständige, gezielte Live-Region.
Unveränderte Zustände und unveränderte Intervallauswahl werden nicht wiederholt
angekündigt. Labels, Überschriften und Gruppen bleiben im Accessibility-Baum
verfügbar; der gesamte Karteninhalt ist keine Live-Region.

Die primären Bedienelemente haben mindestens 44 × 44 CSS-px. Schriftgrößen der
HTML-Inhalte verwenden relative Einheiten. Bei schmaler Ansicht und vergrößertem
Text ordnen sich Kennzahlen untereinander an. Die Tabelle wächst mit dem Text
in einem eigenen beschrifteten und per Tastatur bedienbaren Scrollbereich.
Gewählte Planungszeiten stehen zusätzlich vollständig umbrechbar unter den
nativen Auswahlfeldern. SVG-Diagrammkoordinaten bleiben in ihrer Skalierung;
die wachsende Tabelle bietet den gleichwertigen Zugang zu allen Werten.
Reduzierte Bewegung wird berücksichtigt. Linienmuster, Rechtecke und Texte
unterscheiden die Datenreihen weiterhin zusätzlich zur Farbe.

## Reproduzierbare Browserregression

`node scripts/check_frontend_browser.cjs --prefix ui-116` verwendet dieselben
installierten Playwright-/Chromium-Pfade wie die bisherigen Browserprüfungen.
Die Ausgabe enthält `results.json`, `accessibility.json` und Screenshots.
Sämtliche Messwerte sind synthetisch.

Geprüft wurden die 20 bisherigen Browserfälle (Hell/Dunkel/eigenes Theme,
Mobilansicht, Panel, Touch, DST und Teilstunden) sowie:

- Eingabe `123`, zwei Pfeiltasten nach links, Datenupdate, `9`: Ergebnis `1923`.
  Das fokussierte Eingabeelement bleibt identisch.
- Geöffnete native Auswahl mit gleichzeitig neuer Zeitoption: keine Änderung
  ihrer Knoten bis zum Verlassen.
- Details direkt vor ihrem verzögerten Ereignis, offene Details im normalen
  Update und horizontal gescrollte Tabelle bleiben erhalten.
- Intervallwechsel mit gezielter Ansage; unveränderte Auswahl bleibt still.
- 320 CSS-px mit 200 Prozent Text (mindestens 28 px im HTML), langen Namen und
  großen Werten, ohne horizontales Seitenscrollen; Touchziele nachgemessen.
- Entfernte Dachauswahl und Rückkehr mit sinnvollem Fokus.
- Zwei sichtbare Karten teilen drei Leseaufrufe. Ausblenden beider Karten
  entfernt alle Abnehmer und den Timer; anschließendes Entfernen bleibt sauber.

61 Frontendtests, 1.175 Python-Tests, Ruff, Black und Übersetzungsabgleich bestehen.

## Direkter Smoke-Test und Grenzen

Am 10.09.2026 wurde zusätzlich außerhalb des Regressionsskripts die lokale
360-px-Karte im In-app Browser direkt über UI-Tastaturereignisse bedient:
„Intervalle erkunden“, Pfeil rechts, Escape, Tabfolge zur Planung, Enter,
Laufdauer 90 und Tab zum frühesten Start. Auswahl, Schließen, Fokusfolge und
Feldnamen wurden jeweils im Accessibility-Baum geprüft. Die Intervallansage
enthielt Zeitpunkt mit Offset sowie getrennt Prognose, Archiv und Messung.
Dies ist ein durch das Assistenzwerkzeug ausgeführter Smoke-Test, kein
menschlicher Usability-Test.

Ein **menschlicher Screenreader-Hörtest wurde nicht durchgeführt**. Für dessen
getrennte Prüfung ist folgender kurzer Ablauf dokumentiert: mit VoiceOver/Safari
oder NVDA/Firefox Überschriften und Formularfelder anspringen, Intervall per
Pfeiltaste wechseln, eine reguläre Aktualisierung abwarten, Details schließen
und einen Fehler mit anschließender Erholung prüfen. Erwartet werden sinnvolle
Namen, eine Ansage je Zustandswechsel, kein erneutes Vorlesen unveränderter
Hinweise und kein Fokusverlust. Ergebnis, Browser und Screenreaderversion sind
bei Durchführung nachzutragen. Die technische Prüfung ersetzt diesen Hörtest
nicht und behauptet keine vollständige WCAG-Konformität.

Abschließendes eigenes Review: Fokus-/Cursorerhalt, native Auswahl,
`toggle`-Grenzfall, Rechteentzug, Detailauswahl und Listener-Lebenszyklus geprüft.
Der gefundene `toggle`-Grenzfall wurde vor dem PR korrigiert und im Browser
abgesichert. Keine offenen blockierenden Codebefunde.
Referenz: [WCAG 2.2](https://www.w3.org/TR/WCAG22/); 44 px ist das Projektziel.

![320 px und doppelter Text, hell](../images/ui-116-320-light-text200.png)
![320 px und doppelter Text, dunkel](../images/ui-116-320-dark-text200.png)
