# Geräte für PV-Messquellen ergänzen

Der Assistent arbeitet ausschließlich mit bereits installierten HA-Integrationen.
`measurement_adapters.py` trennt die Geräteerkennung vom Config-/Options-Flow.
`measurement_helpers.py` übernimmt gegebenenfalls den nativen Integral-Helfer.
Der bestehende Messpfad wertet anschließend Energiezählerdifferenzen aus.

## Ein neues Profil

Ein `MeasurementAdapter` in `MEASUREMENT_ADAPTERS` beschreibt einen stabilen
Schlüssel, Produktnamen, HA-Integrationsdomain, ein geprüftes Unique-ID-Suffix
und die Messart (`power`, `total` oder `daily`). Für andere Identitätsverträge
ist die Erkennung gezielt zu erweitern; Anzeigenamen oder erratene Entity-IDs
sind kein zuverlässiger Identitätsnachweis.

Vor Aufnahme anhand der Herstellerdokumentation und des Integrationscodes prüfen:

- Der Wert erfasst AC-PV-Erzeugung der gewählten Anlage, einschließlich Eigenverbrauch.
- Netzbezug, Einspeisung, DC-Erzeugung und Batterieentladung werden unterschieden.
- Energiequellen verwenden Wh/kWh mit `energy` und `total`/`total_increasing`;
  Leistungsquellen W/kW mit `power` und `measurement`.
- Registry-Identität, Meldehäufigkeit und Verhalten bei Ausfall sind bekannt.

Der erste Adapter erkennt KSEM anhand von `ksem` und `_obis_40974`.
Die Zuordnung gilt für die Gesamtanlage ohne Batterie; separate Dachmessungen
und andere fachliche Messgrenzen benötigen einen ausdrücklich passenden Flow.

## Helfer und Datenqualität

Erst beim Speichern wird der native Config Flow von `integration` aufgerufen.
Parameter: Trapezregel, Stunden, vier Nachkommastellen, Präfix `k` für W und kein
Präfix für kW. Kein zeitgesteuertes Fortschreiben (`max_sub_interval`). Ein
kompatibler aktiver Helfer derselben Registry-Quelle wird wiederverwendet.
Gleichzeitige Abschlüsse werden serialisiert. Fehler rollen ausschließlich die
in diesem Abschluss neu angelegten Helfer zurück. Erfolgreich angelegte Helfer
sind eigenständige HA-Helfer und bleiben bei Entfernung der PV-Zuordnung erhalten.
Es gibt keine neue numerische Integrationsimplementierung oder Geräteabfrage.

Abgeleitete Quellen speichern zusätzlich die ursprüngliche Entity-/Registry-ID
und die ID des nativen Helfers. Bestehende Quellen ohne diese optionalen Angaben
behalten ihre bisherigen Identitäten. Messsegmente und Archive bewahren die
Herkunft; beide Quellen benötigen Leserechte. Umbenennung derselben Registry-
Entity ändert die Identität nicht. Entfernen/Ersetzen der Quelle oder Änderung
der Helferkonfiguration wird nicht still übernommen. Lücken und ungültige
Leistungswerte verhindern vollständige Erfassungsintervalle.

## Prüfung

`tests/test_measurement_adapters.py` prüft die Registry-Erkennung, den nativen
HA-Integral-Helfer, Einheiten, Bestätigungen, Abbruch, Wiederverwendung,
Fehlerrücknahme und die ursprüngliche Messquelle. Der tatsächliche KSEM G2 wurde
hier nicht angesprochen. Geräteverfügbarkeit und Ertragsvergleich über einen
vollständigen Tag bleiben als reale Anlagenprüfung erforderlich.
