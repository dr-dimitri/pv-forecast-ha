# Temperaturmodell separat vergleichen

Unter **Optionen → Prognosearchiv → Temperaturvergleich je Dach vorbereiten**
wird je Dach eine Montageannahme gewählt. „Nicht festgelegt“ ist der Standard.
Erst wenn alle aktuellen Dächer zugeordnet sind, lässt sich bei aktiviertem
Archiv mit **Temperaturmodell beobachten** eine vollständige Anlagenvariante
bewerten. Eine gelöschte oder neue Dachfläche übernimmt keine fremden Parameter.
Die Dachleistung, Geometrie, Verluste und produktive Prognose bleiben unverändert.


## Prospektiver Temperaturvergleich zu #17

Eine standardmäßig ausgeschaltete Archivoption vergleicht das unveränderte Rohmodell mit einer Ross-Zelltemperatur-Näherung. Sie verwendet ausschließlich bereits geladene GTI- und Außentemperaturintervalle. Die reine Berechnung ist `T_Zelle = T_Luft + k × GTI`; der kWp×GTI-Ansatz, Verlustfaktor und beide AC-Clippingstufen bleiben erhalten. Referenzparameter aus der offiziellen pvlib-Dokumentation: gut belüftetes Schrägdach 0,02, freistehend 0,0208, gut belüftetes Flachdach 0,026 und schwach belüftetes Schrägdach 0,0342 K m²/W. Das Modell setzt vereinfachend 1 m/s Wind und stationäre beziehungsweise langsam veränderliche Einstrahlung voraus; der tatsächliche Wind am Modul wird nicht behauptet.

Je Dach wird eine Vergleichsannahme bewusst ausgewählt; Standard ist „nicht festgelegt“. Ohne vollständige Dachzuordnung entsteht kein Anlagenvergleich. Keine Pflichtangaben oder vorsorgliche Änderung bestehender Dachmodelle. Die Auswahl gilt nur für die Beobachtung; Sensoren, gemeinsame wirksame Zeitreihe, Kalibrierungsfreigabe und Anwenderverluste bleiben unverändert. Parameteränderungen beginnen durch eine eigene Kennung eine neue Vergleichsgruppe, reine Dachnamenänderungen nicht.

Neue Archivstände speichern ausschließlich rechtzeitig mit derselben Wetterantwort berechnete Alternativenergie mit eigener Modell-/Parameterkennung. Archiv-Store 5 migriert Versionen 1–4 ohne erfundene alte Vergleichsstände. Konfigurationsschema 1.1, Mess- und Lern-Store sowie Schreib-/Aufbewahrungsgrenzen bleiben erhalten. Fehlende Eingaben bleiben Qualitätsmängel und gehen nicht als fehlerfreie Vergleichsfälle ein. Die bestehende Archivaktion berichtet MAE/Bias und Stichprobe auf identischen späteren gültigen Messintervallen, getrennt nach Horizont und Parameterkennung. Ein belegter Nutzen und die Prüfung mit echten Anlagen stehen aus; diese Stufe ändert kein Standardmodell und übernimmt keine bisherige Lernfreigabe in ein anderes Modell. Keine zusätzliche Bibliothek, Wettervariable, HTTP-Abfrage, Entity, Aktion oder Speicherung außerhalb des Archivs.

Referenz: https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.temperature.ross.html


## Interpretation

Die Karte und `get_history.temperature_comparison` zeigen Rohmodell und Ross
auf identischen gültigen späteren Messintervallen. Eine kalibrierte
Produktivprognose ist kein fairer Ersatz für diesen Rohmodellvergleich:
Die alternative Variante übernimmt deshalb keinen dafür gelernten Faktor.
`applied` bleibt false. Der Bericht verwendet unabhängig vom allgemeinen
Kartenbericht 90 abgeschlossene lokale Tage; die Zeitspanne wird angezeigt.
Einzelne positive Fälle reichen nicht für einen allgemeinen Modellwechsel.

Eine Parameteränderung trennt die Auswertung über `parameter_id`; gespeicherte
Vergleichsstände enthalten weiterhin ihre damalige Montageauswahl. Alte Daten
werden weder umgerechnet noch ergänzt. Fehlende Temperaturen, ungeklärte
Qualität oder unvollständige Messung gehören nicht zu den gültigen Paaren.
Nach Messkorrekturen verwenden beide Modellfehler dieselbe aktualisierte
Bewertung. Große Fehler allein sind kein Ausschlussgrund.

Die repräsentativen Koeffizienten gelten nicht automatisch für jedes reale
Modul oder Gebäude. Das Modell bildet weder gemessenen Wind noch schnelle
Temperaturdynamik, Schnee oder unterschiedliche Zelltemperaturen innerhalb
eines Moduls ab. Fachlich ungeklärte Montagearten erhalten keinen erfundenen
Standardwert. Ein späterer produktiver Modellwechsel erfordert reale Auswertung,
eine eigene Modellversion und eine neue Prüfung der Lernfaktoren.
