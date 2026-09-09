# Experimentelle Minderertragshinweise

Unter **Optionen → Prognosearchiv → Experimentelle Minderertragshinweise**
kann die Beobachtung bewusst aktiviert werden. Standard ist aus. Sie benötigt
Archiv und bestätigte AC-Energiequellen; bei fehlender Vergleichsbasis zeigt
die Karte einen begründeten Leerzustand. Die reine Auswertung verwendet nur
vorhandene lokale Daten.

## Die vorab festgelegte Regel 1

1. Die sieben unmittelbar vergangenen lokalen Tage müssen vollständig,
   vergleichbar und abgeschlossen sein. Fehlende Messung oder Forecast,
   unbrauchbare Eingaben, Quellenwechsel, bekannte Abregelung oder Wartung
   unterbrechen die Folge. Nulltage mit weniger als 0,1 kWh Rohprognose zählen
   nicht. Wiederholte DST-Stunden bleiben in den archivierten UTC-Grenzen.
2. Das Rohmodell-Erfahrungsband aus 60 früheren Trainings- und 30 strikt
   späteren Prüftagen muss bereits vor dem ersten Tag der Folge verwendbar
   gewesen sein. Es gelten dieselben Qualitäts-, Abdeckungs- und
   Breitenprüfungen wie bei #29. Diese Basis bleibt für alle sieben Tage fest.
3. Mindestens fünf Tage liegen unter der physisch begrenzten Banduntergrenze
   und zugleich mehr als 20 Prozent sowie 0,1 kWh unter der eigenen
   Rohprognose. Über alle sieben Tage muss die Messung ebenfalls mehr als
   20 Prozent unter der Rohprognose liegen.

Ein Hinweis betrifft ausschließlich die **Gesamtanlage**. Er zeigt Zeitraum,
Rohprognose, Messung, Abweichung, Datenabdeckung und die Vergleichsprüfung.
Die Archivaktion liefert zusätzlich die sieben einzelnen Tagesvergleiche.
Ein vermindertes Rohmodell im Winter ist für sich kein Minderertrag gegenüber
diesem Modell. Nicht modellierte saisonale Einflüsse können trotzdem Hinweise
auslösen; der Feldtest muss die tatsächliche Fehlalarmrate erst bestimmen.

## Prüfhilfe und Lernen

Messquelle und Wechselrichterstatus prüfen, bekannte Abregelung, Wartung oder
Anlagenänderung berücksichtigen. Wetterabweichung, Schnee oder Verschattung
sind mögliche Erklärungen; die Funktion bestimmt keine Ursache und keine
Dachdiagnose aus einem Gesamtzähler. Es gibt keine Geräteaktion.

Der Hinweis hält seine ursprünglichen Belege und den zuvor verwendeten
Lernfaktor fest. Die Bewertung verwendet das Rohmodell, sodass ein neuer
Kalibrierungsfaktor die Abweichung nicht versteckt. Solange der Hinweis besteht,
pausieren Training und Kandidatenprüfung; die wirksame Kalibrierung verwendet
Faktor 1. Wetterabrufzeit und Fehlerzustand bleiben unverändert.

Spätere Messkorrekturen, neue Ausschlussmarkierungen oder entfernte alte
Referenzen machen die Grundlage sichtbar ungültig. Das bestätigt keine
Erholung und beendet den Lernstopp nicht still. Standort-/Quellenwechsel und
Quellenlöschung entfernen abhängige Hinweise.

## Quittieren, Löschen und Ruhezeit

**Prognosearchiv → Prüfhinweis quittieren oder löschen** bietet zwei bewusst
bestätigte Aktionen. Quittieren beendet nur die Mitteilung. Löschen entfernt
Hinweis und Referenzen und lässt das normale Lernen wieder zu. Eine neue
Erkennung braucht dann sieben neue vollständig abgeschlossene Tage. Bekannte
Abregelung oder Wartung vor dem Löschen unter Kalibrierung markieren.
Ausschalten der Beobachtung allein löscht einen vorhandenen Hinweis nicht.

Eine separat aktivierbare lokale HA-Mitteilung verweist allgemein auf die
berechtigungsgeprüfte Karte. Sie enthält keine Erträge, Anlagenbezeichnung
oder Messquellen. Alle Abweichungen eines Hinweises werden unter einer festen
Mitteilungskennung gebündelt. Ein gespeicherter Versandvermerk verhindert
Wiederholungen beim regulären Neustart. Wie andere Archivänderungen wird er
spätestens beim nächsten regulären Schreibtermin oder Entladen gesichert;
ein harter Ausfall vor diesem Termin kann die allgemeine Mitteilung wiederholen.
Zwischen 22 und 08 Uhr in der Anlagenzeitzone entstehen keine neuen Mitteilungen;
ein vorhandener Hinweis bleibt in der Karte sichtbar. Quittierung, Löschen,
Quellenlöschung und Entfernen der Integration entfernen die Mitteilung.

## Grenzen und Nachweise

Archiv-Store 6 enthält maximal einen Hinweis, 97 Referenzen und den Bedienstand,
zusammen höchstens 64 KiB innerhalb der bisherigen Archivgrenze. Alte Archive
werden verlustfrei ohne erfundene Hinweise übernommen. Fehlende Referenzen
werden nicht aus Recorder oder Netzwerk ergänzt. Rechte und Löschpfade gelten
wie für das übrige Archiv; die bestehende Archivaktion bleibt die Schnittstelle.

Synthetische Tests decken wiederholten Leistungsabfall, kurze Einzelabweichungen,
Messlücken, null Ertrag, Jahreszeitenwechsel, DST, Messkorrekturen, Lernstopp,
Quellenlöschung, Opt-in, Ruhezeit und Quittierung ab. Sie belegen **keine reale
Trefferquote**. Feldtest und Ziel von höchstens einem unbegründeten Hinweis pro
Anlage und Monat bleiben offen. Die Funktion bleibt bis dahin experimentell.
