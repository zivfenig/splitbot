# Router failures: embedding · test · 2026-09-26 · threshold 0.2846

## Missed expenses (an expense routed to ignore: the serious kind)

- **r-36** (hard) true: expense · decision: ignore · router label: expense · scores: expense 0.53 · query 0.17 · ignore 0.30
  > תיקון דוד שמש 1.200

## Queries routed to ignore

none

## Wrong label (the router's own label differs from the truth, among messages that passed)

- **r-25** (hard) true: ignore · decision: pass · router label: expense · scores: expense 0.92 · query 0.04 · ignore 0.04
  > העברתי לך 50 בביט על מה שהיה
- **r-28** (hard) true: ignore · decision: pass · router label: expense · scores: expense 0.66 · query 0.23 · ignore 0.11
  > בעל הבית אמר שהשכירות עולה ל-5,500 מינואר
- **r-67** (hard) true: query · decision: pass · router label: expense · scores: expense 0.96 · query 0.02 · ignore 0.02
  > תזכירו לי כמה שילמתי על הגז
- **r-73** (hard) true: query · decision: pass · router label: expense · scores: expense 0.48 · query 0.43 · ignore 0.09
  > שילמנו כבר את המים?
- **r-78** (hard) true: query · decision: pass · router label: expense · scores: expense 0.45 · query 0.44 · ignore 0.11
  > מי שילם על הפיצה אתמול?
- **r-81** (easy) true: ignore · decision: pass · router label: query · scores: expense 0.45 · query 0.48 · ignore 0.08
  > מי לקח את המטען שלי 😤
- **r-84** (hard) true: ignore · decision: pass · router label: expense · scores: expense 0.70 · query 0.16 · ignore 0.15
  > בא לי שווארמה
- **r-89** (hard) true: ignore · decision: pass · router label: query · scores: expense 0.05 · query 0.87 · ignore 0.07
  > שמעתם שהחשמל מתייקר בינואר?
- **r-97** (hard) true: ignore · decision: pass · router label: expense · scores: expense 0.87 · query 0.04 · ignore 0.10
  > ראיתי פיצה ב-20 ש"ח בקניון
