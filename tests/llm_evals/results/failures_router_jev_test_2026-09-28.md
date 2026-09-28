# Router failures: jev · test · 2026-09-28 · threshold 0.5400

## Missed expenses (an expense routed to ignore: the serious kind)

none

## Queries routed to ignore

none

## Wrong label (the router's own label differs from the truth, among messages that passed)

- **r-23** (hard) true: ignore · decision: pass · router label: query · scores: expense 0.01 · query 0.78 · ignore 0.21
  > כמה עולה פיצה בדומינוס?
- **r-25** (hard) true: ignore · decision: pass · router label: expense · scores: expense 0.94 · query 0.00 · ignore 0.06
  > העברתי לך 50 בביט על מה שהיה
- **r-28** (hard) true: ignore · decision: pass · router label: expense · scores: expense 0.90 · query 0.00 · ignore 0.10
  > בעל הבית אמר שהשכירות עולה ל-5,500 מינואר
- **r-70** (easy) true: query · decision: pass · router label: expense · scores: expense 1.00 · query 0.00 · ignore 0.00
  > מי הכי הרבה בחובה?
- **r-76** (hard) true: query · decision: pass · router label: expense · scores: expense 1.00 · query 0.00 · ignore 0.00
  > כמה יוצא החלק שלי בשכירות?
- **r-97** (hard) true: ignore · decision: pass · router label: expense · scores: expense 0.55 · query 0.01 · ignore 0.44
  > ראיתי פיצה ב-20 ש"ח בקניון
