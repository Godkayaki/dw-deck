import re


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