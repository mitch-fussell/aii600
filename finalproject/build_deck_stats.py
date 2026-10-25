#!/usr/bin/env python3
"""Turn a folder of decklists (.txt) into a CSV of deck-level stats.

Decklist format (MTGO / Arena export): lines like "4 Lightning Strike".
Mainboard is everything before a blank line or a line starting with "Sideboard".
A "Companion" section (Arena puts it before "Deck") is skipped, and lines
like "Deck", "About", "Name ..." or "//comment" are ignored.

Card data comes from Scryfall's Oracle Cards bulk file. Either pass a
local copy with --cards, or let the script download it (needs internet;
the download is cached as oracle-cards.json and reused on later runs).

Usage:
    python3 build_deck_stats.py --decks decklists/ --out decks.csv
    python3 build_deck_stats.py --decks decklists/ --cards oracle-cards.json
"""
import argparse, csv, json, re, sys, urllib.request
from pathlib import Path

COLORS = "WUBRG"
LINE = re.compile(r"^\s*(\d+)\s+(.+?)\s*(?:\([A-Za-z0-9]+\)\s*\S*)?\s*$")
# Oracle bulk data also holds tokens and art cards that can share a real card's name
SKIP_LAYOUTS = {"token", "double_faced_token", "emblem", "art_series"}


def download_oracle(dest: Path) -> Path:
    if dest.exists():
        print(f"Using cached {dest}", file=sys.stderr)
        return dest
    req = urllib.request.Request(
        "https://api.scryfall.com/bulk-data",
        headers={"User-Agent": "aii600-mtg-project/1.0", "Accept": "application/json"},
    )
    meta = json.load(urllib.request.urlopen(req))
    uri = next(b["download_uri"] for b in meta["data"] if b["type"] == "oracle_cards")
    print(f"Downloading {uri}", file=sys.stderr)
    req = urllib.request.Request(uri, headers={"User-Agent": "aii600-mtg-project/1.0"})
    dest.write_bytes(urllib.request.urlopen(req).read())
    return dest


def norm(name: str) -> str:
    # "Fire/Ice" (MTGO) and "Fire // Ice" (Arena/Scryfall) should match
    return re.sub(r"\s*/{1,2}\s*", " // ", name.replace("’", "'")).lower()


def load_cards(path: Path) -> dict:
    cards = {}
    for c in json.loads(path.read_text(encoding="utf-8")):
        if c.get("layout") in SKIP_LAYOUTS:
            continue
        cards[norm(c["name"])] = c
        if " // " in c["name"]:
            cards.setdefault(norm(c["name"].split(" // ")[0]), c)
    return cards


def parse_deck(path: Path):
    main = []
    skipping = False  # inside a Companion section
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        low = s.lower()
        if not s:
            if main:
                break
            continue
        if low.startswith("sideboard"):
            break
        if low.startswith("companion"):
            skipping = True
            continue
        if low in ("deck", "mainboard"):
            skipping = False
            continue
        if skipping or s.startswith("//"):
            continue
        m = LINE.match(s)
        if m:
            main.append((int(m.group(1)), m.group(2)))
    return main


def front(card):
    return card["card_faces"][0] if "card_faces" in card else card


def pips(cost: str):
    out = {c: 0 for c in COLORS}
    for sym in re.findall(r"\{([^}]+)\}", cost or ""):
        for part in sym.split("/"):
            if part in out:
                out[part] += 1
    return out


def deck_row(name, entries, cards):
    missing = []
    n_total = n_land = 0
    cmc_sum = 0.0
    curve = {"cmc0_1": 0, "cmc2": 0, "cmc3": 0, "cmc4": 0, "cmc5plus": 0}
    types = {t: 0 for t in ["Creature", "Instant", "Sorcery", "Artifact", "Enchantment", "Planeswalker"]}
    pip_tot = {c: 0 for c in COLORS}
    colors = set()
    for qty, cname in entries:
        c = cards.get(norm(cname))
        if c is None:
            missing.append(cname)
            continue
        n_total += qty
        tl = front(c).get("type_line", c.get("type_line", ""))
        if "Land" in tl:
            n_land += qty
            continue
        cmc = c.get("cmc", 0)
        cmc_sum += cmc * qty
        key = ("cmc0_1" if cmc <= 1 else "cmc2" if cmc <= 2 else "cmc3" if cmc <= 3
               else "cmc4" if cmc <= 4 else "cmc5plus")
        curve[key] += qty
        for t in types:
            if t in tl:
                types[t] += qty
        p = pips(front(c).get("mana_cost", c.get("mana_cost", "")))
        for col in COLORS:
            pip_tot[col] += p[col] * qty
        colors.update(front(c).get("colors", c.get("colors", [])))
    nonland = n_total - n_land
    colour_str = "".join(x for x in COLORS if x in colors)
    row = {
        "deck": name,
        "colors": colour_str,
        "num_colors": len(colour_str),
        "is_two_color": int(len(colour_str) == 2),
        "mainboard_cards": n_total,
        "lands": n_land,
        "nonland_cards": nonland,
        "avg_cmc_nonland": round(cmc_sum / nonland, 3) if nonland else "",
        "avg_cmc_all_cards": round(cmc_sum / n_total, 3) if n_total else "",
        **curve,
        **{f"n_{t.lower()}": v for t, v in types.items()},
        **{f"pips_{c}": pip_tot[c] for c in COLORS},
        "unmatched_cards": "; ".join(missing),
    }
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--decks", required=True, type=Path)
    ap.add_argument("--out", default="decks.csv", type=Path)
    ap.add_argument("--cards", type=Path, help="local Scryfall oracle-cards JSON")
    a = ap.parse_args()

    cards_path = a.cards or download_oracle(Path("oracle-cards.json"))
    cards = load_cards(cards_path)
    rows = [deck_row(p.stem, parse_deck(p), cards) for p in sorted(a.decks.glob("*.txt"))]
    if not rows:
        sys.exit("No .txt decklists found")
    with a.out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    bad = sum(1 for r in rows if r["unmatched_cards"])
    print(f"Wrote {len(rows)} decks to {a.out} ({bad} with unmatched card names)")


if __name__ == "__main__":
    main()
