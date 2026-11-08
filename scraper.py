import os, re, time
import feedparser, requests, trafilatura
from bs4 import BeautifulSoup
import db
from ai_rewriter import rewrite

UA = {"User-Agent": "Mozilla/5.0 (compatible; GlobalPulseBot/1.0)"}

def _feed_image(e):
    for k in ("media_content", "media_thumbnail"):
        for m in e.get(k, []) or []:
            if m.get("url"):
                return m["url"]
    for l in e.get("links", []):
        if l.get("type", "").startswith("image"):
            return l.get("href")
    m = re.search(r'<img[^>]+src="([^"]+)"', e.get("summary", "") or "")
    return m.group(1) if m else None

def _fetch_page(url):
    try:
        r = requests.get(url, headers=UA, timeout=12)
        r.raise_for_status()
    except Exception:
        return None, None
    text = trafilatura.extract(r.text, include_comments=False)
    og = BeautifulSoup(r.text, "html.parser").find("meta", property="og:image")
    return text, (og.get("content") if og else None)

def scrape_all(log=print):
    db.init()
    maxn = int(db.get_setting("max_per_feed") or 8)
    added = 0
    for s in db.q("SELECT * FROM sources WHERE active=1"):
        try:
            feed = feedparser.parse(s["url"], request_headers=UA)
        except Exception as e:
            log(f"[{s['name']}] feed error: {e}")
            continue
        new = 0
        for e in feed.entries:
            if new >= maxn:
                break
            url = e.get("link")
            if not url or db.q("SELECT 1 FROM articles WHERE url=?", (url,)):
                continue
            text, og = _fetch_page(url)
            text = text or re.sub("<[^>]+>", "", e.get("summary", ""))
            db.ex("""INSERT OR IGNORE INTO articles(url,source,category,country,orig_title,orig_text,image,published)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (url, s["name"], s["category"], s["country"], e.get("title", ""), text,
                   _feed_image(e) or og, e.get("published", "")))
            new += 1
            time.sleep(0.3)
        added += new
        log(f"[{s['name']}] +{new}")
    return added

def rewrite_pending(limit=25, log=print):
    auto = db.get_setting("auto_publish") == "1"
    done = 0
    for a in db.q("SELECT * FROM articles WHERE ai_done=0 ORDER BY id DESC LIMIT ?", (limit,)):
        r = rewrite(a["orig_title"], a["orig_text"])
        db.ex("UPDATE articles SET title=?, summary=?, body=?, ai_done=1, status=? WHERE id=?",
              (r["title"], r["summary"], r["body"], "published" if auto else "pending", a["id"]))
        done += 1
        if os.getenv('GEMINI_API_KEY') and db.get_setting('ai_enabled') == '1':
            time.sleep(4)  # stay under free-tier rate limits
    log(f"rewrote {done} articles")
    return done

if __name__ == "__main__":
    scrape_all()
    rewrite_pending(limit=40)
