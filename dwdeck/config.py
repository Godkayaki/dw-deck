import os

# Project root (the folder app.py lives in) — two levels up from this
# file (mtg_card_downloader/mtgdl/config.py -> mtg_card_downloader/).
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The logo assets live in a top-level `logo/` folder (sibling to app.py),
# not inside `static/`, so Flask's default static handling won't serve
# them — they get their own route in routes.py.
LOGO_DIR = os.path.join(BASE_DIR, "logo")

SCRYFALL_COLLECTION_URL = "https://api.scryfall.com/cards/collection"
SCRYFALL_CARDS_URL = "https://api.scryfall.com/cards"
MOXFIELD_API = "https://api2.moxfield.com/v2/decks/all/{deck_id}"

HEADERS = {
    "User-Agent": "MTG Card Image Downloader/1.1 (Flask local app)",
    "Accept": "application/json",
}

# Display/priority order for card type sections in the preview UI.
# type_bucket() in scryfall.py walks this in order (after the
# lands-always-win special case) to decide which section a card lands in.
CARD_TYPES = [
    ("Creatures", "Creature"),
    ("Planeswalkers", "Planeswalker"),
    ("Instants", "Instant"),
    ("Sorceries", "Sorcery"),
    ("Artifacts", "Artifact"),
    ("Enchantments", "Enchantment"),
    ("Lands", "Land"),
    ("Battles", "Battle"),
    ("Kindreds", "Kindred"),
    ("Other", None),
]