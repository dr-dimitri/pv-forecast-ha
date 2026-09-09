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

## Künftige Mess-, Archiv- und Lerndaten

Persistente Messdaten, eingefrorene Prognosen und Lernzustand erhalten bei ihrer
Einführung eigene Speicherversionen und eigene Migrationsprüfungen. Die
Config-Entry-Version wird nicht als Ersatz für deren Datenvertrag verwendet.
Eine Änderung von Anlage, Messgrenze oder Modell muss in der Historie erkennbar
bleiben und gegebenenfalls ein neues Lernsegment beginnen. Bestehende Daten
werden nicht rückwirkend so umgedeutet, als wären sie mit der neuen Konfiguration
entstanden.

## Messdatenspeicher ab #26

Die optionale Quellenliste ergänzt Schema 1.1 kompatibel. Ohne sie wird kein
Messpfad aktiviert; vorhandene Konfigurationen benötigen keine Migration.
Jede Quelle hat eine stabile `source_id`, eine bestätigte Entity-Registry-ID
(soweit vorhanden), Messart, Messgrenze und explizite Herkunftsbestätigungen.

Die lokal erfassten Messdaten verwenden seit #23 einen eigenen HA-Store Version 2
mit dem Schlüssel `pv_forecast.measurements.<entry_id>`. Sie werden höchstens
sieben Tage und bis zu 20.000 Messpunkte sowie 20.000 zugehörige
Zählerdifferenzen pro Quelle vorgehalten. Quellenwechsel
werden als neue Segmente geführt und nicht mit alten Messreihen verrechnet.
Der Datenvertrag ist in [Messdaten](messdaten.md) beschrieben.

## Prognosearchiv ab #27

`history_enabled` und die optionale bestätigte Zuordnung `comparison_forecast`
sind kompatible Options-Ergänzungen des Schemas 1.1. Deaktivierte Erfassung
löscht keine bestehenden Daten. Archivdaten verwenden seit #23 separat Store-Version 3
unter `pv_forecast.history.<entry_id>`. Der
[Archivvertrag](prognosearchiv.md) nennt Zeitfenster, Rohprognose, Bewertungs-
revisionen, Konfigurationsbezug, Aufbewahrungsgrenzen und Löschung.

## Selbstkalibrierung ab #18

`calibration_mode` und `calibration_exclusions` sind optionale Ergänzungen des
Config-Entry-Schemas 1.1. Der Lernzustand hat einen eigenen Store Version 1 unter
`pv_forecast.calibration.<entry_id>`. Die tatsächliche Archivschema-Erweiterung
auf Version 2 übernimmt alte Rohstände verlustfrei und ergänzt keine rückwirkend
geschätzte Lernbasis. Unbekannte Versionen werden nicht überschrieben.

Die [Kalibrierungsregeln](kalibrierung.md) bestimmen Freigabe, Segmentwechsel,
Messrevisionen und bewusstes Rücksetzen. IDs, Nennwerte und Anlagenzeitzone
werden dadurch nicht umgedeutet.

## Standortänderungen ab #23

Der [Reconfigure-Flow](standortwechsel.md) erhält Config Entry und Optionen bei
Schema 1.1. Mess-Store 2 ergänzt originale Standort- und Zeitzonenkontexte je
Segment; Archiv-Store 3 erlaubt erhaltene Datensätze unterschiedlicher
Anlagenzeitzonen. Bestehende Daten werden im bisherigen Kontext migriert.
Alte Tagesgrenzen und Zuordnungen ändern ihre Bedeutung nicht. Ein physischer
Wechsel beginnt neue aktive Segmente und übernimmt keine alte Lernfreigabe.
Unbekannte oder unlesbare Stores verhindern die Änderung.


### Archivversion 4: optionaler kurzfristiger Beobachtungsversuch

Seit dem Folgeschritt zu #30 ergänzt der Archiv-Store rechtzeitig eingefrorene
Korrekturkandidaten und optionale Resttagsstände ab 12 Uhr. Die Versionen 1 bis 3
werden verlustfrei gelesen; ältere Datensätze erhalten keine erfundenen
Versuchsbelege. Config-Entry-Schema 1.1 und Mess-/Lern-Store bleiben unverändert.
Details: [Kurzfristiger Vergleich](kurzfristiger-vergleich.md).


### Archivversion 5: rechtzeitiger Temperaturvergleich

Der Folgeschritt zu #17 ergänzt optional gleichzeitig berechnete Ross-
Vergleichsstände mit ihrer ursprünglichen Parameterwahl. Versionen 1–4 werden
verlustfrei migriert; bestehende Datensätze erhalten keine erfundenen
Alternativprognosen. Config Entries bleiben 1.1.
Siehe [Temperaturvergleich](temperaturvergleich.md).

## Archiv-Store 6: experimentelle Minderertragshinweise

Version 6 ergänzt optional einen begrenzten Hinweiszustand mit höchstens 97
Referenzen und Inhaltsfingerprints. Versionen 1–5 werden ohne Datenverlust
übernommen; alte Hinweise werden nicht erfunden. Die zusätzliche Grenze von
64 KiB zählt zur bestehenden Archivgrenze. Config Entries bleiben bei 1.1.
Details: [Experimentelle Minderertragshinweise](minderertragshinweise.md).
