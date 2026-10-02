#!/usr/bin/env python3
"""run_seo.py - complete standalone auto-blogger: RSS -> AI article (or no-API fallback) -> Blogger. SEO included."""
import calendar, glob, html, json, logging, os, random, re, subprocess, sys, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from difflib import SequenceMatcher
from io import BytesIO
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

try:
    import trafilatura
except Exception:
    trafilatura = None
try:
    from PIL import Image
except Exception:
    Image = None

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = "openrouter/free"
BLOGGER_CLIENT_ID = os.environ.get("BLOGGER_CLIENT_ID", "")
BLOGGER_CLIENT_SECRET = os.environ.get("BLOGGER_CLIENT_SECRET", "")
BLOGGER_REFRESH_TOKEN = os.environ.get("BLOGGER_REFRESH_TOKEN", "")
BLOG_ID = os.environ.get("BLOG_ID", "")
BLOG_NAME = "this blog"

FEEDS_FILE = "feeds.txt"
HISTORY_FILE = "posted.json"
INTERVAL = 3600
MAX_CATCHUP = 6
SPACING = 90
MAX_AGE_HOURS = 48
MAX_IMAGES = 10
MAX_SOURCES_PER_STORY = 6
MAX_RUN_SECONDS = 50 * 60
MIN_WORDS = 750
START = time.time()

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; PoliticsBlogBot/2.0)"})
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("autoblog")


class NoRetry(Exception): pass
class ValidationError(NoRetry): pass
class NotPolitics(ValidationError): pass
class NoImages(ValidationError): pass
class SkipStory(ValidationError): pass
class LLMDown(NoRetry): pass
class NoStory(Exception): pass


FAILS = {}


def esc(t):
    return html.escape(t, quote=False)


def with_retries(fn, tries=5, base=5, what="call"):
    for i in range(tries):
        try:
            return fn()
        except NoRetry:
            raise
        except Exception as e:
            if i == tries - 1:
                raise
            wait = base * (2 ** i) + random.random() * 3
            log.warning("%s failed (%s). retry %d in %.0fs", what, e, i + 1, wait)
            time.sleep(wait)


# ---------------- history ----------------
def load_history():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            h = json.load(f)
            if not isinstance(h, dict):
                raise ValueError
            h.setdefault("posts", [])
            h.setdefault("next_slot", 0)
            return h
    except Exception:
        return {"posts": [], "next_slot": 0}


def save_history(h):
    h["posts"] = h["posts"][-800:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=1)


def push_history():
    if not os.getenv("GITHUB_ACTIONS"):
        return
    try:
        subprocess.run('git config user.name "blog-bot" && git config user.email "bot@users.noreply.github.com" && '
                       'git add posted.json && (git diff --cached --quiet || git commit -qm "history") && '
                       'git pull --rebase -q && git push -q', shell=True, timeout=120)
    except Exception as e:
        log.warning("history push failed: %s", e)


STOP = set("the a an of to in on for and or at by with from as is are was were be been after over amid says say said new "
           "its his her their that this will has have had not but into than more about out up".split())
TOPIC_STOP = set("president minister government talks first could would year years week state states people country "
                 "leader leaders official officials report reports".split())


def toks(s):
    return {w for w in re.findall(r"[a-z0-9']+", s.lower()) if len(w) > 2 and w not in STOP}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def overlap(a, b):
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


# ---------------- feeds ----------------
DEFAULT_FEEDS = [
    ("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Al Jazeera English", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("Deutsche Welle (DW)", "https://rss.dw.com/xml/rss-en-all"),
    ("Euronews", "https://www.euronews.com/rss"),
    ("France 24", "https://www.france24.com/en/rss"),
    ("The Guardian", "https://www.theguardian.com/world/rss"),
    ("Sky News", "https://feeds.skynews.com/feeds/rss/world.xml"),
    ("The Independent", "https://www.independent.co.uk/news/world/rss"),
    ("The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
    ("NPR News", "https://feeds.npr.org/1001/rss.xml"),
    ("CBS News", "https://www.cbsnews.com/latest/rss/world"),
    ("PBS NewsHour", "https://www.pbs.org/newshour/feeds/rss/headlines"),
    ("Politico", "https://rss.politico.com/politics-news.xml"),
    ("The Hill", "https://thehill.com/homenews/feed/"),
    ("Foreign Policy", "https://foreignpolicy.com/feed/"),
    ("CBC News", "https://www.cbc.ca/webfeed/rss/rss-topstories"),
    ("Le Monde", "https://www.lemonde.fr/en/international/rss_full.xml"),
    ("Euractiv", "https://www.euractiv.com/feed/"),
    ("The Japan Times", "https://www.japantimes.co.jp/feed/"),
    ("The Straits Times", "https://www.straitstimes.com/news/world/rss.xml"),
    ("ABC News Australia", "https://www.abc.net.au/news/feed/51120/rss.xml"),
    ("Arab News", "https://www.arabnews.com/rss.xml"),
    ("Middle East Eye", "https://www.middleeasteye.net/rss"),
    ("Kyiv Independent", "https://kyivindependent.com/feed/"),
    ("The Times of India", "https://timesofindia.indiatimes.com/rssfeedstopstories.cms"),
]


def find_feeds_file():
    cands = [FEEDS_FILE] + [p for p in glob.glob("**/*.txt", recursive=True)
                            if os.path.basename(p).lower() != "requirements.txt"]
    for p in cands:
        try:
            with open(p, encoding="utf-8") as f:
                if sum(1 for line in f if re.search(r"https?://", line)) >= 5:
                    return p
        except Exception:
            continue
    return None


def load_feeds():
    path = find_feeds_file()
    if not path:
        log.warning("feeds.txt not found -> using built-in feed list (add feeds.txt to the repo root for all feeds)")
        return DEFAULT_FEEDS
    feeds, seen = [], set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.search(r"(https?://\S+)", line)
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            name = line[:m.start()].strip(" \t—–-:") or urlparse(m.group(1)).netloc
            feeds.append((name, m.group(1)))
    return feeds or DEFAULT_FEEDS


BAD_IMG = re.compile(r"(logo|icon|sprite|avatar|pixel|spacer|1x1|placeholder|default[-_]|/ads?/|banner|badge|favicon)", re.I)
AI_WORDS = re.compile(r"(ai[-_ ]generated|midjourney|dall[-_ ]?e|stable[-_ ]?diffusion|generated[-_ ]by[-_ ]ai|ai[-_ ]image|"
                      r"ai[-_ ]art|firefly|imagen|created[-_ ]with[-_ ]ai)", re.I)


def bad_img(u):
    return (not u.startswith("http")) or bool(BAD_IMG.search(u)) or bool(AI_WORDS.search(u))


def img_key(u):
    return re.sub(r"[-_]?\d{2,4}x\d{2,4}", "", urlparse(u).path.lower())


def entry_images(e):
    out = []
    for k in ("media_content", "media_thumbnail"):
        for m in e.get(k, []) or []:
            u = m.get("url")
            if u and (k == "media_thumbnail" or "image" in (m.get("type", "") + m.get("medium", "image"))):
                out.append((u, ""))
    for en in e.get("enclosures", []) or []:
        if en.get("type", "").startswith("image") and en.get("href"):
            out.append((en["href"], ""))
    for l in e.get("links", []) or []:
        if l.get("type", "").startswith("image") and l.get("href"):
            out.append((l["href"], ""))
    for b in [e.get("summary", "")] + [c.get("value", "") for c in e.get("content", []) or []]:
        if b:
            for img in BeautifulSoup(b, "html.parser").find_all("img"):
                if img.get("src"):
                    out.append((img["src"], img.get("alt", "")))
    return out


def fetch_feed(item):
    name, url = item
    try:
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
        d = feedparser.parse(r.content)
    except Exception as ex:
        log.info("feed failed: %s (%s)", name, str(ex)[:80])
        return []
    out = []
    for e in d.entries[:40]:
        link, title = e.get("link"), (e.get("title") or "").strip()
        if not link or not title:
            continue
        t = e.get("published_parsed") or e.get("updated_parsed")
        ts = calendar.timegm(t) if t else 0
        summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ", strip=True)
        out.append({"source": name, "title": title, "link": link, "summary": summary, "ts": ts,
                    "images": [(urljoin(link, u), a) for u, a in entry_images(e)]})
    return out


POLITICS = re.compile(
    r"\b(election|elections|vote|voters|parliament|president|presidential|prime minister|minister|government|sanction|"
    r"sanctions|diplomat|diplomatic|diplomacy|summit|nato|united nations|u\.n\.|security council|treaty|ceasefire|"
    r"peace talks|peace deal|war|military|coalition|protest|protesters|referendum|senate|congress|legislature|lawmakers|"
    r"tariff|tariffs|trade deal|embassy|foreign policy|geopolitical|regime|coup|opposition|kremlin|white house|"
    r"downing street|european union|eu|nuclear|missile|border|refugee|politic|politics|political|campaign|cabinet|"
    r"impeach|authoritarian|democracy|trump|putin|xi jinping|zelensky|zelenskyy|netanyahu|hamas|gaza|ukraine|taiwan|"
    r"israel|iran|russia|china|brussels|pentagon|envoy|annex|invasion|negotiations|talks)\b", re.I)
EXCLUDE = re.compile(
    r"\b(football|soccer|cricket|tennis|nba|nfl|mlb|premier league|formula 1|f1|grand prix|champions league|"
    r"celebrity|movie|film review|album|recipe|horoscope|fashion|box office|iphone|gadget|review:|wimbledon|"
    r"golf|rugby|transfer window|netflix|oscars|grammy)\b", re.I)
HIGH = re.compile(
    r"\b(war|ceasefire|summit|election|elections|sanction|sanctions|nuclear|coup|invasion|missile|treaty|peace deal|"
    r"peace talks|nato|security council|tariff|tariffs|impeach|referendum|prime minister|president|killed|attack|crisis)\b", re.I)
TIER1 = {"BBC News", "Al Jazeera English", "Deutsche Welle (DW)", "The Guardian", "The New York Times", "NPR News",
         "Financial Times", "The Economist", "France 24", "Foreign Policy", "Foreign Affairs", "CNN", "Sky News",
         "The Washington Post", "Politico", "Axios", "Bloomberg", "Kyiv Independent", "Le Monde", "Euronews"}


def is_politics(e):
    if EXCLUDE.search(e["title"]):
        return False
    return len(POLITICS.findall(e["title"])) >= 1 or len(POLITICS.findall(e["title"] + " " + e["summary"])) >= 2


_cache = {"t": 0, "v": []}


def collect_entries():
    if time.time() - _cache["t"] < 600 and _cache["v"]:
        return _cache["v"]
    feeds = load_feeds()
    log.info("loading %d feeds", len(feeds))
    with ThreadPoolExecutor(16) as ex:
        results = list(ex.map(fetch_feed, feeds))
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    seen, entries = set(), []
    for lst in results:
        for e in lst:
            if e["link"] in seen or (e["ts"] and e["ts"] < cutoff):
                continue
            seen.add(e["link"])
            if is_politics(e):
                entries.append(e)
    log.info("%d political entries", len(entries))
    _cache.update(t=time.time(), v=entries)
    return entries


def cluster_entries(entries):
    clusters = []
    for e in sorted(entries, key=lambda x: -x["ts"]):
        t = toks(e["title"])
        for c in clusters:
            s = c["tokens"]
            if jaccard(t, s) >= 0.3 or (len(t & s) >= 3 and jaccard(t, s) >= 0.2):
                c["items"].append(e)
                break
        else:
            clusters.append({"tokens": t, "items": [e]})
    return [c["items"] for c in clusters]


def topic_tokens(c):
    s = set()
    for e in c:
        s |= toks(e["title"])
    return s - TOPIC_STOP


_remote = {"v": None}


def remote_token_sets():
    """Titles of the latest posts already on the blog (protects against a lost posted.json)."""
    if _remote["v"] is None:
        try:
            r = requests.get(f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts",
                             params={"maxResults": 50, "fields": "items(title)", "orderBy": "published"},
                             headers={"Authorization": f"Bearer {blogger_token()}"}, timeout=30)
            _remote["v"] = [toks(i.get("title", "")) - TOPIC_STOP for i in r.json().get("items", [])]
        except Exception:
            _remote["v"] = []
    return _remote["v"]


def is_dup(cluster, hist, tt, rts):
    links = {l for p in hist["posts"] for l in p.get("links", [])}
    old = [toks(t) for p in hist["posts"][-400:] for t in p.get("source_titles", [])]
    for e in cluster:
        if e["link"] in links:
            return True
        t = toks(e["title"])
        if any(jaccard(t, o) >= 0.5 for o in old):
            return True
    now = time.time()
    for p in hist["posts"][-80:]:
        if now - p.get("time", 0) <= 72 * 3600:
            pt = set(p.get("topic", []))
            if len(tt & pt) >= 4 and overlap(tt, pt) >= 0.6:
                return True
    for rt in rts:
        if len(tt & rt) >= 3 and overlap(tt, rt) >= 0.6:
            return True
    return False


def importance(c, hist, tt):
    n = len({e["source"] for e in c})
    tier = len({e["source"] for e in c if e["source"] in TIER1})
    strong = min(6, sum(len(HIGH.findall(e["title"])) for e in c))
    newest = max(e["ts"] for e in c) or time.time()
    recency = max(0.0, 12 - (time.time() - newest) / 3600) / 12
    img = 2 if any(e["images"] for e in c) else 0
    pen = max([overlap(tt, set(p.get("topic", []))) for p in hist["posts"][-12:]] or [0.0])
    return n * 3 + tier * 1.5 + strong + recency * 4 + img - pen * 6


def pick_cluster(clusters, hist):
    rts = remote_token_sets()
    best, best_score = None, -1e9
    for c in clusters:
        if FAILS.get(c[0]["link"], 0) >= 2:
            continue
        tt = topic_tokens(c)
        if is_dup(c, hist, tt, rts):
            continue
        s = importance(c, hist, tt)
        if s > best_score:
            best, best_score = c, s
    if best:
        log.info("chosen story score=%.1f", best_score)
    return best
    # ---------------- article text + images ----------------
def extract_text(page):
    if trafilatura:
        try:
            t = trafilatura.extract(page, include_comments=False, include_tables=False)
            if t:
                return t
        except Exception:
            pass
    try:
        soup = BeautifulSoup(page, "html.parser")
        ps = [p.get_text(" ", strip=True) for p in (soup.find("article") or soup).find_all("p")]
        return " ".join(p for p in ps if len(p.split()) >= 8)
    except Exception:
        return ""


def fetch_article(url):
    try:
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
        page = r.text
    except Exception:
        return "", []
    text = extract_text(page)
    imgs = []
    try:
        soup = BeautifulSoup(page, "html.parser")
        for prop in ("og:image", "twitter:image"):
            m = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
            if m and m.get("content"):
                imgs.append((urljoin(url, m["content"]), ""))
        art = soup.find("article") or soup
        for img in art.find_all("img")[:6]:
            src = img.get("src") or img.get("data-src")
            if src:
                imgs.append((urljoin(url, src), img.get("alt", "")))
    except Exception:
        pass
    return text, imgs


def check_image(im):
    """Real, reachable, large-enough photo taken from the news source. We never generate images."""
    if AI_WORDS.search(im["url"]) or AI_WORDS.search(im["alt"]):
        return None
    try:
        r = SESSION.get(im["url"], timeout=20, stream=True, headers={"Referer": im["link"]})
        if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith("image"):
            return None
        data = b""
        for chunk in r.iter_content(65536):
            data += chunk
            if len(data) > 2_000_000:
                break
        r.close()
        if Image is None:
            return im if len(data) > 8000 else None
        w, h = Image.open(BytesIO(data)).size
        if w < 400 or h < 220 or w / h > 4:
            return None
        return im
    except Exception:
        return None


def gather(cluster):
    cluster = cluster[:MAX_SOURCES_PER_STORY]
    sources, cands, seen = [], [], set()
    for e in cluster:
        text, page_imgs = fetch_article(e["link"])
        sources.append({"source": e["source"], "title": e["title"], "link": e["link"],
                        "text": (text or e["summary"])[:6000]})
        for u, a in e["images"] + page_imgs:
            k = img_key(u)
            if k in seen or bad_img(u):
                continue
            seen.add(k)
            cands.append({"url": u, "alt": (a or e["title"]).strip()[:160], "source": e["source"], "link": e["link"]})
    with ThreadPoolExecutor(8) as ex:
        ok = [c for c in ex.map(check_image, cands[:24]) if c]
    images = ok[:MAX_IMAGES]
    if not images:
        raise NoImages("no verified feed image for this story")
    for i, im in enumerate(images, 1):
        im["id"] = i
    return {"sources": sources, "images": images, "cluster": cluster}


# ---------------- LLM (OpenRouter) ----------------
def llm(system, user, max_tokens=7000):
    def call():
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json",
                     "HTTP-Referer": "https://github.com", "X-Title": "Auto Politics Blogger"},
            json={"model": OPENROUTER_MODEL, "temperature": 0.6, "max_tokens": max_tokens,
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            timeout=240)
        if r.status_code in (401, 402, 403):
            raise LLMDown(f"OpenRouter HTTP {r.status_code}: {r.text[:150]}")
        if r.status_code in (408, 429, 500, 502, 503, 504):
            raise RuntimeError(f"OpenRouter HTTP {r.status_code}")
        r.raise_for_status()
        j = r.json()
        if "error" in j:
            raise RuntimeError(f"OpenRouter error: {str(j['error'])[:200]}")
        content = (j["choices"][0]["message"].get("content") or "")
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
        if len(content) < 200 and not content.upper().startswith("SKIP"):
            raise RuntimeError("empty/short model output")
        return content
    return with_retries(call, tries=3, base=4, what="openrouter")


WRITER_SYSTEM = """You are a senior international-affairs editor and SEO writer for the blog "{blog}".
Write ONLY in English. Use ONLY facts contained in the SOURCE MATERIAL: never invent quotes, numbers, dates, names or events. If sources disagree, say so and attribute each claim to its outlet by name.
If the story is NOT about international politics / geopolitics / government policy, reply with exactly: SKIP

OUTPUT FORMAT (exactly):
TITLE: <unique, click-worthy SEO title, 55-70 chars, main keyword near the start, do NOT copy any source headline>
META: <meta description, 140-155 chars>
LABELS: <3-5 comma-separated SEO labels>
---BODY---
<article HTML>

BODY RULES
- Clean semantic HTML only: h2, h3, p, ul, ol, li, strong, em, u, mark, blockquote, table. No h1, no markdown, no code fences.
- 1,500-2,200 words. Exactly these 9 sections, in this order, each opened with an <h2> written as "<Section role>: <specific SEO-rich phrase about this story>":
  1. High-Impact Visual Hook - a vivid opening fact or scene; main keyword within the first 100 words; place the first image here.
  2. Core Question / Problem Statement - the central question readers want answered.
  3. Video Topic & Chapter Overview - (write the role as "Topic & Chapter Overview") a short <ul> roadmap of what the article covers.
  4. Historical Context & Root Cause Setup - background and underlying causes.
  5. Evidence & Data-Driven Point-by-Point Breakdown - numbered points; each cites its outlet by name and is followed by the image that proves/illustrates it.
  6. Counter-Argument / Both Sides Analysis - strongest views from each side, fairly.
  7. The Big Picture / Real Impact - consequences for people, markets, alliances, the region.
  8. Practical Solution / Call to Action (CTA) - realistic options and what to watch next.
  9. Outro & Channel Promotion - (write the role as "Outro & Blog Promotion") thank readers, invite them to follow {blog} and comment.
- Emphasis: you decide. Use <strong> for key facts/figures, <u> for the 1-2 most important claims per section, <mark> sparingly, <blockquote> for short attributed quotes (<25 words) taken from the sources.
- IMAGES: you receive a numbered image list (real photos from the news sources). Put the marker [[IMG:n]] alone on its own line right BEFORE the paragraph it supports. Use EVERY image exactly once, in a sensible position. Never write <img> yourself.
- Neutral, factual tone; no unverified allegations stated as fact. Plain language. Vary sentence length."""

REVIEWER_SYSTEM = """You are a meticulous fact-checking editor and SEO specialist. You receive SOURCE MATERIAL and a DRAFT article.
1) Check every claim against the sources; delete or correct anything unsupported or wrong.
2) Add missing context, explanation or data from the sources where the draft is thin; fix unclear passages.
3) Make sure all 9 required <h2> sections exist in order, every image marker [[IMG:n]] appears exactly once, key points are emphasised with <strong>/<u>/<mark>, the title is unique (not a source headline), and the main keyword appears in the first 100 words.
4) Return the COMPLETE final article in EXACTLY the same format (TITLE / META / LABELS / ---BODY--- html). Do not mention this review. If the story is not international politics, reply exactly: SKIP"""


def sources_block(m):
    s = [f"### SOURCE {i}: {x['source']} | {x['title']} | {x['link']}\n{x['text']}" for i, x in enumerate(m["sources"], 1)]
    imgs = "\n".join(f"[[IMG:{i['id']}]] from {i['source']} - {i['alt']}" for i in m["images"])
    return "SOURCE MATERIAL\n" + "\n\n".join(s) + "\n\nIMAGE LIST\n" + imgs


def parse(out):
    if out.strip().upper().startswith("SKIP"):
        return {"skip": True}
    out = re.sub(r"^```\w*\n|\n```$", "", out.strip())
    t = re.search(r"TITLE:\s*(.+)", out)
    mt = re.search(r"META:\s*(.+)", out)
    lb = re.search(r"LABELS:\s*(.+)", out)
    if not t or "---BODY---" not in out:
        raise ValidationError("bad output format")
    body = out.split("---BODY---", 1)[1].strip()
    body = re.sub(r"^```\w*\n|\n```$", "", body).strip()
    labels = [x.strip() for x in (lb.group(1) if lb else "").split(",") if x.strip()]
    return {"skip": False, "title": t.group(1).strip().strip('"*'), "meta": mt.group(1).strip() if mt else "",
            "labels": labels, "body": body}


def validate(a, hist):
    if a.get("skip"):
        raise NotPolitics("not politics")
    words = len(re.sub(r"<[^>]+>", " ", a["body"]).split())
    if words < MIN_WORDS:
        raise ValidationError(f"too short ({words} words)")
    if len(re.findall(r"<h2", a["body"], re.I)) < 9:
        raise ValidationError("fewer than 9 sections")
    if re.search(r"^#{1,3} |\*\*\w", a["body"], re.M):
        raise ValidationError("markdown in html")
    if not 20 <= len(a["title"]) <= 120:
        raise ValidationError("bad title length")
    for p in hist["posts"][-500:]:
        if SequenceMatcher(None, a["title"].lower(), p["title"].lower()).ratio() > 0.8:
            raise ValidationError("title too similar to an old post")


def write_and_review_llm(m, hist):
    user = sources_block(m)
    draft_raw = llm(WRITER_SYSTEM.replace("{blog}", BLOG_NAME), user)
    draft = parse(draft_raw)
    if draft.get("skip"):
        raise NotPolitics("not politics")
    final = None
    try:
        final = parse(llm(REVIEWER_SYSTEM, user + "\n\nDRAFT\n" + draft_raw))
        validate(final, hist)
    except NotPolitics:
        raise
    except Exception as ex:
        log.warning("review pass unusable (%s) -> using draft", ex)
        final = None
    if final is None:
        validate(draft, hist)
        final = draft
    return final


# ---------------- fallback writer (no API) ----------------
GENERIC = set("says say said news live update updates latest after over amid with from this that what will have has about "
              "into more than their they were been its and the for are not but who how why when where which".split())
BOILER = re.compile(r"(subscribe|cookie|sign up|newsletter|copyright|advertis|follow us|all rights reserved|click here|"
                    r"read more|watch live|download the app)", re.I)
CONTEXT = re.compile(r"\b(since|years?|decades?|previously|earlier|last year|history|historic|dispute|tensions?|began|"
                     r"started|ago|following)\b", re.I)
CONTRAST = re.compile(r"\b(however|but|critics?|opposition|denied|denies|rejected|rejects|warned|accus\w+|disputed?|"
                      r"claims?|despite)\b", re.I)
IMPACT = re.compile(r"\b(economy|economic|markets?|prices?|security|civilians?|alliance|region|regional|trade|energy|"
                    r"oil|refugees?|global|international|sanctions|peace)\b", re.I)


def sentences(text):
    out = []
    for s in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text or "")):
        if 8 <= len(s.split()) <= 45 and not BOILER.search(s):
            out.append(s.strip())
    return out


def clip(s, maxw=28):
    w = s.split()
    return s if len(w) <= maxw else " ".join(w[:maxw]).rstrip(",;:") + "…"


def first_unused(sents, used, rx=None):
    for s in sents:
        if s not in used and (rx is None or rx.search(s)):
            used.add(s)
            return s
    return None


def hl_numbers(t):
    return re.sub(r"(\d[\d,.]*%?)", r"<strong>\1</strong>", esc(t))


def keyphrase(cluster):
    freq = Counter()
    for e in cluster:
        freq.update(w for w in toks(e["title"]) if w not in GENERIC)
    top = {w for w, _ in freq.most_common(7)}
    raw = re.findall(r"[A-Za-z0-9][A-Za-z0-9'’\-]*", cluster[0]["title"])
    words = []
    for w in raw:
        if w.lower().strip("'’") in top and w not in words:
            words.append(w)
    if len(words) < 2:
        words = raw[:6]
    words = [w[:1].upper() + w[1:] for w in words[:6]]
    return " ".join(words), [w for w, _ in freq.most_common(5)]


def make_title(kp, n, hist):
    tail = f": What {n} Major News Outlets Report" if n > 1 else ": Key Facts and Context"
    words = kp.split()
    while len(" ".join(words) + tail) > 70 and len(words) > 2:
        words.pop()
    title = " ".join(words) + tail
    old = [p["title"].lower() for p in hist["posts"][-500:]]
    if any(SequenceMatcher(None, title.lower(), o).ratio() > 0.8 for o in old):
        title += datetime.now(timezone.utc).strftime(" (%b %d)")
        if any(SequenceMatcher(None, title.lower(), o).ratio() > 0.8 for o in old):
            raise SkipStory("fallback title too similar")
    return title


def fallback_article(m, hist):
    cl, srcs, imgs = m["cluster"], m["sources"], m["images"]
    if sum(len(s["text"].split()) for s in srcs) < 250:
        raise SkipStory("sources too thin for fallback")
    sents = [sentences(s["text"]) for s in srcs]
    used_s, used_img = set(), set()
    names = []
    for s in srcs:
        if s["source"] not in names:
            names.append(s["source"])
    n = len(names)
    kp, top = keyphrase(cl)
    title = make_title(kp, n, hist)
    blog = esc(BLOG_NAME)

    def link(s):
        return (f'<a href="{html.escape(s["link"], quote=True)}" target="_blank" rel="nofollow noopener">'
                f'Read the full report</a>')

    def marker(source=None):
        for im in imgs:
            if im["id"] in used_img:
                continue
            if source is None or im["source"] == source:
                used_img.add(im["id"])
                return f'[[IMG:{im["id"]}]]\n'
        return ""

    B = []
    B.append(f"<h2>High-Impact Visual Hook: {esc(kp)} in Focus</h2>")
    B.append(marker())
    lead_i = next((i for i, s in enumerate(sents) if s), None)
    if lead_i is not None:
        q = first_unused(sents[lead_i], used_s)
        B.append(f"<p><strong>{esc(kp)}</strong> is making international headlines. {esc(srcs[lead_i]['source'])} "
                 f"reports: <u>“{esc(clip(q))}”</u></p>")
    else:
        B.append(f"<p><strong>{esc(kp)}</strong> is making international headlines. {esc(srcs[0]['source'])} "
                 f"reports: <u>“{esc(srcs[0]['title'])}”</u></p>")
    B.append(f"<h2>Core Question / Problem Statement: What Is Really Happening With {esc(kp)}?</h2>")
    B.append(f"<p>The central question: <strong>what exactly has happened, why does it matter beyond the countries "
             f"involved, and what could come next?</strong> We compared reporting from {n} outlet(s): "
             f"{esc(', '.join(names))}.</p>")
    B.append("<h2>Topic &amp; Chapter Overview: What This Article Covers</h2><ul>"
             "<li>Background and root causes</li><li>Evidence from the reports, point by point</li>"
             "<li>Where the accounts differ</li><li>The wider global impact</li><li>What to watch next</li></ul>")
    B.append(f"<h2>Historical Context &amp; Root Cause Setup: How {esc(kp)} Got Here</h2>")
    ctx = []
    for i, ss in enumerate(sents):
        q = first_unused(ss, used_s, CONTEXT)
        if q:
            ctx.append((srcs[i]["source"], q))
        if len(ctx) == 2:
            break
    if ctx:
        for nm, q in ctx:
            B.append(f"<p>Background reported by <strong>{esc(nm)}</strong>: “{esc(clip(q))}”</p>")
    else:
        B.append("<p>The reports reviewed give limited background, so the linked original reports below are the best "
                 "place for the longer history of this story.</p>")
    B.append(f"<h2>Evidence &amp; Data-Driven Point-by-Point Breakdown: What the Reports Say About {esc(kp)}</h2>")
    for k, s in enumerate(srcs, 1):
        B.append(marker(s["source"]))
        line = f"<p><strong>{k}. {esc(s['source'])}</strong> headline: <em>“{esc(s['title'])}”</em>."
        q = first_unused(sents[k - 1], used_s)
        if q:
            line += f" Key line: “{esc(clip(q))}”"
        B.append(line + f" {link(s)}.</p>")
    figs = []
    for i, ss in enumerate(sents):
        q = first_unused(ss, used_s, re.compile(r"\d"))
        if q:
            figs.append((srcs[i]["source"], q))
        if len(figs) == 3:
            break
    if figs:
        B.append("<p><strong>Figures mentioned in the reports:</strong></p><ul>" + "".join(
            f"<li>{esc(nm)}: “{hl_numbers(clip(q))}”</li>" for nm, q in figs) + "</ul>")
    B.append("<h2>Counter-Argument / Both Sides Analysis: Where Accounts Differ</h2>")
    for i, ss in enumerate(sents):
        q = first_unused(ss, used_s, CONTRAST)
        if q:
            B.append(f"<p><strong>{esc(srcs[i]['source'])}</strong> adds a contrasting angle: “{esc(clip(q))}”</p>")
            break
    B.append("<p>Outlets frame the same events differently. Compare their headlines:</p><ul>" + "".join(
        f"<li><strong>{esc(s['source'])}</strong>: “{esc(s['title'])}”</li>" for s in srcs[:4]) + "</ul>")
    B.append("<p>No single outlet tells the whole story, so <u>weigh these accounts against each other</u> before "
             "drawing conclusions.</p>")
    B.append(f"<h2>The Big Picture / Real Impact: Why {esc(kp)} Matters Globally</h2>")
    for i, ss in enumerate(sents):
        q = first_unused(ss, used_s, IMPACT)
        if q:
            B.append(f"<p><strong>{esc(srcs[i]['source'])}</strong> points to wider consequences: "
                     f"“{esc(clip(q))}”</p>")
            break
    B.append("<p>Developments like this can ripple through diplomacy, trade and security. The key themes to follow "
             f"are <strong>{esc(', '.join(top[:4]))}</strong>.</p>")
    B.append("<h2>Practical Solution / Call to Action (CTA): What to Watch Next and How to Stay Informed</h2><ul>"
             "<li>Watch for official statements and follow-up reporting from the outlets linked below.</li>"
             "<li>Cross-check any claim against at least two independent sources.</li>"
             "<li>Read the original reports for full detail and context.</li>"
             f"<li>Follow {blog} for more world-politics coverage.</li></ul>")
    B.append(f"<h2>Outro &amp; Blog Promotion: Thanks for Reading {blog}</h2>"
             f"<p>Thanks for reading. If this was useful, follow <strong>{blog}</strong> and share your thoughts "
             "in the comments.</p>")

    body = "\n".join(x for x in B if x)
    if len(re.sub(r"<[^>]+>", " ", body).split()) < 250:
        raise SkipStory("fallback article too short")
    meta = clip(f"Latest on {kp}: key facts, background and what {n} major news outlets report, "
                "with photos and source links.", 25)
    labels = ["World Politics", "International Relations"] + [w.title() for w in top if len(w) > 3][:2]
    log.warning("FALLBACK article built (no API): %s", title)
    return {"skip": False, "title": title, "meta": meta, "labels": labels, "body": body}


_llm_down_until = [0.0]
_vfail = {}


def write_article(m, hist):
    key = m["cluster"][0]["link"]
    if OPENROUTER_API_KEY and time.time() >= _llm_down_until[0]:
        try:
            return write_and_review_llm(m, hist)
        except NotPolitics:
            raise
        except ValidationError as e:
            _vfail[key] = _vfail.get(key, 0) + 1
            if _vfail[key] < 2:
                raise
            log.warning("AI output unusable twice (%s) -> fallback", e)
        except Exception as e:
            log.warning("AI unavailable (%s) -> fallback; AI paused 20 min", e)
            _llm_down_until[0] = time.time() + 20 * 60
    return fallback_article(m, hist)
    # ---------------- render + SEO ----------------
def figure(im):
    alt = html.escape(im["alt"], quote=True)
    return (f'<figure style="margin:26px 0;text-align:center">'
            f'<img src="{html.escape(im["url"], quote=True)}" alt="{alt}" loading="lazy" '
            f'style="max-width:100%;height:auto;border-radius:8px"/>'
            f'<figcaption style="font-size:.85em;color:#666;margin-top:6px">{alt} &mdash; Image: '
            f'<a href="{html.escape(im["link"], quote=True)}" target="_blank" rel="nofollow noopener">'
            f'{html.escape(im["source"])}</a></figcaption></figure>')


def _slug(text, i):
    s = re.sub(r"[^a-z0-9]+", "-", re.sub(r"<[^>]+>", "", text).lower()).strip("-")[:50]
    return f"s{i}-{s}" if s else f"s{i}"


def render(a, m):
    images = {i["id"]: i for i in m["images"]}
    used = set()

    def rep(mo):
        i = int(mo.group(1))
        if i in images and i not in used:
            used.add(i)
            return figure(images[i])
        return ""

    body = re.sub(r"(?:<p>\s*)?\[\[IMG:(\d+)\]\](?:\s*</p>)?", rep, a["body"])
    unused = [im for i, im in images.items() if i not in used]
    if unused:
        parts = re.split(r"(?<=</p>)", body)
        n = len(parts)
        for k in reversed(range(len(unused))):
            parts.insert(max(1, int((k + 1) * n / (len(unused) + 1))), figure(unused[k]))
        body = "".join(parts)
    src = "".join(f'<li><a href="{html.escape(s["link"], quote=True)}" target="_blank" rel="nofollow noopener">'
                  f'{esc(s["title"])}</a> &mdash; {esc(s["source"])}</li>' for s in m["sources"])
    body += f"<h2>Sources &amp; Further Reading</h2><ul>{src}</ul>"

    # SEO: anchor ids + table of contents
    toc, counter = [], [0]

    def add_id(mo):
        counter[0] += 1
        attrs, inner = mo.group(1), mo.group(2)
        sid = _slug(inner, counter[0])
        toc.append((sid, re.sub(r"<[^>]+>", "", inner).strip()))
        return f'<h2{attrs} id="{sid}">{inner}</h2>'

    body = re.sub(r"<h2([^>]*)>(.*?)</h2>", add_id, body, flags=re.S | re.I)
    body = body.replace('loading="lazy"', 'loading="eager"', 1)   # first image loads fast

    words = len(re.sub(r"<[^>]+>", " ", body).split())
    minutes = max(1, round(words / 200))
    date = datetime.now(timezone.utc).strftime("%B %d, %Y")
    head = ""
    meta = (a.get("meta") or "").strip()[:160]
    if meta:
        head += f"<p><strong>{esc(meta)}</strong></p>"   # Blogger uses the start of the post as description
    head += f'<p style="color:#666;font-size:.9em">Published {date} &bull; {minutes} min read</p>'
    if toc:
        items = "".join(f'<li><a href="#{i}">{esc(t)}</a></li>' for i, t in toc)
        head += ('<nav style="background:#f6f8fa;border-left:4px solid #1a73e8;padding:12px 18px;margin:18px 0">'
                 '<strong>In this article</strong><ol style="margin:8px 0 0 18px">' + items + "</ol></nav>")
    body = head + body

    # SEO: internal links to related earlier posts
    try:
        posts = [p for p in load_history()["posts"][-200:] if str(p.get("url", "")).startswith("http")]
        tt = topic_tokens(m["cluster"])
        scored = sorted(posts, key=lambda p: -overlap(tt, set(p.get("topic", []))))
        rel = [p for p in scored if overlap(tt, set(p.get("topic", []))) > 0][:5]
        for p in reversed(posts):
            if len(rel) >= 4:
                break
            if p not in rel:
                rel.append(p)
        if rel:
            li = "".join(f'<li><a href="{html.escape(p["url"], quote=True)}">{esc(p["title"])}</a></li>'
                         for p in rel[:5])
            body += f"<h2>Related Reading</h2><ul>{li}</ul>"
    except Exception as e:
        log.warning("related links skipped: %s", e)
    return body


# ---------------- Blogger ----------------
_token = {"v": None}


def blogger_token(force=False):
    if _token["v"] and not force:
        return _token["v"]
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": BLOGGER_CLIENT_ID, "client_secret": BLOGGER_CLIENT_SECRET,
        "refresh_token": BLOGGER_REFRESH_TOKEN, "grant_type": "refresh_token"}, timeout=30)
    if r.status_code in (400, 401):
        sys.exit("FATAL: Google refresh token/client invalid or expired. Create a new refresh token "
                 "(and set the OAuth app to 'In production'). Details: " + r.text[:200])
    r.raise_for_status()
    _token["v"] = r.json()["access_token"]
    return _token["v"]


def set_blog_name():
    global BLOG_NAME
    try:
        r = requests.get(f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}", params={"fields": "name"},
                         headers={"Authorization": f"Bearer {blogger_token()}"}, timeout=30)
        BLOG_NAME = r.json().get("name") or BLOG_NAME
    except Exception as e:
        log.warning("blog name fetch failed: %s", e)


def title_exists(title):
    try:
        r = requests.get(f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts",
                         params={"maxResults": 10, "fields": "items(title)", "orderBy": "published"},
                         headers={"Authorization": f"Bearer {blogger_token()}"}, timeout=30)
        return any(i.get("title") == title for i in r.json().get("items", []))
    except Exception:
        return False


def publish(title, body, labels):
    attempt = {"n": 0}

    def call():
        if attempt["n"] > 0 and title_exists(title):   # an earlier attempt actually succeeded
            return {"url": "(already published)"}
        attempt["n"] += 1
        r = requests.post(
            f"https://www.googleapis.com/blogger/v3/blogs/{BLOG_ID}/posts", params={"isDraft": "false"},
            headers={"Authorization": f"Bearer {blogger_token()}", "Content-Type": "application/json"},
            json={"kind": "blogger#post", "title": title, "content": body, "labels": labels}, timeout=120)
        t = r.text[:300]
        if r.status_code == 401:
            blogger_token(force=True)
            raise RuntimeError("401 -> token refreshed")
        if r.status_code == 404:
            sys.exit("FATAL: BLOG_ID is wrong or this Google account cannot access the blog. " + t)
        if r.status_code == 403 and not re.search(r"rate|quota|limit", t, re.I):
            sys.exit("FATAL: Blogger denied access (403). Check the Google account owns this blog and the "
                     "Blogger API is enabled. " + t)
        if r.status_code in (403, 408, 429, 500, 502, 503, 504):
            raise RuntimeError(f"Blogger HTTP {r.status_code}: {t[:150]}")
        if r.status_code == 400:
            raise ValidationError("Blogger rejected the post: " + t)
        r.raise_for_status()
        return r.json()

    return with_retries(call, tries=6, base=10, what="blogger")


# ---------------- main flow ----------------
def run_once(hist):
    c = pick_cluster(cluster_entries(collect_entries()), hist)
    if not c:
        raise NoStory("no new political story right now")
    key = c[0]["link"]
    log.info("story: %s (%d sources)", c[0]["title"], len({e["source"] for e in c}))
    try:
        m = gather(c)
        article = write_article(m, hist)
        body = render(article, m)
        labels = []
        for l in ["World Politics", "International Relations"] + article["labels"]:
            if l.lower() not in [x.lower() for x in labels]:
                labels.append(l)
        res = publish(article["title"], body, labels[:6])
    except (NotPolitics, NoImages, SkipStory):
        FAILS[key] = 9
        raise
    except ValidationError:
        FAILS[key] = FAILS.get(key, 0) + 1
        raise
    log.info("PUBLISHED: %s -> %s", article["title"], res.get("url"))
    hist["posts"].append({"title": article["title"], "url": res.get("url"), "time": int(time.time()),
                          "links": [e["link"] for e in c], "source_titles": [e["title"] for e in c],
                          "topic": sorted(topic_tokens(c))})
    hist["next_slot"] += INTERVAL
    save_history(hist)
    push_history()


def publish_one(hist):
    attempt = 0
    while True:
        attempt += 1
        try:
            run_once(hist)
            return True
        except Exception as ex:
            log.error("attempt %d failed: %s: %s", attempt, type(ex).__name__, ex)
            if isinstance(ex, (FileNotFoundError, PermissionError)):
                sys.exit(f"FATAL config error: {ex}")
            if time.time() - START > MAX_RUN_SECONDS:
                return False
            if isinstance(ex, (NoImages, SkipStory, NotPolitics)):
                continue
            time.sleep(3 if isinstance(ex, ValidationError) else min(30 * attempt, 300))


def main():
    for k, v in {"BLOGGER_CLIENT_ID": BLOGGER_CLIENT_ID, "BLOGGER_CLIENT_SECRET": BLOGGER_CLIENT_SECRET,
                 "BLOGGER_REFRESH_TOKEN": BLOGGER_REFRESH_TOKEN, "BLOG_ID": BLOG_ID}.items():
        if not v:
            sys.exit(f"FATAL: missing secret {k} (GitHub -> Settings -> Secrets -> Actions)")
    if not OPENROUTER_API_KEY:
        log.warning("OPENROUTER_API_KEY missing -> all posts will use the no-API fallback writer")
    hist = load_history()
    if not hist["next_slot"]:
        hist["next_slot"] = time.time()
    if time.time() < hist["next_slot"]:
        log.info("nothing due yet (next slot in %d min)", (hist["next_slot"] - time.time()) / 60)
        return 0
    set_blog_name()
    done = 0
    while time.time() >= hist["next_slot"] and done < MAX_CATCHUP:
        if not publish_one(hist):
            log.error("could not publish within time budget; next run continues")
            return 1
        done += 1
        if time.time() >= hist["next_slot"] and done < MAX_CATCHUP:
            time.sleep(SPACING)
    return 0


if __name__ == "__main__":
    sys.exit(main())
