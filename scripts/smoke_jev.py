"""Smoke test: ONE Jev call through OpenRouter (a `choice` question: expense / query / ignore).

  python scripts/smoke_jev.py ["message to classify"]

Prints the HTTP status and the RAW JSON so the request/response shape is verified by a real
call. The docs (docs.typesafe.ai) describe TypeSafe's own endpoint
(`POST https://api.typesafe.ai/v1/systemone`, body `{state, model, questions}`) and do not
cover OpenRouter, so the OpenRouter shape below is only confirmed by running this script.
"""

import json
import sys

import httpx

from splitbot.config import optional, require

URL = "https://openrouter.ai/api/alpha/decisions"

QUESTION = {
    "route": {
        "type": "choice",
        "instructions": "What is this group-chat message, in a shared-expenses chat?",
        "criteria": {
            "expense": "Reports an expense someone paid, or corrects or deletes one.",
            "query": "Asks a question about the group's expenses or balances.",
            "ignore": "Anything else: chatter, plans, reminders, jokes.",
        },
    }
}


def main() -> None:
    message = sys.argv[1] if len(sys.argv) > 1 else "פיצה 140 בלי דני"
    body = {"model": optional("JEV_MODEL", "typesafe/jev-1.13"), "state": message, "questions": QUESTION}
    response = httpx.post(
        URL,
        headers={"Authorization": f"Bearer {require('OPENROUTER_API_KEY')}", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    print(f"HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        print(response.text[:500])
        raise SystemExit(1)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if response.status_code == 200:
        answer = data["answers"]["route"]
        print(f"\nmessage: {message!r}\nchoice: {answer['choice']}  confidence: {answer['confidence']}")
        print(f"probabilities: {answer['probabilities']}")


if __name__ == "__main__":
    main()
