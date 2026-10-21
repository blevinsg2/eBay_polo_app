#!/usr/bin/env python3
"""Listing photo builder for golf polos.

Takes a raw hanger/flat-lay photo, cuts the shirt out (rembg u2net), drops the hanger,
and puts it on a soft gradient. Optional "measure" mode adds a brand size-chart panel,
an actual-measurements panel and arrows. Optional store logo panel if you pass --logo.

  python3 listing_photo.py plain   in.jpg out.jpg [--pad 200]
  python3 listing_photo.py measure in.jpg out.jpg --brand "PETER MILLAR" --size L \
          --chart-chest "43 - 45 in" --chest "XX in" --length "XX in" [--logo logo.png]
  python3 listing_photo.py gentle  tag.jpg out.jpg            # light autocontrast for tag shots
  python3 listing_photo.py label   in.jpg out.jpg "Front"     # small caption pill bottom-left

Env: POLO_FONT=path/to/font.ttf (any TTF; falls back to Pillow's default font)
     HANGER_TOL (default 12), HANGER_GRAY (default 1)
Needs: Pillow, numpy, opencv-python-headless, rembg (only for plain/measure).
"""
import argparse, math, os, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

FONT_PATH = os.environ.get("POLO_FONT", "")
HANGER_TOL = int(os.environ.get("HANGER_TOL", "12"))
HANGER_GRAY = os.environ.get("HANGER_GRAY", "1") == "1"
NAVY = (24, 42, 78)
_SESS = None


def font(sz, w=400):
    if FONT_PATH and os.path.exists(FONT_PATH):
        f = ImageFont.truetype(FONT_PATH, sz)
        for ax in ([32, w], [w]):
            try:
                f.set_variation_by_axes(ax); break
            except Exception:
                pass
        return f
    try:
        return ImageFont.load_default(size=sz)
    except TypeError:
        return ImageFont.load_default()


def make_alpha(img,cache=None):
    import cv2
    '''Shirt mask: u2net -> largest blob -> fill interior holes -> drop thin hanger/hook
    protrusions in the top band -> interior alpha exactly 255 (garment pixel-identical).'''
    global _SESS
    if cache and os.path.exists(cache): return Image.open(cache)
    from rembg import remove,new_session
    _SESS=_SESS or new_session('u2net')
    m=np.array(remove(img,session=_SESS,only_mask=True))
    b=(m>110).astype(np.uint8)
    n,lab,st,_=cv2.connectedComponentsWithStats(b);b=(lab==1+np.argmax(st[1:,4])).astype(np.uint8)
    h,w=b.shape;inv=1-b
    n2,l2,s2,_=cv2.connectedComponentsWithStats(inv)
    for i in range(1,n2):
        x,y,ww,hh,a=s2[i]
        if x>0 and y>0 and x+ww<w and y+hh<h: b[l2==i]=1
    band=int(h*0.2);op=cv2.morphologyEx(b,cv2.MORPH_OPEN,np.ones((21,21),np.uint8))
    thin=(b==1)&(op==0);thin[band:]=False
    n3,l3,s3,_=cv2.connectedComponentsWithStats(thin.astype(np.uint8))
    removed=0
    for i in range(1,n3):
        if s3[i,4]>150: b[l3==i]=0;removed+=s3[i,4]
    # neutral-gray hanger body (low saturation, mid tone) in the top band, outside the colored garment
    if HANGER_GRAY:
        hsv=cv2.cvtColor(np.array(img),cv2.COLOR_RGB2HSV)
        A=np.array(img).astype(int);g=(((A[...,2]-A[...,0])<HANGER_TOL)&(A.max(2)<215)).astype(np.uint8)  # neutral (hanger/felt) vs garment hue
        g[int(h*0.3):]=0
        g=cv2.morphologyEx(g,cv2.MORPH_OPEN,np.ones((5,5),np.uint8))
        # only hanger parts that touch the open background (not hanger seen over fabric inside the neck)
        bgd=cv2.dilate((1-b),np.ones((5,5),np.uint8))
        n4,l4,s4,_=cv2.connectedComponentsWithStats(g&b)
        for i in range(1,n4):
            comp=(l4==i)
            if s4[i,4]>800 and (comp&(bgd>0)).sum()>50:
                comp=cv2.dilate(comp.astype(np.uint8),np.ones((5,5),np.uint8))>0
                b[comp&(g>0)]=0;removed+=s4[i,4]
        n,lab,st,_=cv2.connectedComponentsWithStats(b);b=(lab==1+np.argmax(st[1:,4])).astype(np.uint8)
    print('hanger/thin px removed:',removed)
    soft=cv2.GaussianBlur(b.astype(np.float32),(0,0),1.0)
    a=np.where(cv2.erode(b,np.ones((3,3)))>0,1.0,soft)
    out=Image.fromarray((a*255).astype(np.uint8))
    if cache: out.save(cache)
    return out


def gradient(W, H):
    yy, xx = np.mgrid[0:H, 0:W]
    r = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2) / 1.25
    v = 255 - (255 - 0xED) * np.clip(r, 0, 1) ** 1.6
    return Image.fromarray(np.dstack([v] * 3).astype(np.uint8))


def cutout_on_gradient(img, pad):
    al = make_alpha(img)
    W, H = img.width + 2 * pad, img.height
    bg = gradient(W, H)
    sh = Image.new("L", (W, H), 0); sh.paste(al, (pad + 10, 14))
    sh = sh.filter(ImageFilter.GaussianBlur(18)).point(lambda p: int(p * 0.22))
    bg = Image.composite(Image.new("RGB", (W, H), (90, 95, 105)), bg, sh)
    bg.paste(img, (pad, 0), al)
    return bg


def panel(c,box,rad=22):
    x0,y0,x1,y1=box
    reg=c.crop(box).filter(ImageFilter.GaussianBlur(14))
    tint=Image.new('RGB',reg.size,(255,255,255));reg=Image.blend(reg,tint,0.55)
    m=Image.new('L',reg.size,0);ImageDraw.Draw(m).rounded_rectangle([0,0,reg.width-1,reg.height-1],rad,fill=255)
    s=Image.new('L',c.size,0);ImageDraw.Draw(s).rounded_rectangle([x0,y0+6,x1,y1+6],rad,fill=60);s=s.filter(ImageFilter.GaussianBlur(10))
    c.paste(Image.composite(Image.new('RGB',c.size,(120,125,135)),c,s))
    c.paste(reg,(x0,y0),m)
    ImageDraw.Draw(c).rounded_rectangle(box,rad,outline=(255,255,255),width=2)
def arrow(d,p,q,col=NAVY):
    d.line([p,q],fill=(255,255,255),width=7);d.line([p,q],fill=col,width=3)
    for a,b in((p,q),(q,p)):
        g=math.atan2(b[1]-a[1],b[0]-a[0]);L=20
        d.polygon([b,(b[0]-L*math.cos(g-.42),b[1]-L*math.sin(g-.42)),(b[0]-L*math.cos(g+.42),b[1]-L*math.sin(g+.42))],fill=col,outline=(255,255,255))
def tag(d,xy,t):
    x,y=xy;d.ellipse([x-17,y-17,x+17,y+17],fill=NAVY,outline=(255,255,255),width=2)
    d.text((x,y+1),t,font=font(20,600),fill='white',anchor='mm')


def logo_panel(c, logo_path, frac=0.2):
    """Optional store/brand logo panel in the top-left. Your own logo file; none ships with the repo."""
    s = c.width * frac / 360; m = int(28 * s); pw, ph = int(360 * s), int(290 * s)
    panel(c, (m, m, m + pw, m + ph), rad=max(8, int(22 * s)))
    lg = Image.open(logo_path).convert("RGBA")
    a = np.array(lg).astype(int); bgc = a[5, 5, :3]; dist = np.abs(a[..., :3] - bgc).sum(2)
    a[..., 3] = np.minimum(a[..., 3], np.clip((dist - 45) * 6, 0, 255)); lg = Image.fromarray(a.astype(np.uint8))
    lw = int(330 * s); lg = lg.resize((lw, max(1, int(lw * lg.height / lg.width))), Image.LANCZOS)
    c.paste(lg, (m + (pw - lw) // 2, m + (ph - lg.height) // 2), lg)


def measure(src, out, brand, size, chart_chest, chest, length, logo=None, pad=None):
    img = Image.open(src).convert("RGB")
    pad = pad if pad is not None else int(img.width * 0.4)
    c = cutout_on_gradient(img, pad); W, H = c.size
    if logo:
        logo_panel(c, logo, frac=360 / max(W, 1800))
    bx0, by0, bx1, by1 = W - 410, 28, W - 28, 268; panel(c, (bx0, by0, bx1, by1))
    d = ImageDraw.Draw(c)
    d.text((bx0 + 26, by0 + 44), brand.upper(), font=font(34, 800), fill=(17, 17, 17), anchor="lm")
    d.text((bx0 + 26, by0 + 92), f"BRAND SIZE CHART · SIZE {size}", font=font(22, 600), fill=(100, 108, 122), anchor="lm")
    d.line([bx0 + 26, by0 + 114, bx1 - 26, by0 + 114], fill=(205, 210, 218), width=1)
    for i, (k, v) in enumerate([("Chest (body)", chart_chest), ("Length", "not published")]):
        y = by0 + 146 + i * 44
        d.text((bx0 + 26, y), k, font=font(28, 400), fill=(60, 66, 80), anchor="lm")
        d.text((bx1 - 26, y), v, font=font(28, 600), fill=NAVY, anchor="rm")
    mx0, my0, mx1, my1 = bx0, by1 + 22, bx1, by1 + 242; panel(c, (mx0, my0, mx1, my1))
    d = ImageDraw.Draw(c)
    d.text((mx0 + 26, my0 + 40), "ACTUAL MEASUREMENTS", font=font(22, 600), fill=(100, 108, 122), anchor="lm")
    d.line([mx0 + 26, my0 + 62, mx1 - 26, my0 + 62], fill=(205, 210, 218), width=1)
    for i, (n, k, v) in enumerate([("1", "Chest, pit to pit", chest), ("2", "Length", length)]):
        y = my0 + 100 + i * 56; tag(d, (mx0 + 43, y), n)
        d.text((mx0 + 72, y), k, font=font(28, 400), fill=(60, 66, 80), anchor="lm")
        d.text((mx1 - 26, y), v, font=font(30, 600), fill=NAVY, anchor="rm")
    # arrows: chest across ~40% down, length down the left third of the shirt
    pw = img.width; Y = int(H * 0.4)
    x1, x2 = pad + int(pw * 0.21), pad + int(pw * 0.79)
    arrow(d, (x1, Y), (x2, Y)); tag(d, (x2 - 50, Y - 34), "1")
    LX = pad + int(pw * 0.29); arrow(d, (LX, int(H * 0.04)), (LX, H - 14)); tag(d, (LX + 34, int(H * 0.07)), "2")
    c.save(out, quality=93)


def gentle(src, out):
    im = Image.open(src).convert("RGB")
    Image.blend(im, ImageOps.autocontrast(im, cutoff=0.5, preserve_tone=True), 0.5).save(out, quality=93)


def label(src, out, text):
    im = Image.open(src).convert("RGB")
    f = max(im.width / 1800, 0.55); d = ImageDraw.Draw(im); ft = font(int(30 * f), 500)
    tw = d.textlength(text, font=ft); m = int(28 * f); h = int(58 * f); pw = int(tw + 48 * f)
    box = (m, im.height - m - h, m + pw, im.height - m); panel(im, box, rad=max(8, int(18 * f)))
    ImageDraw.Draw(im).text((m + pw // 2, im.height - m - h // 2), text, font=ft, fill=NAVY, anchor="mm")
    im.save(out, quality=95, subsampling=0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plain"); p.add_argument("src"); p.add_argument("out"); p.add_argument("--pad", type=int, default=0)
    m = sub.add_parser("measure"); m.add_argument("src"); m.add_argument("out")
    m.add_argument("--brand", required=True); m.add_argument("--size", required=True)
    m.add_argument("--chart-chest", default="see chart"); m.add_argument("--chest", default="XX in"); m.add_argument("--length", default="XX in")
    m.add_argument("--logo"); m.add_argument("--pad", type=int)
    g = sub.add_parser("gentle"); g.add_argument("src"); g.add_argument("out")
    l = sub.add_parser("label"); l.add_argument("src"); l.add_argument("out"); l.add_argument("text")
    a = ap.parse_args()
    if a.cmd == "plain":
        cutout_on_gradient(Image.open(a.src).convert("RGB"), a.pad).save(a.out, quality=95, subsampling=0)
    elif a.cmd == "measure":
        measure(a.src, a.out, a.brand, a.size, a.chart_chest, a.chest, a.length, a.logo, a.pad)
    elif a.cmd == "gentle":
        gentle(a.src, a.out)
    else:
        label(a.src, a.out, a.text)
    print(a.out)


if __name__ == "__main__":
    main()
