import streamlit as st
import db
from config import CATEGORIES

db.init()
TITLE = db.get_setting("site_title")
st.set_page_config(page_title=TITLE, page_icon="📰", layout="wide")

st.markdown("""
<style>
.block-container{padding-top:2rem;max-width:1250px}
h1.brand{font-family:Georgia,serif;font-size:2.6rem;margin:0}
.tag{display:inline-block;background:#b91c1c;color:#fff;border-radius:4px;padding:1px 8px;font-size:.72rem;letter-spacing:.04em}
.meta{color:#6b7280;font-size:.8rem}
.card-title{font-family:Georgia,serif;font-weight:700;font-size:1.1rem;line-height:1.3;margin:.3rem 0}
[data-testid="stImage"] img{border-radius:10px;object-fit:cover}
</style>""", unsafe_allow_html=True)

PLACEHOLDER = "https://placehold.co/800x450/1f2937/ffffff?text=News"

@st.cache_data(ttl=120)
def load(category=None, search="", country=None):
    sql, args = "SELECT * FROM articles WHERE status='published'", []
    if category:
        sql += " AND category=?"; args.append(category)
    if country:
        sql += " AND country=?"; args.append(country)
    if search:
        sql += " AND (title LIKE ? OR summary LIKE ?)"; args += [f"%{search}%"] * 2
    return db.q(sql + " ORDER BY featured DESC, id DESC LIMIT 60", args)

@st.dialog("Article", width="large")
def reader(a):
    st.markdown(f"<span class='tag'>{a['category'].upper()}</span>", unsafe_allow_html=True)
    st.subheader(a["title"])
    if a["image"]:
        st.image(a["image"], use_container_width=True)
    st.caption(f"{a['source']} · {a['country']} · {a['published'] or a['created_at']}")
    st.write(a["body"])
    st.link_button("Read original source ↗", a["url"])

def card(a):
    with st.container(border=True):
        st.image(a["image"] or PLACEHOLDER, use_container_width=True)
        st.markdown(f"<span class='tag'>{a['category'].upper()}</span> <span class='meta'>{a['source']}</span>"
                    f"<div class='card-title'>{a['title']}</div>", unsafe_allow_html=True)
        st.caption(a["summary"])
        if st.button("Read more", key=f"r{a['id']}"):
            reader(a)

with st.sidebar:
    st.header("Filters")
    search = st.text_input("Search")
    countries = [r["country"] for r in db.q("SELECT DISTINCT country FROM articles WHERE status='published' ORDER BY country")]
    country = st.selectbox("Country / source region", ["All"] + countries)
    if st.button("Refresh"):
        st.cache_data.clear()

st.markdown(f"<h1 class='brand'>📰 {TITLE}</h1>", unsafe_allow_html=True)
st.caption("AI-summarised global news with links to original sources.")

tabs = st.tabs(["Top"] + CATEGORIES)
for tab, cat in zip(tabs, [None] + CATEGORIES):
    with tab:
        items = load(cat, search, None if country == "All" else country)
        if not items:
            st.info("No articles yet. Run the scraper from the Admin page.")
            continue
        hero, rest = items[0], items[1:]
        c1, c2 = st.columns([3, 2])
        with c1:
            st.image(hero["image"] or PLACEHOLDER, use_container_width=True)
        with c2:
            st.markdown(f"<span class='tag'>{hero['category'].upper()}</span> <span class='meta'>{hero['source']}</span>"
                        f"<h2 style='font-family:Georgia,serif'>{hero['title']}</h2>", unsafe_allow_html=True)
            st.write(hero["summary"])
            if st.button("Read full story", key=f"h{cat}{hero['id']}"):
                reader(hero)
        st.divider()
        for i in range(0, len(rest), 3):
            cols = st.columns(3)
            for col, a in zip(cols, rest[i:i + 3]):
                with col:
                    card(a)
