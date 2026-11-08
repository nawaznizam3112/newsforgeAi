import json, os, re
import db

CATS = "World, India, Business, Technology, Health, Science, Sports, Entertainment"

SYSTEM = f"""You are a news editor. Rewrite the given article in your own original wording.
Rules: stay strictly factual, never add facts that are not in the source, neutral tone,
no clickbait. Return ONLY JSON with keys:
"title" (max 90 chars), "summary" (1-2 sentences, max 220 chars),
"body" (3-5 short paragraphs, 150-250 words, separated by blank lines).
Valid categories for context: {CATS}."""

def get_key():
    key = os.getenv("GEMINI_API_KEY")
    if key:
        return key
    try:
        import streamlit as st
        return st.secrets["GEMINI_API_KEY"]
    except Exception:
        return None

def _fallback(title, text):
    text = (text or "").strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    summary = " ".join(sentences[:2])[:260]
    body = "\n\n".join(" ".join(sentences[i:i + 2]) for i in range(0, min(len(sentences), 8), 2))
    return {"title": title, "summary": summary or title, "body": body or summary or title}

def rewrite(title, text):
    """Returns dict(title, summary, body). Falls back to extractive summary if AI is off/unavailable."""
    key = get_key()
    if db.get_setting("ai_enabled") != "1" or not key or not text:
        return _fallback(title, text)
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model=db.get_setting("model"),
            contents=f"TITLE: {title}\n\nARTICLE:\n{text[:6000]}",
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM,
                response_mime_type="application/json",
                max_output_tokens=1200,
                temperature=0.4,
            ),
        )
        raw = re.sub(r"^```(?:json)?|```$", "", (resp.text or "").strip(), flags=re.M).strip()
        data = json.loads(raw)
        return {k: data[k] for k in ("title", "summary", "body")}
    except Exception as e:
        print("Gemini rewrite failed:", e)
        return _fallback(title, text)
