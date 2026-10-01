#!/usr/bin/env python3
"""SEO booster: wraps auto_blogger.py without modifying it."""
import html
import re
import sys
from datetime import datetime, timezone

import auto_blogger as ab

_orig_render = ab.render


def _slug(text, i):
    s = re.sub(r"[^a-z0-9]+", "-", re.sub(r"<[^>]+>", "", text).lower()).strip("-")[:50]
    return f"s{i}-{s}" if s else f"s{i}"


def seo_render(a, m):
    body = _orig_render(a, m)

    # 1) anchor ids on every <h2> + table of contents
    toc, counter = [], [0]

    def add_id(mo):
        counter[0] += 1
        attrs, inner = mo.group(1), mo.group(2)
        sid = _slug(inner, counter[0])
        toc.append((sid, re.sub(r"<[^>]+>", "", inner).strip()))
        return f'<h2{attrs} id="{sid}">{inner}</h2>'

    body = re.sub(r"<h2([^>]*)>(.*?)</h2>", add_id, body, flags=re.S | re.I)

    # 2) first image loads immediately (better page speed), the rest stay lazy
    body = body.replace('loading="lazy"', 'loading="eager"', 1)

    # 3) top block: meta summary (Blogger uses the start of the post as description) + date + reading time
    words = len(re.sub(r"<[^>]+>", " ", body).split())
    minutes = max(1, round(words / 200))
    date = datetime.now(timezone.utc).strftime("%B %d, %Y")
    head = ""
    meta = (a.get("meta") or "").strip()[:160]
    if meta:
        head += f"<p><strong>{html.escape(meta)}</strong></p>"
    head += f'<p style="color:#666;font-size:.9em">Published {date} &bull; {minutes} min read</p>'
    if toc:
        items = "".join(f'<li><a href="#{i}">{html.escape(t)}</a></li>' for i, t in toc)
        head += ('<nav style="background:#f6f8fa;border-left:4px solid #1a73e8;padding:12px 18px;margin:18px 0">'
                 '<strong>In this article</strong><ol style="margin:8px 0 0 18px">' + items + "</ol></nav>")
    body = head + body

    # 4) internal links: related earlier posts (same topic first, then most recent)
    try:
        hist = ab.load_history()
        posts = [p for p in hist["posts"][-200:] if str(p.get("url", "")).startswith("http")]
        tt = ab.topic_tokens(m["cluster"])
        scored = sorted(posts, key=lambda p: -ab.overlap(tt, set(p.get("topic", []))))
        rel = [p for p in scored if ab.overlap(tt, set(p.get("topic", []))) > 0][:5]
        for p in reversed(posts):
            if len(rel) >= 4:
                break
            if p not in rel:
                rel.append(p)
        if rel:
            li = "".join(f'<li><a href="{html.escape(p["url"], quote=True)}">{html.escape(p["title"])}</a></li>'
                         for p in rel[:5])
            body += f"<h2>Related Reading</h2><ul>{li}</ul>"
    except Exception as e:
        ab.log.warning("related links skipped: %s", e)

    return body


ab.render = seo_render

if __name__ == "__main__":
    sys.exit(ab.main())
