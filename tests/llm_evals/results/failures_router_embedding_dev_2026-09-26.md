# Router failures: embedding · dev · 2026-09-26 · threshold 0.2846

## Missed expenses (an expense routed to ignore: the serious kind)

none

## Queries routed to ignore

- **r-71** (easy) true: query · decision: ignore · router label: query · scores: expense 0.15 · query 0.55 · ignore 0.29
  > הבוט, מה ההוצאה האחרונה שנרשמה?

## Wrong label (the router's own label differs from the truth, among messages that passed)

- **r-07** (easy) true: expense · decision: pass · router label: query · scores: expense 0.16 · query 0.79 · ignore 0.05
  > אינטרנט בזק 99 ש״ח החודש
- **r-22** (hard) true: ignore · decision: pass · router label: expense · scores: expense 0.45 · query 0.30 · ignore 0.24
  > מחר ב-8 מגיע הטכנאי של המזגן
- **r-24** (hard) true: ignore · decision: pass · router label: query · scores: expense 0.28 · query 0.54 · ignore 0.18
  > החשמל נפל שוב 😩
- **r-26** (hard) true: ignore · decision: pass · router label: query · scores: expense 0.38 · query 0.42 · ignore 0.19
  > צריך לקנות נייר טואלט, מישהו הולך לסופר?
- **r-27** (hard) true: ignore · decision: pass · router label: expense · scores: expense 0.59 · query 0.15 · ignore 0.26
  > אני אשלם מחר את החלק שלי
- **r-31** (hard) true: ignore · decision: pass · router label: expense · scores: expense 0.66 · query 0.13 · ignore 0.20
  > תזכירו לי לשלם ארנונה עד ה-15
- **r-51** (easy) true: expense · decision: pass · router label: query · scores: expense 0.20 · query 0.78 · ignore 0.02
  > מים 312 לחודשיים
- **r-85** (easy) true: ignore · decision: pass · router label: expense · scores: expense 0.67 · query 0.11 · ignore 0.22
  > המכונת כביסה שוב תקועה
- **r-91** (hard) true: ignore · decision: pass · router label: query · scores: expense 0.35 · query 0.54 · ignore 0.11
  > מישהו רוצה להצטרף לקניות מחר?
- **r-96** (easy) true: ignore · decision: pass · router label: query · scores: expense 0.28 · query 0.64 · ignore 0.08
  > מי השאיר כלים בכיור?
- **r-100** (easy) true: ignore · decision: pass · router label: query · scores: expense 0.17 · query 0.65 · ignore 0.19
  > מישהו יכול להוריד את הזבל?
- **r-101** (easy) true: ignore · decision: pass · router label: query · scores: expense 0.31 · query 0.57 · ignore 0.12
  > יש מים חמים?
- **r-103** (hard) true: ignore · decision: pass · router label: expense · scores: expense 0.72 · query 0.07 · ignore 0.21
  > הזמנתי תור לרופא ב-9:30
- **r-105** (hard) true: ignore · decision: pass · router label: expense · scores: expense 0.69 · query 0.19 · ignore 0.13
  > המחיר של הקוטג' ברמי לוי השתגע
- **r-107** (hard) true: ignore · decision: pass · router label: query · scores: expense 0.08 · query 0.85 · ignore 0.07
  > צריך לשלם את החשמל עד סוף השבוע
- **r-109** (hard) true: ignore · decision: pass · router label: query · scores: expense 0.17 · query 0.73 · ignore 0.10
  > אני בסופר, צריכים משהו?
- **r-111** (hard) true: ignore · decision: pass · router label: expense · scores: expense 0.77 · query 0.15 · ignore 0.08
  > הזמנתי חבילה מעלי, תשימו לב לשליח
- **r-114** (hard) true: ignore · decision: pass · router label: expense · scores: expense 0.43 · query 0.38 · ignore 0.20
  > אם מישהו הולך לסופר תקנו גם חלב
- **r-124** (hard) true: query · decision: pass · router label: expense · scores: expense 0.48 · query 0.32 · ignore 0.20
  > מי עוד לא החזיר על הפיצה?
