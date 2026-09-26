from collections import defaultdict
from urllib.parse import urlparse

import requests

from .config import MOXFIELD_API
from .identity import card_identity


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


# Moxfield's API sits behind Cloudflare-style bot protection, and our
# generic "MTG Card Image Downloader" User-Agent (fine for Scryfall,
# which has no such protection) reads as an obvious script rather than a
# browser. Impersonating a real browser's request headers here is often
# enough to get past a basic UA check — though if the block is instead
# based on IP reputation (common for cloud/datacenter IPs, which is what
# most hosting platforms use), no header combination will fix it.
MOXFIELD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://moxfield.com",
    "Referer": "https://moxfield.com/",
}


def fetch_moxfield(url):
    deck_id = moxfield_deck_id(url)
    endpoint = MOXFIELD_API.format(deck_id=deck_id)

    r = requests.get(endpoint, headers=MOXFIELD_HEADERS, timeout=20)

    if r.status_code == 403:
        raise ValueError(
            "Moxfield blocked this request (HTTP 403). This is usually "
            "Moxfield's bot protection rejecting requests from this "
            "server's IP address, rather than anything wrong with the "
            "deck or the URL."
        )
    if r.status_code != 200:
        raise ValueError(
            f"Moxfield could not be read (HTTP {r.status_code}). "
            "Make sure the deck is public."
        )

    return extract_moxfield_cards(r.json())