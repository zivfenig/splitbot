# Failures: gpt-4o · extract_v2 · test · 2026-09-26 · 3 runs per case

### case-16

> תמחקו את הפיצה של אתמול טעיתי

model: gpt-4o · prompt: extract_v2 · correct runs: 0/3

| field | expected | actual | runs |
|---|---|---|---|
| confidence | not low | low | runs 1, 2, 3 |
| refers_to | הפיצה של אתמול | none | runs 3 |

### case-25

> העברתי לך 50 בביט על מה שהיה

model: gpt-4o · prompt: extract_v2 · correct runs: 0/3

| field | expected | actual | runs |
|---|---|---|---|
| type | chat | new | runs 1, 2, 3 |
| confidence | not low | low | runs 1, 2, 3 |

### case-59

> שילמתי 100, דני 60 מיכל 60

model: gpt-4o · prompt: extract_v2 · correct runs: 0/3

| field | expected | actual | runs |
|---|---|---|---|
| subcategory | other | none | runs 1, 2, 3 |
| category | other | none | runs 1, 2, 3 |

3 of 20 cases have failures.
