# Prognose nach einem Neustart ohne Internet

Unter **Konfigurieren → Erweiterte Funktionen → Letzte Prognose speichern**
kannst du den unabhängigen Neustartcache einschalten. Standard ist aus. Er ist
auch ohne Prognosearchiv nutzbar. Beim nächsten erfolgreichen Wetterabruf wird
der vollständige Rohmodellstand gespeichert. Ausschalten und Entfernen der
Anlage löschen ausschließlich ihren Cache.

Bei einem anschließenden Neustart mit ausgefallenem Wetterabruf kann die Anlage
mit diesem Stand starten, solange er noch die aktuellen lokalen Prognosetage
überlappt. Die Karte zeigt **Gespeicherte Prognose**, den ursprünglichen
Wetterabruf und die bestehende Fehler-/Alterskennzeichnung. Neu hinzugekommene,
nicht abgedeckte Tage bleiben unbekannt. Die Sensoren bleiben bei Abruffehler
unverfügbar; Energy, Zeitfenster und operative Planung geben dann keine nutzbare
Prognose aus. Lokale Messlistener können wieder arbeiten. Die Neustartlücke
bleibt eine Lücke, alte HA-Zählerzustände werden nicht als frische Messung übernommen.

Die Aktion `get_forecast` ergänzt `origin: live|restored` und `restored_at`.
`fetched_at` ist stets der echte ursprüngliche Wetterabruf. Die Kartenansicht
enthält ebenfalls Herkunft und Wiederherstellungszeit. Nach dem nächsten
normalen erfolgreichen Abruf gilt wieder `live`; es gibt keine zweite Abrufschleife
und keine Umgehung einer Anbieterpause.

Der Cache enthält genau eine Rohmodellgeneration mit stabilen Dachzuordnungen,
DC-/AC-Beiträgen, damaligen Grenzen, UTC-Zeiten, Qualitätsmerkmalen und Modell-/
Konfigurationskennung. Dachnamen stammen beim Laden aus der aktuellen Konfiguration.
Es werden keine HTTP-Antworten, Messhistorien, Anschriften oder Zugangsdaten
gespeichert. Ein unabhängig versionierter privater, atomarer HA-Store Version 1
ist auf 8 MiB begrenzt; Übergröße lässt den vorherigen Stand bestehen. Unbekannte
Storeversionen werden nicht überschrieben. Config Entry 1.1 und die Mess-/Archiv-/
Lern-Stores ändern sich nicht.

Nur erfolgreiche vollständige Wetterupdates schreiben den Cache. Minutentakte
und lokale Faktorwechsel schreiben ihn nicht. Ein gespeicherter kalibrierter
Endwert wird niemals zur Rohbasis. Wiederherstellung erzeugt keine neuen
rechtzeitig beobachteten Archiv- oder Versuchsbelege. Aktuelle Lernprüfungen
entscheiden weiterhin über einen Faktor; der Cache speichert keine Lernfreigabe.
Neue Einrichtung und Standortwechsel benötigen weiterhin ihren Wettertest.

Offline geprüft sind Rohmodell-Rundreise, Zeitumstellungen, Kathmandu, zwei/sieben
Tage, Offline-Reload und Wiederverbindung, fehlerhafte und fremde Stände,
Schreibreihenfolge, Löschung und das Ignorieren alter Zählerzustände.
Die Browserprüfung verwendet synthetische Daten; eine reale Ausfallerprobung
wird dadurch nicht als bestanden behauptet.

Grundlage: [HA-Coordinator und Setupfehler](https://developers.home-assistant.io/docs/integration_fetching_data/)
und native Speicherung in [HA 2025.12](https://github.com/home-assistant/core/blob/2025.12.0/homeassistant/helpers/storage.py).

Browsernachweise bei 360 px: [Hell](images/ui-129-360-restored-light.png),
[Dunkel](images/ui-129-360-restored-dark.png). Die vollständige Prüfroutine deckt
zusätzlich 768/1440 px, Tastatur, Touch, Datenlücken und Wiederverbindung ab.
