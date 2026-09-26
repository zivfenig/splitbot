# Failures: gpt-4o-mini · extract_v1 · test · 2026-09-26 · 3 runs per case

### case-25

> העברתי לך 50 בביט על מה שהיה

model: gpt-4o-mini · prompt: extract_v1 · correct runs: 0/3

| field | expected | actual | runs |
|---|---|---|---|
| type | chat | new | runs 1, 2, 3 |
| confidence | not low | low | runs 1 |

### case-36

> תיקון דוד שמש 1.200

model: gpt-4o-mini · prompt: extract_v1 · correct runs: 0/3

| field | expected | actual | runs |
|---|---|---|---|
| type | new | correction | runs 1, 2, 3 |
| amount | 1.200 | 1,200 | runs 1, 3 |
| participants | [1, 2, 3, 4] | none | runs 1, 2 |
| amount | 1.200 | 1200 | runs 2 |

### case-43

> שילמתי 100 על הפיצה של דני ומיכל

model: gpt-4o-mini · prompt: extract_v1 · correct runs: 0/3

| field | expected | actual | runs |
|---|---|---|---|
| participants | [2, 4] | [1, 2, 4] | runs 1, 2, 3 |
| subcategory | restaurant/delivery | other | runs 1, 2, 3 |
| category | eating_out | other | runs 1, 2, 3 |

### case-59

> שילמתי 100, דני 60 מיכל 60

model: gpt-4o-mini · prompt: extract_v1 · correct runs: 0/3

| field | expected | actual | runs |
|---|---|---|---|
| subcategory | other | none | runs 1, 2, 3 |
| category | other | none | runs 1, 2, 3 |

4 of 20 cases have failures.
