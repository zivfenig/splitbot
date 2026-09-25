---
name: checkpoint
description: End-of-step routine for SplitBot. Use when the user types /checkpoint or says a step is finished — runs tests, summarizes, logs the decision, proposes a commit.
---

# Checkpoint

Run these steps in order. Keep every message short and plain.

1. Run `pytest` (unit + integration). Do NOT run LLM evals unless the user asks.
2. Report in max 5 lines: passed / failed counts, and the name of each failing test.
   If anything fails, stop here and ask how to proceed.
3. Summarize what changed in this step in max 3 bullets.
4. Draft ONE new row for DECISIONS.md (type · what happened · what we did / why).
   If during this step you produced something that turned out wrong, log it as AI-WRONG —
   be honest, this log is part of the submission.
5. Propose a commit message: first line ≤ 60 chars, then one line explaining WHY.
6. Ask: "Add the DECISIONS row and commit? (y/n)". Only do it after "y".
7. Remind the user: "Run /export sessions/<stage>-<topic>.txt to save this session."
