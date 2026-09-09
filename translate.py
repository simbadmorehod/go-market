import os
import httpx

DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
LANGS = ["zh", "ms", "th", "en"]
LANG_NAMES = {"zh": "Chinese", "ms": "Malay", "th": "Thai", "en": "English"}


def translate(text: str, target: str) -> str:
    """Translate `text` into target lang via DeepSeek. Falls back to original
    text if no key is configured or the call fails — buyer path never breaks."""
    text = (text or "").strip()
    if not text or not DEEPSEEK_KEY:
        return text
    try:
        r = httpx.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content":
                        f"Translate the user's text into {LANG_NAMES[target]}. "
                        "Output only the translation. No quotes, no notes."},
                    {"role": "user", "content": text},
                ],
                "temperature": 0,
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return text


def translate_all(text: str) -> dict:
    """Return {lang: translation} for all 4 target langs."""
    return {l: translate(text, l) for l in LANGS}
