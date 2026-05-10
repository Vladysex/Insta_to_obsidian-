import json
import os
import time
import random
import requests
import yt_dlp
from google import genai
from instagrapi import Client
from dotenv import load_dotenv

load_dotenv()

IG_USERNAME = os.getenv('IG_USERNAME')
IG_PASSWORD = os.getenv('IG_PASSWORD')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
TARGET_USER = 'self'

INPUT_JSON = 'messages_with_self.json'
OUTPUT_JSON = 'categorized_reels.json'
TEMP_DIR = './temp_media'

if not all([IG_USERNAME, IG_PASSWORD, GEMINI_API_KEY]):
    print("Помилка: Не всі дані завантажено з .env файлу! Перевір наявність файлу та назви змінних.")
    exit()

# Нова ініціалізація клієнта Gemini
client = genai.Client(api_key=GEMINI_API_KEY)

os.makedirs(TEMP_DIR, exist_ok=True)


def get_media_identifier(msg):
    text = msg.get('text', '')
    if 'instagram.com/reel/' in text:
        return [word for word in text.split() if 'instagram.com/reel/' in word][0]

    raw_xma = msg.get('raw_xma', {})
    if raw_xma.get('video_url'):
        return raw_xma['video_url']
    elif raw_xma.get('preview_media_fbid'):
        return f"fbid:{raw_xma['preview_media_fbid']}"

    return None


def download_image(url, save_path):
    response = requests.get(url)
    with open(save_path, 'wb') as f:
        f.write(response.content)


def analyze_level_1_thumbnail(caption, thumbnail_path):
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
        # Новий синтаксис завантаження та генерації
        image_part = client.files.upload(file=thumbnail_path)
        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=[prompt, image_part]
        )
        client.files.delete(name=image_part.name)

        result_text = response.text.replace('```json', '').replace('```', '').strip()
        return json.loads(result_text)
    except Exception as e:
        print(f"Помилка ШІ на Рівні 1: {e}")
        return {"category": "Unknown", "reason": "AI Error"}


def analyze_level_2_audio(audio_path):
    prompt = """
    Listen to this audio from a short video.
    Write a brief summary of what is being discussed and confirm the category.

    Respond strictly in JSON format:
    {"category": "Talking Head", "summary": "They are talking about..."}
    """
    try:
        audio_part = client.files.upload(file=audio_path)
        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=[prompt, audio_part]
        )
        client.files.delete(name=audio_part.name)

        result_text = response.text.replace('```json', '').replace('```', '').strip()
        return json.loads(result_text)
    except Exception as e:
        print(f"Помилка ШІ на Рівні 2: {e}")
        return {"category": "Audio Error", "summary": ""}


def process_reels():
    cl = Client()
    session_file = "ig_sessions.json"

    try:
        if os.path.exists(session_file):
            print("Loading saved session")
            cl.load_settings(session_file)
            cl.login(IG_USERNAME, IG_PASSWORD)
        else:
            print("Session authorising...")
            cl.login(IG_USERNAME, IG_PASSWORD)
            cl.dump_settings(session_file)
            print("Session saved!")
    except Exception as e:
        print(f"Помилка входу: {e}")
        return

    with open(INPUT_JSON, 'r', encoding='utf-8') as f:
        messages = json.load(f)

    categorized_data = []

    for msg in messages:
        identifier = get_media_identifier(msg)
        if not identifier:
            continue

        print(f"\n--- Обробка: {identifier} ---")
        item_data = msg.copy()

        try:
            if str(identifier).startswith('fbid:'):
                fbid = identifier.split(':')[1]
                media_info = cl.media_info_v1(fbid)
            else:
                media_pk = cl.media_pk_from_url(identifier)
                media_info = cl.media_info(media_pk)

            caption = media_info.caption_text if media_info.caption_text else ""
            thumbnail_url = media_info.thumbnail_url
            author = media_info.user.username

            thumb_path = os.path.join(TEMP_DIR, f"thumb_{media_info.pk}.jpg")
            download_image(thumbnail_url, thumb_path)

            level_1_result = analyze_level_1_thumbnail(caption, thumb_path)
            os.remove(thumb_path)

            category = level_1_result.get('category')
            item_data['category'] = category
            item_data['ai_reason'] = level_1_result.get('reason')
            item_data['author'] = author

            if category in ["Unknown", "Talking Head"] and "instagram.com/reel/" in str(identifier):
                audio_path = os.path.join(TEMP_DIR, f"audio_{media_info.pk}.mp3")

                ydl_opts = {
                    'format': 'bestaudio/best',
                    'outtmpl': audio_path,
                    'quiet': True,
                    'no_warnings': True
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([identifier])

                level_2_result = analyze_level_2_audio(audio_path)
                os.remove(audio_path)

                item_data['category'] = level_2_result.get('category', category)
                item_data['summary'] = level_2_result.get('summary', '')

            print(f"Результат: {item_data['category']}")
            categorized_data.append(item_data)

            time.sleep(random.uniform(3.0, 7.0))

        except Exception as e:
            print(f"Не вдалося обробити {identifier}: {e}")
            item_data['category'] = "Помилка парсингу"
            categorized_data.append(item_data)

    with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(categorized_data, f, ensure_ascii=False, indent=4)
    print(f"\nГотово! Дані збережено у {OUTPUT_JSON}")


if __name__ == "__main__":
    process_reels()