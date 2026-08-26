TESTCHI — PRO PRE-PAYMENT FEATURE COMPLETE v3

MAQSAD
Bu build to‘lovlarni qo‘shishdan OLDINGI yakuniy funksional QA versiya.
Payment kodi ataylab qo‘shilmagan. Avval barcha Free/PRO funksiyalar real fayllarda tekshiriladi.

YANGI / KUCHAYTIRILGAN PRO FUNKSIYALAR

1) 🎯 MAXSUS SAVOL ORALIG‘I
   Misol: 121-170
   Private va guruh host rejimida ishlaydi.

2) 🎲 TASODIFIY N TA SAVOL
   Misol: 200 savoldan tasodifiy 75 ta.
   Private va guruh host rejimida ishlaydi.

3) ⚡ KUCHAYTIRILGAN STANDART PARSER
   - 1. / 1) / №1 / Savol 1 / Question 1 / Вопрос 1
   - A) / A. / A: / A-
   - 2–10 variant
   - ko‘p qatorli savol/variant
   - bir qatorda bir nechta variant
   - Javob / Answer / Ответ
   - hujjat oxiridagi Javoblar / Answer key / Ответы
   - +B), *B), ✓B), ✔B) markerlari
   - Word jadvallari
   - aniq option-text javob: masalan “Javob: Toshkent” faqat variant bilan EXACT mos kelsa

4) 🟨 PDF HIGHLIGHT JADVAL PARSERI
   Real PDF Highlight annotation bilan to‘g‘ri javob belgilangan test jadvallari deterministik o‘qiladi.
   Test fayl: “Yakuniy nazorat savollari 340 test.pdf” lokal QA’da 340/340 tayyor bo‘ldi.

5) 🤖 PRO AI TEXT FALLBACK
   Standart parser noodatiy matnli PDF/DOCXni to‘liq tanimasa, Gemini muammoli qismlarni tekshiradi.
   AI testni yechmasligi shart. Javob dalili manbada bo‘lmasa savol qabul qilinmaydi.

6) 🖼 PRO AI SCAN PDF
   Matn qatlami bo‘lmagan skaner/rasm PDF Gemini multimodal orqali sahifa-sahifa o‘qiladi.
   AI o‘z transkripsiyasida javob dalilini aniq ko‘rsatishi shart; javob taxmin qilinmaydi.
   Default scan limit: 50 sahifa / AI import.

7) ⭐ BOOKMARK
   Quiz paytida qiyin savolni belgilash va keyin faqat belgilanganlarni mashq qilish.

8) 🧠 ZAIF SAVOLLAR
   Bir necha urinish bo‘yicha question performance saqlanadi:
   🔴 Juda qiyin
   🟠 Qiyin
   🟡 Takrorlash kerak
   Zaif savollarni alohida mashq qilish mumkin.

9) 📊 PRO STATISTIKA
   - urinishlar
   - umumiy o‘rtacha
   - eng yaxshi
   - oxirgi
   - dastlabki 5 vs oxirgi 5 progress trendi
   - eng uzun streak
   - stable / red / orange / yellow
   - bookmark soni

10) ⚙️ PRO DEFAULTS
   - standart bo‘lim: 30/40/50/100
   - standart taymer
   - savollarni aralashtirish
   - variantlarni aralashtirish
   Mashq/Imtihon har safar STARTdan oldin majburiy tanlanadi.

11) 📚 TEST BOSHQARUVI
   - rename
   - delete
   - duplicate (PRO)
   - natija/weak history reset (PRO), bookmarklar saqlanadi

12) 👥 GURUH PRO
   - custom range
   - random N
   - host lock
   - pause/resume/skip/stop/release
   - leaderboard
   - bir guruhda bir aktiv quiz

13) 🛡 HIMOYA
   - webhook, getUpdates polling yo‘q
   - upload size default 20 MB
   - AI import default 20 / oy
   - scan PDF default 50 sahifa
   - AI provider xatolari oddiy parser natijasini o‘chirmaydi
   - global error handler

MUHIM ENVIRONMENT
TELEGRAM_TOKEN=...
DATABASE_URL=...
OWNER_TELEGRAM_ID=...
GEMINI_API_KEY=...

OPTIONAL:
PRO_AI_IMPORT_LIMIT=20
MAX_UPLOAD_BYTES=20971520
AI_MAX_SCAN_PAGES=50
AI_PDF_PAGES_PER_BATCH=8
GEMINI_MODEL=...
WEBHOOK_BASE_URL=https://... (faqat Render URL auto-detect ishlamasa)

DEPLOY
Repository root’da quyidagi fayllar birga bo‘lsin:
- bot.py
- api.py
- database_quiz.py
- ai_parser.py
- parser_engine.py   <-- YANGI
- pro_features.py
- requirements.txt

Render Start Command o‘zgarmaydi:
python bot.py & uvicorn api:app --host 0.0.0.0 --port $PORT

PAYMENT
Bu buildda payment yo‘q. QA tugagach entitlement/payment/renewal/expiry/affiliate flow alohida qo‘shiladi.
