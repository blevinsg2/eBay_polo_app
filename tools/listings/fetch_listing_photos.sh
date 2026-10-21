#!/usr/bin/env bash
# Download the photo set of public eBay listings you own, so the Listing Bot can re-check
# flaws/tags before a relist. One folder per item ID with 1.jpg, 2.jpg, ...
#
#   ./fetch_listing_photos.sh ITEM_ID [ITEM_ID ...]
#   ./fetch_listing_photos.sh $(cat ids.txt)
#
# Only use it on your own listings, go slow (it sleeps between items), and respect eBay's terms.
# The cookie jar is a temp file that gets deleted at the end.
set -euo pipefail
[ $# -gt 0 ] || { echo "usage: $0 ITEM_ID [ITEM_ID ...]"; exit 1; }
UA="${POLO_FETCH_UA:-Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36}"
OUT="${POLO_FETCH_DIR:-listing_photos}"
JAR="$(mktemp)"; trap 'rm -f "$JAR"' EXIT
mkdir -p "$OUT"
for id in "$@"; do
  [[ "$id" =~ ^[0-9]{9,15}$ ]] || { echo "skip bad id: $id"; continue; }
  page="$OUT/$id.html"
  [ -s "$page" ] && grep -q carousel-item "$page" || curl -s -L -c "$JAR" -b "$JAR" -A "$UA" \
    -H "Accept: text/html" -H "Accept-Language: en-US,en;q=0.9" -o "$page" "https://www.ebay.com/itm/$id"
  mkdir -p "$OUT/$id"
  python3 - "$page" "$OUT/$id.ids" <<'PY'
import re, sys
h = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
urls = []
for m in re.finditer(r'ux-image-carousel-item[^>]*data-idx=(\d+)><img[^>]*?data-zoom-src=(\S+?)[\s>]', h):
    u = m.group(2).strip('"')
    if u not in urls:
        urls.append(u)
open(sys.argv[2], "w").write("\n".join(urls))
print(sys.argv[1], len(urls), "photos")
PY
  n=1; while read -r u; do [ -n "$u" ] && curl -s -A "$UA" -o "$OUT/$id/$n.jpg" "$u"; n=$((n+1)); done < "$OUT/$id.ids"
  sleep 1
done
