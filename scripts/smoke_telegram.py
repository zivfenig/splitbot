"""Stage 0 smoke test for Telegram.

  python scripts/smoke_telegram.py               # getMe: checks the bot token
  python scripts/smoke_telegram.py --find-chat   # lists chats seen via getUpdates
  python scripts/smoke_telegram.py --send        # sends a test message to TELEGRAM_CHAT_ID

For --find-chat: first send any message in the test group, then run it.
"""

import sys

import httpx

from splitbot.config import require


def call(token: str, method: str, **params) -> dict:
    response = httpx.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=15)
    data = response.json()
    if not data.get("ok"):
        raise SystemExit(f"Telegram {method} failed: {data.get('description')}")
    return data["result"]


def main() -> None:
    token = require("TELEGRAM_BOT_TOKEN")
    if "--find-chat" in sys.argv:
        updates = call(token, "getUpdates")
        chats = {}
        for update in updates:
            message = update.get("message") or update.get("my_chat_member") or {}
            chat = message.get("chat")
            if chat:
                chats[chat["id"]] = chat.get("title") or chat.get("first_name") or "?"
        if not chats:
            print("No updates yet. Send a message in the group and run again.")
        for chat_id, title in chats.items():
            print(f"chat_id={chat_id}  title={title!r}")
    elif "--send" in sys.argv:
        chat_id = int(require("TELEGRAM_CHAT_ID"))
        sent = call(token, "sendMessage", chat_id=chat_id, text="SplitBot smoke test: hello")
        print(f"Sent message {sent['message_id']} to chat {chat_id}")
    else:
        me = call(token, "getMe")
        print(f"Bot OK: @{me['username']} (can_read_all_group_messages={me.get('can_read_all_group_messages')})")


if __name__ == "__main__":
    main()
