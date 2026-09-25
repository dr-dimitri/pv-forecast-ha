# Konfiguration und sichere Weiterentwicklung

## Aktuell ausgeliefertes Schema

Config Entries verwenden Version **1.1**. Ein Entry beschreibt eine PV-Anlage;
Standortdaten liegen in `data`, Dachflächen, das optionale gemeinsame
Wechselrichterlimit und optionale `measurement_sources` in `options`. Die UI-Eingabe `system_efficiency` wird als
Verlust-Prozent in `loss_factor` gespeichert. Dies ist der vereinbarte
Speichervertrag und keine zu beseitigende Altlast.

Einträge ohne Wechselrichterlimit bleiben gültig und unbegrenzt. Die
Anlagenzeitzone ist die gespeicherte IANA-Zone; sie wird beim Start weder aus
Home Assistant übernommen noch erneut anhand der Koordinaten ermittelt. Neue
Adresskonfigurationen ermitteln sie während der Einrichtung. Eine bewusste
Standortkorrektur bestehender Anlagen gehört in den Reconfigure-Flow (#23).

## Wann eine Migration erforderlich wird

Eine Migration wird zusammen mit einer tatsächlichen Änderung des gespeicherten
Schemas entwickelt. Kompatible optionale Ergänzungen brauchen keinen
Major-Versionssprung und keinen vorsorglichen Umbau vorhandener Einträge.
Bestehende Felder dürfen nicht für eine andere Bedeutung wiederverwendet werden.
Bei inkompatiblen Änderungen wird die Major-Version erhöht; echte Umwandlungen
laufen über `async_migrate_entry` und `hass.config_entries.async_update_entry`.

Vor dem Schreiben muss der vollständige Zielstand vorbereitet und validiert
sein. Ein fehlgeschlagener Schritt lässt den Ausgangseintrag unverändert.
Migrationen erzeugen keine neuen Anlagen oder Dach-IDs. `entry_id`,
Config-Entry-`unique_id`, Dach-IDs, Entity-`unique_id` und Anwenderwerte werden
erhalten. Netzwerkfehler dürfen keinen teilweise umgeschriebenen Eintrag
hinterlassen. Jeder tatsächliche Schritt benötigt Offline-Tests für Erfolg,
Fehler, wiederholten Aufruf und die übernommenen Identitäten.

Solange keine Datenumwandlung erforderlich ist, bleibt das Schema bei 1.1.
Ein wirkungsloser Migrationshook bietet keinen zusätzlichen Schutz. Home
Assistant lehnt höhere, hier nicht unterstützte Major-Versionen bereits vor dem
Setup ab. Kompatible Minor-Versionen behandelt HA gemäß seinen
[Config-Entry-Migrationsregeln](https://developers.home-assistant.io/docs/core/integration/config_flow/#config-entry-migration).

## Messdatenspeicher ab #26

Die optionale Quellenliste ergänzt Schema 1.1 kompatibel. Ohne sie wird kein
Messpfad aktiviert; vorhandene Konfigurationen benötigen keine Migration.
Jede Quelle hat eine stabile `source_id`, eine bestätigte Entity-Registry-ID
(soweit vorhanden), Messart, Messgrenze und explizite Herkunftsbestätigungen.

Die lokal erfassten Messdaten verwenden seit #152 einen eigenen HA-Store Version 3
mit dem Schlüssel `pv_forecast.measurements.<entry_id>`. Sie werden höchstens
sieben Tage und bis zu 20.000 Messpunkte sowie 20.000 zugehörige
Zählerdifferenzen pro Quelle vorgehalten. Quellenwechsel
werden als neue Segmente geführt und nicht mit alten Messreihen verrechnet.
Der Datenvertrag ist in [Messdaten](messdaten.md) beschrieben.

Version 3 übernimmt Mess-Stores 1 und 2 verlustfrei. Optionale
`retention_losses` dokumentieren tatsächlich durch die harte Mengenbegrenzung
entfernte Messabschnitte mit ihrem bisherigen Segmentbezug; für Altbestände
werden keine Ereignisse erfunden. Die normale Alterslöschung zählt nicht dazu.
Gesunde zusammenhängende Abschnitte innerhalb einer UTC-Minute und eines lokalen
Tages werden ohne Änderung der Energiemenge verdichtet. Lücken, Resets,
Korrekturen sowie Null-/Positivgrenzen bleiben getrennt. Config Entries bleiben
bei 1.1, die bestehenden Mengen- und Zeitgrenzen gelten weiter.

Reguläre Messspeicherschreibungen haben feste Fünfminutentermine mit höchstens
288 Schreibungen am Tag. JSON-Kodierung und Dateiarbeit erfolgen im Executor
aus einem von der weiteren Erfassung getrennten Stand.

## Standortänderungen ab #23

Der [Reconfigure-Flow](standortwechsel.md) erhält Config Entry und Optionen bei
Schema 1.1. Messsegmente enthalten ihren ursprünglichen Standort- und
Zeitzonenkontext. Bestehende Daten werden im bisherigen Kontext migriert.
Alte Tagesgrenzen und Zuordnungen ändern ihre Bedeutung nicht. Ein physischer
Wechsel beginnt ein neues aktives Messsegment. Unbekannte oder unlesbare
Mess-Stores verhindern die Änderung.

## Entfernung der Archivfunktion

Die Archivfunktion und ihre abhängigen Optionen sind entfernt. Beim nächsten
Start einer Anlage werden ausschließlich deren frühere Archiv-, Lern- und
Morgen-Stores sowie die zugehörigen Optionen und Minderertragshinweise gelöscht.
Die Bereinigung liest die ehemaligen Datenformate nicht ein und funktioniert
auch bei unbekannten Versionen. Scheitert das Löschen, wird das Setup erneut
versucht; ein unvollständiger Abschluss wird nicht als Erfolg behandelt.

Messdaten, Prognosecache, Anlagenidentität und Config-Entry-Schema 1.1 bleiben
erhalten. Es gibt keine Archiv-Lese- oder Exportaktionen mehr.
