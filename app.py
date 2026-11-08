"""
RegPulse - medical device & dental regulatory intelligence (single file).
Streamlit + SQLite + Gemini.  No external database needed.
  UI:        streamlit run app.py
  Scraper:   python app.py --scrape      (GitHub Actions)
Secrets/env: GEMINI_API_KEY, ADMIN_PASSWORD
"""
import html, json, os, re, sqlite3, sys, time
from contextlib import contextmanager
from datetime import date

import feedparser
import pandas as pd
import requests
import streamlit as st
import trafilatura
from bs4 import BeautifulSoup

# ============================== CONFIG ==============================
DB_PATH = os.getenv("NEWS_DB", "news.db")
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, text/html;q=0.6, */*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9",
}
NICHE = "regintel-v2"

CATEGORIES = ["US FDA", "EU MDR/IVDR", "UK MHRA", "India CDSCO", "Global & Standards", "Dental", "Industry"]
REGIONS = ["US", "EU", "UK", "India", "Global", "Other"]
IMPACTS = ["High", "Medium", "Low"]
ROLES = ["Regulatory Affairs", "Quality / Compliance", "Clinician / Dentist", "Manufacturer / Industry", "Student", "Other"]
IMPACT_COLOR = {"High": "#b91c1c", "Medium": "#b45309", "Low": "#15803d"}

FDA = "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds"
# (name, rss_url, category, region, keywords-filter)
DEFAULT_SOURCES = [
    ("FDA Press Releases", f"{FDA}/press-releases/rss.xml", "US FDA", "US",
     "device,diagnostic,dental,510(k),clearance,UDI,software,implant"),
    ("FDA Recalls & Alerts", f"{FDA}/recalls/rss.xml", "US FDA", "US",
     "device,pump,implant,catheter,diagnostic,monitor,stent,software,dental"),
    ("FDA MedWatch Alerts", f"{FDA}/medwatch/rss.xml", "US FDA", "US", "device"),
    ("Federal Register - FDA devices",
     "https://www.federalregister.gov/api/v1/documents.rss?conditions%5Bagencies%5D%5B%5D=food-and-drug-administration"
     "&conditions%5Bterm%5D=%22medical+device%22", "US FDA", "US", ""),
    ("MHRA (GOV.UK)", "https://www.gov.uk/government/organisations/medicines-and-healthcare-products-regulatory-agency.atom",
     "UK MHRA", "UK", "device,diagnostic,software,UKCA,implant"),
    ("Easy Medical Device", "https://www.easymedicaldevice.com/feed/", "EU MDR/IVDR", "EU", ""),
    ("Johner Institute", "https://blog.johner-institute.com/feed/", "Global & Standards", "Global", ""),
    ("WHO News", "https://www.who.int/rss-feeds/news-english.xml", "Global & Standards", "Global", "device,diagnostic,regulat"),
    ("Medical Dialogues (India)", "https://medicaldialogues.in/feed", "India CDSCO", "India", "CDSCO,medical device,devices,MDR"),
    ("Dentistry Today", "https://www.dentistrytoday.com/feed/", "Dental", "Global", ""),
    ("MedTech Dive", "https://www.medtechdive.com/feeds/news/", "Industry", "US", ""),
    ("MassDevice", "https://www.massdevice.com/feed/", "Industry", "US", ""),
    ("Medical Device Network", "https://www.medicaldevice-network.com/feed/", "Industry", "Global", ""),
]
DEFAULT_SETTINGS = {
    "site_title": "RegPulse",
    "tagline": "Medical device & dental regulatory intelligence - what changed, who it affects, what to do.",
    "auto_publish": "0",      # 0 = review queue (recommended for regulatory content)
    "ai_enabled": "1",
    "max_per_feed": "4",
    "model": "gemini-2.5-flash",
    "editor_label": "Editor's note",
    "signup_url": "",         # optional external signup page (Substack / Beehiiv / Buttondown)
    "signup_webhook": "",     # optional: POST new subscribers here (Formspree / Google Apps Script)
}

# ============================== DATABASE ==============================
SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, url TEXT UNIQUE,
  category TEXT, country TEXT, active INTEGER DEFAULT 1, keywords TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS articles(
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE, source TEXT, category TEXT, country TEXT,
  orig_title TEXT, orig_text TEXT, image TEXT, published TEXT,
  title TEXT, summary TEXT, body TEXT,
  what_changed TEXT, who_affected TEXT, action TEXT, impact TEXT DEFAULT 'Medium',
  region TEXT, expert_note TEXT DEFAULT '',
  status TEXT DEFAULT 'pending', featured INTEGER DEFAULT 0, ai_done INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS subscribers(
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, role TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""
MIGRATIONS = {  # lets an older news.db upgrade in place
    "sources": {"keywords": "TEXT DEFAULT ''"},
    "articles": {"what_changed": "TEXT", "who_affected": "TEXT", "action": "TEXT",
                 "impact": "TEXT DEFAULT 'Medium'", "region": "TEXT", "expert_note": "TEXT DEFAULT ''"},
}

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
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, ddl in cols.items():
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        row = c.execute("SELECT value FROM settings WHERE key='niche'").fetchone()
        prev = row[0] if row else None
        if prev != NICHE:
            if prev == "regintel-v1":       # fix wrong FDA feed URLs, keep articles and your edits
                c.execute("DELETE FROM sources WHERE url LIKE '%/medical-devices/rss.xml' OR url LIKE '%/medical-device-recalls/rss.xml'")
                c.executemany("INSERT OR IGNORE INTO sources(name,url,category,country,keywords) VALUES(?,?,?,?,?)", DEFAULT_SOURCES)
            else:                           # one-time switch from the old general-news setup
                c.execute("UPDATE articles SET status='archived' WHERE status IN ('published','pending')")
                c.execute("DELETE FROM sources")
                c.executemany("INSERT INTO sources(name,url,category,country,keywords) VALUES(?,?,?,?,?)", DEFAULT_SOURCES)
                for k in ("site_title", "tagline", "auto_publish", "max_per_feed"):
                    c.execute("UPDATE settings SET value=? WHERE key=?", (DEFAULT_SETTINGS[k], k))
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('niche',?)", (NICHE,))

def get_setting(key):
    r = q("SELECT value FROM settings WHERE key=?", (key,))
    return r[0]["value"] if r else DEFAULT_SETTINGS.get(key, "")

def set_setting(key, value):
    ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

def secret(name):
    v = os.getenv(name)
    if v:
        return v
    try:
        return st.secrets[name]
    except Exception:
        return None

# ============================== AI SUMMARISER (GEMINI) ==============================
SYSTEM = f"""You are a regulatory-affairs analyst for medical devices and dental products.
Summarise the source article for busy regulatory professionals, in your own words.
Strict rules: use ONLY facts in the source; never invent dates, regulation numbers, product names or
deadlines; if something is not stated write "Not stated in source". Neutral, precise tone. This is information,
not legal advice.
Return ONLY JSON with keys:
"headline" (max 100 chars, specific),
"summary" (1-2 sentences, max 240 chars),
"what_changed" (1-3 sentences: the new rule, guidance, recall, approval or event),
"who_affected" (1 sentence: manufacturers, importers, notified bodies, clinicians, which device classes/regions),
"action" (1-2 sentences: practical next step or deadline to watch; "No action stated in source" if none),
"impact" (one of High / Medium / Low: High = recalls, safety alerts, new mandatory requirements or imminent deadlines;
Medium = guidance, consultations, notable approvals; Low = general news or commentary),
"region" (one of {", ".join(REGIONS)})."""

def fallback_summary(title, text):
    text = (text or "").strip()
    sents = re.split(r"(?<=[.!?])\s+", text)
    return {"headline": title, "summary": " ".join(sents[:2])[:260] or title,
            "what_changed": " ".join(sents[:3])[:500] or "See source.",
            "who_affected": "See original source.", "action": "Review the original source.",
            "impact": "Medium", "region": ""}

def ai_summarise(title, text):
    key = secret("GEMINI_API_KEY")
    if get_setting("ai_enabled") != "1" or not key or not text:
        return fallback_summary(title, text), False
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model=get_setting("model"),
            contents=f"TITLE: {title}\n\nARTICLE:\n{text[:7000]}",
            config=types.GenerateContentConfig(system_instruction=SYSTEM, response_mime_type="application/json",
                                               max_output_tokens=1200, temperature=0.2))
        raw = re.sub(r"^```(?:json)?|```$", "", (resp.text or "").strip(), flags=re.M).strip()
        d = json.loads(raw)
        out = {k: str(d.get(k, "")).strip() for k in ("headline", "summary", "what_changed", "who_affected", "action", "impact", "region")}
        if out["impact"] not in IMPACTS:
            out["impact"] = "Medium"
        if out["region"] not in REGIONS:
            out["region"] = ""
        return out, True
    except Exception as e:
        print("Gemini failed:", e)
        return fallback_summary(title, text), False

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

def fetch_feed(url):
    """Fetch + parse a feed with browser headers; tries URL variants. Returns (parsed|None, status_text)."""
    variants = [url]
    if url.startswith("http"):
        variants.append(url.replace("://www.", "://", 1) if "://www." in url else url.replace("://", "://www.", 1))
        if "?" not in url:
            variants.append(url.rstrip("/") + "/?feed=rss2")
    last = "no response"
    for u in variants:
        try:
            if os.path.exists(u):
                parsed = feedparser.parse(u)
            else:
                r = requests.get(u, headers=UA, timeout=15)
                if r.status_code != 200:
                    last = f"HTTP {r.status_code}"; continue
                parsed = feedparser.parse(r.content)
            if parsed.entries:
                return parsed, "OK"
            last = "reachable but no feed entries"
        except Exception as e:
            last = type(e).__name__
    return None, last

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
    maxn = int(get_setting("max_per_feed") or 4)
    added = 0
    for s in q("SELECT * FROM sources WHERE active=1"):
        feed, status = fetch_feed(s["url"])
        if feed is None:
            log(f"[{s['name']}] FAILED: {status}"); continue
        kws = [k.strip().lower() for k in (s.get("keywords") or "").split(",") if k.strip()]
        new = 0
        for e in feed.entries[:60]:
            if new >= maxn:
                break
            url = e.get("link")
            if not url or q("SELECT 1 FROM articles WHERE url=?", (url,)):
                continue
            blob = f"{e.get('title', '')} {e.get('summary', '')}".lower()
            if kws and not any(k in blob for k in kws):
                continue
            text, og = fetch_page(url)
            text = text or re.sub("<[^>]+>", " ", e.get("summary", ""))
            ex("""INSERT OR IGNORE INTO articles(url,source,category,country,region,orig_title,orig_text,image,published)
                  VALUES(?,?,?,?,?,?,?,?,?)""",
               (url, s["name"], s["category"], s["country"], s["country"], e.get("title", ""), text,
                feed_image(e) or og, e.get("published", e.get("updated", ""))))
            new += 1
            time.sleep(0.3)
        added += new
        log(f"[{s['name']}] +{new} new")
    return added

def apply_summary(a_id, r, status=None):
    ex("""UPDATE articles SET title=?, summary=?, what_changed=?, who_affected=?, action=?, impact=?,
          region=COALESCE(NULLIF(?, ''), region), ai_done=1, status=COALESCE(?, status) WHERE id=?""",
       (r["headline"], r["summary"], r["what_changed"], r["who_affected"], r["action"], r["impact"], r["region"], status, a_id))

def summarise_pending(limit=20, log=print):
    init_db()
    status = "published" if get_setting("auto_publish") == "1" else "pending"
    done = 0
    for a in q("SELECT * FROM articles WHERE ai_done=0 AND status!='archived' ORDER BY id DESC LIMIT ?", (limit,)):
        r, used = ai_summarise(a["orig_title"], a["orig_text"])
        apply_summary(a["id"], r, status)
        done += 1
        log(f"summarised: {r['headline'][:70]}")
        if used:
            time.sleep(4)   # Gemini free-tier pacing
    log(f"Done - {done} articles processed")
    return done

# ============================== SUBSCRIBERS ==============================
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def subscribe(email, role):
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        return "invalid"
    if q("SELECT 1 FROM subscribers WHERE email=?", (email,)):
        return "exists"
    ex("INSERT INTO subscribers(email, role) VALUES(?,?)", (email, role))
    hook = get_setting("signup_webhook")
    if hook:
        try:
            requests.post(hook, json={"email": email, "role": role, "source": "regpulse"},
                          headers={"Accept": "application/json"}, timeout=8)
        except Exception as e:
            print("webhook failed:", e)
    return "ok"

def signup_box(key):
    with st.container(border=True):
        st.markdown("**📬 Get the weekly regulatory digest**")
        st.caption("Top FDA, EU MDR/IVDR, MHRA and CDSCO changes for device & dental professionals. Free.")
        with st.form(f"signup_{key}", clear_on_submit=True):
            email = st.text_input("Email", key=f"em_{key}", placeholder="you@company.com")
            role = st.selectbox("I work in", ROLES, key=f"ro_{key}")
            go = st.form_submit_button("Subscribe")
        if go:
            res = subscribe(email, role)
            if res == "ok":
                st.success("You're subscribed. Thank you!")
            elif res == "exists":
                st.info("This email is already subscribed.")
            else:
                st.error("Please enter a valid email address.")
        if get_setting("signup_url"):
            st.link_button("Or subscribe on our newsletter page ↗", get_setting("signup_url"))

# ============================== PUBLIC PAGE ==============================
CSS = """
<style>
.block-container{padding-top:2rem;max-width:1200px}
h1.brand{font-family:Georgia,serif;font-size:2.4rem;margin:0}
.badge{display:inline-block;color:#fff;border-radius:4px;padding:1px 8px;font-size:.72rem;font-weight:600;letter-spacing:.04em}
.chip{display:inline-block;background:#e5e7eb;color:#111827;border-radius:4px;padding:1px 8px;font-size:.72rem;margin-left:4px}
.meta{color:#6b7280;font-size:.8rem}
.card-title{font-family:Georgia,serif;font-weight:700;font-size:1.08rem;line-height:1.3;margin:.35rem 0}
[data-testid="stImage"] img{border-radius:8px;object-fit:cover}
.disclaimer{color:#6b7280;font-size:.78rem;margin-top:2rem}
</style>"""
DISCLAIMER = ("Summaries are generated with AI from public sources for information only. They are not legal or regulatory "
              "advice. Always verify against the original source before acting.")

def badges(a):
    imp = a.get("impact") or "Medium"
    return (f"<span class='badge' style='background:{IMPACT_COLOR.get(imp, '#6b7280')}'>{html.escape(imp.upper())} IMPACT</span>"
            f"<span class='chip'>{html.escape(a.get('category') or '')}</span>"
            f"<span class='chip'>{html.escape(a.get('region') or a.get('country') or '')}</span>")

@st.cache_data(ttl=120)
def load_articles(category=None, search="", impacts=None, region=None):
    sql, args = "SELECT * FROM articles WHERE status='published'", []
    if category:
        sql += " AND category=?"; args.append(category)
    if region and region != "All":
        sql += " AND region=?"; args.append(region)
    if impacts:
        sql += f" AND impact IN ({','.join('?' * len(impacts))})"; args += list(impacts)
    if search:
        sql += " AND (title LIKE ? OR summary LIKE ? OR what_changed LIKE ?)"; args += [f"%{search}%"] * 3
    return q(sql + " ORDER BY featured DESC, id DESC LIMIT 60", args)

@st.dialog("Regulatory update", width="large")
def reader(a):
    st.markdown(badges(a), unsafe_allow_html=True)
    st.subheader(a["title"])
    st.caption(f"{a['source']} · {a['published'] or a['created_at']}")
    st.markdown("**What changed**"); st.write(a["what_changed"] or a["summary"])
    st.markdown("**Who is affected**"); st.write(a["who_affected"] or "See source.")
    st.markdown("**What to do**"); st.write(a["action"] or "Review the original source.")
    if a.get("expert_note"):
        st.info(f"**{get_setting('editor_label')}:** {a['expert_note']}")
    if a["image"]:
        st.image(a["image"], width="stretch")
    st.link_button("Read the original source ↗", a["url"])
    st.markdown(f"<div class='disclaimer'>{DISCLAIMER}</div>", unsafe_allow_html=True)

def card(a, tab_key):
    with st.container(border=True):
        st.markdown(f"{badges(a)}<div class='card-title'>{html.escape(a['title'] or '')}</div>"
                    f"<span class='meta'>{html.escape(a['source'] or '')}</span>", unsafe_allow_html=True)
        st.caption(a["summary"])
        if a.get("expert_note"):
            st.caption(f"🩺 {a['expert_note'][:140]}")
        if st.button("Details", key=f"r{tab_key}{a['id']}"):
            reader(a)

def news_page():
    st.markdown(f"<h1 class='brand'>🩺 {html.escape(get_setting('site_title'))}</h1>", unsafe_allow_html=True)
    st.caption(get_setting("tagline"))
    with st.sidebar:
        st.header("Filters")
        search = st.text_input("Search")
        impacts = st.multiselect("Impact", IMPACTS, default=IMPACTS)
        region = st.selectbox("Region", ["All"] + REGIONS)
        if st.button("Refresh"):
            st.cache_data.clear()
        st.divider()
        signup_box("side")
    signup_box("top")
    tabs = st.tabs(["Latest"] + CATEGORIES)
    for tab, cat in zip(tabs, [None] + CATEGORIES):
        with tab:
            items = load_articles(cat, search, tuple(impacts), region)
            if not items:
                st.info("No published updates here yet.")
                continue
            for i in range(0, len(items), 3):
                for col, a in zip(st.columns(3), items[i:i + 3]):
                    with col:
                        card(a, cat or "all")
    st.markdown(f"<div class='disclaimer'>{DISCLAIMER}</div>", unsafe_allow_html=True)

# ============================== ADMIN ==============================
def build_digest(days=7):
    rows = q("""SELECT * FROM articles WHERE status='published' AND date(created_at) >= date('now', ?)
                ORDER BY CASE impact WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, id DESC""", (f"-{days} day",))
    out = [f"# {get_setting('site_title')} Weekly - {date.today():%d %b %Y}", "",
           f"{len(rows)} updates this week. Top items first.", ""]
    icon = {"High": "🔴", "Medium": "🟠", "Low": "🟢"}
    last = None
    for a in rows:
        if a["impact"] != last:
            out += [f"## {icon.get(a['impact'], '')} {a['impact']} impact", ""]
            last = a["impact"]
        out += [f"### {a['title']}", f"*{a['category']} · {a['region'] or a['country']} · {a['source']}*", "",
                a["summary"], "",
                f"- **What changed:** {a['what_changed']}", f"- **Who is affected:** {a['who_affected']}",
                f"- **Action:** {a['action']}"]
        if a["expert_note"]:
            out += [f"- **{get_setting('editor_label')}:** {a['expert_note']}"]
        out += [f"- [Source]({a['url']})", ""]
    out += ["---", DISCLAIMER]
    return "\n".join(out)

def save_article(i, f, status):
    ex("""UPDATE articles SET title=?,summary=?,what_changed=?,who_affected=?,action=?,impact=?,region=?,
          category=?,expert_note=?,image=?,featured=?,status=? WHERE id=?""",
       (f["title"], f["summary"], f["what_changed"], f["who_affected"], f["action"], f["impact"], f["region"],
        f["category"], f["expert_note"], f["image"], int(f["featured"]), status, i))
    st.cache_data.clear()

def check_feeds():
    rows = []
    for s in q("SELECT * FROM sources"):
        fd, status = fetch_feed(s["url"])
        rows.append({"source": s["name"], "entries": len(fd.entries) if fd else 0, "status": status})
    return pd.DataFrame(rows)

def admin_page():
    pw = secret("ADMIN_PASSWORD")
    st.title("⚙️ Admin Dashboard")
    if not pw:
        st.error('ADMIN_PASSWORD is not set. Add it in Manage app > Settings > Secrets:\n\nADMIN_PASSWORD = "your-password"')
        return
    if not st.session_state.get("auth"):
        entered = st.text_input("Password", type="password")
        if st.button("Login"):
            if entered == pw:
                st.session_state.auth = True; st.rerun()
            else:
                st.error("Wrong password")
        return
    if st.sidebar.button("Logout"):
        st.session_state.auth = False; st.rerun()

    t_over, t_art, t_src, t_run, t_sub, t_dig, t_set = st.tabs(
        ["Overview", "Articles", "Sources", "Run", "Subscribers", "Weekly digest", "Settings"])

    with t_over:
        counts = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM articles GROUP BY status")}
        c = st.columns(5)
        c[0].metric("Published", counts.get("published", 0))
        c[1].metric("Pending review", counts.get("pending", 0))
        c[2].metric("Rejected", counts.get("rejected", 0))
        c[3].metric("Active sources", q("SELECT COUNT(*) n FROM sources WHERE active=1")[0]["n"])
        c[4].metric("Subscribers", q("SELECT COUNT(*) n FROM subscribers")[0]["n"])
        df = pd.DataFrame(q("SELECT category, COUNT(*) AS articles FROM articles WHERE status!='archived' GROUP BY category"))
        if not df.empty:
            st.bar_chart(df.set_index("category"))

    with t_art:
        f1, f2, f3 = st.columns(3)
        status = f1.selectbox("Status", ["pending", "published", "rejected"])
        cat = f2.selectbox("Category", ["All"] + CATEGORIES)
        limit = f3.slider("Show", 5, 50, 15)
        if status == "pending" and st.button("Publish ALL pending (only after you reviewed them)"):
            ex("UPDATE articles SET status='published' WHERE status='pending' AND ai_done=1")
            st.cache_data.clear(); st.rerun()
        sql, args = "SELECT * FROM articles WHERE status=?", [status]
        if cat != "All":
            sql += " AND category=?"; args.append(cat)
        for a in q(sql + " ORDER BY id DESC LIMIT ?", args + [limit]):
            with st.expander(f"[{a['impact'] or '-'}] {a['title'] or a['orig_title']} - {a['source']}"):
                i = a["id"]
                st.text_area("Source excerpt (read-only)", (a["orig_text"] or "")[:900], height=110, disabled=True, key=f"x{i}")
                f = {
                    "title": st.text_input("Headline", a["title"] or "", key=f"t{i}"),
                    "summary": st.text_area("Summary", a["summary"] or "", height=70, key=f"s{i}"),
                    "what_changed": st.text_area("What changed", a["what_changed"] or "", height=90, key=f"w{i}"),
                    "who_affected": st.text_area("Who is affected", a["who_affected"] or "", height=70, key=f"o{i}"),
                    "action": st.text_area("What to do", a["action"] or "", height=70, key=f"a{i}"),
                    "expert_note": st.text_area("Your expert note (shown to readers)", a["expert_note"] or "", height=90, key=f"n{i}"),
                }
                c1, c2, c3 = st.columns(3)
                f["impact"] = c1.selectbox("Impact", IMPACTS, index=IMPACTS.index(a["impact"]) if a["impact"] in IMPACTS else 1, key=f"im{i}")
                f["region"] = c2.selectbox("Region", REGIONS, index=REGIONS.index(a["region"]) if a["region"] in REGIONS else 5, key=f"rg{i}")
                f["category"] = c3.selectbox("Category", CATEGORIES, index=CATEGORIES.index(a["category"]) if a["category"] in CATEGORIES else 0, key=f"ct{i}")
                f["image"] = st.text_input("Image URL (optional)", a["image"] or "", key=f"im_{i}")
                f["featured"] = st.checkbox("Pin to top", bool(a["featured"]), key=f"ft{i}")
                b = st.columns(5)
                if b[0].button("✅ Publish", key=f"p{i}"):
                    save_article(i, f, "published"); st.rerun()
                if b[1].button("💾 Save", key=f"sv{i}"):
                    save_article(i, f, a["status"]); st.success("Saved")
                if b[2].button("🚫 Reject", key=f"rj{i}"):
                    save_article(i, f, "rejected"); st.rerun()
                if b[3].button("🔁 Re-summarise", key=f"rs{i}"):
                    r, _ = ai_summarise(a["orig_title"], a["orig_text"]); apply_summary(i, r); st.rerun()
                if b[4].button("🗑 Delete", key=f"d{i}"):
                    ex("DELETE FROM articles WHERE id=?", (i,)); st.cache_data.clear(); st.rerun()
                st.markdown(f"[Original source]({a['url']})")

    with t_src:
        st.caption("Keywords (comma-separated) keep only entries whose title/summary contain one of them. Leave empty to take everything.")
        src = pd.DataFrame(q("SELECT id, name, url, category, country, keywords, active FROM sources"))
        edited = st.data_editor(
            src, num_rows="dynamic", width="stretch", hide_index=True,
            column_config={"id": st.column_config.NumberColumn(disabled=True),
                           "category": st.column_config.SelectboxColumn(options=CATEGORIES),
                           "country": st.column_config.SelectboxColumn("region", options=REGIONS),
                           "active": st.column_config.CheckboxColumn()})
        c1, c2 = st.columns(2)
        if c1.button("Save sources"):
            keep = set()
            for _, r in edited.iterrows():
                if not r["url"]:
                    continue
                act = int(bool(r["active"])) if pd.notna(r["active"]) else 1
                kw = r["keywords"] if pd.notna(r["keywords"]) else ""
                if pd.notna(r["id"]):
                    ex("UPDATE sources SET name=?,url=?,category=?,country=?,keywords=?,active=? WHERE id=?",
                       (r["name"], r["url"], r["category"], r["country"], kw, act, int(r["id"]))); keep.add(int(r["id"]))
                else:
                    keep.add(ex("INSERT OR IGNORE INTO sources(name,url,category,country,keywords,active) VALUES(?,?,?,?,?,?)",
                                (r["name"], r["url"], r["category"] or "Industry", r["country"] or "Global", kw, act)))
            for old in src["id"].tolist():
                if old not in keep:
                    ex("DELETE FROM sources WHERE id=?", (old,))
            st.success("Saved"); st.rerun()
        if c2.button("🔍 Check all feeds"):
            with st.spinner("Testing feeds..."):
                st.dataframe(check_feeds(), width="stretch", hide_index=True)

    with t_run:
        st.write("Step 1: scrape the sources. Step 2: summarise new articles with Gemini. Step 3: review them in the Articles tab.")
        if not secret("GEMINI_API_KEY"):
            st.warning("GEMINI_API_KEY not set: you will get basic extractive summaries instead of AI summaries.")
        c1, c2 = st.columns(2)
        box = st.empty(); logs = []
        def log(m):
            logs.append(str(m)); box.code("\n".join(logs[-25:]))
        if c1.button("▶ Scrape now", type="primary"):
            with st.spinner("Scraping (can take a few minutes)..."):
                log(f"Added {scrape_all(log)} new articles")
        if c2.button("✍️ Summarise pending (AI)", type="primary"):
            with st.spinner("Summarising with Gemini..."):
                summarise_pending(20, log)
            st.cache_data.clear()
        waiting = q("SELECT COUNT(*) n FROM articles WHERE ai_done=0 AND status!='archived'")[0]["n"]
        st.info(f"{waiting} scraped articles are waiting for summarising.")

    with t_sub:
        subs = pd.DataFrame(q("SELECT id, email, role, created_at FROM subscribers ORDER BY id DESC"))
        st.metric("Total subscribers", len(subs))
        st.warning("Subscribers are stored in this app's local file, which Streamlit Cloud resets on restart. "
                   "Download the CSV regularly, or set a Signup webhook in Settings so every signup is also sent to "
                   "a permanent place (Google Sheet, Formspree, Buttondown).")
        if not subs.empty:
            st.dataframe(subs, width="stretch", hide_index=True)
            st.download_button("⬇ Download CSV", subs.to_csv(index=False), "subscribers.csv", "text/csv")
            rm = st.selectbox("Remove subscriber", [""] + subs["email"].tolist())
            if rm and st.button("Remove"):
                ex("DELETE FROM subscribers WHERE email=?", (rm,)); st.rerun()

    with t_dig:
        days = st.slider("Days to include", 3, 14, 7)
        md = build_digest(days)
        st.caption("Copy this into Substack / Beehiiv / Buttondown, or download it. Review and add your own intro first.")
        st.text_area("Digest (Markdown)", md, height=420)
        st.download_button("⬇ Download digest.md", md, f"regpulse-digest-{date.today():%Y%m%d}.md", "text/markdown")

    with t_set:
        s = {
            "site_title": st.text_input("Site title", get_setting("site_title")),
            "tagline": st.text_input("Tagline", get_setting("tagline")),
            "editor_label": st.text_input("Label for your expert notes", get_setting("editor_label")),
            "signup_url": st.text_input("External newsletter page URL (optional)", get_setting("signup_url")),
            "signup_webhook": st.text_input("Signup webhook URL (optional, receives each new subscriber as JSON)", get_setting("signup_webhook")),
            "model": st.text_input("Gemini model", get_setting("model")),
        }
        auto = st.toggle("Auto-publish AI summaries (not recommended for regulatory content)", get_setting("auto_publish") == "1")
        ai = st.toggle("Use AI summaries", get_setting("ai_enabled") == "1")
        maxn = st.number_input("Max new articles per source per run", 1, 20, int(get_setting("max_per_feed")))
        if st.button("Save settings"):
            for k, v in {**s, "auto_publish": int(auto), "ai_enabled": int(ai), "max_per_feed": maxn}.items():
                set_setting(k, v)
            st.cache_data.clear(); st.success("Saved")

# ============================== ENTRY POINT ==============================
def main():
    init_db()
    st.set_page_config(page_title=get_setting("site_title"), page_icon="🩺", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    page = st.sidebar.radio("Navigate", ["📰 Updates", "⚙️ Admin"], label_visibility="collapsed")
    st.sidebar.divider()
    news_page() if page == "📰 Updates" else admin_page()

if "--scrape" in sys.argv:
    init_db(); scrape_all(); summarise_pending(30)
else:
    main()
