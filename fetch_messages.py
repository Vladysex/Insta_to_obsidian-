import json
import os
from instagrapi import Client
from dotenv import load_dotenv

load_dotenv()

IG_USERNAME = os.getenv('IG_USERNAME')
IG_PASSWORD = os.getenv('IG_PASSWORD')

TARGET_USER = os.getenv('TARGET_USER')


def fetch_target_messages(amount=100):
    output_file = f'messages_with_{TARGET_USER}.json'

    cl = Client()
    session_file = "ig_session.json"

    try:
        if os.path.exists(session_file):
            print("Loading existing session...")
            cl.load_settings(session_file)
            cl.login(IG_USERNAME, IG_PASSWORD)
        else:
            print("First time login, creating session...")
            cl.login(IG_USERNAME, IG_PASSWORD)
            cl.dump_settings(session_file)
    except Exception as e:
        print(f"Login failed: {e}")
        return

    print(f"Searching for chat with '{TARGET_USER}'...")
    threads = cl.direct_threads(3)
    target_thread_id = None

    for thread in threads:
        thread_usernames = [user.username.lower() for user in thread.users]

        if TARGET_USER.lower() in thread_usernames:
            target_thread_id = thread.id
            break

    if not target_thread_id:
        print(f"Could not find chat with {TARGET_USER}. Make sure it's in your 20 recent chats.")
        return

    print(f"Fetching last {amount} messages...")
    messages = cl.direct_messages(target_thread_id, amount=amount)

    saved_data = []

    for msg in messages:
        msg_data = {
            "id": msg.id,
            "timestamp": str(msg.timestamp),
            "type": msg.item_type,
            "text": msg.text if msg.text else ""
        }

        if msg.item_type == 'clip' and getattr(msg, 'clip', None):
            msg_data['text'] = f"https://www.instagram.com/reel/{msg.clip.code}/"
        elif msg.item_type == 'reel_share' and getattr(msg, 'reel_share', None):
            if msg.reel_share.media:
                msg_data['text'] = f"https://www.instagram.com/reel/{msg.reel_share.media.code}/"

        elif msg.item_type in ['xma_clip', 'xma_media_share']:
            if getattr(msg, 'xma_share', None):
                target_url = getattr(msg.xma_share, 'target_url', '')
                if target_url:
                    msg_data['text'] = str(target_url)
                msg_data['raw_xma'] = msg.xma_share.model_dump(mode='json')

        saved_data.append(msg_data)

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(saved_data, f, ensure_ascii=False, indent=4, default=str)

    print(f"Success! {len(saved_data)} messages saved to {output_file}")


if __name__ == "__main__":
    fetch_target_messages()