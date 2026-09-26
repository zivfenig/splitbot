"""Smoke test: ONE OpenAI embeddings call. The model comes from OPENAI_EMBEDDING_MODEL in .env."""

from openai import OpenAI

from splitbot.config import require


def main() -> None:
    model = require("OPENAI_EMBEDDING_MODEL")
    client = OpenAI(api_key=require("OPENAI_API_KEY"))
    response = client.embeddings.create(model=model, input=["פיצה 140 בלי דני", "מי בבית הערב?"])
    vectors = [item.embedding for item in response.data]
    print(f"model={response.model} vectors={len(vectors)} dimensions={len(vectors[0])}")
    print(f"tokens: {response.usage.prompt_tokens}")
    print(f"first values: {[round(x, 4) for x in vectors[0][:4]]}")


if __name__ == "__main__":
    main()
