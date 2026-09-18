<p align="center"> <img src="logo/logo.png" width="120"> </p>

# Dw-Deck

**Features:**

- Reads both pasted lists and moxfield links.
- Differentiates between EDH and 60 card formats.
- Previews both mainboard and sideboard accordingly for 60 card formats.
- Previews cards grouped by card type.
- Previews MDFC cards with no issues and downloads both frontside and backside of cards.
- Includes a `manifest.csv` file inside the ZIP.
- Zip will also include multiple copies of a card under the **"*_05"** format.

**Extra info:**

- Imports only the Moxfield mainboard for Commander/EDH decks.
- Support user inputting moxfield link into the list of cards (smh).
- Moxfield lists need to be *public*, or at least *unlisted*.

## Preview

<img src="https://i.imgur.com/79HIB9I.png">

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

**If the deck contains:**

```text
4 Lightning Bolt
```

The ZIP file would contain 4 copies of Lightning Bolt with the "*_04" format;

```text
cards/Lightning_Bolt_01.png
cards/Lightning_Bolt_02.png
cards/Lightning_Bolt_03.png
cards/Lightning_Bolt_04.png
```

**For an MDFC:**

```text
4 Sink into Stupor // Soporific Springs
```

If you would download 4 copies of *Sink into Stupor // Soporific Springs*, the ZIP would contain four PNGs of each face (8 PNG files total);

```text
cards/Sink_into_Stupor_01.png
cards/Sink_into_Stupor_02.png
cards/Sink_into_Stupor_03.png
cards/Sink_into_Stupor_04.png
cards/Soporific_Springs_01.png
cards/Soporific_Springs_02.png
cards/Soporific_Springs_03.png
cards/Soporific_Springs_04.png
```

## WIP

- ~~Add "go to the top" button~~
- Add support to change the desired print of the card.
- ~~Be able to download a single card from the preview.~~
- ~~Add support for printings from Moxfield.~~
- ~~Add support for printings when pasting lists.~~ (Still needs to allow for text with no set number)
- Language support (*this seems kinda unrealistic*).
- Archidekt support.
- For some reason buttons overlap the text written, to be fixed.
- Add checkbox that indicates if the download should download multiple copies of the same card if existing in the deck.