"""Receipt OCR + parsing for Receipt Tracker (reuses vision.py RapidOCR engine)."""
import base64, io, re
from datetime import datetime
from PIL import Image, ImageOps
from vision import _engine

STORES = {"goodwill":"Goodwill","salvation army":"Salvation Army","savers":"Savers","value village":"Value Village",
 "habitat":"Habitat for Humanity ReStore","restore":"Habitat for Humanity ReStore","plato":"Plato's Closet",
 "st. vincent":"St. Vincent de Paul","vincent de paul":"St. Vincent de Paul","america's thrift":"America's Thrift Stores",
 "thrift":None,"hospice":"Hospice Thrift","red white":"Red White & Blue Thrift","platos":"Plato's Closet","once upon":"Once Upon A Child"}
TOTAL_RE = re.compile(r"\b(grand\s*total|total\s*due|amount\s*due|balance\s*due|total|amount)\b", re.I)
SKIP_RE = re.compile(r"sub\s*total|tax|change|cash|visa|master|debit|credit|tender|card|savings|discount|you saved|points|auth|approval", re.I)
MONEY = re.compile(r"-?\$?\s?(\d{1,4}[.,]\d{2})(?!\d)")
DATES = [(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b","mdY"),(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b","mdy"),
 (r"\b(\d{4})-(\d{2})-(\d{2})\b","Ymd"),(r"\b(\d{1,2})-(\d{1,2})-(\d{2,4})\b","mdy2")]
ADDR = re.compile(r"\b\d{2,6}\s*[A-Za-z0-9 .,'-]{3,40}\b(st|street|rd|road|ave|avenue|blvd|blyd|hwy|highway|pkwy|parkway|dr|drive|ln|lane|way|pike|ct|plaza|route|rt)\b\.?", re.I)
CITY = re.compile(r"(?m)^\s*([A-Za-z .'-]{3,30}),?\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?\b")
PHONE = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]\d{4}")

def _money(s):
    m = MONEY.findall(s); return float(m[-1].replace(",", ".")) if m else None

def _lines(res):
    """Group OCR boxes into visual lines by y-center so labels pair with prices."""
    boxes = []
    for box, t, sc in res or []:
        if float(sc) < 0.45: continue
        ys = [p[1] for p in box]; xs = [p[0] for p in box]
        boxes.append(((min(ys)+max(ys))/2, max(ys)-min(ys), min(xs), t))
    boxes.sort()
    lines, cur = [], []
    for b in boxes:
        if cur and abs(b[0]-cur[-1][0]) > max(8, 0.6*b[1]): lines.append(cur); cur = []
        cur.append(b)
    if cur: lines.append(cur)
    return [" ".join(t for *_, t in sorted(l, key=lambda x: x[2])) for l in lines]

def _date(blob):
    for pat, kind in DATES:
        for m in re.finditer(pat, blob):
            a, b, c = (int(x) for x in m.groups())
            try:
                if kind == "Ymd": d = datetime(a, b, c)
                else:
                    y = c if c > 100 else 2000 + c
                    d = datetime(y, a, b)
                if 2000 <= d.year <= datetime.now().year + 1: return d.strftime("%Y-%m-%d")
            except ValueError: pass
    return None

def analyze(images):
    lines = []
    for s in images[:4]:
        if "," in s: s = s.split(",", 1)[1]
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(base64.b64decode(s)))); img.thumbnail((1800, 1800))
        buf = io.BytesIO(); img.convert("RGB").save(buf, "JPEG", quality=90)
        res, _ = _engine()(buf.getvalue())
        lines += _lines(res)
    blob = "\n".join(lines); low = blob.lower()
    out = {"store": None, "address": None, "city": None, "date": None, "total": None, "subtotal": None, "tax": None,
           "items": [], "itemCount": 0, "phone": None, "confidence": "low", "ocrText": lines[:80], "engine": "rapidocr-local"}
    chain = None
    for k in sorted(STORES, key=len, reverse=True):
        if k in low and STORES[k]: chain = STORES[k]; break
    hdr = [l for l in lines[:4] if re.search(r"[A-Za-z]{3}", l) and not MONEY.search(l) and not re.match(r"\s*\d{2,6}\s*[A-Za-z]", l)]
    branch = hdr[0].strip()[:60] if hdr else None
    if chain and branch and chain.lower() not in branch.lower(): out["store"] = f"{chain} · {branch}"
    else: out["store"] = branch or chain
    out["chain"] = chain
    m = re.search(r"(?:t[i1]cket|trans(?:action)?|receipt)\s*#?\s*:?\s*([0-9][0-9-]{4,})", low)
    out["ticket"] = m.group(1) if m else None
    am = [ADDR.search(l) for l in lines[:10]]
    am = [x for x in am if x]
    out["address"] = re.sub(r"(\d)([A-Za-z])", r"\1 \2", am[0].group(0).strip().replace(",", ". ", 1).replace(".  ", ". ")).replace("Blyd", "Blvd") if am else None
    m = CITY.search(blob)
    if m: out["city"] = f"{m.group(1).strip().title()}, {m.group(2)} {m.group(3)}"
    if out["address"] and out["city"]: out["address"] += ", " + out["city"]
    elif out["city"]: out["address"] = out["city"]
    m = PHONE.search(blob); out["phone"] = m.group(0) if m else None
    out["date"] = _date(blob)
    totals = []; pending_total = False
    for idx, l in enumerate(lines):
        v = _money(l)
        if v is None and TOTAL_RE.fullmatch(l.strip()): pending_total = True; continue
        if pending_total and v is not None and re.fullmatch(r"\$?\s?\d+[.,]\d{2}", l.strip()): totals.append(v); pending_total = False; continue
        pending_total = False
        if v is None: continue
        ll = l.lower()
        if re.search(r"sub\s*total", ll): out["subtotal"] = v; continue
        if re.search(r"\btax\b", ll): out["tax"] = v; continue
        if TOTAL_RE.search(ll) and not SKIP_RE.search(ll): totals.append(v); continue
        if re.fullmatch(r"\$?\s?\d+[.,]\d{2}", l.strip()) and out["subtotal"] is not None and out["tax"] is None and not totals:
            out["tax"] = v; continue
        if re.search(r"round\s*up|donation", ll):
            out["roundUp"] = v; out["items"].append({"name": "Change Round Up (donation)", "qty": 1, "unit": v, "amount": v, "donation": True}); continue
        if SKIP_RE.search(ll) or re.search(r"\bitems?\s*(sold|count)?\b\s*\d*$", ll): continue
        name = MONEY.sub("", l).strip(" $-:*#")
        name = re.sub(r"\bqty\s*:?\s*\d+", "", name, flags=re.I)
        name = re.sub(r"\s+\d{3,4}\b", "", name).strip()  # stray SKU/price-tag digits
        name = re.sub(r"\s+[A-Z]$", "", name)  # trailing size/tax flag
        if len(re.findall(r"[A-Za-z]", name)) < 2 or v <= 0: continue
        qty = 1; q = re.search(r"\b(\d{1,3})\s*[@x]\s*\$?\s?(\d+[.,]\d{2})", l, re.I)
        unit = v
        if q: qty = int(q.group(1)); unit = float(q.group(2).replace(",", "."))
        else:
            q2 = re.match(r"^(\d{1,2})\s+(?=[A-Za-z])", name)
            if q2 and int(q2.group(1)) < 50: qty = int(q2.group(1)); name = name[q2.end():]; unit = round(v/qty, 2)
        out["items"].append({"name": name[:60], "qty": qty, "unit": unit, "amount": v})
    if totals: out["total"] = max(totals)
    elif out["subtotal"] is not None: out["total"] = round(out["subtotal"] + (out["tax"] or 0), 2)
    elif out["items"]: out["total"] = round(sum(i["amount"] for i in out["items"]), 2)
    m = re.search(r"(?:number of ite[mn]s|ite[mn]s|total qty)\s*(?:sold|count|purchased)\s*[:#]?\s*(\d{1,3})\b", low)
    out["itemCount"] = int(m.group(1)) if m else sum(i["qty"] for i in out["items"] if not i.get("donation"))
    if out["tax"] is None and out["total"] and out["subtotal"]:
        out["tax"] = round(out["total"] - out["subtotal"], 2)
    hits = sum(bool(out[k]) for k in ("store", "date", "total", "address"))
    out["confidence"] = "high" if hits >= 3 else ("med" if hits == 2 else "low")
    return out
