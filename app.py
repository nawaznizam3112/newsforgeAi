"""
Global Pulse - single-file AI news aggregator (Streamlit + SQLite + Gemini).
Run UI:        streamlit run app.py
Run scraper:   python app.py --scrape        (used by GitHub Actions)
Secrets/env:   GEMINI_API_KEY, ADMIN_PASSWORD
"""
import json, os, re, sqlite3, sys, time
from contextlib import contextmanager

import feedparser
import pandas as pd
import requests
import streamlit as st
import trafilatura
from bs4 import BeautifulSoup

# ============================== CONFIG ==============================
DB_PATH = os.getenv("NEWS_DB", "news.db")
CATEGORIES = ["World", "India", "Business", "Technology", "Health", "Science", "Sports", "Entertainment"]
UA = {"User-Agent": "Mozilla/5.0 (compatible; GlobalPulseBot/1.0)"}
PLACEHOLDER = "https://placehold.co/800x450/1f2937/ffffff?text=News"

DEFAULT_SOURCES = [  # (name, rss_url, category, country)
    ("BBC World", "http://feeds.bbci.co.uk/news/world/rss.xml", "World", "UK"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "World", "Qatar"),
    ("The Guardian World", "https://www.theguardian.com/world/rss", "World", "UK"),
    ("DW", "https://rss.dw.com/rdf/rss-en-all", "World", "Germany"),
    ("The Hindu", "https://www.thehindu.com/news/national/feeder/default.rss", "India", "India"),
    ("Times of India", "https://timesofindia.indiatimes.com/rssfeedstopstories.cms", "India", "India"),
    ("BBC Business", "http://feeds.bbci.co.uk/news/business/rss.xml", "Business", "UK"),
    ("TechCrunch", "https://techcrunch.com/feed/", "Technology", "USA"),
    ("BBC Health", "http://feeds.bbci.co.uk/news/health/rss.xml", "Health", "UK"),
    ("BBC Science", "http://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "Science", "UK"),
    ("BBC Sport", "http://feeds.bbci.co.uk/sport/rss.xml", "Sports", "UK"),
    ("BBC Entertainment", "http://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml", "Entertainment", "UK"),
]
DEFAULT_SETTINGS = {
    "site_title": "Global Pulse",
    "auto_publish": "1",     # 1 = rewritten articles go live automatically
    "ai_enabled": "1",       # 0 = plain extractive summary, no Gemini calls
    "max_per_feed": "5",     # new articles per source per run
    "model": "gemini-2.5-flash",
}

# ============================== DATABASE ==============================
SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, url TEXT UNIQUE,
  category TEXT, country TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS articles(
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE, source TEXT, category TEXT, country TEXT,
  orig_title TEXT, orig_text TEXT, image TEXT, published TEXT,
  title TEXT, summary TEXT, body TEXT,
  status TEXT DEFAULT 'pending', featured INTEGER DEFAULT 0, ai_done INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""

@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()

def q(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]

def ex(sql, args=()):
    with conn() as c:
        return c.execute(sql, args).lastrowid

def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0:
            c.executemany("INSERT INTO sources(name,url,category,country) VALUES(?,?,?,?)", DEFAULT_SOURCES)
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))

def get_setting(key):
    r = q("SELECT value FROM settings WHERE key=?", (key,))
    return r[0]["value"] if r else DEFAULT_SETTINGS.get(key, "")

def set_setting(key, value):
    ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, str(value)))

# ============================== SECRETS ==============================
def secret(name):
    v = os.getenv(name)
    if v:
        return v
    try:
        return st.secrets[name]
    except Exception:
        return None

# ============================== AI REWRITER (GEMINI) ==============================
SYSTEM = f"""You are a news editor. Rewrite the given article in your own original wording.
Rules: stay strictly factual, never add facts that are not in the source, neutral tone, no clickbait.
Return ONLY JSON with keys: "title" (max 90 chars), "summary" (1-2 sentences, max 220 chars),
"body" (3-5 short paragraphs, 150-250 words, separated by blank lines).
Categories for context: {", ".join(CATEGORIES)}."""

def fallback_rewrite(title, text):
    text = (text or "").strip()
    sents = re.split(r"(?<=[.!?])\s+", text)
    summary = " ".join(sents[:2])[:260]
    body = "\n\n".join(" ".join(sents[i:i + 2]) for i in range(0, min(len(sents), 8), 2))
    return {"title": title, "summary": summary or title, "body": body or summary or title}

def ai_rewrite(title, text):
    key = secret("GEMINI_API_KEY")
    if get_setting("ai_enabled") != "1" or not key or not text:
        return fallback_rewrite(title, text), False
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model=get_setting("model"),
            contents=f"TITLE: {title}\n\nARTICLE:\n{text[:6000]}",
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM, response_mime_type="application/json",
                max_output_tokens=1200, temperature=0.4),
        )
        raw = re.sub(r"^```(?:json)?|```$", "", (resp.text or "").strip(), flags=re.M).strip()
        data = json.loads(raw)
        return {k: data[k] for k in ("title", "summary", "body")}, True
    except Exception as e:
        print("Gemini rewrite failed:", e)
        return fallback_rewrite(title, text), False

# ============================== SCRAPER ==============================
def feed_image(e):
    for k in ("media_content", "media_thumbnail"):
        for m in e.get(k, []) or []:
            if m.get("url"):
                return m["url"]
    for l in e.get("links", []):
        if str(l.get("type", "")).startswith("image"):
            return l.get("href")
    m = re.search(r'<img[^>]+src="([^"]+)"', e.get("summary", "") or "")
    return m.group(1) if m else None

def fetch_page(url):
    try:
        r = requests.get(url, headers=UA, timeout=12)
        r.raise_for_status()
    except Exception:
        return None, None
    text = trafilatura.extract(r.text, include_comments=False)
    og = BeautifulSoup(r.text, "html.parser").find("meta", property="og:image")
    return text, (og.get("content") if og else None)

def scrape_all(log=print):
    init_db()
    maxn = int(get_setting("max_per_feed") or 5)
    added = 0
    for s in q("SELECT * FROM sources WHERE active=1"):
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
            if not url or q("SELECT 1 FROM articles WHERE url=?", (url,)):
                continue
            text, og = fetch_page(url)
            text = text or re.sub("<[^>]+>", "", e.get("summary", ""))
            ex("""INSERT OR IGNORE INTO articles(url,source,category,country,orig_title,orig_text,image,published)
                  VALUES(?,?,?,?,?,?,?,?)""",
               (url, s["name"], s["category"], s["country"], e.get("title", ""), text,
                feed_image(e) or og, e.get("published", "")))
            new += 1
            time.sleep(0.3)
        added += new
        log(f"[{s['name']}] +{new} new")
    return added

def rewrite_pending(limit=25, log=print):
    init_db()
    auto = get_setting("auto_publish") == "1"
    done = 0
    for a in q("SELECT * FROM articles WHERE ai_done=0 ORDER BY id DESC LIMIT ?", (limit,)):
        r, used_ai = ai_rewrite(a["orig_title"], a["orig_text"])
        ex("UPDATE articles SET title=?, summary=?, body=?, ai_done=1, status=? WHERE id=?",
           (r["title"], r["summary"], r["body"], "published" if auto else "pending", a["id"]))
        done += 1
        log(f"rewrote: {r['title'][:70]}")
        if used_ai:
            time.sleep(4)  # stay under Gemini free-tier rate limits
    log(f"Done - {done} articles processed")
    return done

# ============================== PUBLIC NEWS PAGE ==============================
CSS = """
<style>
.block-container{padding-top:2rem;max-width:1250px}
h1.brand{font-family:Georgia,serif;font-size:2.6rem;margin:0}
.tag{display:inline-block;background:#b91c1c;color:#fff;border-radius:4px;padding:1px 8px;font-size:.72rem;letter-spacing:.04em}
.meta{color:#6b7280;font-size:.8rem}
.card-title{font-family:Georgia,serif;font-weight:700;font-size:1.1rem;line-height:1.3;margin:.3rem 0}
[data-testid="stImage"] img{border-radius:10px;object-fit:cover}
</style>"""

@st.cache_data(ttl=120)
def load_articles(category=None, search="", country=None):
    sql, args = "SELECT * FROM articles WHERE status='published'", []
    if category:
        sql += " AND category=?"; args.append(category)
    if country:
        sql += " AND country=?"; args.append(country)
    if search:
        sql += " AND (title LIKE ? OR summary LIKE ?)"; args += [f"%{search}%"] * 2
    return q(sql + " ORDER BY featured DESC, id DESC LIMIT 60", args)

@st.dialog("Article", width="large")
def reader(a):
    st.markdown(f"<span class='tag'>{a['category'].upper()}</span>", unsafe_allow_html=True)
    st.subheader(a["title"])
    if a["image"]:
        st.image(a["image"], width="stretch")
    st.caption(f"{a['source']} · {a['country']} · {a['published'] or a['created_at']}")
    st.write(a["body"])
    st.link_button("Read original source ↗", a["url"])

def card(a, tab_key):
    with st.container(border=True):
        st.image(a["image"] or PLACEHOLDER, width="stretch")
        st.markdown(f"<span class='tag'>{a['category'].upper()}</span> <span class='meta'>{a['source']}</span>"
                    f"<div class='card-title'>{a['title']}</div>", unsafe_allow_html=True)
        st.caption(a["summary"])
        if st.button("Read more", key=f"r{tab_key}{a['id']}"):
            reader(a)

def news_page():
    title = get_setting("site_title")
    st.markdown(f"<h1 class='brand'>📰 {title}</h1>", unsafe_allow_html=True)
    st.caption("AI-summarised global news with links to original sources.")
    with st.sidebar:
        st.header("Filters")
        search = st.text_input("Search")
        countries = [r["country"] for r in q("SELECT DISTINCT country FROM articles WHERE status='published' ORDER BY country")]
        country = st.selectbox("Country / source region", ["All"] + countries)
        if st.button("Refresh"):
            st.cache_data.clear()
    tabs = st.tabs(["Top"] + CATEGORIES)
    for tab, cat in zip(tabs, [None] + CATEGORIES):
        with tab:
            items = load_articles(cat, search, None if country == "All" else country)
            if not items:
                st.info("No articles yet. Open Admin (sidebar) → Run → Scrape now.")
                continue
            hero, rest = items[0], items[1:]
            c1, c2 = st.columns([3, 2])
            with c1:
                st.image(hero["image"] or PLACEHOLDER, width="stretch")
            with c2:
                st.markdown(f"<span class='tag'>{hero['category'].upper()}</span> <span class='meta'>{hero['source']}</span>"
                            f"<h2 style='font-family:Georgia,serif'>{hero['title']}</h2>", unsafe_allow_html=True)
                st.write(hero["summary"])
                if st.button("Read full story", key=f"h{cat}{hero['id']}"):
                    reader(hero)
            st.divider()
            for i in range(0, len(rest), 3):
                for col, a in zip(st.columns(3), rest[i:i + 3]):
                    with col:
                        card(a, cat or "top")

# ============================== ADMIN PAGE ==============================
def save_article(a_id, title, summary, body, image, cat, feat, status):
    ex("UPDATE articles SET title=?,summary=?,body=?,image=?,category=?,featured=?,status=? WHERE id=?",
       (title, summary, body, image, cat, int(feat), status, a_id))
    st.cache_data.clear()

def admin_page():
    pw = secret("ADMIN_PASSWORD")
    st.title("⚙️ Admin Dashboard")
    if not pw:
        st.error("ADMIN_PASSWORD is not set. On Streamlit Cloud: Manage app → Settings → Secrets, add:\n\n"
                 "ADMIN_PASSWORD = \"your-password\"\n\nGEMINI_API_KEY = \"your-key\"")
        return
    if not st.session_state.get("auth"):
        entered = st.text_input("Password", type="password")
        if st.button("Login"):
            if entered == pw:
                st.session_state.auth = True
                st.rerun()
            else:
                st.error("Wrong password")
        return
    if st.sidebar.button("Logout"):
        st.session_state.auth = False
        st.rerun()

    t_over, t_art, t_src, t_run, t_set = st.tabs(["Overview", "Articles", "Sources", "Run", "Settings"])

    with t_over:
        counts = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM articles GROUP BY status")}
        c = st.columns(4)
        c[0].metric("Published", counts.get("published", 0))
        c[1].metric("Pending review", counts.get("pending", 0))
        c[2].metric("Rejected", counts.get("rejected", 0))
        c[3].metric("Active sources", q("SELECT COUNT(*) n FROM sources WHERE active=1")[0]["n"])
        df = pd.DataFrame(q("SELECT category, COUNT(*) AS articles FROM articles GROUP BY category"))
        if not df.empty:
            st.bar_chart(df.set_index("category"))
        st.subheader("Articles per source")
        st.dataframe(pd.DataFrame(q("SELECT source, COUNT(*) AS articles FROM articles GROUP BY source ORDER BY 2 DESC")),
                     width="stretch", hide_index=True)

    with t_art:
        f1, f2, f3 = st.columns(3)
        status = f1.selectbox("Status", ["pending", "published", "rejected"], index=1)
        cat = f2.selectbox("Category", ["All"] + CATEGORIES)
        limit = f3.slider("Show", 5, 50, 15)
        sql, args = "SELECT * FROM articles WHERE status=?", [status]
        if cat != "All":
            sql += " AND category=?"; args.append(cat)
        for a in q(sql + " ORDER BY id DESC LIMIT ?", args + [limit]):
            with st.expander(f"[{a['category']}] {a['title'] or a['orig_title']} — {a['source']}"):
                if a["image"]:
                    st.image(a["image"], width=280)
                i = a["id"]
                title = st.text_input("Title", a["title"] or "", key=f"t{i}")
                summary = st.text_area("Summary", a["summary"] or "", key=f"s{i}", height=70)
                body = st.text_area("Body", a["body"] or "", key=f"b{i}", height=220)
                image = st.text_input("Image URL", a["image"] or "", key=f"i{i}")
                newcat = st.selectbox("Category", CATEGORIES,
                                      index=CATEGORIES.index(a["category"]) if a["category"] in CATEGORIES else 0, key=f"c{i}")
                feat = st.checkbox("Featured (pin to top)", bool(a["featured"]), key=f"f{i}")
                b1, b2, b3, b4 = st.columns(4)
                if b1.button("✅ Publish", key=f"p{i}"):
                    save_article(i, title, summary, body, image, newcat, feat, "published"); st.rerun()
                if b2.button("💾 Save", key=f"sv{i}"):
                    save_article(i, title, summary, body, image, newcat, feat, a["status"]); st.success("Saved")
                if b3.button("🚫 Reject", key=f"r{i}"):
                    save_article(i, title, summary, body, image, newcat, feat, "rejected"); st.rerun()
                if b4.button("🗑 Delete", key=f"d{i}"):
                    ex("DELETE FROM articles WHERE id=?", (i,)); st.cache_data.clear(); st.rerun()
                st.markdown(f"[Original source]({a['url']})")

    with t_src:
        st.caption("Add RSS feeds, edit categories, toggle sources. Select a row and press Delete to remove it.")
        src = pd.DataFrame(q("SELECT id, name, url, category, country, active FROM sources"))
        edited = st.data_editor(
            src, num_rows="dynamic", width="stretch", hide_index=True,
            column_config={"id": st.column_config.NumberColumn(disabled=True),
                           "category": st.column_config.SelectboxColumn(options=CATEGORIES),
                           "active": st.column_config.CheckboxColumn()})
        if st.button("Save sources"):
            keep = set()
            for _, r in edited.iterrows():
                if not r["url"]:
                    continue
                active = int(bool(r["active"])) if pd.notna(r["active"]) else 1
                if pd.notna(r["id"]):
                    ex("UPDATE sources SET name=?,url=?,category=?,country=?,active=? WHERE id=?",
                       (r["name"], r["url"], r["category"], r["country"], active, int(r["id"])))
                    keep.add(int(r["id"]))
                else:
                    ex("INSERT OR IGNORE INTO sources(name,url,category,country,active) VALUES(?,?,?,?,?)",
                       (r["name"], r["url"], r["category"] or "World", r["country"], active))
            for old in src["id"].tolist():
                if old not in keep:
                    ex("DELETE FROM sources WHERE id=?", (old,))
            st.success("Sources saved"); st.rerun()

    with t_run:
        st.write("Step 1: scrape the feeds. Step 2: rewrite new articles with Gemini.")
        if not secret("GEMINI_API_KEY"):
            st.warning("GEMINI_API_KEY not set: articles will get a basic summary instead of an AI rewrite.")
        c1, c2 = st.columns(2)
        box = st.empty()
        logs = []
        def log(m):
            logs.append(str(m)); box.code("\n".join(logs[-25:]))
        if c1.button("▶ Scrape now", type="primary"):
            with st.spinner("Scraping feeds (can take a couple of minutes)..."):
                log(f"Added {scrape_all(log)} new articles")
        if c2.button("✍️ Rewrite pending (AI)", type="primary"):
            with st.spinner("Rewriting with Gemini..."):
                rewrite_pending(25, log)
            st.cache_data.clear()
        pend = q("SELECT COUNT(*) n FROM articles WHERE ai_done=0")[0]["n"]
        st.info(f"{pend} scraped articles are waiting for rewriting.")

    with t_set:
        title = st.text_input("Site title", get_setting("site_title"))
        auto = st.toggle("Auto-publish rewritten articles (off = manual review queue)", get_setting("auto_publish") == "1")
        ai = st.toggle("Use AI rewriting", get_setting("ai_enabled") == "1")
        maxn = st.number_input("Max new articles per source per run", 1, 30, int(get_setting("max_per_feed")))
        model = st.text_input("Gemini model", get_setting("model"))
        if st.button("Save settings"):
            for k, v in {"site_title": title, "auto_publish": int(auto), "ai_enabled": int(ai),
                         "max_per_feed": maxn, "model": model}.items():
                set_setting(k, v)
            st.cache_data.clear()
            st.success("Saved")

# ============================== ENTRY POINT ==============================
def main():
    init_db()
    st.set_page_config(page_title=get_setting("site_title"), page_icon="📰", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    page = st.sidebar.radio("Navigate", ["📰 News", "⚙️ Admin"], label_visibility="collapsed")
    st.sidebar.divider()
    if page == "📰 News":
        news_page()
    else:
        admin_page()

if "--scrape" in sys.argv:           # headless mode for GitHub Actions
    init_db()
    scrape_all()
    rewrite_pending(40)
else:
    main()
