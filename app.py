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
from urllib.parse import quote

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

def signup_form(key):
    with st.form(f"signup_{key}", clear_on_submit=True):
        email = st.text_input("Email", key=f"em_{key}", placeholder="you@company.com")
        role = st.selectbox("I work in", ROLES, key=f"ro_{key}")
        go = st.form_submit_button("Subscribe to the digest")
    if go:
        res = subscribe(email, role)
        if res == "ok":
            st.success("You're subscribed. Thank you!")
        elif res == "exists":
            st.info("This email is already subscribed.")
        else:
            st.error("Please enter a valid email address.")
    if get_setting("signup_url"):
        st.link_button("Or join on our newsletter page ↗", get_setting("signup_url"))

# ============================== PUBLIC PAGE (luxury theme) ==============================
LUX_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@500;600;700&family=Inter:wght@400;500;600&display=swap');
:root{--bg:#0a0f1c;--panel:#111a2e;--panel2:#16213a;--gold:#c9a24b;--gold2:#e8d08a;--ivory:#f3ede0;--muted:#9aa4b8;--line:rgba(201,162,75,.28)}
.stApp{background:radial-gradient(1100px 520px at 85% -8%,#1c2c4d 0%,transparent 62%),radial-gradient(900px 480px at -10% 30%,#14203a 0%,transparent 60%),var(--bg);font-family:'Inter',sans-serif}
[data-testid="stHeader"]{background:transparent}
.block-container{padding-top:1.2rem;max-width:1240px}
.stApp p,.stApp li,.stApp label,.stApp h1,.stApp h2,.stApp h3,.stApp h4,[data-testid="stMarkdownContainer"] p{color:var(--ivory)!important}
[data-testid="stCaptionContainer"],[data-testid="stCaptionContainer"] p{color:var(--muted)!important}
[data-testid="stSidebar"]{background:#0c1322;border-right:1px solid var(--line)}
[data-testid="stSidebar"] h2{font-family:'Playfair Display',serif;color:var(--gold2)!important}
/* inputs */
.stApp input,.stApp textarea,.stApp [data-baseweb="select"]>div,.stApp [data-baseweb="input"]{background:var(--panel2)!important;color:var(--ivory)!important;border-color:var(--line)!important}
.stApp [data-baseweb="select"] *{color:var(--ivory)!important}
[data-baseweb="popover"] ul,[data-baseweb="popover"] li{background:var(--panel2)!important;color:var(--ivory)!important}
[data-baseweb="tag"]{background:var(--gold)!important}
[data-baseweb="tag"] *{color:#0a0f1c!important}
/* buttons */
.stApp .stButton>button,.stApp .stLinkButton a{background:transparent;border:1px solid var(--gold);color:var(--gold2)!important;border-radius:999px;font-weight:500;letter-spacing:.03em;transition:all .2s}
.stApp .stButton>button:hover,.stApp .stLinkButton a:hover{background:var(--gold);color:#0a0f1c!important;border-color:var(--gold)}
.stApp .stButton>button p{color:inherit!important}
.stApp [data-testid="stFormSubmitButton"] button{background:linear-gradient(135deg,#e8d08a,#c9a24b);color:#0a0f1c!important;border:none;font-weight:600}
.stApp [data-testid="stFormSubmitButton"] button p{color:#0a0f1c!important}
[data-testid="stForm"]{background:rgba(17,26,46,.7);border:1px solid var(--line);border-radius:14px}
/* tabs */
.stTabs [data-baseweb="tab-list"]{gap:4px;border-bottom:1px solid var(--line)}
.stTabs [data-baseweb="tab"]{color:var(--muted)!important;font-weight:500;padding:.6rem 1rem}
.stTabs [aria-selected="true"]{color:var(--gold2)!important}
.stTabs [data-baseweb="tab-highlight"]{background:var(--gold)!important;height:2px}
/* hero */
.hero{position:relative;border:1px solid var(--line);border-radius:20px;padding:3rem 2.6rem 2.2rem;margin:.4rem 0 1.6rem;overflow:hidden;
 background:linear-gradient(120deg,rgba(15,27,52,.95),rgba(9,14,28,.92)),url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='56' height='56'%3E%3Cpath d='M0 28h56M28 0v56' stroke='%23c9a24b' stroke-opacity='.08'/%3E%3C/svg%3E");box-shadow:0 20px 60px rgba(0,0,0,.45)}
.hero:before{content:"";position:absolute;right:-90px;top:-90px;width:380px;height:380px;border-radius:50%;border:1px solid rgba(201,162,75,.35);box-shadow:0 0 0 38px rgba(201,162,75,.05),0 0 0 80px rgba(201,162,75,.035)}
.eyebrow{color:var(--gold);font-size:.72rem;letter-spacing:.28em;font-weight:600;text-transform:uppercase}
.hero-title{font-family:'Playfair Display',serif;font-size:4.2rem;line-height:1.05;font-weight:700;margin:.5rem 0 .6rem;background:linear-gradient(90deg,#f7f1e3,#e8d08a 70%);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero-tag{color:#c4ccdc;font-size:1.12rem;max-width:640px;line-height:1.55}
.stats{display:flex;gap:2.6rem;margin-top:1.8rem;flex-wrap:wrap}
.stat-n{font-family:'Playfair Display',serif;font-size:2rem;color:var(--gold2)}
.stat-l{color:var(--muted);font-size:.74rem;letter-spacing:.16em;text-transform:uppercase}
.sec-title{font-family:'Playfair Display',serif;font-size:1.7rem;color:var(--ivory);margin:.2rem 0 .1rem}
/* visual panel */
.vis{position:relative;display:flex;align-items:flex-end;justify-content:space-between;padding:.8rem 1rem}
.vis-icon{font-size:2.6rem;filter:drop-shadow(0 4px 10px rgba(0,0,0,.5));opacity:.95}
.vis-cat{color:#f3ede0;font-size:.68rem;letter-spacing:.2em;text-transform:uppercase;background:rgba(8,12,24,.55);border:1px solid var(--line);padding:3px 10px;border-radius:999px}
/* cards */
[class*="st-key-card_"]{background:linear-gradient(180deg,var(--panel),#0e1628);border:1px solid var(--line);border-radius:16px;overflow:hidden;gap:.3rem;padding-bottom:1rem;transition:transform .25s,box-shadow .25s,border-color .25s}
[class*="st-key-card_"]:hover{transform:translateY(-4px);box-shadow:0 18px 40px rgba(0,0,0,.5);border-color:var(--gold)}
[class*="st-key-card_"] .stButton{padding:0 1.1rem}
.rc-body{padding:.9rem 1.1rem .2rem}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:.66rem;letter-spacing:.14em;font-weight:600;text-transform:uppercase}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.rc-region{color:var(--muted);font-size:.72rem;float:right;letter-spacing:.1em;text-transform:uppercase}
.rc-title{font-family:'Playfair Display',serif;font-weight:600;font-size:1.22rem;line-height:1.28;margin:.55rem 0 .45rem;color:var(--ivory)}
.rc-sum{color:#b9c2d3;font-size:.9rem;line-height:1.55}
.rc-src{color:var(--muted);font-size:.74rem;margin-top:.7rem}
[class*="st-key-lead_"]{background:linear-gradient(120deg,#13203a,#0d1526);border:1px solid var(--line);border-left:3px solid var(--gold);border-radius:18px;overflow:hidden;padding:0;margin-bottom:1.4rem;box-shadow:0 16px 50px rgba(0,0,0,.4)}
.lead-body{padding:1.6rem 1.4rem .2rem}
.lead-title{font-family:'Playfair Display',serif;font-weight:700;font-size:2.05rem;line-height:1.18;margin:.7rem 0;color:var(--ivory)}
.lead-act{color:#d9cfa8;font-size:.92rem;border-top:1px solid var(--line);margin-top:.9rem;padding-top:.8rem}
.empty{border:1px dashed var(--line);border-radius:16px;padding:2.4rem;text-align:center;color:var(--muted);margin:1rem 0}
/* newsletter band */
[class*="st-key-band"]{background:linear-gradient(120deg,#16233f,#0c1426);border:1px solid var(--line);border-radius:20px;padding:2rem 2rem 1.2rem;margin:2.2rem 0 1rem;box-shadow:0 20px 60px rgba(0,0,0,.4)}
.band-title{font-family:'Playfair Display',serif;font-size:2rem;line-height:1.2;color:var(--ivory);margin:.5rem 0 .9rem}
.band-li{color:#c4ccdc;font-size:.95rem;margin:.4rem 0}
.band-li b{color:var(--gold2)}
.disc{color:var(--muted);font-size:.74rem;line-height:1.5;text-align:center;margin:1.6rem auto 2rem;max-width:760px}
/* dialog */
div[role="dialog"]{background:#0e1626!important;border:1px solid var(--line);border-radius:18px}
div[role="dialog"] :where(h1,h2,h3,p,span,div,label){color:#f3ede0}
.dlg-title{font-family:'Playfair Display',serif;font-weight:700;font-size:1.9rem;line-height:1.2;margin:.7rem 0 .3rem}
.dlg-meta{color:var(--muted)!important;font-size:.8rem;margin-bottom:.9rem}
.dlg-h{color:var(--gold)!important;font-size:.7rem;letter-spacing:.22em;font-weight:600;margin:1.1rem 0 .25rem}
.dlg-t{color:#dfe4ee!important;font-size:.97rem;line-height:1.65}
.dlg-note{border-left:3px solid var(--gold);background:rgba(201,162,75,.09);padding:.8rem 1rem;border-radius:0 10px 10px 0;margin-top:1.2rem}
.gold-link{display:inline-block;margin-top:1.3rem;padding:.55rem 1.3rem;border:1px solid var(--gold);border-radius:999px;color:var(--gold2)!important;text-decoration:none!important;font-weight:500}
.gold-link:hover{background:var(--gold);color:#0a0f1c!important}
</style>"""

DISCLAIMER = ("Summaries are generated with AI from public sources for information only. They are not legal or regulatory "
              "advice. Always verify against the original source before acting.")
IMPACT_DOT = {"High": "#ef5350", "Medium": "#e6a23c", "Low": "#43b78d"}
CAT_STYLE = {   # emoji, gradient start, gradient end
    "US FDA": ("🛡️", "#1b4175", "#0a1b38"),
    "EU MDR/IVDR": ("🇪🇺", "#26399a", "#0f1745"),
    "UK MHRA": ("🏛️", "#6b2236", "#260c15"),
    "India CDSCO": ("🧭", "#8a5513", "#2a1905"),
    "Global & Standards": ("🌐", "#11585a", "#06262a"),
    "Dental": ("🦷", "#4a3480", "#171034"),
    "Industry": ("📈", "#34415a", "#10172a"),
}

def esc(t):
    return html.escape(str(t or "")).replace("\n", "<br>")

def safe_url(u):
    u = (u or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return None
    return quote(u, safe=":/?&=%#@+,;~!*$-_.")

def pill(a):
    imp = a.get("impact") or "Medium"
    col = IMPACT_DOT.get(imp, "#9aa4b8")
    return f"<span class='pill' style='color:{col}'><span class='dot' style='background:{col}'></span>{esc(imp)} impact</span>"

def visual(a, height):
    icon, c1, c2 = CAT_STYLE.get(a.get("category"), ("🩺", "#1f2a44", "#0e1424"))
    layers = []
    img = safe_url(a.get("image"))
    if img:
        layers += ["linear-gradient(180deg,rgba(8,12,24,.10),rgba(8,12,24,.82))", f"url('{img}')"]
    layers.append(f"linear-gradient(135deg,{c1},{c2})")
    return (f"<div class='vis' style=\"height:{height}px;background:{','.join(layers)};background-size:cover;background-position:center\">"
            f"<span class='vis-icon'>{icon}</span><span class='vis-cat'>{esc(a.get('category'))}</span></div>")

@st.cache_data(ttl=120)
def site_stats():
    r = q("SELECT COUNT(*) n, COALESCE(SUM(impact='High'),0) h, COUNT(DISTINCT NULLIF(region,'')) rg "
          "FROM articles WHERE status='published'")[0]
    wk = q("SELECT COUNT(*) n FROM articles WHERE status='published' AND date(created_at)>=date('now','-7 day')")[0]["n"]
    return r["n"], r["h"], r["rg"], wk

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

def hero_html():
    total, high, regions, week = site_stats()
    stats = "".join(f"<div><div class='stat-n'>{n}</div><div class='stat-l'>{l}</div></div>"
                    for n, l in [(total, "Briefings"), (week, "This week"), (high, "High impact"), (regions, "Regions")])
    return (f"<div class='hero'><div class='eyebrow'>Medical devices &nbsp;·&nbsp; Dental &nbsp;·&nbsp; Regulatory intelligence</div>"
            f"<div class='hero-title'>{esc(get_setting('site_title'))}</div>"
            f"<div class='hero-tag'>{esc(get_setting('tagline'))}</div><div class='stats'>{stats}</div></div>")

@st.dialog("Regulatory briefing", width="large")
def reader(a):
    secs = [("WHAT CHANGED", a["what_changed"] or a["summary"]), ("WHO IS AFFECTED", a["who_affected"] or "See source."),
            ("WHAT TO DO", a["action"] or "Review the original source.")]
    body = "".join(f"<div class='dlg-h'>{h}</div><div class='dlg-t'>{esc(t)}</div>" for h, t in secs)
    note = (f"<div class='dlg-note'><div class='dlg-h' style='margin-top:0'>{esc(get_setting('editor_label')).upper()}</div>"
            f"<div class='dlg-t'>{esc(a['expert_note'])}</div></div>") if a.get("expert_note") else ""
    src = safe_url(a["url"]) or "#"
    st.markdown(f"<div>{pill(a)}<div class='dlg-title'>{esc(a['title'])}</div>"
                f"<div class='dlg-meta'>{esc(a['source'])} · {esc(a['region'] or a['country'])} · {esc((a['created_at'] or '')[:10])}</div>"
                f"{visual(a, 150)}{body}{note}"
                f"<a class='gold-link' href='{src}' target='_blank' rel='noopener'>Read the original source →</a>"
                f"<div class='disc' style='text-align:left;margin:1.4rem 0 0'>{esc(DISCLAIMER)}</div></div>", unsafe_allow_html=True)

def card(a, tab_key):
    with st.container(key=f"card_{tab_key}_{a['id']}"):
        st.markdown(f"{visual(a, 120)}<div class='rc-body'>{pill(a)}<span class='rc-region'>{esc(a['region'] or a['country'])}</span>"
                    f"<div class='rc-title'>{esc(a['title'])}</div><div class='rc-sum'>{esc(a['summary'])}</div>"
                    f"<div class='rc-src'>{esc(a['source'])} · {esc((a['created_at'] or '')[:10])}</div></div>", unsafe_allow_html=True)
        if st.button("Read briefing", key=f"b{tab_key}{a['id']}"):
            reader(a)

def lead_card(a, tab_key):
    with st.container(key=f"lead_{tab_key}"):
        c1, c2 = st.columns([5, 6], gap="small")
        with c1:
            st.markdown(visual(a, 330), unsafe_allow_html=True)
        with c2:
            st.markdown(f"<div class='lead-body'><div class='eyebrow'>Lead briefing</div>{pill(a)}"
                        f"<div class='lead-title'>{esc(a['title'])}</div><div class='rc-sum'>{esc(a['summary'])}</div>"
                        f"<div class='lead-act'><b>What to do:</b> {esc(a['action'] or 'Review the original source.')}</div>"
                        f"<div class='rc-src'>{esc(a['source'])} · {esc(a['region'] or a['country'])}</div></div>", unsafe_allow_html=True)
            if st.button("Read the full briefing →", key=f"lb{tab_key}{a['id']}"):
                reader(a)

def render_feed(items, tab_key):
    if not items:
        st.markdown("<div class='empty'>No published briefings here yet.</div>", unsafe_allow_html=True)
        return
    lead = next((a for a in items if a["featured"]), next((a for a in items if a["impact"] == "High"), items[0]))
    lead_card(lead, tab_key)
    rest = [a for a in items if a["id"] != lead["id"]]
    for i in range(0, len(rest), 3):
        for col, a in zip(st.columns(3, gap="medium"), rest[i:i + 3]):
            with col:
                card(a, tab_key)

def signup_band():
    with st.container(key="band"):
        c1, c2 = st.columns([5, 4], gap="large")
        with c1:
            st.markdown("<div class='eyebrow'>The weekly digest</div><div class='band-title'>Regulatory change, distilled.</div>"
                        "<div class='band-li'><b>◆</b>&nbsp; FDA, EU MDR/IVDR, MHRA and CDSCO in one email</div>"
                        "<div class='band-li'><b>◆</b>&nbsp; Every update impact-rated, with the action to take</div>"
                        "<div class='band-li'><b>◆</b>&nbsp; Free, written for device &amp; dental professionals</div>", unsafe_allow_html=True)
        with c2:
            signup_form("band")

def news_page():
    st.markdown(LUX_CSS, unsafe_allow_html=True)
    st.markdown(hero_html(), unsafe_allow_html=True)
    with st.sidebar:
        st.header("Refine")
        search = st.text_input("Search")
        impacts = st.multiselect("Impact", IMPACTS, default=IMPACTS)
        region = st.selectbox("Region", ["All"] + REGIONS)
        if st.button("Refresh"):
            st.cache_data.clear()
        st.divider()
        st.markdown("**📬 Weekly digest**")
        signup_form("side")
    st.markdown("<div class='eyebrow'>The briefing room</div><div class='sec-title'>Latest regulatory intelligence</div>", unsafe_allow_html=True)
    tabs = st.tabs(["Latest"] + CATEGORIES)
    for tab, cat in zip(tabs, [None] + CATEGORIES):
        with tab:
            render_feed(load_articles(cat, search, tuple(impacts), region), cat or "all")
    signup_band()
    st.markdown(f"<div class='disc'>{esc(DISCLAIMER)}</div>", unsafe_allow_html=True)


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
    page = st.sidebar.radio("Navigate", ["📰 Updates", "⚙️ Admin"], label_visibility="collapsed")
    st.sidebar.divider()
    news_page() if page == "📰 Updates" else admin_page()

if "--scrape" in sys.argv:
    init_db(); scrape_all(); summarise_pending(30)
else:
    main()
