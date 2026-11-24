#!/usr/bin/env python3
"""Fetch news feeds, let Gemini choose the most important stories and
rewrite them in Dutch at CEFR level B1 (using the Gemini API), with a glossary per story.
Standard library only. Writes data/news.json."""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "news.json")

# Models are tried in this order. Google renames models and changes free limits
# from time to time: if one fails, change this list (or set the GEMINI_MODELS secret/env).
MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS", "gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite").split(",") if m.strip()]
API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
PER_CATEGORY = int(os.environ.get("PER_CATEGORY", "4"))
MAX_CANDIDATES = 15
UA = "Mozilla/5.0 (compatible; NieuwsB1/1.0)"

# Edit this list freely. A feed that fails is skipped.
CATEGORIES = {
    "nl": {
        "label": "Nederland",
        "brief": "Important news in the Netherlands: politics, economy, housing, health, safety, weather warnings, "
                 "big incidents, rules and prices that affect people living in the Netherlands.",
        "feeds": [
            ("NOS", "https://feeds.nos.nl/nosnieuwsalgemeen"),
            ("NU.nl", "https://www.nu.nl/rss/Algemeen"),
        ],
    },
    "tr": {
        "label": "Turkije",
        "brief": "Important news about Turkey: politics, economy and lira, earthquakes and disasters, foreign policy, "
                 "relations with the Netherlands and the EU. Skip celebrity news.",
        "feeds": [
            ("Hürriyet Daily News", "https://www.hurriyetdailynews.com/rss"),
            ("Daily Sabah", "https://www.dailysabah.com/rssFeed/turkey"),
            ("Google Nieuws", "https://news.google.com/rss/search?q=Turkey+when:1d&hl=en-US&gl=US&ceid=US:en"),
        ],
    },
    "sport": {
        "label": "Sport",
        "brief": "Important sport news and results: Dutch and Turkish national teams, clubs and athletes "
                 "(football, speed skating, cycling, F1, tennis), plus big international events.",
        "feeds": [
            ("NOS Sport", "https://feeds.nos.nl/nossportalgemeen"),
            ("NU.nl Sport", "https://www.nu.nl/rss/Sport"),
            ("Google Nieuws", "https://news.google.com/rss/search?q=T%C3%BCrkiye+spor+when:1d&hl=tr&gl=TR&ceid=TR:tr"),
        ],
    },
}

SYSTEM = """You are the editor of a daily news page for an adult learner of Dutch at CEFR level B1.
You receive candidate news items (title, summary, source) and must choose and rewrite the most important ones.

Rules for every story you write:
- Language: Dutch, level B1. Short sentences (mostly under 15 words), common words, simple tenses
  (present, perfect, simple past of common verbs). No idioms, no passive-heavy sentences. Explain hard terms in plain words.
- Title: maximum 12 words, clear, not clickbait.
- Text: 4 to 6 sentences, about 70 to 110 words. Answer who, what, where and why it matters.
- Use ONLY facts that are in the given title and summary. Never invent numbers, names, quotes or causes.
  If the summary is thin, write fewer sentences instead of guessing.
- Translate from English or Turkish into Dutch where needed.
- Choose stories that matter most. Avoid duplicates (the same event from two sources: pick one) and gossip.

Glossary: for each story give 5 to 8 words or short expressions from YOUR text that a B1 learner may not know
(skip very basic words). For each entry:
  "forms": array of the exact lowercase spellings as they appear in your text (e.g. ["verhoogd","verhoogt"]),
  "lemma": dictionary form (nouns with de/het, verbs as infinitive),
  "pos": part of speech in English, short,
  "en": English meaning,
  "example_nl": a NEW natural B1 sentence using the word (not copied from your text),
  "example_en": English translation of that sentence,
  "grammar": 1 to 2 short sentences in English: de/het and plural, verb forms (irregular, separable), word order, or a useful pattern.

Answer with ONLY valid JSON, no markdown."""


def log(*a):
    print(*a, file=sys.stderr)


def http_get(url, timeout=20, limit=600_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit)


def clean(t):
    t = re.sub(r"<[^>]+>", " ", t or "")
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def parse_feed(source, data):
    root = ET.fromstring(data)
    out = []
    for it in root.iter():
        if it.tag.split("}")[-1] not in ("item", "entry"):
            continue

        def text(name):
            for ch in it:
                if ch.tag.split("}")[-1] == name and (ch.text or "").strip():
                    return ch.text.strip()
            return ""

        title = clean(text("title"))
        link = text("link")
        if not link:
            for ch in it:
                if ch.tag.split("}")[-1] == "link" and ch.get("href"):
                    link = ch.get("href")
                    break
        raw_desc = text("description") or text("summary") or text("content") or text("encoded")
        pub = parse_date(text("pubDate") or text("published") or text("updated") or text("date"))

        image = None
        for ch in it.iter():
            t = ch.tag.split("}")[-1]
            u = ch.get("url")
            if u and t in ("thumbnail", "content", "enclosure"):
                kind = (ch.get("type") or ch.get("medium") or "").lower()
                if t == "thumbnail" or "image" in kind:
                    image = u
                    break
        if not image:
            m = re.search(r'<img[^>]+src=["\']([^"\']+)', html.unescape(raw_desc or ""))
            if m:
                image = m.group(1)

        if title and link:
            out.append({
                "title": title,
                "summary": clean(raw_desc)[:600],
                "link": link.strip(),
                "image": image,
                "published": pub.isoformat(timespec="seconds") if pub else None,
                "source": source,
            })
    return out


def og_image(url):
    if "news.google.com" in url:
        return None
    try:
        page = http_get(url, timeout=12, limit=200_000).decode("utf-8", "ignore")
    except Exception:
        return None
    for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']'):
        m = re.search(pat, page, re.I)
        if m:
            return html.unescape(m.group(1))
    return None


def fix_image(u):
    if not u:
        return None
    if u.startswith("//"):
        u = "https:" + u
    if u.startswith("http://"):
        u = "https://" + u[7:]
    return u if u.startswith("https://") else None


def collect(cat):
    seen, items = set(), []
    for source, url in CATEGORIES[cat]["feeds"]:
        try:
            got = parse_feed(source, http_get(url))
        except Exception as e:
            log(f"  ! {source}: {e}")
            continue
        log(f"  {source}: {len(got)} items")
        for it in got[:12]:
            if it["link"] not in seen:
                seen.add(it["link"])
                items.append(it)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(timespec="seconds")
    items = [i for i in items if not i["published"] or i["published"] >= cutoff]
    items.sort(key=lambda i: i["published"] or "", reverse=True)
    return items[:MAX_CANDIDATES]


def gemini(user):
    """Try each model in MODELS until one answers. Returns parsed JSON."""
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.4,
            "maxOutputTokens": 16000,
        },
    }).encode()
    last = None
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(2):
            req = urllib.request.Request(
                url, data=body,
                headers={"content-type": "application/json", "x-goog-api-key": API_KEY})
            try:
                with urllib.request.urlopen(req, timeout=240) as r:
                    data = json.load(r)
                parts = data["candidates"][0]["content"]["parts"]
                txt = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                out = json.loads(txt[txt.index("{"): txt.rindex("}") + 1])
                log(f"  model {model} ok")
                return out
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "ignore")[:300]
                last = f"{model}: HTTP {e.code} {detail}"
                log(f"  ! {last}")
                if e.code == 429 and attempt == 0:
                    time.sleep(65)  # per-minute limit: wait and retry once
                    continue
                break  # other errors (404 unknown model, 403, daily limit): next model
            except Exception as e:
                last = f"{model}: {e}"
                log(f"  ! {last}")
                break
    raise RuntimeError(last or "no model answered")


def write_category(cat, cands):
    meta = CATEGORIES[cat]
    listing = [{"idx": i, "source": c["source"], "published": c["published"],
                "title": c["title"], "summary": c["summary"]} for i, c in enumerate(cands)]
    user = (f"Category: {meta['label']}\n{meta['brief']}\n\n"
            f"Choose the {PER_CATEGORY} most important stories from the candidates below and write them.\n"
            f"Return JSON in this shape:\n"
            '{"items":[{"idx":0,"title":"...","text":"...","glossary":[{"forms":["..."],"lemma":"...","pos":"...",'
            '"en":"...","example_nl":"...","example_en":"...","grammar":"..."}]}]}\n\n'
            f"Candidates:\n{json.dumps(listing, ensure_ascii=False)}")
    try:
        result = gemini(user)
    except Exception as e:
        log(f"  ! all models failed: {e}")
        return []

    items = []
    for r in result.get("items", [])[:PER_CATEGORY]:
        try:
            c = cands[int(r["idx"])]
        except Exception:
            continue
        title, text = (r.get("title") or "").strip(), (r.get("text") or "").strip()
        if not title or not text:
            continue
        glossary = []
        for g in (r.get("glossary") or [])[:10]:
            if g.get("lemma") and g.get("en"):
                forms = [str(f).lower() for f in (g.get("forms") or [])]
                glossary.append({
                    "word": forms[0] if forms else str(g["lemma"]).lower(),
                    "forms": forms,
                    "lemma": g.get("lemma", ""),
                    "pos": g.get("pos", ""),
                    "en": g.get("en", ""),
                    "example_nl": g.get("example_nl", ""),
                    "example_en": g.get("example_en", ""),
                    "grammar": g.get("grammar", ""),
                })
        items.append({
            "id": hashlib.sha1(c["link"].encode()).hexdigest()[:10],
            "category": cat,
            "title": title,
            "text": text,
            "url": c["link"],
            "image": fix_image(c["image"]) or fix_image(og_image(c["link"])),
            "source": c["source"],
            "published": c["published"],
            "glossary": glossary,
        })
    return items


def main():
    if not API_KEY:
        log("GEMINI_API_KEY is missing. Add it as a repository secret.")
        sys.exit(1)

    old = {}
    try:
        with open(OUT, encoding="utf-8") as f:
            for it in json.load(f).get("items", []):
                old.setdefault(it["category"], []).append(it)
    except Exception:
        pass

    all_items = []
    for cat in CATEGORIES:
        log(f"[{cat}]")
        cands = collect(cat)
        new = write_category(cat, cands) if cands else []
        time.sleep(15)  # stay under the free per-minute limit
        if not new:
            log(f"  no new items for {cat}, keeping the previous ones")
            new = old.get(cat, [])
        all_items += new
        log(f"  -> {len(new)} stories")

    if not all_items:
        log("Nothing to write.")
        sys.exit(1)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "items": all_items}, f, ensure_ascii=False, indent=1)
    log(f"Wrote {len(all_items)} stories to {OUT}")


if __name__ == "__main__":
    main()
