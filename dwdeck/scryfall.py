import json

import requests

from .config import CARD_TYPES, HEADERS, SCRYFALL_CARDS_URL, SCRYFALL_COLLECTION_URL
from .identity import card_identity


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
            elif ident[0] == "set_name":
                identifier = {"name": card["name"], "set": ident[1]}
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