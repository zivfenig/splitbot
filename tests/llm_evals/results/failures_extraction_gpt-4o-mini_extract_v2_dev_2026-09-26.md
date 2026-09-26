# Failures: gpt-4o-mini · extract_v2 · dev · 2026-09-26 · 3 runs per case

### case-04

> יוחננוף 287 היום

model: gpt-4o-mini · prompt: extract_v2 · correct runs: 0/3

| field | expected | actual | runs |
|---|---|---|---|
| amount | 287 | none | runs 1, 2, 3 |
| confidence | not low | low | runs 1, 2, 3 |

### case-19

> paid 85 for cleaning stuff

model: gpt-4o-mini · prompt: extract_v2 · correct runs: 2/3

| field | expected | actual | runs |
|---|---|---|---|
| currency | ILS | USD | runs 2 |

### case-27

> אני אשלם מחר את החלק שלי

model: gpt-4o-mini · prompt: extract_v2 · correct runs: 0/3

| field | expected | actual | runs |
|---|---|---|---|
| type | chat | new | runs 1, 2, 3 |
| confidence | not low | low | runs 1, 2, 3 |

### case-37

> סופר 300 בלי דני

model: gpt-4o-mini · prompt: extract_v2 · correct runs: 0/3

| field | expected | actual | runs |
|---|---|---|---|
| amount | 300 | none | runs 1, 2, 3 |
| participants | only=none exclude=['ambiguous [2, 5]'] | [1, 2, 3, 4] | runs 1, 2, 3 |
| confidence | not low | low | runs 1, 2, 3 |
| asks | needs_clarification | ok | runs 1, 2, 3 |

### case-48

> שילמתי על הפיצה אתמול

model: gpt-4o-mini · prompt: extract_v2 · correct runs: 0/3

| field | expected | actual | runs |
|---|---|---|---|
| subcategory | restaurant/delivery | other | runs 1, 2, 3 |
| category | eating_out | other | runs 1, 2, 3 |

### case-51

> מים 312 לחודשיים

model: gpt-4o-mini · prompt: extract_v2 · correct runs: 0/3

| field | expected | actual | runs |
|---|---|---|---|
| amount | 312 | none | runs 1, 2, 3 |
| confidence | not low | low | runs 1, 2, 3 |

### case-58

> שילמתי 200 על הסושי, אני 80 מיכל 120

model: gpt-4o-mini · prompt: extract_v2 · correct runs: 2/3

| field | expected | actual | runs |
|---|---|---|---|
| exact_amounts | member 2=80, member 4=120 | member 4=120 | runs 1 |

7 of 39 cases have failures.
