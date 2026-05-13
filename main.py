import json
import os
import random
import time
import re
from pathlib import Path

import requests
import yt_dlp
from dotenv import load_dotenv
from google import genai

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TARGET_USER = os.getenv("TARGET_USER")

INPUT_JSON = "messages_with_self.json"
OUTPUT_JSONL = "categorized_reels.jsonl"
TEMP_DIR = Path("./temp_media")

AI_MODEL = os.getenv("GEMINI_AI_MODEL")

AI_REQUEST_MIN_INTERVAL_S = float(os.getenv("AI_REQUEST_MIN_INTERVAL_S") or "3.2")
AI_MAX_RETRIES = int(os.getenv("AI_MAX_RETRIES") or "6")

REQUESTS_TIMEOUT_S = float(os.getenv("REQUESTS_TIMEOUT_S") or "25")

MAX_ITEMS = int(os.getenv("MAX_ITEMS") or "15")  # set 0 to disable limit


if not GEMINI_API_KEY:
    print("Error: missing GEMINI_API_KEY in .env.")
    raise SystemExit(1)

client = genai.Client(api_key=GEMINI_API_KEY)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

_retry_delay_re = re.compile(r"'retryDelay'\s*:\s*'(?P<secs>\d+)s'")

def load_processed_keys(path: str) -> set[str]:
    processed: set[str] = set()
    if not os.path.exists(path):
        return processed

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                key = obj.get("source_key")
                if key:
                    processed.add(str(key))
            except Exception:
                continue

    return processed


def append_jsonl(path: str, obj: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str))
        f.write("\n")


def safe_unlink(path: str | Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def download_image(url: str, save_path: str | Path) -> None:
    r = requests.get(url, timeout=REQUESTS_TIMEOUT_S)
    r.raise_for_status()
    with open(save_path, "wb") as f:
        f.write(r.content)


def extract_retry_delay_seconds(exc: Exception) -> int | None:
    """Best-effort parsing of Gemini retryDelay from exception string."""
    text = str(exc)

    # Common case includes "'retryDelay': '56s'"
    m = _retry_delay_re.search(text)
    if m:
        try:
            return int(m.group("secs"))
        except ValueError:
            return None

    # Fallback: any "...retryDelay...56s..."
    m = re.search(r"retryDelay[^\d]*(\d+)s", text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None

    return None


class AiRateLimiter:
    def __init__(self, min_interval_s: float):
        self._min_interval_s = max(0.0, min_interval_s)
        self._next_allowed_ts = 0.0

    def wait(self) -> None:
        now = time.time()
        if now < self._next_allowed_ts:
            time.sleep(self._next_allowed_ts - now)
        # jitter to avoid sync spikes
        self._next_allowed_ts = time.time() + self._min_interval_s + random.uniform(0.0, 0.4)


ai_limiter = AiRateLimiter(AI_REQUEST_MIN_INTERVAL_S)


def gemini_generate_contents(*, model: str, contents: list) -> str:
    """Generate content with retries and 429 handling."""
    last_exc: Exception | None = None

    for attempt in range(1, AI_MAX_RETRIES + 1):
        try:
            ai_limiter.wait()
            resp = client.models.generate_content(model=model, contents=contents)
            return resp.text
        except Exception as e:
            last_exc = e

            retry_after = extract_retry_delay_seconds(e)
            if retry_after is not None:
                sleep_s = retry_after + random.uniform(0.0, 2.0)
                print(
                    f"Gemini quota hit. Waiting {sleep_s:.1f}s before retry "
                    f"(attempt {attempt}/{AI_MAX_RETRIES})."
                )
                time.sleep(sleep_s)
                continue

            backoff = min(60.0, 1.5 * (2 ** (attempt - 1)) + random.uniform(0.0, 1.5))
            print(f"Gemini call failed: {e}. Retrying in {backoff:.1f}s (attempt {attempt}/{AI_MAX_RETRIES}).")
            time.sleep(backoff)

    assert last_exc is not None
    raise last_exc


def analyze_level_1_thumbnail(caption: str, thumbnail_path: str | Path) -> dict:
    prompt = f"""
    Analyze this video thumbnail and its caption/metadata.
    Caption: "{caption}"
    Respond strictly in JSON format:
        {{
        "format": "talking_head|text_slides|screencast|demo|broll_montage|mixed|unknown",
        "topic": "programming|business|marketing|finance|career|productivity|ai_ml|design|self_development|other",
        "intent": "tutorial|explanation|news|opinion|case_study|motivation|ad|entertainment|other",
        "confidence": 0.0,
        "reason": "short"
        }}
        
    Output only raw JSON, no ```json fences.
    """

    try:
        image_part = client.files.upload(file=str(thumbnail_path))
        try:
            text = gemini_generate_contents(model=AI_MODEL, contents=[prompt, image_part])
        finally:
            try:
                client.files.delete(name=image_part.name)
            except Exception:
                pass

        result_text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(result_text)
    except Exception as e:
        print(f"AI Level 1 error: {e}")
        return {
            "format": "unknown",
            "topic": "other",
            "intent": "other",
            "confidence": 0.0,
            "reason": "AI Error"
        }


def analyze_level_2_audio(audio_path: str | Path) -> dict:
    prompt = """
Listen to this audio from a short video.

Your job:
- infer what the video is about (topic)
- infer the intent (tutorial/explanation/news/etc.)
- write a short summary

Allowed values:
- topic: programming|business|marketing|finance|career|productivity|ai_ml|design|self_development|other
- intent: tutorial|explanation|news|opinion|case_study|motivation|ad|entertainment|other

Return ONLY raw JSON (no markdown, no code fences), exactly with these keys:
{
  "topic": "one_of_allowed_values",
  "intent": "one_of_allowed_values",
  "summary": "1-2 sentences",
  "confidence": 0.0,
  "reason": "short"
}
"""

    try:
        audio_part = client.files.upload(file=str(audio_path))
        try:
            text = gemini_generate_contents(model=AI_MODEL, contents=[prompt, audio_part])
        finally:
            try:
                client.files.delete(name=audio_part.name)
            except Exception:
                pass

        result_text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(result_text)
    except Exception as e:
        print(f"AI Level 2 error: {e}")
        return {
            "topic": "other",
            "intent": "other",
            "summary": "",
            "confidence": 0.0,
            "reason": "AI Error",
        }

def process_reels() -> None:
    processed_keys = load_processed_keys(OUTPUT_JSONL)
    if processed_keys:
        print(f"Resume mode: {len(processed_keys)} items already processed.")

    if not os.path.exists(INPUT_JSON):
        print(f"Error: input file not found: {INPUT_JSON}")
        raise SystemExit(1)

    with open(INPUT_JSON, "r", encoding="utf-8") as f:
        messages = json.load(f)

    print(f"Input: {INPUT_JSON} | Output: {OUTPUT_JSONL} | Model: {AI_MODEL}")

    processed_this_run = 0
    for msg in messages:
        if MAX_ITEMS > 0 and processed_this_run >= MAX_ITEMS:
            print(f"Reached MAX_ITEMS={MAX_ITEMS}. Stopping early.")
            break
        # Prefer normalized fields from fetch_messages.py
        source_key = msg.get("source_key")
        reel_url = msg.get("reel_url") or msg.get("video_url") or msg.get("text", "")
        thumbnail_url = msg.get("thumbnail_url")
        author_username = msg.get("author_username")
        title = msg.get("title")

        # Backward-compatible fallback for older JSONs
        if not thumbnail_url:
            raw_xma = msg.get("raw_xma") or {}
            thumbnail_url = raw_xma.get("preview_url") or raw_xma.get("thumbnail_url")

        if not source_key:
            raw_xma = msg.get("raw_xma") or {}
            fbid = raw_xma.get("preview_media_fbid")
            if fbid:
                source_key = f"fbid:{fbid}"
            elif msg.get("id"):
                source_key = f"msg_id:{msg['id']}"
            else:
                source_key = f"fallback:{hash(json.dumps(msg, ensure_ascii=False, default=str))}"

        if not thumbnail_url:
            continue

        if source_key in processed_keys:
            continue

        print(f"\n--- Processing: {reel_url or source_key} ---")
        processed_this_run += 1

        item_data = msg.copy()
        item_data["source_key"] = source_key
        item_data["thumbnail_url"] = thumbnail_url
        if reel_url:
            item_data["reel_url"] = reel_url
        if author_username:
            item_data["author_username"] = author_username

        caption_bits: list[str] = []
        if author_username:
            caption_bits.append(f"Author: {author_username}")
        if title:
            caption_bits.append(f"Title: {title}")
        caption = " | ".join(caption_bits)

        thumb_path = TEMP_DIR / f"thumb_{source_key.replace(':', '_').replace('/', '_')}.jpg"

        try:
            download_image(thumbnail_url, thumb_path)

            level_1_result = analyze_level_1_thumbnail(caption, thumb_path)

            fmt = level_1_result.get("format")
            topic = level_1_result.get("topic")
            intent = level_1_result.get("intent")
            confidence = level_1_result.get("confidence")

            item_data["format"] = fmt
            item_data["topic"] = topic
            item_data["intent"] = intent
            item_data["confidence"] = confidence
            item_data["ai_reason"] = level_1_result.get("reason")

            # (опційно) для сумісності/зручності:
            item_data["category"] = topic or fmt or "unknown"
            # Level 2 only when we have an IG reel URL
            # Run Level 2 only when it likely contains speech (or we are unsure)
            if fmt in ["unknown", "talking_head", "mixed"] and "instagram.com/reel/" in str(reel_url):
                audio_path = TEMP_DIR / f"audio_{source_key.replace(':', '_').replace('/', '_')}.mp3"

                ydl_opts = {
                    "format": "bestaudio/best",
                    "outtmpl": str(audio_path),
                    "quiet": True,
                    "no_warnings": True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([reel_url])

                level_2_result = analyze_level_2_audio(audio_path)
                safe_unlink(audio_path)

                # Level 2 contract should return JSON like:
                # {"topic":"...", "intent":"...", "summary":"...", "confidence":0.0, "reason":"..."}
                item_data["format"] = "talking_head"
                item_data["topic"] = level_2_result.get("topic", item_data.get("topic"))
                item_data["intent"] = level_2_result.get("intent", item_data.get("intent"))
                item_data["summary"] = level_2_result.get("summary", "")
                item_data["confidence_l2"] = level_2_result.get("confidence")
                item_data["ai_reason_l2"] = level_2_result.get("reason")

            # Optional: keep a single string field for quick filtering/search
            item_data["category"] = item_data.get("topic") or item_data.get("format") or "unknown"

            append_jsonl(OUTPUT_JSONL, item_data)
            processed_keys.add(source_key)

            print(
                f"Result: format={item_data.get('format')} topic={item_data.get('topic')} intent={item_data.get('intent')}"
            )
            time.sleep(random.uniform(1.0, 2.5))

        except Exception as e:
            print(f"Failed to process {reel_url or source_key}: {e}")
            item_data["category"] = "Parsing error"
            item_data["error"] = str(e)
            append_jsonl(OUTPUT_JSONL, item_data)
            processed_keys.add(source_key)

        finally:
            safe_unlink(thumb_path)

    print(f"\nDone. Output saved to {OUTPUT_JSONL}")


if __name__ == "__main__":
    process_reels()