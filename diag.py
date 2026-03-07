import json, hashlib, feedparser, httpx
from config import F1_SOURCES

seen = set(json.load(open('seen_news.json')))
print(f'Seen hashes: {len(seen)}')

for source in F1_SOURCES:
    name = source['name']
    try:
        r = httpx.get(source['rss'], headers={'User-Agent': 'Mozilla/5.0'}, timeout=15, follow_redirects=True)
        feed = feedparser.parse(r.text)
        for entry in feed.entries[:15]:
            url = entry.get('link','').strip()
            title = entry.get('title','').strip()
            uid = hashlib.md5(url.encode()).hexdigest()
            if uid not in seen:
                print(f'NEW [{name}] uid={uid}')
                print(f'    URL: {url}')
                print(f'    Title: {title}')
    except Exception as e:
        print(f'ERR {name}: {e}')
