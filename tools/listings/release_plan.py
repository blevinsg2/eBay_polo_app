#!/usr/bin/env python3
"""Build a re-price / relist plan for a group of listings.

Input is a CSV you (or the Listing Bot) fill in, one row per listing:
  sku,item_id,current_price,new_price,min_offer,watchers,median_sold,n_comps,flaws,spec_corrections,new_title,description

Outputs (next to --out, default ./plan):
  <name>_plan.csv        cleaned rows
  <name>_plan.md         table + "needs a human check" list, for review
  <name>_checklist.txt   step-by-step relist sheet (Sell Similar -> title/price/min offer/specifics/description)

Rules it enforces: eBay titles max 80 chars, prices end in .67 (the house style), min offer below new price.
Nothing here talks to eBay. It only writes files for a person to approve and apply.

  python3 release_plan.py examples/release_plan.example.csv --name group1 --release 2026-11-01
"""
import argparse, csv, sys
from pathlib import Path

COLS = "sku,item_id,current_price,new_price,min_offer,watchers,median_sold,n_comps,flaws,spec_corrections,new_title,description".split(",")


def money(v):
    try:
        return float(str(v).replace("$", "").strip())
    except ValueError:
        return None


def check(r):
    probs = []
    if len(r["new_title"]) > 80:
        probs.append(f"title is {len(r['new_title'])} chars (max 80)")
    np_, mo = money(r["new_price"]), money(r["min_offer"])
    if np_ is not None and f"{np_:.2f}"[-2:] != "67":
        probs.append("new price doesn't end in .67")
    if np_ is not None and mo is not None and mo >= np_:
        probs.append("min offer is not below the new price")
    flaws = r["flaws"].lower()
    if any(w in flaws for w in ("certain", "may be", "might", "could be", "can't judge", "only one photo", "only 3", "no back")):
        probs.append("flaw call needs a human look")
    return probs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--name", default="group")
    ap.add_argument("--release", default="TBD", help="release date, YYYY-MM-DD")
    ap.add_argument("--out", default="plan")
    a = ap.parse_args()
    rows = list(csv.DictReader(open(a.csv, newline="", encoding="utf-8")))
    missing = [c for c in COLS if rows and c not in rows[0]]
    if missing:
        sys.exit(f"missing columns: {missing}")
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    with open(out / f"{a.name}_plan.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS); w.writeheader(); w.writerows({c: r[c] for c in COLS} for r in rows)
    md = [f"# {a.name} (release {a.release}), DRAFT for approval, nothing changed on eBay", "",
          "| SKU | Item | Now | New | Min | Watch | Flaws | Spec corrections | New title |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['sku'] or '—'} | {r['item_id']} | {r['current_price']} | ${r['new_price']} | ${r['min_offer']} | {r['watchers']} | {r['flaws']} | {r['spec_corrections']} | {r['new_title']} |")
    md += ["", "## Needs a human check"]
    flagged = [(r, check(r)) for r in rows]
    for r, p in flagged:
        if p:
            md.append(f"- {r['item_id']} ({r['sku'] or 'no SKU'}): " + "; ".join(p))
    md += ["", "## Descriptions"] + [f"- **{r['item_id']}** ({r['sku'] or 'no SKU'}): {r['description']}" for r in rows]
    (out / f"{a.name}_plan.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    ck = [f"{a.name} relist checklist - release {a.release}", "Action: Sell Similar each old item, apply the title/price/min below, schedule for the release date.", ""]
    for i, r in enumerate(rows, 1):
        ck += [f"{i}. OLD ITEM {r['item_id']} | SKU {r['sku'] or '(none - leave blank)'}",
               f"   Title: {r['new_title']}",
               f"   Buy It Now price: ${r['new_price']} | Best Offer ON, auto-accept EMPTY, minimum offer ${r['min_offer']}",
               f"   Item specifics: {r['spec_corrections'] or 'No spec changes needed.'}",
               f"   Description (replace whole text): {r['description']}", ""]
    (out / f"{a.name}_checklist.txt").write_text("\n".join(ck), encoding="utf-8")
    bad = sum(1 for _, p in flagged if p)
    print(f"{len(rows)} rows -> {out}/ ({bad} flagged for a human check)")


if __name__ == "__main__":
    main()
