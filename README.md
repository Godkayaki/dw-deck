<p style="text-align:center"> <img src="logo/logo.png" width="60"> </p>

# Dw-Deck

Features:

- Reads both pasted lists and moxfield links.
- Differentiates between EDH and 60 card formats.
- Previews both mainboard and sideboard accordingly for 60 card formats.
- Previews cards grouped by card type.
- Previews MDFC cards with no issues and downloads both frontside and backside of cards.
- Includes a `manifest.csv` file inside the ZIP.
- Zip will also include multiple copies of a card under the "*_05" format.

Extra info:

- Imports only the Moxfield mainboard for Commander/EDH decks.
- Support user inputting moxfield link into the list of cards (smh).

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate       # macOS/Linux
# .venv\Scripts\activate        # Windows

pip install -r requirements.txt
python app.py
```

Runs on http://127.0.0.1:5000 by default.

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
