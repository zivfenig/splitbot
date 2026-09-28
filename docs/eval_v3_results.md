# v3 evaluation results

## extract_v3 · gpt-4o-mini · dev · 2026-09-27

- 39 verified cases, 3 runs each (117 calls)
- Full-case accuracy: 41.88%
- Consistency: 76.92%
- Grounding failure rate: 4.39%
- Field accuracy: amount 92.86%, currency 87.18%, payer 96.30%, participants 72.84%,
  exact amounts 95.06%, subcategory 80.25%, main category 83.95%
- Reported cost: $0.02978550; latency p50 1.72s, p95 2.48s
- Result: `tests/llm_evals/results/extraction_gpt-4o-mini_extract_v3_dev_2026-09-27.json`

`message_type`/type accuracy is intentionally N/A: action selection moved to the Agent. The
historical dataset still contains action labels, so its false/missed-expense metrics are not valid
for the storage-only v3 extractor. The field metrics remain useful, but the run uses generic
`operation: extract`; production write tools pass `create`, `correct`, or `settle`.

The CLI completed and saved the result but then hit a reporting-only `None` formatting bug for the
removed type metric. The reporter was fixed to print `n/a`; the paid calls were not repeated.

## agent_v3 · gpt-5.4-mini

The authorized five-run consistency evaluation completed once successfully before the final two
regression fixes:

- 11 scenarios, 5 runs each
- 9/11 scenarios passed in every run
- Scenarios 1–4, 6–8, 10–11: 5/5
- Scenario 5 (`לי ולדני` participant phrasing): 0/5
- Scenario 9 (confirmed-expense correction): 1/5
- Reported cost: $0.03229425

Those two failures produced focused changes: code now recognizes the unambiguous Hebrew phrase
`לי ול<member>` (including punctuation after the name), and the Agent prompt/tool contract makes
the code-supplied target state authoritative when choosing `revise_pending` versus
`propose_correction`. Deterministic regression tests for both pass, and the complete local suite is
748/748.

A post-fix five-run attempt and a later one-run probe both returned `llm_error` for every LLM call,
with zero tool steps and $0 cost. They are infrastructure failures, not model-quality results, and
are retained under filenames containing `llm_error_2026-09-27`. The pre-existing tracked
`2026-09-15` reports are `agent_v2` historical evidence and were restored after the harness's fixed
evaluation date caused the failed attempt to overwrite them. A valid post-fix `agent_v3`
consistency run therefore remains to be collected when API access is available again.
