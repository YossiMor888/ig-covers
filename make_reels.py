# -*- coding: utf-8 -*-
"""
Build the Instagram Reel for each queued property from its own YouTube video.

Rule (Yossi, 2026-11-17): Instagram publishes the property's YouTube video as a Reel, never
photos. A property with no usable video gets a record with video_url = null and stays out of
reels/queue.json, so the Make scenario never lands on it. Caption = the site's own copy plus
hashtags and his closing block, NO price (built by extract_queue.build_reel_caption).

Opening lines (Yossi, 2026-11-18): the first seconds of every Reel - and so the cover frame
the Make scenario picks at 5 s - carry two lines, the way the photo covers did:
  line 1 = property type + rooms (+ city), from structured fields
  line 2 = the hook he wrote himself in the listing title, verbatim; when the title has none,
           an approved line copied verbatim from the listing text (HOOK_OVERRIDES), else the
           fixed line "▶ סיור וידאו מלא" (his choice). Never a price.
He waived per-listing approval as long as these rules hold.

Per property, keyed by md5(url) exactly like the covers (the Make scenario computes it):
  reels/<id>.json        - {url, yt_id, caption, video_url|null, status, v, cover}
  reels/queue.json       - the Reels the scenario rotates through (only ready videos)
  <outdir>/<id>.clean.mp4 - 1080x1920 H.264 with no text (uploaded once as the master, so the
                           opening lines can be re-rendered later without YouTube)
  <outdir>/<id>.mp4      - the published Reel: the master plus the two opening lines
Videos are release assets, never committed - 30+ videos in git would bloat the public repo.

Usage: python make_reels.py <outdir> <assets.txt> [--only <yt_id or id>]
  assets.txt = names of the files already on the release (one per line).
Needs: yt-dlp (only for new videos), ffmpeg, pillow, python-bidi.
"""
import hashlib, json, os, re, shutil, subprocess, sys, urllib.request
from PIL import Image, ImageDraw, ImageFont, features
from bidi.algorithm import get_display

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = "YossiMor888/ig-covers"
TAG = "reels"
BASE = "https://github.com/" + REPO + "/releases/download/" + TAG + "/"
VIDEO_URL = BASE + "{id}.mp4"
CLEAN_URL = BASE + "{id}.clean.mp4"
VERSION = 4  # 2: bitrate capped. 3: two opening lines (2026-11-18). 4: lines moved up, solid band
OVERLAY_SECONDS = 5.5  # must cover the cover frame the scenario asks for (thumb_offset 5000)
MAX_BYTES = 900 * 1024 * 1024  # Instagram's limit is 1 GB; release assets allow 2 GB
FILTER = (
    "[0:v]split=2[a][b];"
    "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:2[bg];"
    "[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30,format=yuv420p[v]"
)
# Instagram Reel spec (Make CreateAReelPost): H.264 closed GOP, 4:2:0, 23-60 fps,
# video VBR 5 Mbps max, AAC 48 kHz max stereo 128k, moov atom first, 3 s - 15 min.
# The first encode at plain crf 23 came out at ~5.2 Mbps, so the rate is capped.
X264 = ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-maxrate", "4500k",
        "-bufsize", "9000k", "-profile:v", "high", "-g", "60"]

FALLBACK_HOOK = "▶ סיור וידאו מלא"
# Listings whose title has no hook get a line taken verbatim from their own site text instead
# of the fixed line (Yossi approved each of these on 2026-11-18). A line is used only while it
# still appears word for word in the listing text; a title hook, if one appears later, wins.
HOOK_OVERRIDES = {
    "6a7d9c17e38c9569325d9cf44b92b8e5": "בית עם אופי, גינה פרטית",
    "d54e037bbacc0163615f72e4bbbfa79f": "חצי חדר במתנה!",
    "622ee9120d18955f06873eaedaa9baaf": "מרווח, מתוכנן מוקפד, וגינה הפונה ליער",
    "b968edc3e962ab30b13ac1f15d66daf3": "מרפסת בודדת – ללא שכנים צמודים!",
    "531debb5a261cf8bea5b10bbb8f4cdb8": "בית עם נשמה ופוטנציאל",
    "f44a2cba80125f992c3a33371444919c": 'בפרויקט היוקרתי "רום תדהר"',
    "b22565661f9c725194f6ee6445de479c": "שכונה כפרית שקטה ורגועה",
}
PLAY = "▶"
FONT_DIRS = [os.path.join(HERE, "fonts"), "C:/Windows/Fonts", "/usr/share/fonts/truetype/heebo"]
SYMBOL_FONTS = ["C:/Windows/Fonts/seguisym.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
# Pillow on Linux ships libraqm, which lays out right-to-left text itself; on Windows it does
# not. Reordering by hand AND letting raqm reorder prints Hebrew backwards (seen 2026-11-10).
RAQM = features.check("raqm")


def rid(url):
    return hashlib.md5(url.encode("utf-8")).hexdigest()


# Branded cover (cover-builder Phase 4, plan approved by Yossi 2026-11-26). cover-builder's
# stage_cover.py puts an approved cover in covers/ and records it in covers/index.json. A queue
# item gets `cover_url` only when that file is here with the recorded sha256; otherwise the item
# has no cover_url and the Make scenario keeps its thumb_offset frame, as before.
def approved_covers(root=None):
    path = os.path.join(root or HERE, "covers", "index.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            items = json.load(fh).get("items", {})
    except (OSError, ValueError):
        return {}
    out = {}
    for pid, e in items.items():
        f = os.path.join(root or HERE, "covers", os.path.basename(e.get("file", "")))
        if e.get("file") and os.path.isfile(f):
            with open(f, "rb") as fh:
                if hashlib.sha256(fh.read()).hexdigest() == e.get("sha256"):
                    out[pid] = e["cover_url"]
    return out


def queue_item(i, r, video_url, caption, covers):
    item = {"id": i, "url": r["url"], "title": r["title"][:60], "video_url": video_url, "caption": caption}
    if i in covers:
        item["cover_url"] = covers[i]
    return item


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout)[-600:])


# --- the two opening lines -------------------------------------------------------------

TYPE_ROOMS_RE = (r"(?:דירת גן|דירת|דירה|דופלקס גג|דופלקס גן|דופלקס|דו משפחתי|קוטג'|בית פרטי|בית)"
                 r"\s+\d+(?:[.,]\d+)?(?:\s*(?:חדרים|חדרם|חד'))?")
LEAD_TYPE_RE = r"^(?:דו משפחתי|קוטג'|בית פרטי|מגרש|דירה)\s+"
PLACES = ("ראש העין", "פסגות אפק", "נווה אפק", "פסגות", "אפק", "אורנית", "רמת אלקנה", "אלקנה",
          "עץ אפרים", "שערי תקווה", "מתחם", "שכונת", "שכונה", "היישוב", "הישוב")


def _strip_places(seg):
    """Drop leading place words ("בפסגות אפק", "בשכונת ..."). Returns what is left."""
    s, changed = seg.strip(), True
    while changed and s:
        changed = False
        for p in PLACES:
            m = re.match(r"^[בהלמו]?" + re.escape(p) + r"(?:\s+|$)", s)
            if m:
                s, changed = s[m.end():].strip(), True
                break
    return s


def title_hook(title):
    """The marketing hook in Yossi's own listing title, verbatim, or None.

    "דירת 5 חדרים עורפית ליד שמורת טבע !!!" -> "עורפית ליד שמורת טבע". Type, rooms, "למכירה",
    exclamation marks and pure place names are taken out; nothing is added or reworded."""
    t = (title or "").split("|")[0]
    t = re.sub(r"!+", " ", t).replace("(", " ").replace(")", " ")
    t = re.sub(TYPE_ROOMS_RE, " ", t)
    t = re.sub(r"(?<!\S)למכירה(?!\S)", " ", t)
    cands = []
    for seg in re.split(r"\s[-–]\s|,|\s[-–](?=\S)", t):
        seg = re.sub(r"\s+", " ", seg).strip(" -–.")
        seg = re.sub(LEAD_TYPE_RE, "", seg).strip()
        rest = _strip_places(seg)
        if rest != seg and len(rest.split()) <= 1:
            continue  # the segment is a place ("בפסגות אפק", "בשכונת פסגות המבוקשת")
        seg = re.sub(r"^עם\s+", "", rest).strip()
        if 4 <= len(seg) <= 42:
            cands.append(seg)
    return max(cands, key=len) if cands else None


def cover_lines(r):
    head = (r.get("ptype") or "").replace(" - ", " ").strip()
    if head == "דירה":
        head = "דירת"
    # A plot (מגרש) carries a meaningless rooms value on the site - never print it.
    if "מגרש" in head:
        head = "מגרש למכירה"
    elif r.get("rooms"):
        head = (head + " " + str(r["rooms"]) + " חדרים").strip()
    line1 = " · ".join(p for p in (head, r.get("city")) if p)
    hook = title_hook(r.get("title"))
    if not hook:
        ov = HOOK_OVERRIDES.get(rid(r["url"]))
        hook = ov if ov and ov in (r.get("description") or "") else FALLBACK_HOOK
    return [line1, hook]


def _font(name, size):
    for d in FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    raise SystemExit("font not found: " + name)


def _shape(text):
    return text if RAQM else get_display(text)


def _fit(draw, text, size, width, extra=0):
    while size > 34:
        f = _font("Heebo-Bold.ttf", size)
        if draw.textlength(_shape(text), font=f) + extra <= width:
            return f
        size -= 2
    return f


def render_overlay(lines, path):
    """Transparent 1080x1920 PNG: a dark band with the two lines, in the blurred strip ABOVE
    the video. A 16:9 source sits at y 656-1264, and most of Yossi's videos open with their own
    title inside that frame. v3 put the band in the middle, right on top of those titles, and
    they showed through it (seen on the first full build, 2026-11-18). y 310-630 is clear of
    the video, inside the profile grid's 3:4 crop (y 240-1680), and below the Reels top bar."""
    W, H, band = 1080, 1920, 320
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    top = 310
    d.rectangle([0, top, W, top + band], fill=(10, 20, 35, 215))
    l1, l2 = lines
    f1 = _fit(d, l1, 72, 900)
    t1 = _shape(l1)
    d.text(((W - d.textlength(t1, font=f1)) / 2, top + 48), t1, font=f1, fill=(255, 255, 255, 255))
    gold = (255, 205, 80, 255)
    play = l2.startswith(PLAY)
    body = l2[len(PLAY):].strip() if play else l2
    sym = next((p for p in SYMBOL_FONTS if os.path.exists(p)), None) if play else None
    fs = ImageFont.truetype(sym, 60) if sym else None
    ws, gap = (d.textlength(PLAY, font=fs), 22) if fs else (0, 0)
    f2 = _fit(d, body, 80, 940, extra=ws + gap)
    t2 = _shape(body)
    w2 = d.textlength(t2, font=f2)
    x = (W - (w2 + gap + ws)) / 2
    y2 = top + 168
    d.text((x, y2), t2, font=f2, fill=gold)
    if fs:  # right-to-left: the play sign leads, so it sits on the right of the words
        d.text((x + w2 + gap, y2 + (f2.size - 60) * 0.55), PLAY, font=fs, fill=gold)
    img.save(path)


# --- video -----------------------------------------------------------------------------

def build_clean(yt_id, work, dest):
    src = os.path.join(work, yt_id + ".src.mp4")
    if os.path.exists(src):
        os.remove(src)
    # "res" sorts by the SMALLER side, so a vertical source (1080x2336) still comes in at
    # full width; a plain height<=1080 filter picked a ~500 px wide stream for it.
    run(["yt-dlp", "-q", "--no-playlist", "-f", "bv*+ba/b", "-S", "res:1080,ext:mp4:m4a",
         "--merge-output-format", "mp4", "-o", src, "https://www.youtube.com/watch?v=" + yt_id])
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-filter_complex", FILTER,
         "-map", "[v]", "-map", "0:a?", "-t", "900", *X264,
         "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
         "-movflags", "+faststart", dest])
    os.remove(src)


def fetch(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "yossimor-reels/1.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def overlay_video(clean, png, dest):
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", clean, "-i", png, "-filter_complex",
         f"[0:v][1:v]overlay=0:0:enable='lt(t,{OVERLAY_SECONDS})',format=yuv420p[v]",
         "-map", "[v]", "-map", "0:a?", *X264, "-c:a", "copy",
         "-movflags", "+faststart", dest])
    if os.path.getsize(dest) > MAX_BYTES:
        raise RuntimeError("video over the size limit")


def main():
    outdir, assets_file = sys.argv[1], sys.argv[2]
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    os.makedirs(outdir, exist_ok=True)
    recdir = os.path.join(HERE, "reels")
    os.makedirs(recdir, exist_ok=True)
    assets = set()
    if os.path.exists(assets_file):
        assets = {l.strip() for l in open(assets_file, encoding="utf-8") if l.strip()}
    props = json.load(open(os.path.join(HERE, "properties.json"), encoding="utf-8"))
    built, kept, novideo, failed = 0, 0, 0, 0
    queue = []
    covers = approved_covers()
    for r in props["eligible"]:
        i = rid(r["url"])
        path = os.path.join(recdir, i + ".json")
        old = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
        if only and only not in (i, r.get("yt_id")):
            if old.get("status") == "ok":
                queue.append(queue_item(i, r, old["video_url"], r["reel_caption"], covers))
            continue
        cover = cover_lines(r)
        rec = {"url": r["url"], "yt_id": r.get("yt_id"), "caption": r["reel_caption"],
               "video_url": None, "status": "no_video", "v": VERSION, "cover": cover}
        if r.get("yt_id"):
            same_src = old.get("yt_id") == r["yt_id"]
            clean_name, final_name = i + ".clean.mp4", i + ".mp4"
            try:
                if (same_src and old.get("v") == VERSION and old.get("cover") == cover
                        and final_name in assets and clean_name in assets):
                    kept += 1
                    print("KEEP ", i, r["yt_id"], file=sys.stderr)
                else:
                    clean = os.path.join(outdir, clean_name)
                    if same_src and clean_name in assets:
                        fetch(CLEAN_URL.format(id=i), clean)  # only the opening lines changed
                        new_clean = False
                    elif same_src and old.get("v") == 2 and final_name in assets:
                        # One-time move to v3: the v2 file on the release has no text on it,
                        # so it IS the clean master. Saves 33 YouTube downloads.
                        fetch(VIDEO_URL.format(id=i), clean)
                        new_clean = True
                    else:
                        # YouTube refuses GitHub's servers ("confirm you're not a bot", first
                        # run 2026-11-17), so brand-new videos get built on Yossi's machine.
                        build_clean(r["yt_id"], outdir, clean)
                        new_clean = True
                    png = os.path.join(outdir, i + ".overlay.png")
                    render_overlay(cover, png)
                    overlay_video(clean, png, os.path.join(outdir, final_name))
                    os.remove(png)
                    if not new_clean:
                        os.remove(clean)  # unchanged master: no need to upload it again
                    built += 1
                    print("BUILT", i, r["yt_id"], " | ".join(cover), file=sys.stderr)
                rec.update(video_url=VIDEO_URL.format(id=i), status="ok")
            except Exception as e:  # noqa
                rec["status"] = "unavailable"
                rec["error"] = str(e)[-300:]
                failed += 1
                print("FAIL ", i, r["yt_id"], str(e)[-200:], file=sys.stderr)
        else:
            novideo += 1
            print("NOVID", i, file=sys.stderr)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        if rec["status"] == "ok":
            queue.append(queue_item(i, r, rec["video_url"], rec["caption"], covers))
    # The Make scenario rotates through this list only, so a property without a ready video
    # is skipped instead of costing the day its Reel (Yossi approved 2026-11-18).
    with open(os.path.join(recdir, "queue.json"), "w", encoding="utf-8") as f:
        json.dump({"items": queue}, f, ensure_ascii=False, indent=1)
    print(f"built {built}  kept {kept}  no_video {novideo}  unavailable {failed}  queue {len(queue)}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
