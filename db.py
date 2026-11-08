import os, sqlite3
from contextlib import contextmanager
from config import DEFAULT_SOURCES, DEFAULT_SETTINGS

DB_PATH = os.getenv("NEWS_DB", "news.db")

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

def init():
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
