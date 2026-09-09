# Experimentelles Horizontprofil je Dach

Ein entfernter Bergrücken oder ein genügend weit entferntes Gebäude kann eine
Dachfläche abhängig vom Sonnenstand abschatten. Unter **Konfigurieren →
Horizontprofil je Dach (experimentell)** lässt sich dafür ein optionales Profil
hinterlegen. Das Profil ist standardmäßig aus. Es braucht keine neue Datenquelle,
Sensoren oder Services. Sensoren, Karte, Planung, Energy und Archiv verwenden
weiter dieselbe wirksame Prognose.

## Eingabe und Entfernen

1. Die Dachfläche auswählen.
2. Genau 12 oder 24 Höhenwinkel zwischen 0 und 90 Grad eingeben: **Nord zuerst,
   dann im Uhrzeigersinn**. Bei 12 Werten beträgt der Abstand 30°, bei 24 Werten 15°.
3. Dezimalpunkt verwenden; Komma, Semikolon, Leerzeichen oder Zeilenumbruch trennen
   die Werte. Keine zusätzlichen Azimutspalten, Einheiten oder Kopfzeilen einfügen.
4. Die Eignung für einen entfernten Horizont der gesamten Dachfläche bestätigen.

Ein synthetisches Beispiel mit erhöhtem Westhorizont:

```text
0, 0, 0, 0, 0, 0, 0, 10, 25, 30, 25, 10
```

Hier liegen Nord/Ost/Süd/West bei den Werten 1/4/7/10. Zwischen den Stützpunkten
wird zyklisch linear interpoliert, auch über Nord. Leer oder ausschließlich
Nullen entfernt das Profil. Umbenennen oder Bearbeiten des Dachs erhält seine
Zuordnung; Dachlöschung entfernt das zugehörige Profil.

Diese Richtungs- und Höhenkonvention entspricht dem manuellen Horizont-Upload
im [PVGIS-Handbuch, Abschnitt 2](https://joint-research-centre.ec.europa.eu/photovoltaic-geographical-information-system-pvgis/using-pvgis-5/pvgis-5-user-manual_en).
PVGIS akzeptiert aber auch andere Anzahlen. Ein beliebiger PVGIS- oder
Solarkatasterexport ist daher **kein direkt unterstütztes Importformat**.
Seine Winkelkonvention und Anzahl müssen vor einer Übernahme geprüft werden.

## Was das Modell berechnet

Regelversion 1 verwendet die veröffentlichte
[NOAA-Näherung für den Sonnenstand](https://gml.noaa.gov/grad/solcalc/solareqns.PDF),
ohne atmosphärische Refraktion. Jedes absolute UTC-Wetterintervall wird an zwölf
gleichmäßig verteilten Mittelpunkten ausgewertet: bei einer Stunde alle fünf
Minuten. Der stündliche DNI-Mittelwert gilt dabei näherungsweise konstant.
Die Rechnung verwendet die gespeicherten Koordinaten, keine lokale Uhrzeit als
Sonnenzeit; wiederholte DST-Stunden bleiben unterschiedliche Intervalle.

Nur betroffene Geometrieabrufe ergänzen `direct_normal_irradiance` (DNI) und
`diffuse_radiation` (DHI). Diese sind wie GTI Mittelwerte der vorhergehenden Stunde
([Open-Meteo-Strahlungsdaten](https://open-meteo.com/en/docs)). Dächer gleicher
Geometrie teilen weiter einen Abruf, auch bei unterschiedlichen Profilen.

Für jede Probe wird der positive Direktanteil auf der geneigten Dachfläche als
`DNI × cos(Einfallswinkel)` geschätzt; unterhalb des geometrischen Horizonts gibt
es keinen Direktanteil. Die Projektion folgt der üblichen
[Direktstrahlungsgeometrie](https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.irradiance.beam_component.html).
Der konservativ erhaltene diffuse Anteil ist
`min(GTI, DHI × (1 + cos(Dachneigung)) / 2)`; Grundlage ist die
[isotrope Himmelsnäherung](https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.irradiance.isotropic.html).

Vom GTI abziehbar ist höchstens der kleinere Wert aus dem mittleren projizierten
Direktanteil und dem GTI oberhalb dieses diffusen Restes. Abgezogen wird davon der
Anteil der blockierten Direktproben. Das Ergebnis bleibt mindestens null.
DNI und horizontales DHI werden somit nicht ungeprüft direkt vom geneigten GTI
abgezogen. Nicht zugeordnete Reststrahlung bleibt erhalten. Das ist bewusst keine
Rekonstruktion des vollständigen Open-Meteo-Transpositionsmodells.

Fehlen einzelne DNI-/DHI-Werte, bleibt das ursprüngliche GTI unverändert und das
Intervall erhält `horizon_input_fallback`. Die Karte zeigt die eingeschränkten
Eingaben. Fehlende oder falsch strukturierte Zusatzreihen erzeugen einen
kontrollierten Abruffehler. Ein unverschattetes Dach, das denselben Abruf nutzt,
erhält allein deswegen keinen zusätzlichen Eingabefallback in der Berechnung.

Erst danach folgen das bisherige kWp-Modell, Temperatur und pauschale Verluste,
ein gegebenenfalls freigegebener Anlagenfaktor sowie Gruppen- und Gesamtclipping.
Die reine Berechnung liegt in `shading.py` und wird in `calculations.py` verwendet.

## Aufwand und Nutzen gegenüber einem konstanten Faktor

Das Opt-in benötigt 12 oder 24 bewusst beschaffte Werte und zwei zusätzliche
Wettervariablen je betroffener Geometrie. Die Zahl der HTTP-Abrufe ändert sich
nicht; die Antwort wird größer. Bei sieben Tagen entstehen je aktivem Dach
2.016 Sonnenstandsproben pro normalem Update. Neue Bibliotheken, dauerhafte
Sonnenstandscaches oder Speicherdateien entstehen nicht.

Ein deterministischer Vergleich bei 50° Nord zeigt den unterschiedlichen
Mechanismus: Ein rundum 30° hoher Horizont blockiert die Wintermittagssonne,
aber nicht die Sommermittagssonne. Bei einem 35° geneigten Süddach, GTI 600,
DNI 900 und DHI 100 W/m² bleiben im geprüften Winterintervall rund 90,96 W/m²,
im Sommerintervall die vollen 600 W/m². Ein gleichbleibender Verlust oder
Anlagenfaktor kann diese beiden Verhältnisse nicht gleichzeitig abbilden.
Die Werte sind synthetisch und belegen **keine gemessene Verbesserung**.

Eine reale Vergleichsstudie gegen das bisherige Rohmodell und die freigegebene
Kalibrierung auf denselben späteren Messintervallen steht aus. Deshalb bleibt
auch die Kartendarstellung ausdrücklich experimentell. Die Aussage aus der alten
Issue-Skizze, die vorhandene Selbstkalibrierung lerne zeitaufgelöste Verschattung,
trifft nicht auf deren implementierten einzelnen Anlagenfaktor zu.

Das Profil beschreibt keine partielle Nahverschattung, Bäume, einzelne Module,
Strings oder Bypassdioden. Es modelliert auch keine diffuse Himmelsverdeckung oder
Bodenreflexion hinter Hindernissen. Refraktion, die grobe Zeitabtastung, konstantes
stündliches DNI und die vereinfachte diffuse Behandlung begrenzen die Aussage,
insbesondere nahe Sonnenauf-/untergang und an scharfen Hinderniskanten.

## Vergleichsdaten und Nachweise

Ein wirksames Profil und seine Regelversion erweitern die physische
Konfigurationskennung. Profiländerung oder Entfernen eines aktiven Profils beginnt
eine neue Vergleichsgrundlage; bisherige Lernfreigaben werden nicht übertragen.
Nullprofile ändern die Kennung nicht. Bereits archivierte DC-Basen werden weder
neu berechnet noch umgedeutet. Config Entries bleiben bei Schema 1.1; Archiv 6,
Mess-Store 2 und Lern-Store 1 bleiben unverändert.

`get_forecast` ergänzt `horizon_shading` einschließlich Regelversion, Aktivierung,
Experimentstatus und fehlendem realem Verbesserungsnachweis. Die Kartenansicht
verwendet diese Metadaten ohne eigene Strahlungsberechnung.

Offline-Tests prüfen unter anderem Winkelkonvention und Nordübergang, den
veröffentlichten [NREL-SPA-Referenzfall](https://docs.nlr.gov/docs/fy08osti/34302.pdf)
mit zur NOAA-Näherung passender Toleranz, Winter/Sommer, Polarnacht, diffuse Reste,
Teilintervalle, UTC/DST, fehlende Werte, unterschiedliche Dächer sowie Kalibrierung
vor beiden Clippingstufen. Regressionsprüfungen sichern den unveränderten Standard
und Nullprofile. Die Kartenscreenshots bei 360 px zeigen synthetische Daten:
[Hell](images/karte-horizontprofil-mobil-light.png),
[Dunkel](images/karte-horizontprofil-mobil-dark.png).
