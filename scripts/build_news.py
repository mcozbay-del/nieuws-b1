#!/usr/bin/env python3
"""Fetch news from many sources, let Gemini choose the most important stories per
source and rewrite them in Dutch at CEFR level B1 (with a glossary per story).

The app filters by source on the phone, so changing your sources in the app
works immediately. Standard library only.

Writes:  data/news.json            (latest day)
         data/archive/DATE.json    (one file per day, last KEEP_DAYS days)
         data/archive/index.json   (list of dates)
"""
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
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(DATA, "news.json")
ARCHIVE = os.path.join(DATA, "archive")
KEEP_DAYS = 60

# Models are tried in this order. Google renames models and changes free limits
# from time to time: if one fails, edit this list (or set the GEMINI_MODELS env).
MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS", "gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite").split(",") if m.strip()]
API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

PER_SOURCE = 2            # stories written per source per day
CANDIDATES_PER_SOURCE = 8 # recent items per source shown to the model
CHUNK = 4                 # sources per model call
PAUSE = 12                # seconds between calls (free tier: requests per minute)
UA = "Mozilla/5.0 (compatible; NieuwsB1/1.0)"

CATEGORIES = {
    "nl": {"label": "Nederland",
           "brief": "Important news in the Netherlands: politics, economy, housing, health, safety, weather warnings, "
                    "big incidents, and rules or prices that affect people living in the Netherlands."},
    "tr": {"label": "Turkije",
           "brief": "Important news about Turkey: politics, economy and lira, courts, earthquakes and disasters, "
                    "foreign policy, relations with the Netherlands and the EU. Skip celebrity news."},
    "sport": {"label": "Sport",
              "brief": "Important sport news and results: Dutch and Turkish national teams, clubs and athletes "
                       "(football, speed skating, cycling, F1, tennis) and big international events."},
}

# (id, name, category, rough political leaning label, feed url, on by default)
# The leaning labels are only a rough, commonly used indication, not a judgement of every article.
# Feeds that do not work are skipped and shown as "geen feed" in the app. Add or remove lines freely.
SOURCES = [
    # --- Nederland ---
    ("nos",        "NOS",              "nl", "publieke omroep",   "https://feeds.nos.nl/nosnieuwsalgemeen", True),
    ("nu",         "NU.nl",            "nl", "midden",            "https://www.nu.nl/rss/Algemeen", True),
    ("volkskrant", "de Volkskrant",    "nl", "midden-links",      "https://www.volkskrant.nl/voorpagina/rss.xml", True),
    ("trouw",      "Trouw",            "nl", "midden-links",      "https://www.trouw.nl/voorpagina/rss.xml", True),
    ("parool",     "Het Parool",       "nl", "midden-links",      "https://www.parool.nl/voorpagina/rss.xml", True),
    ("joop",       "Joop",             "nl", "links",             "https://www.bnnvara.nl/joop/rss", True),
    ("nrc",        "NRC",              "nl", "midden (liberaal)", "https://www.nrc.nl/rss/", True),
    ("ad",         "AD",               "nl", "midden",            "https://www.ad.nl/home/rss.xml", True),
    ("telegraaf",  "De Telegraaf",     "nl", "midden-rechts",     "https://www.telegraaf.nl/rss", True),
    ("tpo",        "The Post Online",  "nl", "rechts",            "https://www.tpo.nl/feed/", False),
    ("nltimes",    "NL Times",         "nl", "midden (Engels)",   "https://nltimes.nl/rss", False),
    # --- Turkije ---
    ("bbc_tr",     "BBC Türkçe",       "tr", "internationaal",    "https://feeds.bbci.co.uk/turkce/rss.xml", True),
    ("dw_tr",      "DW Türkçe",        "tr", "internationaal",    "https://rss.dw.com/xml/rss-tur-all", True),
    ("guardian_tr","The Guardian Turkey","tr","midden-links",     "https://www.theguardian.com/world/turkey/rss", True),
    ("birgun",     "BirGün",           "tr", "links, oppositie",  "https://www.birgun.net/rss/home", True),
    ("cumhuriyet", "Cumhuriyet",       "tr", "seculier, oppositie","https://www.cumhuriyet.com.tr/rss", True),
    ("bianet",     "bianet",           "tr", "onafhankelijk, links","https://bianet.org/english/rss", True),
    ("evrensel",   "Evrensel",         "tr", "links (socialistisch)","https://www.evrensel.net/rss/haber.xml", False),
    ("t24",        "T24",              "tr", "liberaal, oppositie","https://t24.com.tr/rss", False),
    ("turkishminute","Turkish Minute", "tr", "oppositie (Engels)","https://turkishminute.com/feed/", False),
    ("dailysabah", "Daily Sabah",      "tr", "regeringsgezind",   "https://www.dailysabah.com/rssFeed/turkey", False),
    ("google_tr",  "Google Nieuws (Turkije)", "tr", "gemengd",
     "https://news.google.com/rss/search?q=Turkey+when:1d&hl=en-US&gl=US&ceid=US:en", False),
    # --- Sport ---
    ("nos_sport",  "NOS Sport",        "sport", "publieke omroep", "https://feeds.nos.nl/nossportalgemeen", True),
    ("nu_sport",   "NU.nl Sport",      "sport", "midden",          "https://www.nu.nl/rss/Sport", True),
    ("google_spor","Google Nieuws (Turkse sport)", "sport", "gemengd",
     "https://news.google.com/rss/search?q=T%C3%BCrkiye+spor+when:1d&hl=tr&gl=TR&ceid=TR:tr", True),
    ("bbc_football","BBC Sport Football","sport", "internationaal", "https://feeds.bbci.co.uk/sport/football/rss.xml", False),
    ("guardian_football","The Guardian Football","sport","internationaal", "https://www.theguardian.com/football/rss", False),
]


def src_dict(t):
    return {"id": t[0], "name": t[1], "cat": t[2], "lean": t[3], "url": t[4], "default": t[5]}


SYSTEM = """You are the editor of a daily news page for an adult learner of Dutch at CEFR level B1.
You receive candidate news items (title, summary, source) from several outlets and must choose and rewrite the most important ones.

Selection:
- For EACH source, choose up to {per} stories that matter most to a reader living in the Netherlands (fewer if nothing is important).
  Choose from every source you are given; do not skip a source that has important items.
- Give each story "importance" from 1 to 5: 5 = major news that affects many people, 1 = minor.

Rules for every story you write:
- Language: Dutch, level B1. Short sentences (mostly under 15 words), common words, simple tenses
  (present, perfect, simple past of common verbs). No idioms, little passive voice. Explain hard terms in plain words.
- Title: maximum 12 words, clear, neutral, not clickbait.
- Text: 4 to 6 sentences, about 70 to 110 words. Answer who, what, where and why it matters.
- Keep the tone neutral and factual. Do not add your own opinion or political framing. If the summary contains an
  opinion or a claim, say whose it is (for example "Volgens de krant ..." or "De regering zegt dat ...").
- Use ONLY facts that are in the given title and summary. Never invent numbers, names, quotes or causes.
  If the summary is thin, write fewer sentences instead of guessing.
- Translate from English or Turkish into Dutch where needed.

Glossary: for each story give 5 or 6 words or short expressions from YOUR text that a B1 learner may not know
(skip very basic words). For each entry:
  "forms": array of the exact lowercase spellings as they appear in your text (e.g. ["verhoogd","verhoogt"]),
  "lemma": dictionary form (nouns with de/het, verbs as infinitive),
  "pos": part of speech in English, short,
  "en": English meaning,
  "example_nl": a NEW natural B1 sentence using the word (not copied from your text),
  "example_en": English translation of that sentence,
  "grammar": 1 to 2 short sentences in English: de/het and plural, verb forms (irregular, separable), word order, or a useful pattern.

Answer with ONLY valid JSON, no markdown.""".replace("{per}", str(PER_SOURCE))


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
        page = http_get(url, timeout=8, limit=200_000).decode("utf-8", "ignore")
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


def collect_source(src):
    try:
        got = parse_feed(src["name"], http_get(src["url"]))
    except Exception as e:
        log(f"  ! {src['name']}: {e}")
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(timespec="seconds")
    got = [i for i in got if not i["published"] or i["published"] >= cutoff]
    got.sort(key=lambda i: i["published"] or "", reverse=True)
    log(f"  {src['name']}: {len(got)} recent items")
    return got[:CANDIDATES_PER_SOURCE]


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


def write_chunk(cat, chunk, pool):
    meta = CATEGORIES[cat]
    cands = []
    for s in chunk:
        for c in pool[s["id"]]:
            cands.append(dict(c, source_id=s["id"], source=s["name"], lean=s["lean"]))
    listing = [{"idx": i, "source": c["source"], "published": c["published"],
                "title": c["title"], "summary": c["summary"]} for i, c in enumerate(cands)]
    user = (f"Category: {meta['label']}\n{meta['brief']}\n\n"
            f"Sources in this batch: {', '.join(s['name'] for s in chunk)}.\n"
            f"Choose up to {PER_SOURCE} stories per source and write them.\n"
            f"Return JSON in this shape:\n"
            '{"items":[{"idx":0,"importance":3,"title":"...","text":"...","glossary":[{"forms":["..."],"lemma":"...",'
            '"pos":"...","en":"...","example_nl":"...","example_en":"...","grammar":"..."}]}]}\n\n'
            f"Candidates:\n{json.dumps(listing, ensure_ascii=False)}")
    try:
        result = gemini(user)
    except Exception as e:
        log(f"  ! all models failed for this batch: {e}")
        return None

    items, per_source = [], {}
    for r in result.get("items", []):
        try:
            c = cands[int(r["idx"])]
        except Exception:
            continue
        if per_source.get(c["source_id"], 0) >= PER_SOURCE:
            continue
        title, text = (r.get("title") or "").strip(), (r.get("text") or "").strip()
        if not title or not text:
            continue
        glossary = []
        for g in (r.get("glossary") or [])[:8]:
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
        try:
            importance = max(1, min(5, int(r.get("importance", 3))))
        except Exception:
            importance = 3
        per_source[c["source_id"]] = per_source.get(c["source_id"], 0) + 1
        items.append({
            "id": hashlib.sha1(c["link"].encode()).hexdigest()[:10],
            "category": cat,
            "source_id": c["source_id"],
            "source": c["source"],
            "lean": c["lean"],
            "importance": importance,
            "title": title,
            "text": text,
            "url": c["link"],
            "image": fix_image(c["image"]) or fix_image(og_image(c["link"])),
            "published": c["published"],
            "glossary": glossary,
        })
    return items


def today_nl():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Amsterdam")).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


def save(payload, day):
    os.makedirs(ARCHIVE, exist_ok=True)
    for path in (OUT, os.path.join(ARCHIVE, f"{day}.json")):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    days = sorted((f[:-5] for f in os.listdir(ARCHIVE) if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", f)), reverse=True)
    for old in days[KEEP_DAYS:]:
        os.remove(os.path.join(ARCHIVE, f"{old}.json"))
    days = days[:KEEP_DAYS]
    with open(os.path.join(ARCHIVE, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"dates": days}, f)


def main():
    if not API_KEY:
        log("GEMINI_API_KEY is missing. Add it as a repository secret.")
        sys.exit(1)

    old_by_source = {}
    try:
        with open(OUT, encoding="utf-8") as f:
            for it in json.load(f).get("items", []):
                if it.get("source_id"):
                    old_by_source.setdefault(it["source_id"], []).append(it)
    except Exception:
        pass

    sources = [src_dict(t) for t in SOURCES]
    all_items, meta = [], []

    for cat in CATEGORIES:
        log(f"[{cat}]")
        cat_sources = [s for s in sources if s["cat"] == cat]
        pool = {s["id"]: collect_source(s) for s in cat_sources}
        live = [s for s in cat_sources if pool[s["id"]]]
        got = {s["id"]: 0 for s in cat_sources}
        for i in range(0, len(live), CHUNK):
            chunk = live[i:i + CHUNK]
            log(f"  batch: {', '.join(s['name'] for s in chunk)}")
            items = write_chunk(cat, chunk, pool)
            if items is None:  # model failed: keep yesterday's stories of these sources
                items = [it for s in chunk for it in old_by_source.get(s["id"], [])]
            for it in items:
                got[it["source_id"]] = got.get(it["source_id"], 0) + 1
            all_items += items
            time.sleep(PAUSE)
        for s in cat_sources:
            meta.append({"id": s["id"], "name": s["name"], "cat": cat, "lean": s["lean"],
                         "default": s["default"], "ok": bool(pool[s["id"]]), "stories": got.get(s["id"], 0)})

    if not all_items:
        log("Nothing to write.")
        sys.exit(1)

    day = today_nl()
    payload = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "date": day, "sources": meta, "items": all_items}
    save(payload, day)
    log(f"Wrote {len(all_items)} stories for {day}")


if __name__ == "__main__":
    main()
