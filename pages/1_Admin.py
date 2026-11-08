import os
import pandas as pd
import streamlit as st
import db
from config import CATEGORIES
from scraper import scrape_all, rewrite_pending
from ai_rewriter import get_key

db.init()
st.set_page_config(page_title="Admin", page_icon="⚙️", layout="wide")

def admin_password():
    try:
        return st.secrets["ADMIN_PASSWORD"]
    except Exception:
        return os.getenv("ADMIN_PASSWORD")

pw = admin_password()
if not pw:
    st.error("Set ADMIN_PASSWORD in Streamlit secrets or environment variables.")
    st.stop()
if not st.session_state.get("auth"):
    st.title("Admin login")
    entered = st.text_input("Password", type="password")
    if st.button("Login"):
        if entered == pw:
            st.session_state.auth = True
            st.rerun()
        else:
            st.error("Wrong password")
    st.stop()

st.title("⚙️ Admin Dashboard")
if st.sidebar.button("Logout"):
    st.session_state.auth = False
    st.rerun()

t_over, t_art, t_src, t_run, t_set = st.tabs(["Overview", "Articles", "Sources", "Run", "Settings"])

with t_over:
    rows = db.q("SELECT status, COUNT(*) n FROM articles GROUP BY status")
    counts = {r["status"]: r["n"] for r in rows}
    c = st.columns(4)
    c[0].metric("Published", counts.get("published", 0))
    c[1].metric("Pending review", counts.get("pending", 0))
    c[2].metric("Rejected", counts.get("rejected", 0))
    c[3].metric("Active sources", db.q("SELECT COUNT(*) n FROM sources WHERE active=1")[0]["n"])
    df = pd.DataFrame(db.q("SELECT category, COUNT(*) AS articles FROM articles GROUP BY category"))
    if not df.empty:
        st.bar_chart(df.set_index("category"))
    st.subheader("Articles per source")
    st.dataframe(pd.DataFrame(db.q("SELECT source, COUNT(*) AS articles FROM articles GROUP BY source ORDER BY 2 DESC")),
                 use_container_width=True, hide_index=True)

with t_art:
    f1, f2, f3 = st.columns(3)
    status = f1.selectbox("Status", ["pending", "published", "rejected"])
    cat = f2.selectbox("Category", ["All"] + CATEGORIES)
    limit = f3.slider("Show", 5, 50, 15)
    sql, args = "SELECT * FROM articles WHERE status=?", [status]
    if cat != "All":
        sql += " AND category=?"; args.append(cat)
    for a in db.q(sql + " ORDER BY id DESC LIMIT ?", args + [limit]):
        with st.expander(f"[{a['category']}] {a['title'] or a['orig_title']} — {a['source']}"):
            if a["image"]:
                st.image(a["image"], width=280)
            title = st.text_input("Title", a["title"] or "", key=f"t{a['id']}")
            summary = st.text_area("Summary", a["summary"] or "", key=f"s{a['id']}", height=70)
            body = st.text_area("Body", a["body"] or "", key=f"b{a['id']}", height=220)
            image = st.text_input("Image URL", a["image"] or "", key=f"i{a['id']}")
            newcat = st.selectbox("Category", CATEGORIES, index=CATEGORIES.index(a["category"]) if a["category"] in CATEGORIES else 0, key=f"c{a['id']}")
            feat = st.checkbox("Featured (pin to top)", bool(a["featured"]), key=f"f{a['id']}")
            b1, b2, b3, b4 = st.columns(4)
            def save(new_status, a=a, title=title, summary=summary, body=body, image=image, newcat=newcat, feat=feat):
                db.ex("UPDATE articles SET title=?,summary=?,body=?,image=?,category=?,featured=?,status=? WHERE id=?",
                      (title, summary, body, image, newcat, int(feat), new_status, a["id"]))
            if b1.button("✅ Publish", key=f"p{a['id']}"): save("published"); st.rerun()
            if b2.button("💾 Save", key=f"sv{a['id']}"): save(a["status"]); st.success("Saved")
            if b3.button("🚫 Reject", key=f"r{a['id']}"): save("rejected"); st.rerun()
            if b4.button("🗑 Delete", key=f"d{a['id']}"): db.ex("DELETE FROM articles WHERE id=?", (a["id"],)); st.rerun()
            st.markdown(f"[Original source]({a['url']})")

with t_src:
    st.caption("Add RSS feeds, edit categories, toggle sources on/off. Tick the row selector and press Delete to remove.")
    src = pd.DataFrame(db.q("SELECT id, name, url, category, country, active FROM sources"))
    edited = st.data_editor(
        src, num_rows="dynamic", use_container_width=True, hide_index=True,
        column_config={"id": st.column_config.NumberColumn(disabled=True),
                       "category": st.column_config.SelectboxColumn(options=CATEGORIES),
                       "active": st.column_config.CheckboxColumn()})
    if st.button("Save sources"):
        keep = set()
        for _, r in edited.iterrows():
            if not r["url"]:
                continue
            if pd.notna(r["id"]):
                db.ex("UPDATE sources SET name=?,url=?,category=?,country=?,active=? WHERE id=?",
                      (r["name"], r["url"], r["category"], r["country"], int(bool(r["active"])), int(r["id"])))
                keep.add(int(r["id"]))
            else:
                keep.add(db.ex("INSERT OR IGNORE INTO sources(name,url,category,country,active) VALUES(?,?,?,?,?)",
                               (r["name"], r["url"], r["category"], r["country"], int(bool(r["active"])))))
        for old in src["id"].tolist():
            if old not in keep:
                db.ex("DELETE FROM sources WHERE id=?", (old,))
        st.success("Sources saved"); st.rerun()

with t_run:
    st.write("Scrape feeds, then rewrite new articles with AI.")
    if not get_key():
        st.warning("GEMINI_API_KEY not set: articles will get a basic extractive summary instead of an AI rewrite.")
    c1, c2 = st.columns(2)
    box = st.empty(); logs = []
    def log(m):
        logs.append(str(m)); box.code("\n".join(logs[-25:]))
    if c1.button("▶ Scrape now"):
        with st.spinner("Scraping..."):
            log(f"Added {scrape_all(log)} new articles")
    if c2.button("✍️ Rewrite pending (AI)"):
        with st.spinner("Rewriting..."):
            rewrite_pending(25, log)
    st.info("Scheduled scraping runs via GitHub Actions (see .github/workflows/scrape.yml).")

with t_set:
    title = st.text_input("Site title", db.get_setting("site_title"))
    auto = st.toggle("Auto-publish rewritten articles (off = manual review queue)", db.get_setting("auto_publish") == "1")
    ai = st.toggle("Use AI rewriting", db.get_setting("ai_enabled") == "1")
    maxn = st.number_input("Max new articles per source per run", 1, 30, int(db.get_setting("max_per_feed")))
    model = st.text_input("Gemini model", db.get_setting("model"))
    if st.button("Save settings"):
        for k, v in {"site_title": title, "auto_publish": int(auto), "ai_enabled": int(ai),
                     "max_per_feed": maxn, "model": model}.items():
            db.set_setting(k, v)
        st.success("Saved")
