TEST TUZUVCHI — BEPUL YAKUNIY UZBEK VERSIYA

DEPLOY:
1. GitHub'dagi bot.py ni paketdagi bot.py bilan almashtiring.
2. database_quiz.py ni paketdagi database_quiz.py bilan almashtiring.
3. requirements.txt ni paketdagi requirements.txt bilan almashtiring.
4. Render deploy bo‘lishini kuting.
5. DATABASE_URL va TELEGRAM_TOKEN Environment'da qolishi kerak.
6. OWNER_TELEGRAM_ID testchi/owner uchun saqlanib qolishi mumkin.

ASOSIY FUNKSIYALAR:
- Oyiga 1 ta yangi test importi
- Saqlangan testlarni cheksiz ishlash
- PostgreSQL'da testlar va natijalar
- Testni qayta nomlash, o‘chirish va natijalarini ko‘rish
- 30/40/50/100 savollik bo‘limlar
- 10/15/20/30/40/60 soniya yoki 2 daqiqa taymer
- Savollarni aralashtirish
- Javob variantlarini aralashtirish
- Mashq va Imtihon rejimi
- Xatolarni progressiv qayta mashq qilish
- 1 soniyalik savollar oralig‘i
- Pauza / davom / to‘xtatish
- Shaxsiy testda 3 ta javobsiz savoldan keyin avtomatik pauza
- Guruhda 3 ta ketma-ket javobsiz savoldan keyin avtomatik to‘xtash
- Guruh host/boshqaruvchi tizimi
- /skip va /stop guruh boshqaruvchisi uchun
- Parser hisoboti
- Qo‘llab-quvvatlanadigan formatlar bo‘limi
- Tashqi AI orqali formatlash yo‘riqnomasi
- Maxfiylik va Bot haqida bo‘limlari
- Kutilmagan xatolar uchun xavfsiz xabar

MUHIM:
Bu versiyada botning o‘zida AI parser hali yo‘q. Qo‘llab-quvvatlanmaydigan fayl uchun
Yordam → AI bilan formatlash yo‘riqnomasi mavjud. Built-in AI recovery keyingi bosqich.
