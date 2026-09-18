"""
A card entry's "identity" is what makes two different printings of the
same card name trackable as separate rows instead of collapsing into one
generic name-based lookup. decklist.py, moxfield.py, and scryfall.py all
share this same notion of identity so a card pinned to a printing in one
place stays pinned to it everywhere downstream.
"""


def card_identity(card):
    """
    A hashable key identifying exactly which physical printing a card
    entry refers to (when known), so two different printings of the same
    card name are tracked, fetched, and rendered as separate entries
    instead of being collapsed into one generic name-based lookup.

    Preference order: an exact Scryfall id (Moxfield's representative
    printing for the entry) > a specific set + collector number (from a
    Moxfield printingData split, or a pasted line like "(H1R) 11") > a
    set with no collector number (a pasted line like "(H1R)" with the
    number left off — still enough to pin the printing within that set)
    > falling back to name-only, which is what a plain pasted line with
    no print annotation at all uses.
    """
    scryfall_id = card.get("scryfall_id")
    if scryfall_id:
        return ("id", scryfall_id)

    set_code = card.get("set")
    number = card.get("collector_number")
    if set_code and number:
        return ("print", str(set_code).lower(), str(number))

    if set_code:
        return ("set_name", str(set_code).lower(), card["name"].casefold())

    return ("name", card["name"].casefold())


def has_pinned_printing(card):
    """True when a card entry already points at one exact Scryfall printing."""
    return card_identity(card)[0] in ("id", "print", "set_name")