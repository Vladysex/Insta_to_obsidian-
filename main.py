import json
import os
import random
import time
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

if not GEMINI_API_KEY:
    print("Error: missing GEMINI_API_KEY in .env.")
    raise SystemExit(1)

client = genai.Client(api_key=GEMINI_API_KEY)
TEMP_DIR.mkdir(parents=True, exist_ok=True)


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


def get_media_identifier(msg: dict) -> str | None:
    text = msg.get("text", "")
    if "instagram.com/reel/" in text:
        candidates = [word for word in text.split() if "instagram.com/reel/" in word]
        return candidates[0] if candidates else None

    raw_xma = msg.get("raw_xma", {})
    if raw_xma.get("video_url"):
        return raw_xma["video_url"]
    if raw_xma.get("preview_media_fbid"):
        return f"fbid:{raw_xma['preview_media_fbid']}"

    return None


def build_source_key(msg: dict) -> str:
    raw_xma = msg.get("raw_xma") or {}

    if raw_xma.get("preview_media_fbid"):
        return f"fbid:{raw_xma['preview_media_fbid']}"

    if raw_xma.get("video_url"):
        return f"url:{raw_xma['video_url']}"

    identifier = get_media_identifier(msg)
    if identifier:
        return f"id:{identifier}"

    if msg.get("id"):
        return f"msg_id:{msg['id']}"

    return f"fallback:{hash(json.dumps(msg, ensure_ascii=False, default=str))}"


def download_image(url: str, save_path: str | Path) -> None:
    response = requests.get(url)
    with open(save_path, "wb") as f:
        f.write(response.content)


def analyze_level_1_thumbnail(caption: str, thumbnail_path: str | Path) -> dict:
    prompt = f"""
    Analyze this video thumbnail and its caption/metadata.
    Caption: "{caption}"

    Determine the category of this video. Possible options:
    - "Text/Slides" (main point is in the text)
    - "Visual" (demonstration, aesthetics, recipe without speaking)
    - "Talking Head" (a person is speaking/explaining something)
    - "Unknown" (if impossible to determine from thumbnail and caption)

    Respond strictly in JSON format:
    {{"category": "Your category", "reason": "Short explanation"}}
    """
    try:
        image_part = client.files.upload(file=str(thumbnail_path))
        response = client.models.generate_content(
            model=AI_MODEL,
            contents=[prompt, image_part],
        )
        try:
            client.files.delete(name=image_part.name)
        except Exception:
            pass

        result_text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(result_text)
    except Exception as e:
        print(f"AI Level 1 error: {e}")
        return {"category": "Unknown", "reason": "AI Error"}


def analyze_level_2_audio(audio_path: str | Path) -> dict:
    prompt = """
    Listen to this audio from a short video.
    Write a brief summary of what is being discussed and confirm the category.

    Respond strictly in JSON format:
    {"category": "Talking Head", "summary": "They are talking about..."}
    """
    try:
        audio_part = client.files.upload(file=str(audio_path))
        response = client.models.generate_content(
            model=AI_MODEL,
            contents=[prompt, audio_part],
        )
        try:
            client.files.delete(name=audio_part.name)
        except Exception:
            pass

        result_text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(result_text)
    except Exception as e:
        print(f"AI Level 2 error: {e}")
        return {"category": "Audio Error", "summary": ""}


def process_reels() -> None:
    processed_keys = load_processed_keys(OUTPUT_JSONL)
    if processed_keys:
        print(f"Resume mode: {len(processed_keys)} items already processed.")

    with open(INPUT_JSON, "r", encoding="utf-8") as f:
        messages = json.load(f)

    for msg in messages:
        raw_xma = msg.get("raw_xma") or {}

        preview_url = raw_xma.get("preview_url")
        video_url = raw_xma.get("video_url") or msg.get("text", "")
        author = raw_xma.get("header_title_text")

        if not preview_url:
            continue

        source_key = build_source_key(msg)
        if source_key in processed_keys:
            continue

        print(f"\n--- Processing: {video_url or source_key} ---")

        item_data = msg.copy()
        item_data["source_key"] = source_key
        item_data["thumbnail_url"] = preview_url
        item_data["reel_url"] = video_url
        if author:
            item_data["author"] = author

        caption = ""
        if author:
            caption = f"Author: {author}"

        thumb_path = TEMP_DIR / f"thumb_{source_key.replace(':', '_').replace('/', '_')}.jpg"

        try:
            download_image(preview_url, thumb_path)

            level_1_result = analyze_level_1_thumbnail(caption, thumb_path)
            category = level_1_result.get("category")

            item_data["category"] = category
            item_data["ai_reason"] = level_1_result.get("reason")

            if category in ["Unknown", "Talking Head"] and "instagram.com/reel/" in str(video_url):
                audio_path = TEMP_DIR / f"audio_{source_key.replace(':', '_').replace('/', '_')}.mp3"

                ydl_opts = {
                    "format": "bestaudio/best",
                    "outtmpl": str(audio_path),
                    "quiet": True,
                    "no_warnings": True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([video_url])

                level_2_result = analyze_level_2_audio(audio_path)
                safe_unlink(audio_path)

                item_data["category"] = level_2_result.get("category", category)
                item_data["summary"] = level_2_result.get("summary", "")

            append_jsonl(OUTPUT_JSONL, item_data)
            processed_keys.add(source_key)

            print(f"Result category: {item_data.get('category')}")
            time.sleep(random.uniform(1.5, 4.0))

        except Exception as e:
            print(f"Failed to process {video_url or source_key}: {e}")
            item_data["category"] = "Parsing error"
            item_data["error"] = str(e)
            append_jsonl(OUTPUT_JSONL, item_data)
            processed_keys.add(source_key)

        finally:
            safe_unlink(thumb_path)

    print(f"\nDone. Output saved to {OUTPUT_JSONL}")


if __name__ == "__main__":
    process_reels()