import io
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


def expand_mdfcs(cards):
    """
    Expand double-faced/deadly split entries such as:
      Sink into Stupor // Soporific Springs

    into two independent lookup/display/download entries, retaining the
    original quantity on both faces.
    """
    expanded = []
    for card in cards:
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


def parse_decklist(text):
    # Keyed by (board, name) so the same card name can be tracked
    # separately if it shows up in both the mainboard and the sideboard.
    counts = defaultdict(int)
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

        m = re.match(r"^\s*(\d+)\s*x?\s+(.+?)\s*$", line, re.I)
        if not m:
            continue

        qty = int(m.group(1))
        name = clean_name(m.group(2))

        if qty > 0 and name:
            counts[(board, name)] += qty

    if not counts:
        raise ValueError("No cards were found. Use lines such as '4 Lightning Bolt'.")

    return [
        {"name": name, "quantity": qty, "board": board}
        for (board, name), qty in counts.items()
    ]


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
    """Extract {name, quantity} entries from a Moxfield board."""
    counts = defaultdict(int)

    def add_card(obj, fallback_qty=1):
        if not isinstance(obj, dict):
            return False

        qty = obj.get("quantity", fallback_qty)
        if not isinstance(qty, (int, float)):
            qty = fallback_qty

        card = obj.get("card")
        if isinstance(card, dict):
            name = card.get("name")
        else:
            name = obj.get("name")

        if name:
            counts[str(name)] += int(qty)
            return True
        return False

    if isinstance(board, dict):
        # Common Moxfield shape: {"card-id": {"card": {...}, "quantity": 4}}
        for key, value in board.items():
            if add_card(value):
                continue
            if isinstance(value, dict):
                add_card(value.get("card"), value.get("quantity", 1))

    elif isinstance(board, list):
        for item in board:
            add_card(item)

    return [{"name": name, "quantity": qty} for name, qty in counts.items()]


def extract_moxfield_cards(payload):
    """
    Import the commanders (if any), the mainboard, plus the sideboard when
    the deck is not Commander.

    Moxfield has changed response shapes over time, so board extraction accepts
    both dictionaries and lists. Commander/EDH detection is intentionally
    tolerant of several likely format fields.
    """
    mainboard = payload.get("mainboard")
    sideboard = payload.get("sideboard")
    commanders = payload.get("commanders")

    if mainboard is None:
        # Some responses nest boards under "boards".
        boards = payload.get("boards")
        if isinstance(boards, dict):
            mainboard = boards.get("mainboard")
            sideboard = boards.get("sideboard")
            commanders = boards.get("commanders")

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
    # commander/mainboard/sideboard counts of the same card name separate.
    merged = defaultdict(int)
    for card in cards:
        merged[(card["board"], card["name"])] += card["quantity"]

    return [
        {"name": name, "quantity": qty, "board": board}
        for (board, name), qty in merged.items()
    ]


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
        payload = {"identifiers": [{"name": card["name"]} for card in batch]}

        r = requests.post(
            SCRYFALL_COLLECTION_URL,
            json=payload,
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()

        result.extend(data.get("data", []))
        missing.extend(
            item.get("name") or item.get("id")
            for item in data.get("not_found", [])
        )

    return result, missing


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

        # Aggregate quantity per (name, board) — the same card name can
        # legitimately appear in both the mainboard and the sideboard with
        # separate counts (e.g. a Companion).
        quantities = defaultdict(int)
        for c in cards:
            key = (c["name"].casefold(), c.get("board", "main"))
            quantities[key] += c["quantity"]

        # Only fetch each unique card name from Scryfall once, even if it
        # appears on both boards.
        seen_names = set()
        unique_cards = []
        for c in cards:
            key = c["name"].casefold()
            if key not in seen_names:
                seen_names.add(key)
                unique_cards.append(c)

        scryfall_cards, missing = scryfall_collection(unique_cards)

        # Cards with multiple faces (MDFC, transform, split, Adventure,
        # Omen, ...) are requested by a single face's name (e.g. "Sagu
        # Wildling") but Scryfall returns them under their full combined
        # name (e.g. "Sagu Wildling // Roost Seek"). Index by every name
        # the card could have been requested under so the lookup below
        # always finds it, instead of silently dropping the card.
        scryfall_by_name = {}
        for card in scryfall_cards:
            names = {card.get("name", "")}
            for face in card.get("card_faces") or []:
                if face.get("name"):
                    names.add(face["name"])
            for name in names:
                scryfall_by_name.setdefault(name.casefold(), card)

        rendered = []
        for (name_key, board), qty in quantities.items():
            card = scryfall_by_name.get(name_key)
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
                # so both sides get written to the ZIP.
                "faces": [{"name": n, "image": u} for n, u in faces],
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
                    base = slug_filename(face_name)

                    # IMPORTANT: write one PNG for EVERY copy, per face.
                    # A 4x Hydroelectric Specimen // Hydroelectric Laboratory
                    # therefore becomes 4 front PNGs + 4 back PNGs.
                    for copy_number in range(1, quantity + 1):
                        filename = f"{base}_{copy_number:02d}.png"
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
                download_name=f"{slug_filename(faces[0].get('name') or name)}.png",
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
                filename = f"{slug_filename(face.get('name') or name)}.png"
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