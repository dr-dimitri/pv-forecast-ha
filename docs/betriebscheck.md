# Betrieb prüfen

Öffne **Einstellungen → Geräte & Dienste → PV-Prognose → Konfigurieren → Betrieb prüfen**.
Der Bericht gruppiert Wetter/Prognose, lokale Messquellen, Prognosearchiv,
Selbstkalibrierung und Neustartcache. Jeder Befund hat einen festen Code,
Schweregrad und einen übersetzten Erklärungstext mit nächstem Prüfschritt.
**Erneut prüfen** und **Zurück zur Übersicht** ändern keine Einstellungen.

Der Check liest ausschließlich bereits vorhandene Laufzeit- und Registryzustände.
Er startet keinen Wettertest, liest oder schreibt keinen Store und bewertet das
Archiv nicht erneut. Ohne Runtime bleiben Detailzustände ausdrücklich nicht
prüfbar. Ein deaktiviertes Archiv, ausgeschaltetes Lernen und eine zu kleine
Lernstichprobe sind keine technischen Defekte.

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

Nachweise mit echten lokalen HA-Dialogen und synthetischen Laufzeitdaten:
[360 px Hell](images/ui-131-360-light.png), [360 px Dunkel](images/ui-131-360-dark.png).
Tastaturbedienung, lange Hinweise, erneutes Prüfen und Rücksprung sind geprüft.
Dies ist eine Betriebsprüfung, keine reale Güte- oder Defektbewertung.
