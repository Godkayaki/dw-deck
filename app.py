import io
import json
import os
import re
import zipfile
from collections import defaultdict
from urllib.parse import urlparse

import requests
from flask import Flask, render_template, request, jsonify, send_file, send_from_directory

app = Flask(__name__)

# The logo assets live in a top-level `logo/` folder (sibling to app.py),
# not inside `static/`, so Flask's default static handling won't serve
# them — they need their own route.
LOGO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo")

SCRYFALL_COLLECTION_URL = "https://api.scryfall.com/cards/collection"
SCRYFALL_CARDS_URL = "https://api.scryfall.com/cards"
MOXFIELD_API = "https://api2.moxfield.com/v2/decks/all/{deck_id}"

HEADERS = {
    "User-Agent": "MTG Card Image Downloader/1.1 (Flask local app)",
    "Accept": "application/json",
}

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


def clean_name(name):
    name = name.strip()
    name = re.sub(r"\s+\[[A-Za-z0-9]+\]\s*\d+\s*$", "", name)
    name = re.sub(r"\s+\([A-Za-z0-9]+\)\s*\d+\s*$", "", name)
    return name.strip()


def card_identity(card):
    """
    A hashable key identifying exactly which physical printing a card
    entry refers to (when known), so two different printings of the same
    card name are tracked, fetched, and rendered as separate entries
    instead of being collapsed into one generic name-based lookup.

    Preference order: an exact Scryfall id (Moxfield's representative
    printing for the entry) > a specific set + collector number (from a
    Moxfield printingData split) > falling back to name-only, which is
    what plain pasted decklists use.
    """
    scryfall_id = card.get("scryfall_id")
    if scryfall_id:
        return ("id", scryfall_id)

    set_code = card.get("set")
    number = card.get("collector_number")
    if set_code and number:
        return ("print", str(set_code).lower(), str(number))

    return ("name", card["name"].casefold())


def has_pinned_printing(card):
    """True when a card entry already points at one exact Scryfall printing."""
    return card_identity(card)[0] in ("id", "print")


def expand_mdfcs(cards):
    """
    Expand double-faced/deadly split entries such as:
      Sink into Stupor // Soporific Springs

    into two independent lookup/display/download entries, retaining the
    original quantity on both faces.
    """
    expanded = []
    for card in cards:
        # When we already know the exact printing (from Moxfield's
        # scryfall_id or a set+collector_number split), there's no need
        # to guess at face names from the combined "A // B" string — the
        # ID/print lookup resolves the full card object, both faces
        # included, on its own.
        if has_pinned_printing(card):
            expanded.append(card)
            continue

        parts = [clean_name(p) for p in card["name"].split("//")]
        parts = [p for p in parts if p]
        if len(parts) <= 1:
            expanded.append(card)
        else:
            for part in parts:
                expanded.append({
                    "name": part,
                    "quantity": card["quantity"],
                    "source_name": card["name"],
                    "face_of": card["name"],
                    "board": card.get("board", "main"),
                })
                break
    return expanded


SECTION_HEADERS = {
    "commander": "commander", "commanders": "commander",
    "mainboard": "main", "deck": "main", "companion": "main",
    "sideboard": "side", "maybeboard": "side",
}

# Headers that just label a type grouping within whichever board is
# currently active (e.g. "Creatures" under a deck list) — they don't
# switch the board, just get skipped.
TYPE_HEADERS = {
    "creatures", "instants", "sorceries", "artifacts", "enchantments",
    "lands", "planeswalkers", "battles", "tokens",
}


def parse_card_line(line):
    """
    Parse one decklist line into (quantity, name, set_code, collector_number).
    set_code/collector_number are None when the line doesn't pin a specific
    printing.

    Handles Moxfield's own export shape:
        1 Urza, Lord High Artificer (H1R) 11 *E*
        4 Counterspell (MMQ) 67
    as well as plain lines with no print info:
        3 Swamp

    The trailing finish marker (*E*, *F*, etc.) is recognized and
    discarded — this app doesn't distinguish foil/etched copies, it just
    downloads the print's art.
    """
    m = re.match(r"^\s*(\d+)\s*x?\s+(.+?)\s*$", line, re.I)
    if not m:
        return None

    qty = int(m.group(1))
    rest = m.group(2)

    # Drop a trailing foil/finish marker such as "*E*" or "*F*".
    rest = re.sub(r"\s*\*[A-Za-z]+\*\s*$", "", rest).strip()

    # "Name (SET) 123" or "Name [SET] 123" at the end of the line.
    print_match = re.search(
        r"^(.*?)\s+[\(\[]([A-Za-z0-9]{2,8})[\)\]]\s+([A-Za-z0-9\-]+)\s*$",
        rest,
    )
    if print_match:
        name = clean_name(print_match.group(1))
        set_code = print_match.group(2).lower()
        number = print_match.group(3)
    else:
        name = clean_name(rest)
        set_code = None
        number = None

    return qty, name, set_code, number


def parse_decklist(text):
    # Keyed by (board, name, set, collector_number) so the same card name
    # is tracked separately if it shows up in both the mainboard and the
    # sideboard, or pinned to two different printings within one board.
    counts = defaultdict(int)
    representative = {}
    board = "main"

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        header = line.lower().rstrip(":")
        if header in SECTION_HEADERS:
            board = SECTION_HEADERS[header]
            continue
        if header in TYPE_HEADERS:
            continue

        parsed = parse_card_line(line)
        if not parsed:
            continue

        qty, name, set_code, number = parsed
        if qty <= 0 or not name:
            continue

        key = (board, name.casefold(), set_code, number)
        counts[key] += qty
        representative[key] = (name, set_code, number)

    if not counts:
        raise ValueError("No cards were found. Use lines such as '4 Lightning Bolt'.")

    result = []
    for key, qty in counts.items():
        board_val = key[0]
        name, set_code, number = representative[key]
        entry = {"name": name, "quantity": qty, "board": board_val}
        if set_code and number:
            entry["set"] = set_code
            entry["collector_number"] = number
        result.append(entry)
    return result


def moxfield_deck_id(url):
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"moxfield.com", "www.moxfield.com"}:
        raise ValueError("That is not a Moxfield URL.")

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0].lower() != "decks":
        raise ValueError(
            "Expected a Moxfield deck URL such as https://moxfield.com/decks/ABC123."
        )

    return parts[1]


def _extract_cards_from_board(board):
    """
    Extract card entries from a Moxfield board.

    Most entries carry one quantity plus one representative printing
    (card.scryfall_id). But when someone splits their copies of the same
    card across different physical printings inside Moxfield, the entry
    instead carries a `printingData` list — one {quantity, set, cn} group
    per printing actually used. When that's present, expand it into one
    entry per printing instead of collapsing everything onto the single
    representative scryfall_id, which would silently discard the other
    printings the person picked.
    """
    entries = []

    def add_entry(obj, fallback_qty=1):
        if not isinstance(obj, dict):
            return False

        card = obj.get("card")

        if not isinstance(card, dict):
            name = obj.get("name")
            if not name:
                return False
            qty = obj.get("quantity", fallback_qty)
            if not isinstance(qty, (int, float)):
                qty = fallback_qty
            entries.append({"name": str(name), "quantity": int(qty)})
            return True

        name = card.get("name")
        if not name:
            return False
        name = str(name)

        total_qty = obj.get("quantity", fallback_qty)
        if not isinstance(total_qty, (int, float)):
            total_qty = fallback_qty
        total_qty = int(total_qty)

        printing_data = obj.get("printingData") or obj.get("printing_data")
        if isinstance(printing_data, list) and printing_data:
            used_qty = 0
            for p in printing_data:
                if not isinstance(p, dict):
                    continue
                p_qty = p.get("quantity", 0)
                if not isinstance(p_qty, (int, float)) or p_qty <= 0:
                    continue
                set_code = p.get("set")
                number = p.get("cn") or p.get("number")
                if not (set_code and number):
                    continue
                entries.append({
                    "name": name,
                    "quantity": int(p_qty),
                    "set": set_code,
                    "collector_number": str(number),
                })
                used_qty += int(p_qty)

            # Any copies not accounted for in printingData fall back to
            # the entry's single representative printing.
            remainder = total_qty - used_qty
            if remainder > 0:
                scryfall_id = card.get("scryfall_id") or card.get("scryfallId")
                entries.append({
                    "name": name,
                    "quantity": remainder,
                    "scryfall_id": scryfall_id,
                })
            return True

        scryfall_id = card.get("scryfall_id") or card.get("scryfallId")
        entries.append({
            "name": name,
            "quantity": total_qty,
            "scryfall_id": scryfall_id,
        })
        return True

    if isinstance(board, dict):
        # Common Moxfield shape: {"card-id": {"card": {...}, "quantity": 4}}
        for key, value in board.items():
            if add_entry(value):
                continue
            if isinstance(value, dict):
                add_entry(value.get("card"), value.get("quantity", 1))

    elif isinstance(board, list):
        for item in board:
            add_entry(item)

    return entries


def _get_board(payload, name):
    """
    Moxfield has used a couple of response shapes: a flat top-level board
    (payload["mainboard"] is itself the card map), and a nested one
    (payload["boards"]["mainboard"]["cards"] is the card map, with
    "count" alongside it). Handle both.
    """
    board = payload.get(name)
    if board is not None:
        return board

    boards = payload.get("boards")
    if isinstance(boards, dict):
        nested = boards.get(name)
        if isinstance(nested, dict) and "cards" in nested:
            return nested.get("cards")
        return nested

    return None


def extract_moxfield_cards(payload):
    """
    Import the commanders (if any), the mainboard, plus the sideboard when
    the deck is not Commander.

    Moxfield has changed response shapes over time, so board extraction accepts
    both dictionaries and lists. Commander/EDH detection is intentionally
    tolerant of several likely format fields.
    """
    mainboard = _get_board(payload, "mainboard")
    sideboard = _get_board(payload, "sideboard")
    commanders = _get_board(payload, "commanders")

    if mainboard is None:
        raise ValueError("Moxfield returned a deck without a readable mainboard.")

    cards = _extract_cards_from_board(mainboard)
    for c in cards:
        c["board"] = "main"

    if commanders:
        commander_cards = _extract_cards_from_board(commanders)
        for c in commander_cards:
            c["board"] = "commander"
        cards = commander_cards + cards

    # Detect Commander/EDH from common Moxfield format fields.
    format_values = []
    for key in ("format", "game", "deckFormat"):
        value = payload.get(key)
        if value:
            format_values.append(str(value).lower())

    fmt = " ".join(format_values)
    is_commander = any(
        token in fmt
        for token in ("commander", "edh", "brawl", "historicbrawl")
    )

    # If Moxfield exposes the format as an object.
    if isinstance(payload.get("format"), dict):
        fmt_obj = payload["format"]
        fmt_text = " ".join(str(v).lower() for v in fmt_obj.values())
        is_commander = is_commander or "commander" in fmt_text or "edh" in fmt_text

    if not is_commander and sideboard is not None:
        side_cards = _extract_cards_from_board(sideboard)
        for c in side_cards:
            c["board"] = "side"
        cards.extend(side_cards)

    if not cards:
        raise ValueError("Moxfield returned a deck, but no mainboard cards could be read.")

    # Merge identical entries that might occur across boards, keeping
    # commander/mainboard/sideboard counts separate — and keeping distinct
    # printings of the same card name separate too, so a deck that splits
    # its copies across different prints (e.g. 2x old-border Forest + 2x
    # new-border Forest) surfaces both instead of collapsing into one.
    merged = defaultdict(int)
    representative = {}
    for card in cards:
        key = (card["board"], card_identity(card))
        merged[key] += card["quantity"]
        representative[key] = card

    result = []
    for (board, ident), qty in merged.items():
        base = representative[(board, ident)]
        entry = {"name": base["name"], "quantity": qty, "board": board}
        if base.get("scryfall_id"):
            entry["scryfall_id"] = base["scryfall_id"]
        if base.get("set") and base.get("collector_number"):
            entry["set"] = base["set"]
            entry["collector_number"] = base["collector_number"]
        result.append(entry)

    return result


def fetch_moxfield(url):
    deck_id = moxfield_deck_id(url)
    endpoint = MOXFIELD_API.format(deck_id=deck_id)

    r = requests.get(
        endpoint,
        headers={**HEADERS, "Referer": "https://moxfield.com/"},
        timeout=20,
    )
    if r.status_code != 200:
        raise ValueError(
            f"Moxfield could not be read (HTTP {r.status_code}). "
            "Make sure the deck is public."
        )

    return extract_moxfield_cards(r.json())


def scryfall_collection(cards):
    result = []
    missing = []

    for start in range(0, len(cards), 75):
        batch = cards[start:start + 75]
        identifiers = []
        label_by_identifier = {}

        for card in batch:
            ident = card_identity(card)
            if ident[0] == "id":
                identifier = {"id": ident[1]}
            elif ident[0] == "print":
                identifier = {"set": ident[1], "collector_number": ident[2]}
            else:
                identifier = {"name": card["name"]}

            identifiers.append(identifier)
            # not_found echoes back exactly the identifier we sent, so this
            # lets us translate it back to a readable card name below.
            label_by_identifier[json.dumps(identifier, sort_keys=True)] = card["name"]

        r = requests.post(
            SCRYFALL_COLLECTION_URL,
            json={"identifiers": identifiers},
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()

        result.extend(data.get("data", []))
        for item in data.get("not_found", []):
            key = json.dumps(item, sort_keys=True)
            missing.append(
                label_by_identifier.get(key, item.get("name") or item.get("id") or "Unknown card")
            )

    return result, missing


def prefer_english_printing(card):
    """
    Moxfield lets someone pin a specific printing (or per-copy split of
    printings), which can point at a foreign-language print. We only want
    non-English art when it's genuinely the only print available, so swap
    in the English version of that exact printing (same set + collector
    number) when Scryfall has one; otherwise keep what was pinned.
    """
    if not isinstance(card, dict) or card.get("lang", "en") == "en":
        return card

    set_code = card.get("set")
    number = card.get("collector_number")
    if not set_code or not number:
        return card

    try:
        r = requests.get(
            f"{SCRYFALL_CARDS_URL}/{set_code}/{number}/en",
            headers=HEADERS,
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
    except requests.RequestException:
        pass

    return card


def type_bucket(card):
    # For MDFC/transform cards, Scryfall's top-level type_line is the
    # combination of both faces (e.g. "Creature — Homunculus // Land"), so
    # checking it directly would misclassify cards like Hydroelectric
    # Specimen // Hydroelectric Laboratory as a land just because their
    # back face is one. Bucket by the front face's own type line instead.
    # Single-faced cards (including land Sagas like Urza's Saga) have no
    # card_faces, so they fall back to the normal top-level type_line.
    faces = card.get("card_faces") or []
    type_line = faces[0].get("type_line", "") if faces else card.get("type_line", "")

    # Lands take priority over every other type on the line. Cards like
    # Urza's Saga ("Enchantment Land") or artifact lands should always end
    # up in the Lands section, not Enchantments/Artifacts, regardless of
    # where "Land" falls in CARD_TYPES below.
    if "Land" in type_line:
        return "Lands"

    for label, token in CARD_TYPES:
        if token and token in type_line:
            return label
    return "Other"


def card_image_url(card):
    if card.get("image_uris", {}).get("png"):
        return card["image_uris"]["png"]

    for face in card.get("card_faces", []):
        if face.get("image_uris", {}).get("png"):
            return face["image_uris"]["png"]

    return None


def card_face_images(card):
    """
    Return every distinct printable face of a card as (name, image_url).

    Single-faced cards (and old-style split/transform cards that only carry
    one shared image) yield a single entry. Modal DFCs, transform cards, and
    split cards with per-face art (e.g. Hydroelectric Specimen //
    Hydroelectric Laboratory) yield one entry per face, each with its own
    artwork and its own name, so both sides can be downloaded independently
    even though the preview only ever shows the front face.
    """
    faces = card.get("card_faces") or []

    face_images = [
        (face.get("name") or card.get("name"), face["image_uris"]["png"])
        for face in faces
        if face.get("image_uris", {}).get("png")
    ]

    if len(face_images) >= 2:
        return face_images

    url = card_image_url(card)
    return [(card.get("name"), url)] if url else []


def slug_filename(name):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return value or "card"


def scryfall_slug(value):
    """Lowercase, hyphen-separated slug — matches how Scryfall itself
    formats a card name within its own download filenames."""
    value = re.sub(r"[^A-Za-z0-9]+", "-", (value or "").lower())
    return value.strip("-") or "card"


def scryfall_filename(face):
    """
    Build a filename in the same shape Scryfall uses for its own card
    downloads: "<set>-<collector-number>-<name-slug>", e.g. Scryfall's
    own Hullbreaker Horror download from Innistrad Remastered is named
    "inr-357-hullbreaker-horror". Falls back to just the name slug when
    set/collector number aren't known (plain pasted decklists have no
    pinned printing to pull them from).
    """
    set_code = (face.get("set_code") or "").lower()
    number = str(face.get("collector_number") or "")
    name_slug = scryfall_slug(face.get("name") or "card")
    return "-".join(part for part in (set_code, number, name_slug) if part)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/logo/<path:filename>")
def logo(filename):
    return send_from_directory(LOGO_DIR, filename)


@app.post("/api/preview")
def preview():
    try:
        source = request.form.get("source", "list").strip()

        if source == "moxfield":
            raw = request.form.get("moxfield_url", "").strip()
            if not raw:
                raise ValueError("Enter a Moxfield deck URL.")
            cards = fetch_moxfield(raw)
        else:
            cards = parse_decklist(request.form.get("decklist", ""))

        # Expand MDFCs before asking Scryfall to resolve names.
        cards = expand_mdfcs(cards)

        # Aggregate quantity per (printing identity, board) — the same
        # card name can legitimately appear in both the mainboard and the
        # sideboard with separate counts (e.g. a Companion), and can also
        # appear as two different physical printings within the same
        # board (Moxfield's per-copy printingData split) — those need to
        # stay as separate rows too, not merged into one generic lookup.
        quantities = defaultdict(int)
        for c in cards:
            key = (card_identity(c), c.get("board", "main"))
            quantities[key] += c["quantity"]

        # Only fetch each unique printing from Scryfall once, even if it
        # appears on both boards.
        seen = set()
        unique_cards = []
        for c in cards:
            ident = card_identity(c)
            if ident not in seen:
                seen.add(ident)
                unique_cards.append(c)

        scryfall_cards, missing = scryfall_collection(unique_cards)

        # Index results three ways so any of the identity kinds above can
        # find its match:
        #  - by Scryfall id (Moxfield's pinned printing)
        #  - by (set, collector_number) (a Moxfield printingData split)
        #  - by name/face name (plain decklists, or MDFC/transform/split/
        #    Adventure/Omen cards, which Scryfall returns under their full
        #    combined name even when requested by a single face's name)
        scryfall_by_id = {}
        scryfall_by_print = {}
        scryfall_by_name = {}
        for card in scryfall_cards:
            cid = card.get("id")
            if cid:
                scryfall_by_id[cid] = card

            set_code, number = card.get("set"), card.get("collector_number")
            if set_code and number:
                scryfall_by_print[(str(set_code).lower(), str(number))] = card

            names = {card.get("name", "")}
            for face in card.get("card_faces") or []:
                if face.get("name"):
                    names.add(face["name"])
            for name in names:
                scryfall_by_name.setdefault(name.casefold(), card)

        # Moxfield can pin a specific, possibly foreign-language, printing.
        # Swap in the English version of that exact printing where one
        # exists (keeping the same lookup keys above, so entries built
        # from `quantities` below still resolve correctly).
        for cid in list(scryfall_by_id):
            scryfall_by_id[cid] = prefer_english_printing(scryfall_by_id[cid])
        for print_key in list(scryfall_by_print):
            scryfall_by_print[print_key] = prefer_english_printing(scryfall_by_print[print_key])

        rendered = []
        for (ident, board), qty in quantities.items():
            kind = ident[0]
            if kind == "id":
                card = scryfall_by_id.get(ident[1])
            elif kind == "print":
                card = scryfall_by_print.get((ident[1], ident[2]))
            else:
                card = scryfall_by_name.get(ident[1])

            if not card:
                continue
            faces = card_face_images(card)

            rendered.append({
                "id": card.get("id"),
                "name": card.get("name"),
                "quantity": qty,
                "board": board,
                "type_line": card.get("type_line", ""),
                "type": type_bucket(card),
                "mana_value": card.get("cmc", 0) or 0,
                # Front-only image for the preview grid, so MDFCs still
                # render as a single card instead of two.
                "image": faces[0][1] if faces else None,
                # Every face (1 for normal cards, 2 for MDFC/transform/split
                # cards), each with its own name + art, used by /api/download
                # so both sides get written to the ZIP. set_code/collector_number
                # ride along so downloads can name files the way Scryfall does.
                "faces": [
                    {
                        "name": n,
                        "image": u,
                        "set_code": card.get("set", ""),
                        "collector_number": card.get("collector_number", ""),
                    }
                    for n, u in faces
                ],
                "set": card.get("set_name", ""),
                "collector_number": card.get("collector_number", ""),
            })

        type_order = {name: i for i, (name, _) in enumerate(CARD_TYPES)}
        board_order = {"commander": 0, "main": 1, "side": 2}
        rendered.sort(key=lambda c: (
            board_order.get(c["board"], 1),
            # Mainboard cards are grouped by type in the UI, so sort by
            # type first. Commander and Sideboard cards are shown as flat,
            # uncategorized lists, so type shouldn't affect their order —
            # just mana value, then name.
            type_order.get(c["type"], 999) if c["board"] == "main" else 0,
            c["mana_value"],
            c["name"].lower(),
        ))

        sideboard_count = sum(c["quantity"] for c in rendered if c["board"] == "side")

        return jsonify({
            "cards": rendered,
            "missing": missing,
            "total_cards": sum(c["quantity"] for c in rendered),
            "unique_cards": len(rendered),
            "mainboard_total_cards": sum(c["quantity"] for c in rendered) - sideboard_count,
            "sideboard_total_cards": sideboard_count,
            "has_sideboard": sideboard_count > 0,
        })

    except requests.HTTPError as e:
        return jsonify({"error": f"Scryfall request failed: {e}"}), 502
    except requests.RequestException as e:
        return jsonify({"error": f"Network error: {e}"}), 502
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        app.logger.exception("Preview failed")
        return jsonify({"error": f"Unexpected error: {e}"}), 500


@app.post("/api/download")
def download():
    try:
        payload = request.get_json(force=True)
        cards = payload.get("cards", [])
        if not cards:
            return jsonify({"error": "Preview the deck first."}), 400

        memory_file = io.BytesIO()

        with zipfile.ZipFile(
            memory_file,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as zf:
            manifest = [
                "name,face,copy_number,type,set,collector_number,filename"
            ]
            image_cache = {}

            def fetch_image(url):
                if url not in image_cache:
                    r = requests.get(
                        url,
                        headers={"User-Agent": HEADERS["User-Agent"]},
                        timeout=60,
                    )
                    r.raise_for_status()
                    image_cache[url] = r.content
                return image_cache[url]

            for card in cards:
                name = card.get("name", "card")
                quantity = int(card.get("quantity", 1))

                # "faces" carries one entry per printable side (2 for MDFC /
                # transform / split cards like Hydroelectric Specimen //
                # Hydroelectric Laboratory). Fall back to the old single
                # "image" field for compatibility with older preview data.
                faces = card.get("faces") or (
                    [{"name": name, "image": card.get("image")}]
                    if card.get("image") else []
                )
                is_multi_face = len(faces) > 1

                for face in faces:
                    image_url = face.get("image")
                    face_name = face.get("name") or name
                    if not image_url:
                        continue

                    content = fetch_image(image_url)
                    base = scryfall_filename(face)

                    # IMPORTANT: write one PNG for EVERY copy, per face.
                    # A 4x Hydroelectric Specimen // Hydroelectric Laboratory
                    # therefore becomes 4 front PNGs + 4 back PNGs.
                    for copy_number in range(1, quantity + 1):
                        filename = f"{base}-{copy_number:02d}.png"
                        zf.writestr(f"cards/{filename}", content)

                        manifest.append(
                            ",".join([
                                '"' + name.replace('"', '""') + '"',
                                '"' + (face_name if is_multi_face else "").replace('"', '""') + '"',
                                str(copy_number),
                                '"' + card.get("type", "").replace('"', '""') + '"',
                                '"' + card.get("set", "").replace('"', '""') + '"',
                                '"' + card.get("collector_number", "").replace('"', '""') + '"',
                                '"' + filename + '"',
                            ])
                        )

            zf.writestr("manifest.csv", "\n".join(manifest))

        memory_file.seek(0)

        return send_file(
            memory_file,
            as_attachment=True,
            download_name="mtg-card-images.zip",
            mimetype="application/zip",
        )

    except requests.RequestException as e:
        return jsonify({"error": f"Could not download a card image: {e}"}), 502
    except Exception as e:
        app.logger.exception("Download failed")
        return jsonify({"error": f"Download failed: {e}"}), 500


@app.post("/api/download_card")
def download_card():
    """
    Download a single card from the hover button on a card tile. Always
    fetches exactly one copy of each face, regardless of how many copies
    are in the deck — this is "grab this card", not "grab my playset".
    """
    try:
        payload = request.get_json(force=True)
        card = payload.get("card")
        if not card:
            return jsonify({"error": "No card provided."}), 400

        name = card.get("name", "card")

        faces = card.get("faces") or (
            [{"name": name, "image": card.get("image")}]
            if card.get("image") else []
        )
        faces = [f for f in faces if f.get("image")]

        if not faces:
            return jsonify({"error": "No image available for this card."}), 400

        # Single-faced card: hand back a bare PNG, no zip needed.
        if len(faces) == 1:
            r = requests.get(
                faces[0]["image"],
                headers={"User-Agent": HEADERS["User-Agent"]},
                timeout=60,
            )
            r.raise_for_status()

            buffer = io.BytesIO(r.content)
            buffer.seek(0)

            return send_file(
                buffer,
                as_attachment=True,
                download_name=f"{scryfall_filename(faces[0])}.png",
                mimetype="image/png",
            )

        # MDFC/transform/split/Adventure card: zip both faces together.
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_DEFLATED) as zf:
            for face in faces:
                r = requests.get(
                    face["image"],
                    headers={"User-Agent": HEADERS["User-Agent"]},
                    timeout=60,
                )
                r.raise_for_status()
                filename = f"{scryfall_filename(face)}.png"
                zf.writestr(filename, r.content)

        memory_file.seek(0)

        return send_file(
            memory_file,
            as_attachment=True,
            download_name=f"{slug_filename(name)}.zip",
            mimetype="application/zip",
        )

    except requests.RequestException as e:
        return jsonify({"error": f"Could not download the card image: {e}"}), 502
    except Exception as e:
        app.logger.exception("Single-card download failed")
        return jsonify({"error": f"Download failed: {e}"}), 500


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)