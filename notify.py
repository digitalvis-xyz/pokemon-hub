"""Optional Telegram alerts. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable."""
from __future__ import annotations

import os

from common import post_json


def telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    try:
        post_json("https://api.telegram.org/bot%s/sendMessage" % token,
                  {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": False}, {})
        return True
    except Exception as e:
        print("  telegram error:", e)
        return False
