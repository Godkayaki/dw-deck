# MTG Card Image Downloader

A Flask web app that:

- accepts a pasted Magic: The Gathering deck list or a public Moxfield deck URL
- resolves cards through the Scryfall API
- previews cards grouped by card type
- expands MDFC entries such as `Sink into Stupor // Soporific Springs`
- imports only the Moxfield mainboard for Commander/EDH decks
- imports mainboard + sideboard for non-Commander formats
- downloads one PNG **per physical copy** into a ZIP
- includes a `manifest.csv`

## Run

```bash
python -m venv .venv
source .venv/bin/activate       # macOS/Linux
# .venv\Scripts\activate        # Windows

pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000

## Download behavior

If the deck contains:

```text
4 Lightning Bolt
```

the ZIP contains:

```text
cards/Lightning_Bolt_01.png
cards/Lightning_Bolt_02.png
cards/Lightning_Bolt_03.png
cards/Lightning_Bolt_04.png
```

For an MDFC:

```text
4 Sink into Stupor // Soporific Springs
```

the preview contains both:

- Sink into Stupor
- Soporific Springs

and the ZIP contains four PNGs of each face (8 PNG files total).

## Moxfield behavior

Moxfield imports are restricted to:

- Commander/EDH: mainboard only
- Other formats: mainboard + sideboard

Moxfield's public API response format can change, so the board parsing is isolated in
`extract_moxfield_cards()` and `_extract_cards_from_board()`.
