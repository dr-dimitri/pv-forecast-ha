# Optionale Selbstkalibrierung

Die Integration kann einen gemeinsamen Anlagenfaktor lernen, wenn ihr Rohmodell
den bestätigten AC-Ertrag deiner Anlage dauerhaft über- oder unterschätzt.
Die Funktion ist standardmäßig **aus**. Sie ist kein Defektmelder und garantiert
keine bessere Prognose auf jeder Anlage oder in jeder Wetterlage.

## Einrichtung und Bedienung

1. Bestätigte PV-Energiezähler zuordnen und das Prognosearchiv aktivieren.
   Ein Leistungssensor allein genügt nicht. Netzexport ist kein Erzeugungszähler.
2. Unter **Konfigurieren → Selbstkalibrierung → Modus auswählen** zunächst
   **Beobachten** wählen. In diesem Modus werden Kandidaten geprüft, während
   die Prognose unverändert bleibt.
3. Unter **Lern- und Prüfstatus ansehen** Lerntage, spätere Prüftage, Kandidatenfaktor und MAE-Vergleich
   ansehen. Fehlende Daten sind kein Fehler von null kWh und kein Gütenachweis.
4. **Nach erfolgreicher Prüfung automatisch anwenden** erlaubt ausschließlich
   einen nach den folgenden Regeln freigegebenen Faktor. Bei unzureichendem
   Nachweis bleibt beziehungsweise gilt wieder Faktor 1.

Das Archiv und Messquellen werden nicht still eingeschaltet. Deine Nennleistung,
der Systemwirkungsgrad, Dachgeometrie und Anlagenzeitzone bleiben unverändert.
Es entstehen keine zusätzlichen Sensoren oder Wetterabrufe. Bestehende Sensoren,
Leseaktionen, eigene Karte und Energy Dashboard verwenden dieselbe wirksame
Prognose für heute und morgen.

Bekannte Abregelung oder Wartung lässt sich ausdrücklich für einen lokalen Tag
markieren und zurücknehmen. Es sind höchstens 90 Markierungen im Zeitraum heute
minus 364 Tage bis heute möglich. Verwende dies für bekannte Ereignisse;
ein großer Prognosefehler allein belegt weder Abregelung noch Schnee.

**Aus** verwendet das Grundmodell. **Lernzustand zurücksetzen** verwirft nach
Bestätigung die bisherigen Lernreferenzen und beginnt ein neues Segment ab jetzt.
Prognose- und Messhistorie bleiben dabei erhalten. Quellen-/Archivlöschung entfernt
zusätzlich davon abhängige Lernbelege. Eine Änderung von Geometrie, Nennleistung,
Verlustanteil, AC-Limit oder Messgrenze beginnt ebenfalls ein neues Segment.
Eine reine Umbenennung mit unveränderter Registry-Identität tut dies nicht.

## Feste Lernregeln, Version 1

Verwendet wird ausschließlich die Tagesprognose bis 18 Uhr am Vortag
(`daily_previous_18`). Die jüngsten 30 gültigen Lerntage müssen innerhalb von
60 lokalen Kalendertagen liegen. Ein Tag benötigt vollständige AC-Messung,
rechtzeitigen Forecast und brauchbare Wetterintervalle. Für das Training sind
mindestens 0,1 kWh ungekürzte Tagesenergie und höchstens 10 % Energieverlust
durch das bekannte Wechselrichterlimit nötig. Stärker begrenzte Tage erlauben
keinen verlässlichen Rückschluss auf den ungekürzten Ertrag.

Die feste Suche prüft Faktoren von 0,5 bis 1,5 in Schritten von 0,01 und minimiert
den mittleren absoluten Tagesfehler (MAE). Bei gleich guten Werten gewinnt der
Faktor näher an 1. Jeder Versuch wendet den Faktor vor dem gespeicherten AC-Limit
an. Beispiel: 10 kW vor einem 5-kW-Limit ergeben auch mit Faktor 0,8 weiterhin
5 kW; eine Multiplikation der bereits gekürzten 5 kW würde ein falsches Ergebnis
liefern. Alle Dachanteile werden weiterhin proportional begrenzt.

Große Fehler werden nicht allein wegen ihrer Größe ausgeschlossen. Der absolute
Fehler gibt Extremtagen kein zusätzliches quadratisches Gewicht. Ungeklärte starke
Abweichungen bleiben sichtbar. Ein Gesamtzähler begründet keine gemessenen
Einzelfaktoren für Dächer oder Tageszeiten.

## Prüfung an tatsächlich späteren Tagen

Der gelernte Kandidat bleibt während der Prüfung fest. Seine Werte müssen bei
einem erst nach seiner Erzeugung liegenden Prognosestichtag wirklich archiviert
worden sein. Nachträglich aus bekannten Messungen berechnete Kandidatenwerte
zählen nicht. Mindestens 14 gültige Prüftage innerhalb von 28 lokalen
Kalendertagen sind erforderlich.

Beide Modelle verwenden genau dieselben Prüftage. Bekannte Abregelung/Wartung,
fehlende Messabdeckung und unbrauchbare Eingaben sind ausgeschlossen; reguläres
Clipping und große Prognosefehler bleiben in der Prüfung erhalten. Freigabe
verlangt gleichzeitig:

- mindestens 5 % niedrigeren Tages-MAE bei positivem Roh-MAE;
- höchstens 5 % Verschlechterung des 90%-Quantils absoluter Tagesfehler.

Das Quantil verwendet den aufgerundeten Rang `ceil(0,9 × Anzahl)` der sortierten
absoluten Fehler. Die Regeln sind vorab festgelegt; sie sind kein statistisches
Konfidenzversprechen und kein Nachweis für andere Anlagen.

Nach Freigabe werden die jüngsten 14 geeigneten Tage innerhalb von 28 Tagen
weiter geprüft. Fehlender oder nicht mehr ausreichender Nachweis bedeutet
Rückfall auf Faktor 1. Nach Ablehnung oder Abbruch beginnt frühestens nach
14 Tagen ein neuer Lernversuch, der wieder spätere Prüftage benötigt.

## Speicherung und Nachvollziehbarkeit

Das Archiv erhält die ungekürzte UTC-Berechnungsbasis und das damalige AC-Limit.
Rohprognose, angewendete Korrektur und bloßer Testkandidat bleiben getrennt.
Die Rohprognose wird niemals durch den bereits korrigierten eigenen Forecast
ersetzt. Alte Archivstände bleiben nach der Migration von Store-Version 1 auf 2
lesbar, erhalten jedoch keine rekonstruierte Lernbasis und zählen deshalb nicht
rückwirkend als neue Lerntage.

Der private Lern-Store `pv_forecast.calibration.<entry_id>` verwendet Version 1,
unabhängig vom unveränderten Config-Entry-Schema 1.1. Er enthält Segment,
Kandidat, 30 Trainingsreferenzen und höchstens 28 Prüfbelege mit Fingerprints der
Bewertungsinhalte. Er ist auf 1 MiB begrenzt; die Datenbasis liegt weiterhin im
auf 365 Tage, 6.000 Ziele und 32 MiB begrenzten Archiv. Reguläre Schreibungen
werden auf feste Fünfminutentermine gebündelt; Entladen und bewusste Löschung
können zusätzlich schreiben. Ein hartes Ausschalten kann das letzte noch nicht
gespeicherte Stück verlieren. Unbekannte Speicherversionen werden nicht überschrieben.

Messkorrekturen oder gelöschte Belege entziehen betroffenen Faktoren ihre
Freigabe. Der Lernstatus steht im Options Flow und zusätzlich unter `calibration`
in der bestehenden Aktion `pv_forecast.get_history`. Dieselben Rechte auf Anlage
und archivierte Messquellen gelten; es gibt keine neue öffentliche Leseaktion.
`calibrated_comparison` im Archivbericht vergleicht nur tatsächlich angewendete
Korrekturen auf denselben gültigen Tagen. Ohne solche Belege bleibt der Vergleich
leer; ein bloßer Testkandidat wird dort nicht als produktive Verbesserung ausgegeben.

Die Tests verwenden synthetische, zeitlich getrennte Daten und prüfen unter anderem
Neustart, Abschalten, Reset, Clipping und Messkorrekturen. Die reale Erprobung mit
mindestens 30 Lerntagen und 14 späteren Prüftagen auf echten Anlagen steht aus.
