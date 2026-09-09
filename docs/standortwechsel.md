# Standort einer bestehenden Anlage korrigieren

Unter **Einstellungen → Geräte & Dienste → PV-Ertragsprognose → Neu konfigurieren**
kannst du den aktuellen Home-Assistant-Standort übernehmen oder eine Anschrift
eingeben. Die Adresse wird einmalig über Nominatim aufgelöst. Bei Adresseingabe
wird die IANA-Zeitzone anhand der gefundenen Koordinaten einmalig ermittelt.
Ein erfolgreicher Open-Meteo-Test mit den vorhandenen Dachflächen ist vor dem
Speichern erforderlich. Ohne Dachfläche muss zunächst eine hinzugefügt werden.

Der Abschlussdialog zeigt Standort, Koordinaten und Anlagenzeitzone. Erst
**Geprüften Standort speichern** verändert den Eintrag. Ein Verbindungsfehler
oder Abbruch bewahrt den bisherigen Standort. Dachflächen, Leistungsgrenzen,
optionale Mess-/Archiveinstellungen, Config-Entry-ID und Entity-IDs bleiben
erhalten. Home Assistant lädt die Anlage einmal neu und berechnet die Prognose
für den bestätigten Standort. Eine Löschung oder Neuerstellung ist unnötig;
auch eine Löschung würde nicht zwingend sofort alle Recorder-Zeilen entfernen.

## Standortgrenzen für Messung und Auswertung

Andere Koordinaten oder eine andere gespeicherte Anlagenzeitzone beginnen eine
neue physische Vergleichsgrundlage. Eine reine Änderung des Standortnamens oder
der Adressbeschriftung bei identischen Koordinaten und identischer Zone tut dies
nicht. Gespeicherte Zeitzonen werden beim normalen Start weiterhin nicht neu
aufgelöst oder aus Home Assistant übernommen.

Der Messspeicher verwendet Version 2. Zu jedem Messsegment gehören ein
Standortfingerprint, seine ursprüngliche Zeitzone und gegebenenfalls der Beginn
des neuen Standortsegments. Die verlustfreie Übernahme aus Version 1 ergänzt
diese Metadaten im bisherigen Standortkontext; Messpunkte, Zählerdifferenzen,
Quellen-IDs und Tageskorrekturen bleiben erhalten. Beim Wechsel wird die alte
Zählerbasis verworfen. Ein bereits vor dem Wechsel gemeldeter HA-Zustand wird
nicht als neue Basis übernommen; erst neue Meldungen belegen Energie am neuen
Standort. Es gibt keine Zählerdifferenz über die Standortgrenze.

Historische UTC-Abfragen liefern weiterhin alte Messwerte mit deren
`segment_contexts` und ursprünglicher Tageszeitzone. Die explizit angefragten
Kartenintervalle verwenden ausschließlich Messungen des aktuellen
Standortsegments. Messlücken am Umzugstag bleiben erkennbar; die Speicherung
füllt sie weder mit früherer Energie noch mit erfundenen Nullwerten auf.

Die öffentliche Messabfrage ergänzt `current_location_total_energy` für das
angefragte UTC-Fenster. Die Karte bevorzugt diesen Teilwert für „Ist heute“ und
kennzeichnet unvollständige Erfassung weiterhin ausdrücklich. Das historische
`total_energy` bleibt für bestehende Leser erhalten. Bei einer älteren
Backendversion ohne das Zusatzfeld bleibt die bisherige Kartenanzeige nutzbar.

Archivversion 3 bewahrt alte und neue Prognosestände mit ihrer jeweiligen
Record-Zeitzone. Tagesgrenzen und Stichtage werden in dieser ursprünglichen
Zone geprüft. Die aktuelle Berichtssumme bezieht sich auf die aktive
Konfiguration und Zone; `configuration_groups` enthält getrennte frühere
Vergleichsgrundlagen. Die Archivmigration aus Version 1 oder 2 verändert die
bisherigen Prognosestände nicht. Bisherige Lernfaktoren benötigen für die neue
physische Konfiguration einen neuen Nachweis; sie werden nicht übernommen.

Unbekannte oder unlesbare Mess-, Archiv- und Lernspeicher verhindern einen
physischen Standortwechsel. Dadurch können fehlende alte Standortmetadaten
nicht nachträglich mit dem neuen Standort verwechselt werden. Vor dem
Config-Entry-Update werden die laufenden Manager beendet und erforderliche
Migrationen im bisherigen Kontext gesichert. Eine erneute schreibgeschützte
Prüfung verlangt tatsächlich gespeicherte aktuelle Versionen; intern nur
protokollierte HA-Schreibfehler gelten nicht als Erfolg. Bewusst gelöschte
Daten werden nicht aus einem älteren Leseergebnis wiederhergestellt.
Scheitert dieser Schritt, bleibt der gespeicherte Standort unverändert; eine bereits entladene Anlage wird mit
ihrem bisherigen Stand wieder gestartet. Es werden keine Quelldaten gelöscht.

Ein von Home Assistant nach `.corrupt.*` umbenannter unlesbarer Speicher bleibt
auch beim erneuten Bestätigen ein Hindernis, solange kein gültiger Stand
wiederhergestellt wurde. Eine gültig wiederhergestellte Datei erlaubt den
Wechsel, auch wenn die Diagnosekopie erhalten bleibt. Der verwendete
[HA-Store-Vertrag](https://github.com/home-assistant/core/blob/2025.12.0/homeassistant/helpers/storage.py)
ist bereits in der unterstützten Mindestversion vorhanden.

Die Config-Entry-Version bleibt 1.1. Die Speicher sind davon unabhängig
versioniert. Die bestehenden Aufbewahrungs- und Größengrenzen gelten weiterhin
über alle Standortsegmente hinweg.
