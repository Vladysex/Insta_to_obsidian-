import json
import os
from instagrapi import Client
from dotenv import load_dotenv

load_dotenv()

IG_SESSIONID = os.getenv("SESSION_ID")
TARGET_USER = os.getenv("TARGET_USER")

SETTINGS_FILE = "session.json"


def login(cl: Client) -> bool:
    # cl.set_device({
    #     "app_version": "269.0.0.18.75",
    #     "android_version": "31",
    #     "android_release": "12.0",
    #     "dpi": "600dpi",
    #     "resolution": "1440x3088",
    #     "manufacturer": "Samsung",
    #     "device": "SM-S908B",
    #     "model": "b0s",
    #     "cpu": "exynos2200",
    #     "version_code": "314665256"
    # })

    if os.path.exists(SETTINGS_FILE):
        try:
            print(f"Завантажую {SETTINGS_FILE}...")
            cl.load_settings(SETTINGS_FILE)
            me = cl.account_info()
            print("Успішний вхід із session.json! Профіль:", me.username)
            return True
        except Exception as e:
            print(f"Не вдалось зайти через session.json: {e}")

    if not IG_SESSIONID:
        print("Немає SESSIONID в .env")
        return False

    try:
        print("Авторизація через sessionid...")
        cl.login_by_sessionid(IG_SESSIONID)
        me = cl.account_info()
        print("Успішний вхід! Профіль:", me.username)

        cl.dump_settings(SETTINGS_FILE)
        print(f"Session збережено у {SETTINGS_FILE}")
        return True
    except Exception as e:
        print(f"Критична помилка входу: {e}")
        print("Можливо, sessionid застарів або Instagram запідозрив підміну.")
        return False


def fetch_target_messages(amount=100):
    output_file = f"messages_with_{TARGET_USER}.json"

    cl = Client()

    if not login(cl):
        return

    print(f"Шукаємо чат з '{TARGET_USER}'...")
    threads = cl.direct_threads(20)
    target_thread_id = None

    for thread in threads:
        thread_usernames = [user.username.lower() for user in thread.users]
        if TARGET_USER.lower() in thread_usernames:
            target_thread_id = thread.id
            break

    if not target_thread_id:
        print(f"Чат з {TARGET_USER} не знайдено в останніх 20 діалогах.")
        return

    print(f"Завантаження {amount} останніх повідомлень...")
    messages = cl.direct_messages(target_thread_id, amount=amount)

    saved_data = []

    for msg in messages:
        msg_data = {
            "id": msg.id,
            "timestamp": str(msg.timestamp),
            "type": msg.item_type,
            "text": msg.text if msg.text else ""
        }

        if msg.item_type == "clip" and getattr(msg, "clip", None):
            msg_data["text"] = f"https://www.instagram.com/reel/{msg.clip.code}/"
        elif msg.item_type == "reel_share" and getattr(msg, "reel_share", None):
            if msg.reel_share.media:
                msg_data["text"] = f"https://www.instagram.com/reel/{msg.reel_share.media.code}/"
        elif msg.item_type in ["xma_clip", "xma_media_share"]:
            if getattr(msg, "xma_share", None):
                target_url = getattr(msg.xma_share, "target_url", "")
                if target_url:
                    msg_data["text"] = str(target_url)
                msg_data["raw_xma"] = msg.xma_share.model_dump(mode="json")

        saved_data.append(msg_data)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(saved_data, f, ensure_ascii=False, indent=4, default=str)

    print(f"Успішно! {len(saved_data)} повідомлень збережено у {output_file}")


if __name__ == "__main__":
    fetch_target_messages()