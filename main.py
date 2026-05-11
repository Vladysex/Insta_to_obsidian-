import json
import os
import random
import time

import requests
import yt_dlp
from dotenv import load_dotenv
from google import genai
from instagrapi import Client

load_dotenv()

IG_USERNAME = os.getenv("IG_USERNAME")
IG_PASSWORD = os.getenv("IG_PASSWORD")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TARGET_USER = os.getenv("TARGET_USER")

INPUT_JSON = "messages_with_self.json"
OUTPUT_JSONL = "categorized_reels.jsonl"
TEMP_DIR = "./temp_media"
SESSION_FILE = "session.json"

AI_MODEL = "gemini-1.5-flash"

if not all([IG_USERNAME, IG_PASSWORD, GEMINI_API_KEY]):
    print("Error: missing required values in .env (IG_USERNAME, IG_PASSWORD, GEMINI_API_KEY).")
    raise SystemExit(1)

client = genai.Client(api_key=GEMINI_API_KEY)
os.makedirs(TEMP_DIR, exist_ok=True)


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


def build_source_key(identifier: str, media_info=None) -> str:
    if media_info is not None and getattr(media_info, "pk", None):
        return f"ig_pk:{media_info.pk}"
    if identifier and str(identifier).startswith("fbid:"):
        return str(identifier)
    return f"url:{identifier}"


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


def download_image(url: str, save_path: str) -> None:
    response = requests.get(url)
    with open(save_path, "wb") as f:
        f.write(response.content)


def analyze_level_1_thumbnail(caption: str, thumbnail_path: str) -> dict:
    prompt = f"""
    Analyze this video thumbnail and its caption.
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
        image_part = client.files.upload(file=thumbnail_path)
        response = client.models.generate_content(
            model=AI_MODEL,
            contents=[prompt, image_part],
        )
        client.files.delete(name=image_part.name)

        result_text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(result_text)
    except Exception as e:
        print(f"AI Level 1 error: {e}")
        return {"category": "Unknown", "reason": "AI Error"}


def analyze_level_2_audio(audio_path: str) -> dict:
    prompt = """
    Listen to this audio from a short video.
    Write a brief summary of what is being discussed and confirm the category.

    Respond strictly in JSON format:
    {"category": "Talking Head", "summary": "They are talking about..."}
    """
    try:
        audio_part = client.files.upload(file=audio_path)
        response = client.models.generate_content(
            model=AI_MODEL,
            contents=[prompt, audio_part],
        )
        client.files.delete(name=audio_part.name)

        result_text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(result_text)
    except Exception as e:
        print(f"AI Level 2 error: {e}")
        return {"category": "Audio Error", "summary": ""}


def login_instagram(cl: Client) -> bool:
    try:
        if os.path.exists(SESSION_FILE):
            cl.load_settings(SESSION_FILE)
            me = cl.account_info()
            print(f"Logged in using session.json as: {me.username}")
            return True
    except Exception as e:
        print(f"Failed to login using session.json: {e}")

    try:
        print("Logging in using username/password...")
        cl.login(IG_USERNAME, IG_PASSWORD)
        me = cl.account_info()
        print(f"Logged in successfully as: {me.username}")
        cl.dump_settings(SESSION_FILE)
        print(f"Session saved to {SESSION_FILE}")
        return True
    except Exception as e:
        print(f"Login failed: {e}")
        return False


def process_reels() -> None:
    processed_keys = load_processed_keys(OUTPUT_JSONL)
    if processed_keys:
        print(f"Resume mode: {len(processed_keys)} items already processed.")

    cl = Client()
    if not login_instagram(cl):
        return

    with open(INPUT_JSON, "r", encoding="utf-8") as f:
        messages = json.load(f)

    for msg in messages:
        identifier = get_media_identifier(msg)
        if not identifier:
            continue

        item_data = msg.copy()

        try:
            if str(identifier).startswith("fbid:"):
                fbid = identifier.split(":", 1)[1]
                media_info = cl.media_info_v1(fbid)
            else:
                media_pk = cl.media_pk_from_url(identifier)
                media_info = cl.media_info(media_pk)

            source_key = build_source_key(identifier, media_info=media_info)
            if source_key in processed_keys:
                continue

            print(f"\n--- Processing: {identifier} ---")

            caption = media_info.caption_text or ""
            thumbnail_url = media_info.thumbnail_url
            author = media_info.user.username

            item_data["source_key"] = source_key
            item_data["author"] = author
            item_data["caption"] = caption
            item_data["thumbnail_url"] = thumbnail_url

            if getattr(media_info, "pk", None):
                item_data["media_pk"] = str(media_info.pk)

            if "instagram.com/reel/" in str(identifier):
                item_data["reel_url"] = str(identifier)

            thumb_path = os.path.join(TEMP_DIR, f"thumb_{getattr(media_info, 'pk', 'x')}.jpg")
            download_image(thumbnail_url, thumb_path)

            level_1_result = analyze_level_1_thumbnail(caption, thumb_path)
            os.remove(thumb_path)

            category = level_1_result.get("category")
            item_data["category"] = category
            item_data["ai_reason"] = level_1_result.get("reason")

            if category in ["Unknown", "Talking Head"] and "instagram.com/reel/" in str(identifier):
                audio_path = os.path.join(TEMP_DIR, f"audio_{getattr(media_info, 'pk', 'x')}.mp3")

                ydl_opts = {
                    "format": "bestaudio/best",
                    "outtmpl": audio_path,
                    "quiet": True,
                    "no_warnings": True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([identifier])

                level_2_result = analyze_level_2_audio(audio_path)
                os.remove(audio_path)

                item_data["category"] = level_2_result.get("category", category)
                item_data["summary"] = level_2_result.get("summary", "")

            append_jsonl(OUTPUT_JSONL, item_data)
            processed_keys.add(source_key)

            print(f"Result category: {item_data.get('category')}")
            time.sleep(random.uniform(3.0, 7.0))

        except Exception as e:
            source_key = build_source_key(identifier, media_info=None)
            if source_key in processed_keys:
                continue

            print(f"Failed to process {identifier}: {e}")
            item_data["source_key"] = source_key
            item_data["category"] = "Parsing error"
            item_data["error"] = str(e)
            append_jsonl(OUTPUT_JSONL, item_data)
            processed_keys.add(source_key)

    print(f"\nDone. Output saved to {OUTPUT_JSONL}")


if __name__ == "__main__":
    process_reels()