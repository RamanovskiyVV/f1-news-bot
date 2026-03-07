"""One-time script: rebuild seen_news.json with normalized URL hashes."""
import json, hashlib, feedparser, httpx
from urllib.parse import urlparse, urlunparse
from config import F1_SOURCES

def norm(url):
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path.rstrip('/'), '', '', ''))

seen = []
seen_s = set()

# Add all current RSS entries with normalized hashes
for source in F1_SOURCES:
    try:
        r = httpx.get(source["rss"], headers={"User-Agent": "Mozilla/5.0"}, timeout=15, follow_redirects=True)
        feed = feedparser.parse(r.text)
        for entry in feed.entries[:15]:
            url = entry.get("link", "").strip()
            uid = hashlib.md5(norm(url).encode()).hexdigest()
            if uid not in seen_s:
                seen.append(uid)
                seen_s.add(uid)
        print(f"  {source['name']}: added {len(feed.entries[:15])} entries")
    except Exception as e:
        print(f"  {source['name']}: error {e}")

# Keep old entries too
old = json.load(open("seen_news.json"))
for h in old:
    if h not in seen_s:
        seen.append(h)
        seen_s.add(h)

if len(seen) > 1000:
    seen = seen[-1000:]

json.dump(seen, open("seen_news.json", "w"))
print(f"\nSaved {len(seen)} entries to seen_news.json")
