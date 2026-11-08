import json, os, re
import db

CATS = "World, India, Business, Technology, Health, Science, Sports, Entertainment"

SYSTEM = f"""You are a news editor. Rewrite the given article in your own original wording.
Rules: stay strictly factual, never add facts that are not in the source, neutral tone,
no clickbait. Return ONLY JSON with keys:
"title" (max 90 chars), "summary" (1-2 sentences, max 220 chars),
"body" (3-5 short paragraphs, 150-250 words, separated by blank lines).
Valid categories for context: {CATS}."""

def _fallback(title, text):
    text = (text or "").strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    summary = " ".join(sentences[:2])[:260]
    body = "\n\n".join(" ".join(sentences[i:i + 2]) for i in range(0, min(len(sentences), 8), 2))
    return {"title": title, "summary": summary or title, "body": body or summary or title}

def rewrite(title, text):
    """Returns dict(title, summary, body). Falls back to extractive summary if AI is off/unavailable."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if db.get_setting("ai_enabled") != "1" or not key or not text:
        return _fallback(title, text)
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=db.get_setting("model"), max_tokens=900, system=SYSTEM,
            messages=[{"role": "user", "content": f"TITLE: {title}\n\nARTICLE:\n{text[:6000]}"}],
        )
        raw = "".join(b.text for b in msg.content if b.type == "text")
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        data = json.loads(raw)
        return {k: data[k] for k in ("title", "summary", "body")}
    except Exception as e:
        print("AI rewrite failed:", e)
        return _fallback(title, text)
