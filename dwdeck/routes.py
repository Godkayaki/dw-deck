import io
import zipfile
from collections import defaultdict

import requests
from flask import (
    Blueprint,
    current_app,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)

from .config import CARD_TYPES, HEADERS, LOGO_DIR
from .decklist import expand_mdfcs, parse_decklist
from .filenames import scryfall_filename, slug_filename
from .identity import card_identity
from .moxfield import fetch_moxfield
from .scryfall import card_face_images, prefer_english_printing, scryfall_collection, type_bucket

bp = Blueprint("main", __name__)


@bp.get("/")
def index():
    return render_template("index.html")


@bp.get("/logo/<path:filename>")
def logo(filename):
    return send_from_directory(LOGO_DIR, filename)


@bp.post("/api/preview")
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

        # Index results four ways so any of the identity kinds above can
        # find its match:
        #  - by Scryfall id (Moxfield's pinned printing)
        #  - by (set, collector_number) (a Moxfield printingData split, or
        #    a pasted "(SET) 123" line)
        #  - by (set, name) (a pasted "(SET)" line with no collector
        #    number — pins the set but lets Scryfall pick within it)
        #  - by name/face name (plain decklists, or MDFC/transform/split/
        #    Adventure/Omen cards, which Scryfall returns under their full
        #    combined name even when requested by a single face's name)
        scryfall_by_id = {}
        scryfall_by_print = {}
        scryfall_by_set_name = {}
        scryfall_by_name = {}
        for card in scryfall_cards:
            cid = card.get("id")
            if cid:
                scryfall_by_id[cid] = card

            set_code, number = card.get("set"), card.get("collector_number")
            if set_code and number:
                scryfall_by_print[(str(set_code).lower(), str(number))] = card
            if set_code:
                scryfall_by_set_name[(str(set_code).lower(), card.get("name", "").casefold())] = card

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
        for set_name_key in list(scryfall_by_set_name):
            scryfall_by_set_name[set_name_key] = prefer_english_printing(scryfall_by_set_name[set_name_key])

        rendered = []
        for (ident, board), qty in quantities.items():
            kind = ident[0]
            if kind == "id":
                card = scryfall_by_id.get(ident[1])
            elif kind == "print":
                card = scryfall_by_print.get((ident[1], ident[2]))
            elif kind == "set_name":
                card = scryfall_by_set_name.get((ident[1], ident[2]))
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
        current_app.logger.exception("Preview failed")
        return jsonify({"error": f"Unexpected error: {e}"}), 500


@bp.post("/api/download")
def download():
    try:
        payload = request.get_json(force=True)
        cards = payload.get("cards", [])
        if not cards:
            return jsonify({"error": "Preview the deck first."}), 400

        # Defaults to True so older frontend payloads (or anything that
        # doesn't send the flag) keep getting a manifest, same as before
        # this became optional.
        generate_manifest = bool(payload.get("generate_manifest", True))

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
                    # therefore becomes 4 front PNGs + 4 back PNGs. Only
                    # append a "-01"/"-02" copy suffix when there's actually
                    # more than one copy — a lone card just gets the plain
                    # Scryfall-style filename, no numbering.
                    for copy_number in range(1, quantity + 1):
                        filename = (
                            f"{base}.png" if quantity == 1
                            else f"{base}-{copy_number:02d}.png"
                        )
                        zf.writestr(f"cards/{filename}", content)

                        if generate_manifest:
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

            if generate_manifest:
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
        current_app.logger.exception("Download failed")
        return jsonify({"error": f"Download failed: {e}"}), 500


@bp.post("/api/download_card")
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
        current_app.logger.exception("Single-card download failed")
        return jsonify({"error": f"Download failed: {e}"}), 500