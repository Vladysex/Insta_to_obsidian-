import json
import os
import re
from urllib.parse import urlparse

from dotenv import load_dotenv
from instagrapi import Client

load_dotenv()

IG_SESSIONID = os.getenv("SESSION_ID")
TARGET_USER = os.getenv("TARGET_USER")

SETTINGS_FILE = "session.json"

REEL_CODE_RE = re.compile(r"/reel/([A-Za-z0-9_-]+)/?")


def normalize_reel_url(url: str) -> tuple[str, str | None]:
    """Return (normalized_reel_url, reel_code)."""
    url = (url or "").strip()
    if not url:
        return "", None

    m = REEL_CODE_RE.search(url)
    code = m.group(1) if m else None
    if code:
        return f"https://www.instagram.com/reel/{code}/", code

    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        return clean, None

    return url, None


def build_source_key(msg_id: str, raw_xma: dict | None, reel_code: str | None, reel_url: str | None) -> str:
    raw_xma = raw_xma or {}

    fbid = raw_xma.get("preview_media_fbid")
    if fbid:
        return f"fbid:{fbid}"

    if reel_code:
        return f"reel_code:{reel_code}"

    if reel_url:
        return f"url:{reel_url}"

    if msg_id:
        return f"msg_id:{msg_id}"

    return "unknown"


def login(cl: Client) -> bool:
    if os.path.exists(SETTINGS_FILE):
        try:
            print(f"Loading {SETTINGS_FILE}...")
            cl.load_settings(SETTINGS_FILE)
            me = cl.account_info()
            print(f"Logged in using session.json as: {me.username}")
            return True
        except Exception as e:
            print(f"Failed to login using session.json: {e}")

    if not IG_SESSIONID:
        print("Missing SESSIONID/SESSION_ID in .env")
        return False

    try:
        print("Logging in using sessionid...")
        cl.login_by_sessionid(IG_SESSIONID)
        me = cl.account_info()
        print(f"Logged in successfully as: {me.username}")

        cl.dump_settings(SETTINGS_FILE)
        print(f"Session saved to {SETTINGS_FILE}")
        return True
    except Exception as e:
        print(f"Critical login error: {e}")
        return False


def iter_threads_safely(cl: Client, max_pages: int = 10):
    """Iterate threads without loading messages (prevents XMA parsing issues)."""
    for box in ("primary", "general"):
        cursor = None
        for _ in range(max_pages):
            threads, cursor = cl.direct_threads_chunk(
                box=box,
                thread_message_limit=0,
                cursor=cursor,
            )
            for t in threads:
                yield t
            if not cursor:
                break


def find_thread_id(cl: Client, target_user: str) -> str | None:
    target_user = (target_user or "").strip()
    if not target_user:
        return None

    is_self = target_user.lower() in {"self", "me"}

    for thread in iter_threads_safely(cl, max_pages=10):
        if is_self:
            if not thread.users:
                return thread.id
        else:
            thread_usernames = [user.username.lower() for user in thread.users]
            if target_user.lower() in thread_usernames:
                return thread.id

    return None


def fetch_target_messages(amount: int = 100) -> None:
    if not TARGET_USER:
        print("Missing TARGET_USER in .env")
        return

    output_file = f"messages_with_{TARGET_USER}.json"

    cl = Client()
    if not login(cl):
        return

    print(f"Searching for chat target: '{TARGET_USER}'...")
    target_thread_id = find_thread_id(cl, TARGET_USER)

    if not target_thread_id:
        print(f"Chat thread not found for target '{TARGET_USER}'.")
        return

    print(f"Downloading last {amount} messages...")
    messages = cl.direct_messages(target_thread_id, amount=amount)

    saved_data: list[dict] = []

    for msg in messages:
        msg_data: dict = {
            "id": msg.id,
            "timestamp": str(msg.timestamp),
            "type": msg.item_type,
            "item_type": msg.item_type,
            "text": msg.text if msg.text else "",
        }

        reel_url = ""
        reel_code: str | None = None
        raw_xma: dict | None = None

        if msg.item_type == "clip" and getattr(msg, "clip", None):
            reel_url, reel_code = normalize_reel_url(f"https://www.instagram.com/reel/{msg.clip.code}/")
            msg_data["text"] = reel_url
            msg_data["reel_url"] = reel_url
            msg_data["reel_code"] = reel_code

        elif msg.item_type == "reel_share" and getattr(msg, "reel_share", None):
            if msg.reel_share.media and getattr(msg.reel_share.media, "code", None):
                reel_url, reel_code = normalize_reel_url(
                    f"https://www.instagram.com/reel/{msg.reel_share.media.code}/"
                )
                msg_data["text"] = reel_url
                msg_data["reel_url"] = reel_url
                msg_data["reel_code"] = reel_code

        elif msg.item_type in ["xma_clip", "xma_media_share"]:
            if getattr(msg, "xma_share", None):
                raw_xma = msg.xma_share.model_dump(mode="json")
                msg_data["raw_xma"] = raw_xma

                # Copy useful raw fields
                video_url = raw_xma.get("video_url") or ""
                target_url = raw_xma.get("target_url") or ""
                preview_url = raw_xma.get("preview_url") or ""
                header_title = raw_xma.get("header_title_text")
                title = raw_xma.get("title")

                if video_url:
                    msg_data["video_url"] = video_url
                if preview_url:
                    msg_data["thumbnail_url"] = preview_url
                if header_title:
                    msg_data["author_username"] = header_title
                if title:
                    msg_data["title"] = title

                # Prefer video_url, then target_url, then msg.text for normalization
                base_url = video_url or target_url or msg_data.get("text", "")
                reel_url, reel_code = normalize_reel_url(base_url)

                if target_url and not msg_data.get("text"):
                    msg_data["text"] = str(target_url)

                if reel_url:
                    msg_data["reel_url"] = reel_url
                if reel_code:
                    msg_data["reel_code"] = reel_code

        # Build stable key for offline main.py dedup/resume
        source_key = build_source_key(msg.id, raw_xma, reel_code, msg_data.get("reel_url") or msg_data.get("text"))
        msg_data["source_key"] = source_key

        saved_data.append(msg_data)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(saved_data, f, ensure_ascii=False, indent=4, default=str)

    print(f"Done! Saved {len(saved_data)} messages to {output_file}")


if __name__ == "__main__":
    fetch_target_messages()