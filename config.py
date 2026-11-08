CATEGORIES = ["World", "India", "Business", "Technology", "Health", "Science", "Sports", "Entertainment"]

# (name, rss_url, category, country)
DEFAULT_SOURCES = [
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
    "auto_publish": "1",      # 1 = AI-rewritten articles go live automatically
    "ai_enabled": "1",        # 0 = skip AI, use extractive summary
    "max_per_feed": "8",      # new articles per source per run
    "model": "gemini-2.5-flash",
}
