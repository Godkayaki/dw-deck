import re
from collections import defaultdict

from .identity import has_pinned_printing


def clean_name(name):
    name = name.strip()
    name = re.sub(r"\s+\[[A-Za-z0-9]+\]\s*\d+\s*$", "", name)
    name = re.sub(r"\s+\([A-Za-z0-9]+\)\s*\d+\s*$", "", name)
    return name.strip()


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
    a set with no collector number, which still pins the printing to
    that set (just not one exact copy of a multi-printing set — Scryfall
    picks a match within it):
        1 Urza, Lord High Artificer (H1R)
    as well as plain lines with no print info at all:
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
        # "Name (SET)" or "Name [SET]" with no collector number after it.
        set_only_match = re.search(
            r"^(.*?)\s+[\(\[]([A-Za-z0-9]{2,8})[\)\]]\s*$",
            rest,
        )
        if set_only_match:
            name = clean_name(set_only_match.group(1))
            set_code = set_only_match.group(2).lower()
            number = None
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
        if set_code:
            entry["set"] = set_code
            if number:
                entry["collector_number"] = number
        result.append(entry)
    return result


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