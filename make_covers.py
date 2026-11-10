# -*- coding: utf-8 -*-
"""
Build the Instagram cover image (first photo of each carousel) with a two-line info
overlay, from properties.json produced by extract_queue.py.

Rule (Yossi, 2026-11-10): the profile grid shows only the first photo of each post, so
that photo carries line 1 = property type + rooms + city, line 2 = price. Every word comes
from structured fields (see extract_queue.parse_property) - no model writes any of it. A
field that is missing is left out, never guessed.

Output: <outdir>/<md5(url)>.jpg  - the Make scenario computes the same md5 of the stored
`url` field, so no data-store schema change is needed. Also writes <outdir>/index.json.

Needs: pillow, python-bidi (pip). Fonts: fonts/Heebo-Bold.ttf (Hebrew, no shekel glyph)
and any system bold font that has the shekel sign (Segoe UI on Windows, DejaVu on Linux).

Usage: python make_covers.py <outdir>
"""
import hashlib, io, json, os, sys, time, urllib.request
from PIL import Image, ImageDraw, ImageFont
from bidi.algorithm import get_display

HERE = os.path.dirname(os.path.abspath(__file__))
HEEBO = os.path.join(HERE, "fonts", "Heebo-Bold.ttf")
SHEKEL_FONTS = [
    "C:/Windows/Fonts/segoeuib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
SAFE_W = 700  # the profile grid crops the square to ~3:4, so keep text in the middle
UA = {"User-Agent": "Mozilla/5.0 (compatible; yossimor-covers/1.0)"}


def cover_id(url):
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def line1(r):
    head = r.get("ptype") or ""
    # A plot (מגרש) carries a meaningless rooms value on the site - never print it.
    if r.get("rooms") and "מגרש" not in head:
        head = (head + " " + r["rooms"] + " חדרים").strip()
    parts = [p for p in (head, r.get("city")) if p]
    return " · ".join(parts)


def fetch_base(img_url):
    # Same smart square crop the live scenario already uses, so the cover matches.
    src = img_url.replace("https://", "")
    u = "https://images.weserv.nl/?url=" + src + "&w=1080&h=1080&fit=cover&a=attention&output=jpg"
    last = None
    for _ in range(3):
        try:
            req = urllib.request.Request(u, headers=UA)
            data = urllib.request.urlopen(req, timeout=60).read()
            return Image.open(io.BytesIO(data)).convert("RGB")
        except Exception as e:  # noqa
            last = e
            time.sleep(3)
    raise last


def fit(draw, text, size):
    while size > 30:
        f = ImageFont.truetype(HEEBO, size)
        if draw.textlength(get_display(text), font=f) <= SAFE_W:
            return f
        size -= 2
    return f


def render(base, l1, price):
    W, H = base.size
    band_h = 300 if l1 else 190
    top = (H - band_h) // 2
    ov = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(ov).rectangle([0, top, W, top + band_h], fill=(10, 20, 35, 170))
    img = Image.alpha_composite(base.convert("RGBA"), ov)
    d = ImageDraw.Draw(img)
    if l1:
        f1 = fit(d, l1, 66)
        t = get_display(l1)
        d.text(((W - d.textlength(t, font=f1)) / 2, top + 50), t, font=f1, fill=(255, 255, 255, 255))
        y_price = top + 145
    else:
        y_price = top + 45
    fnum = ImageFont.truetype(HEEBO, 92)
    sh = next((p for p in SHEKEL_FONTS if os.path.exists(p)), None)
    if sh is None:
        raise SystemExit("No font with the shekel sign found")
    fsh = ImageFont.truetype(sh, 80)
    wn, ws, gap = d.textlength(price, font=fnum), d.textlength("₪", font=fsh), 16
    x = (W - (wn + gap + ws)) / 2
    gold = (255, 205, 80, 255)
    d.text((x + ws + gap, y_price), price, font=fnum, fill=gold)
    d.text((x, y_price + 12), "₪", font=fsh, fill=gold)
    return img.convert("RGB")


def main():
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    props = json.load(open(os.path.join(HERE, "properties.json"), encoding="utf-8"))
    index, failed = {}, []
    old = {}
    ipath = os.path.join(out, "index.json")
    if os.path.exists(ipath):
        old = json.load(open(ipath, encoding="utf-8"))
    for r in props["eligible"]:
        if not r.get("price") or not r.get("images"):
            failed.append((r["url"], "no price or no images"))
            continue
        try:
            cid = cover_id(r["url"])
            entry = {"url": r["url"], "line1": line1(r), "price": r["price"], "src": r["images"][0]}
            fpath = os.path.join(out, cid + ".jpg")
            # Unchanged inputs and the file is there: keep it, so the public repo does not
            # collect a binary diff every day.
            if old.get(cid) == entry and os.path.exists(fpath):
                index[cid] = entry
                print("KEEP", cid, file=sys.stderr)
                continue
            img = render(fetch_base(r["images"][0]), line1(r), r["price"])
            img.save(fpath, quality=90)
            index[cid] = entry
            print("OK  ", cid, line1(r) or "(type/rooms/city missing)", r["price"], file=sys.stderr)
        except Exception as e:  # noqa
            failed.append((r["url"], str(e)))
            print("FAIL", r["url"], e, file=sys.stderr)
    json.dump(index, open(os.path.join(out, "index.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"covers: {len(index)}  failed: {len(failed)}", file=sys.stderr)
    if failed:
        for u, why in failed:
            print("  failed:", u, why, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
