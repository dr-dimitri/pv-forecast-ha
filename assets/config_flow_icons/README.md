# Bilder für Options- und Config-Flow

Diese SVGs sind die Quelle für die Symbolbilder in den Beschreibungen des
Options Flow (`init`, `add_roof`, `edit_roof`, `remove_roof`, `system`) und
des Abschlussdialogs im Config Flow (`summary`).

Sie werden nicht ausgeliefert, sondern base64-kodiert direkt als
`data:image/svg+xml;base64,…` in die `description`-Felder von
`custom_components/pv_forecast/strings.json`,
`custom_components/pv_forecast/translations/de.json` und
`custom_components/pv_forecast/translations/en.json` eingebettet (alle drei
Dateien bleiben inhaltsgleich).

Palette entnommen aus `custom_components/pv_forecast/brand/icon@2x.png`:

- Hintergrund `#011749`
- Blau (Panel) `#0e77ea`
- Gelb (Sonne) `#fcb607`
- Türkis (Akzent) `#0dcbba`

Nach einer Änderung an einer Datei hier muss die base64-Kodierung in den drei
Übersetzungsdateien manuell aktualisiert werden, z. B.:

```bash
python3 -c "import base64; print(base64.b64encode(open('add_roof.svg','rb').read()).decode())"
```
