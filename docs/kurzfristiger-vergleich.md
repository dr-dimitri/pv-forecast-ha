# Kurzfristige Korrektur beobachten

Unter **Optionen → Prognosearchiv → Archivierung aktivieren oder pausieren**
kann ein freiwilliger Vergleich aktiviert werden. Er benötigt ein aktives Archiv
und bestätigte AC-Energiequellen. Ohne ausreichende aktuelle Stundenmessungen
werden nur Gründe für fehlende Kandidaten gespeichert. Die produktive Prognose,
die Tagesaussicht, Sensoren, Energy und Solarplanung bleiben unverändert.


## Prospektiver Beobachtungsversuch zu #30

Die Archivoption „Kurzfristige Korrektur beobachten“ ist standardmäßig aus. Sie verändert keine produktiven Prognosewerte. Nur bei aktiviertem Archiv werden mit neuen Prognoseständen begrenzte Vergleichskandidaten rechtzeitig eingefroren: volle UTC-Stunden mit einer/drei Stunden Vorlauf und der lokale Resttag von 12 Uhr bis Mitternacht. Für den Resttag gilt maximal eine Stunde Datenalter. Alte Stände erhalten keine nachträglichen Kandidaten. Archiv-Store 4 migriert Versionen 1–3 verlustfrei ohne erfundene Belege; Config Entries bleiben 1.1.

Versuchsregel 1 verwendet drei aufeinanderfolgende vollständig bewertete Stunden derselben Anlagen-/Quellenbasis, deren Ende höchstens 90 Minuten zurückliegt und deren Bewertung zum Beobachtungszeitpunkt bekannt war. Mindestens 0,3 kWh Basisenergie, keine Eingabefallbacks oder bekannten Abregelungs-/Wartungstage. Ein Verhältnis außerhalb 0,5–1,5 liefert einen Leerzustand. Innerhalb dieses Bereichs wird der Faktor auf 0,8–1,2 begrenzt und seine Wirkung linear innerhalb sechs Stunden bis null reduziert. Er betrifft nur zukünftige Intervalle des laufenden lokalen Tages. Die eingefrorene DC-Basis wird vor Gruppen- und Gesamtclipping skaliert; der langfristige Anlagenfaktor bleibt unverändert. Ohne Basis entsteht kein Kandidat.

Kandidat, Beobachtungszeit und Inhaltsfingerprints der drei damals bekannten Mess-/Forecastbelege werden im bestehenden Archivdatensatz bewahrt. Änderungen oder fehlende Belege verhindern seine Verwendung im Vergleich. Die vorhandene berechtigungsgeprüfte Archivaktion liefert getrennt nach Vorlauf MAE, Bias, Stichprobe, Abdeckung und Ausschlüsse auf denselben späteren Messintervallen. Mindestens 30 abgeschlossene lokale Tage und mindestens fünf Prozent geringerer MAE bei positivem Basis-MAE sind das vorab festgelegte Prüfziel; große Fehler bleiben enthalten. Die jüngsten 60 Tage werden geprüft. Selbst bei erreichtem Ziel bleibt die Korrektur in dieser Stufe ausschließlich beobachtend. Freigabe produktiver Anwendung benötigt einen gesonderten belegten Auswertungsschritt. Löschung und Aufbewahrung folgen dem bestehenden Archiv; keine zusätzlichen Wetterabrufe, Stores, Entities oder Aktionen.


## Lesen und Datenpflege

`pv_forecast.get_history` liefert `short_term` unter denselben Quellenrechten
wie das Archiv. Die Karte zeigt den Vergleich in ihrem Archivbericht. Das
Prüffenster umfasst unabhängig vom gewählten allgemeinen 7-/30-Tage-Bericht
60 abgeschlossene Tage. Jede Horizontgruppe nennt Basis-/Kandidaten-MAE und
Bias in kWh, gültige Messpaare, unterschiedliche lokale Tage, gespeicherte
Prognosefälle, Abdeckung dieser gespeicherten Fälle und Ausschlussgründe.
Nicht gespeicherte Forecasts werden damit nicht als vorhandene Fälle behauptet.

`criterion_met` ist das dokumentierte Prüfziel auf tatsächlich eingefrorenen
Kandidaten. Es aktiviert nichts. `applied` bleibt false. Eine positive Zahl
ersetzt keinen Feldversuch über Wetterlagen und Anlagen hinweg. Die gleichen
Messkorrekturen aktualisieren beide Fehlerreihen; Änderungen an verwendeten
Eingabebelegen schließen den betreffenden Kandidaten aus. Große Fehler allein
werden nicht ausgeschlossen.

Ausschalten beendet die neue Kandidatenerfassung; alte Vergleiche bleiben
lesbar. Die bewusste Archiv-/Quellenlöschung entfernt auch abhängige
Versuchsdaten. Das Archiv behält seine bisherigen Schreib-, Größen- und
Aufbewahrungsgrenzen. Alte Store-Versionen werden ohne erfundene Versuchsdaten
gelesen. Die tatsächliche Verbesserung über mindestens 30 spätere Tage bleibt
zu prüfen. Offline-Tests belegen nur Auswahl, Begrenzung und Vergleichsregeln.
