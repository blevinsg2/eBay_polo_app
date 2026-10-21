"""Local photo reader for Pricing: OCR (RapidOCR/ONNX, offline) + color heuristics."""
import base64, io, re
from PIL import Image

_ocr = None
def _engine():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    return _ocr

BRANDS = {"peter millar":"Peter Millar","johnnie-o":"johnnie-O","johnnie o":"johnnie-O","johnnieo":"johnnie-O",
 "g/fore":"G/FORE","gfore":"G/FORE","greyson":"Greyson","rlx":"RLX Ralph Lauren","polo golf":"Polo Golf",
 "ralph lauren":"Ralph Lauren","polo":"Polo Ralph Lauren","vineyard vines":"Vineyard Vines","travismathew":"TravisMathew",
 "travis mathew":"TravisMathew","holderness":"Holderness & Bourne","nike":"Nike","titleist":"Titleist","footjoy":"FootJoy",
 "under armour":"Under Armour","adidas":"adidas","callaway":"Callaway","puma":"PUMA","bonobos":"Bonobos","mizzen":"Mizzen+Main",
 "linksoul":"Linksoul","b. draddy":"B. Draddy","b draddy":"B. Draddy","zero restriction":"Zero Restriction","dunning":"Dunning",
 "criquet":"Criquet","lululemon":"lululemon","rhone":"Rhone","faherty":"Faherty","southern tide":"Southern Tide",
 "brooks brothers":"Brooks Brothers","lacoste":"Lacoste","tommy bahama":"Tommy Bahama","j.crew":"J.Crew","patagonia":"Patagonia",
 "fairway & greene":"Fairway & Greene","fairway":"Fairway & Greene","dri-fit":"Nike","drifit":"Nike","crown crafts":"Peter Millar"}
STYLES = ["summer comfort","crown crafts","crown sport","e4","dri-fit","dri fit","tour performance","performance","pique","jersey","stretch","oxford","prep-formance","tour","heather","signature"]
SIZES = [("XXXL","3XL"),("3XL","3XL"),("XXL","XXL"),("2XL","XXL"),("XL","XL"),("XS","XS"),("SMALL","S"),("MEDIUM","M"),("LARGE","L"),("S","S"),("M","M"),("L","L")]
COLORS = [((20,20,20),"black"),((240,240,240),"white"),((128,128,128),"gray"),((25,40,90),"navy"),((40,90,200),"blue"),
 ((140,190,230),"light blue"),((200,30,40),"red"),((240,150,170),"pink"),((170,150,210),"lavender"),((110,40,140),"purple"),
 ((40,130,60),"green"),((150,200,140),"mint"),((240,210,60),"yellow"),((240,130,30),"orange"),((210,190,150),"khaki"),
 ((120,80,40),"brown"),((100,40,50),"maroon"),((40,140,140),"teal")]

def _color(img):
    w,h = img.size
    c = img.crop((w//4,h//4,3*w//4,3*h//4)).convert("RGB").resize((40,40))
    px = list(c.getdata()); px.sort(key=sum); px = px[len(px)//10: -len(px)//10 or None]
    r,g,b = (sum(p[i] for p in px)//len(px) for i in range(3))
    return min(COLORS, key=lambda kc: sum((a-b2)**2 for a,b2 in zip(kc[0],(r,g,b))))[1]

def analyze(images):
    """images: list of data-URL/base64 strings. Returns structured fields."""
    texts, colors = [], []
    for s in images[:6]:
        if "," in s: s = s.split(",",1)[1]
        img = Image.open(io.BytesIO(base64.b64decode(s))); img.thumbnail((1400,1400))
        colors.append(_color(img))
        buf = io.BytesIO(); img.convert("RGB").save(buf,"JPEG")
        res,_ = _engine()(buf.getvalue())
        texts.append([(t, float(sc)) for _,t,sc in (res or []) if float(sc) > 0.5])
    words = [t for tl in texts for t,_ in tl]
    blob = " ".join(words).lower()
    out = {"brand":None,"styleLine":None,"size":None,"color":None,"defects":[],"notes":"","condition":None,
           "confidence":"low","ocrText":words[:40],"engine":"rapidocr-local"}
    for k in sorted(BRANDS, key=len, reverse=True):
        if re.search(r"(?<![a-z])"+re.escape(k)+r"(?![a-z])", blob) or (len(k)>5 and k.replace(" ","") in blob.replace(" ","")):
            out["brand"]=BRANDS[k]; break
    for st in STYLES:
        if st in blob or st.replace(" ","") in blob.replace(" ",""): out["styleLine"]=st.title(); break
    for w in words:  # exact tag tokens first
        u = w.strip().upper().replace(" ","")
        for k,v in SIZES:
            if u == k or u in (f"SIZE{k}", f"{k}/", f"T.{k}"): out["size"]=v; break
        if out["size"]: break
    if not out["size"]:
        m = re.search(r"\bsize[:\s]*(xxxl|3xl|xxl|2xl|xl|xs|s|m|l|small|medium|large)\b", blob)
        if m: out["size"]=dict(SIZES)[m.group(1).upper()]
    # front photo is usually first; prefer it for color
    out["color"] = colors[0] if colors else None
    mat = re.findall(r"\d{1,3}\s?%\s?[a-z]+", blob)
    if re.search(r"\$\s?\d|msrp|retail|nwt|new with tag", blob): out["condition"]="New with tags"; out["notes"]+="price tag visible; "
    for kw,d in (("stain","stain"),("hole","hole"),("pill","pilling"),("fade","fading")):
        if kw in blob: out["defects"].append(d)
    if mat: out["notes"] += "fabric: " + ", ".join(dict.fromkeys(mat)) + "; "
    hits = sum(bool(out[k]) for k in ("brand","size","styleLine"))
    out["confidence"] = "high" if hits>=2 else ("med" if hits==1 else "low")
    out["notes"] = out["notes"].strip("; ")
    return out
