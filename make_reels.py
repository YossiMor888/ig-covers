# -*- coding: utf-8 -*-
"""
Build the Instagram Reel for each queued property from its own YouTube video.

Rule (Yossi, 2026-11-17): Instagram publishes the property's YouTube video as a Reel, never
photos. A property with no usable video gets a record with video_url = null, and the Make
scenario then sends a Telegram alert and posts nothing. Caption = the site's own copy plus
hashtags, NO price (built by extract_queue.build_reel_caption).

Per property, keyed by md5(url) exactly like the covers (the Make scenario already computes it):
  reels/<id>.json  - {url, yt_id, caption, video_url|null, status}   (committed to the repo)
  <outdir>/<id>.mp4 - 1080x1920 H.264, blurred-fill background      (uploaded as a release asset,
                      never committed - 30+ videos in git history would bloat the public repo)

Usage: python make_reels.py <outdir> <assets.txt> [--only <yt_id or id>]
  assets.txt = names of the files already on the release (one per line), so unchanged videos
  are not downloaded and converted again every day.
Needs: yt-dlp, ffmpeg on PATH.
"""
import hashlib, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = "YossiMor888/ig-covers"
TAG = "reels"
VIDEO_URL = "https://github.com/" + REPO + "/releases/download/" + TAG + "/{id}.mp4"
VERSION = 2  # bump to force every reel to be rebuilt (2: bitrate capped for Instagram)
MAX_BYTES = 900 * 1024 * 1024  # Instagram's limit is 1 GB; release assets allow 2 GB
FILTER = (
    "[0:v]split=2[a][b];"
    "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:2[bg];"
    "[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30,format=yuv420p[v]"
)


def rid(url):
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout)[-600:])


def build(yt_id, work, dest):
    src = os.path.join(work, yt_id + ".src.mp4")
    if os.path.exists(src):
        os.remove(src)
    # "res" sorts by the SMALLER side, so a vertical source (1080x2336) still comes in at
    # full width; a plain height<=1080 filter picked a ~500 px wide stream for it.
    run(["yt-dlp", "-q", "--no-playlist", "-f", "bv*+ba/b", "-S", "res:1080,ext:mp4:m4a",
         "--merge-output-format", "mp4", "-o", src, "https://www.youtube.com/watch?v=" + yt_id])
    # Instagram Reel spec (Make CreateAReelPost): H.264 closed GOP, 4:2:0, 23-60 fps,
    # video VBR 5 Mbps max, AAC 48 kHz max stereo 128k, moov atom first, 3 s - 15 min.
    # The first encode at plain crf 23 came out at ~5.2 Mbps, so the rate is capped.
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-filter_complex", FILTER,
         "-map", "[v]", "-map", "0:a?", "-t", "900",
         "-c:v", "libx264", "-preset", "medium", "-crf", "23",
         "-maxrate", "4500k", "-bufsize", "9000k", "-profile:v", "high", "-g", "60",
         "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
         "-movflags", "+faststart", dest])
    if os.path.getsize(dest) > MAX_BYTES:
        raise RuntimeError("video over the size limit")
    os.remove(src)


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
    for r in props["eligible"]:
        i = rid(r["url"])
        if only and only not in (i, r.get("yt_id")):
            continue
        path = os.path.join(recdir, i + ".json")
        old = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
        rec = {"url": r["url"], "yt_id": r.get("yt_id"), "caption": r["reel_caption"],
               "video_url": None, "status": "no_video", "v": VERSION}
        if r.get("yt_id"):
            same = old.get("yt_id") == r["yt_id"] and old.get("v") == VERSION
            # The video file already on the release is what counts: YouTube blocks downloads
            # from GitHub's servers ("confirm you're not a bot", seen on the first run
            # 2026-11-17), so new videos are built on Yossi's machine and the daily GitHub
            # run only has to see that the file is there.
            if same and (i + ".mp4") in assets:
                rec.update(video_url=VIDEO_URL.format(id=i), status="ok")
                kept += 1
                print("KEEP ", i, r["yt_id"], file=sys.stderr)
            else:
                try:
                    build(r["yt_id"], outdir, os.path.join(outdir, i + ".mp4"))
                    rec.update(video_url=VIDEO_URL.format(id=i), status="ok")
                    built += 1
                    print("BUILT", i, r["yt_id"], file=sys.stderr)
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
    print(f"built {built}  kept {kept}  no_video {novideo}  unavailable {failed}", file=sys.stderr)


if __name__ == "__main__":
    main()
