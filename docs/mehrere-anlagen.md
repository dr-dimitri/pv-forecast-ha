# Mehrere unabhängige PV-Anlagen

Über **Einstellungen → Geräte & Dienste → Integration hinzufügen** kann
`PV Ertragsprognose` mehrfach eingerichtet werden. Jede logische Anlage hat
separate Koordinaten, Zeitzone, Dächer und AC-Limits. Mehrere Dächer eines
Hauses mit gemeinsamer Messgrenze gehören weiterhin in dieselbe Anlage.

Weitere Anlagen benötigen einen Namen, der sich von vorhandenen Anlagen
unterscheidet. Bei ähnlichen Koordinaten erscheint zusätzlich die Bestätigung,
dass es sich tatsächlich um eine unabhängige Anlage mit eigenen Dachflächen,
AC-Grenzen und Messquellen handelt. Ohne Bestätigung wird die Einrichtung als
bereits vorhanden abgebrochen. Hausanlage und ein unabhängig gemessenes
Balkonkraftwerk am selben Ort können so ausdrücklich getrennt bleiben.

Vier Dezimalstellen der Koordinaten dienen nur als Duplikathinweis. Sie sind
kein Nachweis einer gemeinsamen physischen Anlage. Neue Einträge bekommen
eine zufällig erzeugte dauerhafte Unique-ID. Bestehende Einträge behalten
auch die alte Domain-Unique-ID, ihre Entry-ID, Entity-IDs und gespeicherten
Daten. Es gibt keine vorsorgliche Migration; Schema 1.1 bleibt bestehen.
Reconfigure bewahrt die Anlagenidentität und den bestehenden Titel, auch bei
einem Standortwechsel. Die historischen Standortsegmente bleiben erhalten.

Eine gleichörtliche laufende Einrichtung sperrt weitere Einrichtungsversuche
bis zu ihrem Abschluss beziehungsweise Abbruch. Vor dem endgültigen Anlegen
werden inzwischen angelegte Nachbarn und vergebene Namen erneut geprüft.
Eine alte Bestätigung gilt nicht für einen neu gewählten Standort.

## Getrennte Nutzung und Speicherung

Jede Karte wählt ihre Anlage im visuellen Editor. Leseaktionen benötigen deren
`config_entry_id`; die vorhandenen Anlagen- und Quellenrechte gelten unverändert.
Dach-IDs dürfen sich zwischen Anlagen wiederholen: Entity- und Store-Zuordnung
verwenden zusätzlich die eindeutige Entry-ID. Daten werden niemals automatisch
summiert oder zwischen Standorten beziehungsweise Messgrenzen kopiert.

Auch beim Entfernen einer Anlage bleiben Sensoren, Messlistener, Archiv,
Lernzustand und Aktionen der anderen erhalten. Eine echte Erzeugungsquelle
soll nur der Anlage zugeordnet werden, deren AC-Grenze sie misst. Unabhängige
Zähler sind nicht automatisch durch ihre Namen oder Einheiten bewiesen.

Die gemeinsame Open-Meteo-Anbieterpause und maximal vier HTTP-Slots bleiben
HA-weit geteilt. Jede Anlage benötigt ihre eigenen Geometrieabfragen; mehrere
Anlagen vervielfachen deshalb den Bedarf gegenüber einer einzigen Anlage.
Die bereits dokumentierten Grenzen der nativen Energy-Grafik bleiben bestehen.

## Verifikation

Offline-Tests richten unterschiedliche Standorte und bestätigte getrennte
Anlagen mit gleichen Koordinaten ein. Sie prüfen gleichzeitige/versehentliche
Doppeleinrichtung, neue Nachbarn oder Namen während eines offenen Flows,
Reconfigure und die unveränderte alte Domain-ID.

Ein Laufzeittest betreibt zwei echte Config Entries mit getrennten Zählern und
AC-Limits, prüft Sensoren und Messdifferenzen und entfernt anschließend eine
Anlage. Die andere bleibt mit unverändertem Archiv, eigenen Listenern und
funktionierenden Forecast-/Energy-Abfragen erhalten. Das ersetzt keine reale
Anlagenerprobung.

Grundlage: [Home Assistant Config Flow und stabile Unique-IDs](https://developers.home-assistant.io/docs/core/integration/config_flow/).
