"""Stage 0 smoke test: one tiny OpenAI call. Model comes from OPENAI_MODEL in .env."""

from openai import OpenAI

from splitbot.config import require


def main() -> None:
    model = require("OPENAI_MODEL")
    client = OpenAI(api_key=require("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Reply with the single word: pong"}],
    )
    print(f"model={response.model} reply={response.choices[0].message.content!r}")
    print(f"tokens: in={response.usage.prompt_tokens} out={response.usage.completion_tokens}")


if __name__ == "__main__":
    main()
