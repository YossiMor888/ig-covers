# -*- coding: utf-8 -*-
"""
Extract publish-ready Facebook post records from yossimor.com property pages.

Design rule (from the אליהו/אלישע incident): NO summarization, NO LLM rewriting of
facts. The post body is the site's own marketing copy, copied verbatim. Agent name,
phone and price are pulled from structured markup, not from prose.
"""
import re, json, sys, html, urllib.request, urllib.parse, time

SITEMAP = "https://yossimor.com/?sitemap=property"
PHONE_RE = r"05\d[\-\s]?\d{3}[\-\s]?\d{4}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; yossimor-autopost/1.0)"}


def fetch(url, retries=2):
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt == retries:
                raise
            time.sleep(2)


def clean_html_text(frag):
    """h2 marketing block -> plain text, preserving line breaks and emojis."""
    frag = re.sub(r"<br\s*/?>", "\n", frag, flags=re.I)
    frag = re.sub(r"</?(h2|p|div|span|strong|em|b|i)[^>]*>", "", frag, flags=re.I)
    frag = re.sub(r"<[^>]+>", "", frag)
    frag = html.unescape(frag)
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in frag.split("\n")]
    return "\n".join(l for l in lines if l)


def parse_property(url, raw):
    flat = raw.replace("\n", "")
    d = {"url": url}

    # --- status tags (property type, מכירה / נמכר, בלעדי) ---
    d["tags"] = re.findall(r'rel="tag">([^<]*)</a>', flat)
    d["sold"] = any("נמכר" in t for t in d["tags"])
    d["for_sale"] = any("מכירה" in t for t in d["tags"])
    d["exclusive"] = any("בלעדי" in t for t in d["tags"])

    # --- title ---
    m = re.search(r"<title>(.*?)</title>", flat)
    title = html.unescape(m.group(1)).strip() if m else ""
    d["title"] = re.sub(r"\s*-\s*יוסי מור\s*$", "", title)

    # --- price (structured markup, never prose) ---
    m = re.search(r'<div class="price">₪<span>([^<]*)</span>', flat)
    d["price"] = m.group(1).strip() if m else None

    # --- cover-overlay facts (added 2026-11-10, Instagram first-photo text) ---
    # Structured fields only, never prose and never model-written: the property type is a
    # category tag, rooms is the page's "Bedrooms" meta item, city is the JSON-LD
    # addressLocality. Any of them may be missing on a given page - a missing one is
    # simply left out of the overlay, never guessed. Neighborhood is NOT a structured
    # field on this site, so it is deliberately not attempted.
    m = re.search(r'([\d.]+)\s*<span class="tooltip">Bedrooms</span>', flat)
    d["rooms"] = m.group(1).strip() if m else None
    ptype = [t.strip() for t in d["tags"]
             if t.strip() and not any(w in t for w in ("מכירה", "השכרה", "נמכר", "בלעדי"))]
    d["ptype"] = html.unescape(ptype[0]) if ptype else None
    m = re.search(r'"addressLocality"\s*:\s*"([^"]+)"', flat)
    d["city"] = html.unescape(m.group(1)).strip() if m else None

    # --- agent name + phone (structured markup) ---
    # The site moved from Plain permalinks (?agent=slug) to pretty ones (/agent/slug/)
    # between 2026-10-06 and 10-07; accept both so neither form silently drops the phone.
    m = re.search(r'(?:\?agent=|/agent/)[^"]*">([^<]*)</a>', flat)
    d["agent_name"] = html.unescape(m.group(1)).strip() if m else None
    d["agent_phone"] = None
    if m:
        after = flat[m.end():m.end() + 900]
        p = re.search(r"<li>\s*(0\d[\d\-\s]{7,12})\s*</li>", after)
        if p:
            d["agent_phone"] = re.sub(r"[\s\-]", "", p.group(1))

    # --- gallery images ---
    # Scoped to the property's own carousel container, so sidebar/related-listing
    # thumbnails can't leak in. Size suffixes (-1040x641 etc.) are stripped so we
    # post the full-resolution original; some properties have no resized variant
    # at all, which is why we can't key on the size suffix itself.
    d["images"] = []
    i = flat.find("carousel-wrap")
    if i >= 0:
        seg = flat[i:]
        j = seg.find("</div></div></div>")
        seg = seg[:j] if j > 0 else seg[:30000]
        imgs = re.findall(
            r'(https://yossimor\.com/wp-content/uploads/[^"\'\s]+?)(?:-\d+x\d+)?(\.(?:jpe?g|png))',
            seg,
        )
        # Branding assets (the agent headshot, logos, site credits) sit inside the
        # same carousel markup and would otherwise appear as a "property photo".
        BRANDING = ("dan06164", "רקע-לבן", "logo", "websitecredit", "wh.png",
                    "title-bg", "/2014/", "/2015/", "/2016/")
        seen, gallery = set(), []
        for base, ext in imgs:
            full = base + ext
            low = full.lower()
            if any(b in low for b in BRANDING):
                continue
            if full not in seen:
                seen.add(full)
                gallery.append(full)
        d["images"] = gallery

    # --- marketing copy: the site's own words, verbatim ---
    # Two authoring formats exist on the site: older listings put the copy in <h2>
    # blocks, newer ones in <p data-start=".."> paragraphs. Take both, keep order,
    # dedupe. Nothing here is rewritten or summarized.
    blocks = re.findall(r"<h2[^>]*>(.*?)</h2>", flat, flags=re.S)
    blocks += re.findall(
        r'<p data-start="\d+" data-end="\d+">(.*?)</p>', flat, flags=re.S
    )
    parts, seen = [], set()
    for b in blocks:
        t = clean_html_text(b).lstrip(": ").strip()
        if len(t) > 40 and t not in seen:
            seen.add(t)
            parts.append(t)
    d["description"] = "\n\n".join(parts).strip()

    body = d["description"]
    # Does the body already carry its own contact block? Several listings do,
    # and appending a second one reads badly.
    # Suppress our contact block ONLY when the body already carries a real phone
    # number. A call-to-action phrase like "לפרטים ותיאום סיור" is not a contact
    # block - suppressing on that alone published a post with no phone at all.
    # Phones appear as 0505679639, 050-5679639 AND 050-567-9639 on this site,
    # so allow separators anywhere - matching only one variant caused a post to
    # get two contact blocks.
    d["contact_in_body"] = bool(re.search(PHONE_RE, body))

    # Compare the price stated in the body against the structured price field.
    # A DIFFERENT number means the site itself is inconsistent - never publish
    # that, it would show two prices for one property.
    pd = re.sub(r"[^0-9]", "", d.get("price") or "")
    # Every numeric run in the body, normalized. Prices are written both as
    # 5,850,000 and 5850000, so we can't key on comma grouping alone.
    body_nums = {re.sub(r"[^0-9]", "", m) for m in re.findall(r"[\d,]{5,}", body)}
    # Asking prices are round and 6-8 digits; anything else is a parcel no.,
    # a phone or an ID, and must not be treated as a competing price.
    body_prices = {
        n for n in body_nums
        if 6 <= len(n) <= 8 and n.endswith("000") and not n.startswith("05")
    }
    d["price_in_body"] = bool(pd and pd in body_prices)
    d["price_conflict"] = bool(pd and body_prices and pd not in body_prices)
    return d


def build_post(d):
    """Assemble the caption. Every fact here came from structured extraction."""
    body = d["description"]
    lines = [body, ""]
    # Only add the price line if the site's own copy doesn't already state it,
    # otherwise the post shows the price twice.
    if d.get("price") and not d.get("price_in_body"):
        lines.append(f"💰 מחיר מבוקש: {d['price']} ₪")
    # Several listings end with their own contact block; don't append a second.
    if not d.get("contact_in_body"):
        lines.append("")
        lines.append("לפרטים ותיאום ביקור:")
        if d.get("agent_name"):
            lines.append(d["agent_name"])
        if d.get("agent_phone"):
            lines.append(f"📞 {d['agent_phone']}")
    lines.append("")
    lines.append(urllib.parse.unquote(d["url"]))
    return "\n".join(lines).strip()


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    sm = fetch(SITEMAP)
    urls = re.findall(r"<loc>([^<]*)</loc>", sm)
    # Old form ?property=slug, new form /property/slug/. The bare /property/ archive
    # page is also in the sitemap and must not be treated as a listing.
    urls = [u for u in urls if re.search(r"\?property=|/property/[^/]+", u)]
    if limit:
        urls = urls[:limit]

    records, skipped = [], []
    for i, u in enumerate(urls, 1):
        try:
            d = parse_property(u, fetch(u))
        except Exception as e:
            skipped.append({"url": u, "reason": f"fetch/parse error: {e}"})
            continue

        reason = None
        if d["sold"]:
            reason = "נמכר"
        elif not d["for_sale"]:
            reason = "no מכירה tag"
        elif len(d["images"]) < 1:
            reason = "no gallery images"
        elif len(d["description"]) < 150:
            reason = "description too short"
        elif not d["agent_phone"]:
            reason = "no agent phone"
        elif d["price_conflict"]:
            reason = "price conflict (description vs price field)"

        if reason:
            skipped.append({"url": u, "reason": reason, "title": d.get("title")})
        else:
            d["post_text"] = build_post(d)
            records.append(d)
        print(f"[{i}/{len(urls)}] {'SKIP:'+reason if reason else 'OK'}  {d.get('title','')[:50]}",
              file=sys.stderr)

    out = {"generated": time.strftime("%Y-%m-%d %H:%M"),
           "eligible": records, "skipped": skipped}
    with open("properties.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\nELIGIBLE: {len(records)}   SKIPPED: {len(skipped)}", file=sys.stderr)


if __name__ == "__main__":
    main()
