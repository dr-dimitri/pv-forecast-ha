# Betrieb prüfen

Öffne **Einstellungen → Geräte & Dienste → PV-Prognose → Konfigurieren → Betrieb prüfen**.
Der Bericht gruppiert Wetter/Prognose, lokale Messquellen und Neustartcache. Jeder Befund hat einen festen Code,
Schweregrad und einen übersetzten Erklärungstext mit nächstem Prüfschritt.
**Erneut prüfen** und **Zurück zur Übersicht** ändern keine Einstellungen.

Der Check liest ausschließlich bereits vorhandene Laufzeit- und Registryzustände.
Er startet keinen Wettertest und liest oder schreibt keinen Store. Ohne Runtime
bleiben Detailzustände ausdrücklich nicht prüfbar.

Schreibfehler von Messdaten werden erst nach dem tatsächlichen
Dateischreibversuch gemeldet. Ein fehlgeschlagener Stand bleibt als ungespeichert
erkennbar. Solange die Anlage geladen ist, erfolgt ein weiterer Versuch im
bestehenden Speichertakt, auch ohne neue Messmeldung; neue Daten verschieben
diesen Termin nicht. Erfolgreiches Schreiben hebt den Schreibfehler wieder auf.
Beim Entladen gibt es einen abschließenden Versuch und danach keine Wiederholungen.
Ein weiterhin fehlerhafter Datenträger kann die Persistenz verhindern. Unbekannte
oder unlesbare Speicherversionen bleiben davon getrennt und werden nicht überschrieben.

Eine bewusst angeforderte Löschung wird bei fehlgeschlagenem
Speicherabschluss nicht als erfolgreich bestätigt. Die Änderung kann bereits im
Arbeitsspeicher wirksam sein; prüfe freien Speicher und HA-Protokoll und wiederhole
die Bestätigung. Die Betriebsprüfung selbst löst keinen Speicherversuch aus.

Eine Anbieterpause bedeutet **frühestens wieder möglich**, nicht den Termin eines
garantierten Abrufs. Neustarts oder Force-Updates werden nicht zum Umgehen der Pause
empfohlen. Datenalter ist keine Fehlerwahrscheinlichkeit. Die Abdeckung des gesamten
konfigurierten Horizonts und die aktuellen Heute-/Morgen-Grenzen für Energy werden
getrennt nach UTC geprüft; sieben Tage sind deshalb nicht mehr irrtümlich unvollständig.

Messprüfungen unterscheiden entfernte, deaktivierte, nicht verfügbare und nicht
sicher identifizierbare Quellen sowie frische Messlücken. Ein täglicher Reset belegt
keinen endgültigen Vortagswert. Bei Integral-Helfern bleibt auch die zugrunde liegende
Leistungsquelle relevant. Unbekannte Messgrenzen werden nicht automatisch bestätigt.

Der Diagnostics-Download bleibt eine eigene feste Allowlist ohne Standort, Namen,
IDs, URLs, Fehlermeldungen oder Ertragsreihen. Der ausführliche UI-Bericht wird
nicht in diesen Download kopiert. Schema 1.1, öffentliche Aktionen und alle
Speicherversionen bleiben unverändert.

Dies ist eine Betriebsprüfung, keine reale Güte- oder Defektbewertung.
