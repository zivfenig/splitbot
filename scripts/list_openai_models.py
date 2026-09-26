"""List the OpenAI models available to the account (ONE API call), newest first, chat-capable
families only. Used to choose the extraction models (cheap / mid / strong)."""

from datetime import datetime, timezone

from openai import OpenAI

from splitbot.config import require

CHAT_FAMILIES = ("gpt-", "o1", "o3", "o4", "chatgpt-")
SKIP = ("audio", "realtime", "transcribe", "tts", "image", "search", "embedding", "moderation", "whisper", "dall-e", "codex")


def main() -> None:
    models = OpenAI(api_key=require("OPENAI_API_KEY")).models.list().data
    chat = [m for m in models if m.id.startswith(CHAT_FAMILIES) and not any(word in m.id for word in SKIP)]
    for m in sorted(chat, key=lambda m: m.created, reverse=True):
        print(f"{datetime.fromtimestamp(m.created, timezone.utc):%Y-%m-%d}  {m.id}")
    print(f"\n{len(chat)} chat models of {len(models)} total")


if __name__ == "__main__":
    main()
