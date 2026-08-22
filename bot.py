import os
import re
import logging
import asyncio
from io import BytesIO
from math import ceil
from typing import List, Dict, Optional, Tuple

from docx import Document
from pypdf import PdfReader

import database_quiz as db
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PollAnswerHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

# Fast prototype storage.
# NOTE: This is kept in RAM, so a Render restart clears current sessions.
USER_DATA: Dict[int, dict] = {}
GROUP_DATA: Dict[int, dict] = {}
POLL_MAP: Dict[str, dict] = {}
WELCOME_SHOWN = set()

GROUP_SIZES = [30, 40, 50, 100]

# Telegram supports timed polls. These are the study choices shown before Start.
TIMER_CHOICES = [10, 15, 20, 30, 40, 60, 120]
GROUP_EMPTY_STOP_THRESHOLD = 3


def format_duration(seconds: int) -> str:
    if seconds == 60:
        return "1 daqiqa"
    if seconds == 120:
        return "2 daqiqa"
    return f"{seconds} soniya"


# -----------------------------
# TEXT EXTRACTION
# -----------------------------
def extract_pdf(data: bytes) -> str:
    reader = PdfReader(BytesIO(data))
    parts = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            parts.append(text)
    return "\n".join(parts)


def extract_docx(data: bytes) -> str:
    doc = Document(BytesIO(data))
    parts = []

    for p in doc.paragraphs:
        if p.text.strip():
            parts.append(p.text)

    # Also read tables because many test files are stored in Word tables.
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))

    return "\n".join(parts)


# -----------------------------
# TEST PARSER
# -----------------------------
# The parser is deliberately tolerant about QUESTION NUMBER formatting.
# These all count as the same kind of question start:
#   № 123. Question
#   №123. Question
#   № 123.Question
#   № 123 Question
#   123. Question
#   123) Question
# Correct answers are still taken only from explicit answer information.
OPTION_RE = re.compile(
    r"^\s*([+*✓✔✅☑]?)\s*([A-Ha-h])\s*[\.\)\-:]\s*(.+?)\s*$"
)
ANSWER_LINE_RE = re.compile(
    r"^\s*(?:answer|correct\s*answer|javob|to['’`ʻ]?g['’`ʻ]?ri\s*javob)\s*[:\-]?\s*(.*?)\s*$",
    re.IGNORECASE,
)
ANSWER_KEY_PAIR_RE = re.compile(
    r"(?<!\d)(\d{1,4})\s*[\.\)\-:]?\s*([A-Ha-h])\b"
)


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    text = text.replace("–", "-").replace("—", "-")

    # IMPORTANT:
    # Do NOT globally split before patterns such as "D. ...". In teacher-made
    # documents this is often an author's initial (e.g. "Quronov D.") rather
    # than answer option D. Splitting it used to break otherwise valid questions.
    return text


def parse_question_start(line: str) -> Optional[Tuple[int, str]]:
    """Recognize a numbered question without caring about missing spaces/dots."""
    s = line.strip()

    # №-style numbering: punctuation after the number is optional.
    # Examples: №123, № 123., № 123.Question, № 123 Question
    m = re.match(r"^№\s*(\d{1,4})\s*[\.\)\-:]?\s*(.*)$", s, re.IGNORECASE)
    if m:
        return int(m.group(1)), m.group(2).strip()

    # Plain numbering: punctuation is expected, but spacing is optional.
    # Examples: 123.Question, 123. Question, 123) Question
    m = re.match(r"^(\d{1,4})\s*([\.\)\-:])\s*(.*)$", s)
    if m:
        return int(m.group(1)), m.group(3).strip()

    # A number on its own can also introduce a question on the next line.
    m = re.match(r"^(\d{1,4})\s*$", s)
    if m:
        return int(m.group(1)), ""

    return None


def is_source_header(text: str) -> bool:
    """Return True when the text after the question number is source metadata."""
    cleaned = re.sub(r"^\s*#\s*", "", text).strip().lower()
    return cleaned.startswith("manba") or cleaned.startswith("source")


def clean_question_line(line: str) -> str:
    """Remove harmless teacher markers without changing the actual question."""
    s = line.strip()
    if s in {"#", "=", "*", "+", "-"}:
        return ""
    return re.sub(r"^\s*#\s*", "", s).strip()


def parse_answer_key(text: str) -> Dict[int, str]:
    """
    Finds answer-key pairs such as:
      1-A
      2. B
      3) C
    Works best when the answer key is near the end of the file.
    """
    answers: Dict[int, str] = {}

    lower = text.lower()
    anchors = [
        "answer key",
        "answers",
        "javoblar",
        "javob kaliti",
        "to'g'ri javoblar",
        "to‘g‘ri javoblar",
    ]

    start = -1
    for anchor in anchors:
        pos = lower.rfind(anchor)
        if pos > start:
            start = pos

    if start < 0:
        return answers

    candidate = text[start:]
    for num, letter in ANSWER_KEY_PAIR_RE.findall(candidate):
        answers[int(num)] = letter.upper()

    return answers


def parse_questions(text: str) -> Tuple[List[dict], List[str]]:
    """
    Flexible question detection + strict answer validation.

    The parser tolerates missing spaces and punctuation around question numbers,
    but it NEVER guesses a correct answer. If the answer is missing or multiple
    answers are explicitly supplied, that block is reported as a warning.
    """
    text = normalize_text(text)
    answer_key = parse_answer_key(text)
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    questions: List[dict] = []
    warnings: List[str] = []

    current: Optional[dict] = None
    current_option_letter: Optional[str] = None

    def save_current():
        nonlocal current, current_option_letter
        if not current:
            return

        number = current["number"]
        options = current["options"]
        letter_to_index = current["letter_to_index"]

        answer_letters = list(current.get("answer_letters") or [])
        if not answer_letters and number in answer_key:
            answer_letters = [answer_key[number]]

        correct_index = None
        if len(answer_letters) == 1:
            correct_index = letter_to_index.get(answer_letters[0])

        reasons = []
        if not current["question"].strip():
            reasons.append("question text missing")
        if not (2 <= len(options) <= 10):
            reasons.append(f"{len(options)} options")
        if len(answer_letters) == 0:
            reasons.append("correct answer not found")
        elif len(answer_letters) > 1:
            reasons.append("multiple correct answers: " + ", ".join(answer_letters))
        elif correct_index is None:
            reasons.append(f"answer {answer_letters[0]} has no matching option")

        if not reasons:
            questions.append(
                {
                    "number": number,
                    "question": current["question"].strip(),
                    "options": [x.strip() for x in options],
                    "correct_index": correct_index,
                }
            )
        else:
            warnings.append(f"Question {number}: " + ", ".join(reasons))

        current = None
        current_option_letter = None

    for line in lines:
        q_start = parse_question_start(line)
        opt_match = OPTION_RE.match(line)
        ans_match = ANSWER_LINE_RE.match(line)

        # A numbered question start always wins over normal continuation text.
        # OPTION_RE is checked only to avoid treating something like "A) ..." as
        # a numeric question in unusual documents.
        if q_start and not opt_match:
            number, tail = q_start

            # Skip answer-key-style lines such as "1. B".
            if len(tail) == 1 and tail.upper() in "ABCDEFGH":
                continue

            save_current()
            current = {
                "number": number,
                # "№ 1. Manba: ..." is metadata; the real question follows.
                "question": "" if is_source_header(tail) else clean_question_line(tail),
                "options": [],
                "letter_to_index": {},
                "answer_letters": [],
            }
            current_option_letter = None
            continue

        if current is None:
            continue

        # Explicit answer line. Capture the WHOLE value so "Javob: B, D" is
        # recognized as ambiguous instead of silently choosing B.
        if ans_match:
            letters = [
                x.upper()
                for x in re.findall(r"\b([A-Ha-h])\b", ans_match.group(1))
            ]
            current["answer_letters"] = list(dict.fromkeys(letters))
            current_option_letter = None
            continue

        if opt_match:
            marker = opt_match.group(1)
            letter = opt_match.group(2).upper()
            option_text = opt_match.group(3).strip()

            # Conservative support for common teacher correct-answer markers:
            # +A) ..., *B) ..., ✓C) ..., ✔D) ...
            if marker:
                if letter not in current["answer_letters"]:
                    current["answer_letters"].append(letter)

            # Keep compatibility with the earlier "A) option *" convention.
            if option_text.endswith("*"):
                option_text = option_text[:-1].rstrip()
                if letter not in current["answer_letters"]:
                    current["answer_letters"].append(letter)

            current["letter_to_index"][letter] = len(current["options"])
            current["options"].append(option_text)
            current_option_letter = letter
            continue

        # Continuation lines. A leading # is a common teacher marker for the
        # question itself and should not become part of the visible Telegram text.
        if current_option_letter and current["options"]:
            current["options"][-1] += " " + line
        else:
            q_line = clean_question_line(line)
            if q_line:
                if current["question"]:
                    current["question"] += " " + q_line
                else:
                    current["question"] = q_line

    save_current()
    return questions, warnings


# -----------------------------
# TELEGRAM UI
# -----------------------------
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📚 Testlarim", callback_data="menu_quizzes"),
                InlineKeyboardButton("📄 Yangi test", callback_data="menu_new"),
            ],
            [
                InlineKeyboardButton("▶️ Davom etish", callback_data="menu_continue"),
                InlineKeyboardButton("📊 Natijalar", callback_data="menu_progress"),
            ],
            [
                InlineKeyboardButton("👥 Guruh testi", callback_data="menu_group"),
                InlineKeyboardButton("⚙️ Sozlamalar", callback_data="menu_settings"),
            ],
            [
                InlineKeyboardButton("❓ Yordam", callback_data="menu_help"),
            ],
        ]
    )


def home_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 Bosh menyu", callback_data="menu_home")]]
    )


def is_group_chat(chat) -> bool:
    return bool(chat and chat.type in ("group", "supergroup"))


def group_size_keyboard(group_mode: bool = False) -> InlineKeyboardMarkup:
    prefix = "gsize" if group_mode else "size"
    back_callback = "g_home" if group_mode else "menu_home"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("30", callback_data=f"{prefix}:30"),
                InlineKeyboardButton("40", callback_data=f"{prefix}:40"),
            ],
            [
                InlineKeyboardButton("50", callback_data=f"{prefix}:50"),
                InlineKeyboardButton("100", callback_data=f"{prefix}:100"),
            ],
            [InlineKeyboardButton("🏠 Bosh menyu", callback_data=back_callback)],
        ]
    )


def group_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📚 Mening testlarim", callback_data="g_saved"),
                InlineKeyboardButton("📄 Test yuklash", callback_data="g_new"),
            ],
            [
                InlineKeyboardButton("📚 Joriy test", callback_data="g_current"),
                InlineKeyboardButton("🏆 Oxirgi reyting", callback_data="g_leaderboard"),
            ],
            [InlineKeyboardButton("❓ Yordam", callback_data="g_help")],
        ]
    )


async def send_home(message):
    await message.reply_text(
        "🎓 Exam Quiz Bot\n\n"
        "Kerakli bo‘limni tanlang:",
        reply_markup=main_menu(),
    )


async def send_group_home(message):
    await message.reply_text(
        "👥 Exam Quiz Bot — guruh rejimi\n\n"
        "Bir xil savol barcha qatnashchilarga beriladi. "
        "Javob berganlar ham vaqt tugaguncha kutadi. "
        "Keyingi savol faqat taymer tugagach chiqadi.\n\n"
        "Istalgan foydalanuvchi o‘zining saqlangan testini tanlashi yoki yangi PDF/DOCX yuborishi mumkin.\n\n"
        "Boshqaruvchi buyruqlari: /stop va /skip",
        reply_markup=group_home_keyboard(),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    # A single emoji may animate in Telegram clients; show it only once per
    # running process so /start does not become noisy.
    welcome_key = (chat.id, update.effective_user.id if update.effective_user else 0)
    if welcome_key not in WELCOME_SHOWN:
        WELCOME_SHOWN.add(welcome_key)
        try:
            await context.bot.send_message(chat_id=chat.id, text="👋")
        except Exception:
            pass

    if is_group_chat(chat):
        await send_group_home(update.message)
    else:
        await send_home(update.message)


async def home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await send_home(query.message)


async def send_new_quiz_prompt(message):
    await message.reply_text(
        "📄 Yangi test\n\n"
        "PDF yoki Word (.docx) test faylini shu chatga yuboring.\n"
        "Men savollarni aniqlayman va keyin ularni 30 / 40 / 50 / 100 savoldan "
        "guruhlarga ajratishga imkon beraman.",
        reply_markup=home_button(),
    )


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update.effective_chat):
        await update.message.reply_text(
            "📄 PDF yoki DOCX test faylini shu guruhga yuboring. "
            "Faylni yuborgan foydalanuvchi quiz boshqaruvchisi bo‘ladi."
        )
    else:
        await send_new_quiz_prompt(update.message)


async def new_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await send_new_quiz_prompt(query.message)


# Keep the old callback name because other parts of the bot already use it.
async def help_upload_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await send_new_quiz_prompt(query.message)


async def show_saved_quizzes(message, tg_user, group_mode: bool = False):
    if not db.is_enabled():
        # Database is optional during the transition; keep old RAM behavior usable.
        session = GROUP_DATA.get(message.chat.id) if group_mode else USER_DATA.get(tg_user.id)
        if session:
            target = "g_current" if group_mode else "groups"
            await message.reply_text(
                "⚠️ Doimiy database hali ulanmagan. Hozirgi test faqat vaqtinchalik xotirada.\n\n"
                f"📄 {session.get('filename', 'Test')}\n"
                f"✅ {len(session.get('questions', []))} ta savol",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("📚 Joriy test", callback_data=target)]]
                ),
            )
        else:
            await message.reply_text(
                "⚠️ Doimiy database hali ulanmagan. Avval yangi test yuklang."
            )
        return

    quizzes = await db.list_quizzes(tg_user.id, limit=20)
    if not quizzes:
        rows = []
        if group_mode:
            rows.append([InlineKeyboardButton("📄 Test yuklash", callback_data="g_new")])
            rows.append([InlineKeyboardButton("🏠 Guruh menyusi", callback_data="g_home")])
        else:
            rows.append([InlineKeyboardButton("📄 Yangi test", callback_data="menu_new")])
            rows.append([InlineKeyboardButton("🏠 Bosh menyu", callback_data="menu_home")])

        await message.reply_text(
            "📚 Sizda hali saqlangan test yo‘q.\n\n"
            "PDF/DOCX testni bir marta yuklasangiz, u keyingi safar shu yerda chiqadi.",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    rows = []
    prefix = "gload" if group_mode else "pload"
    for quiz in quizzes:
        name = quiz["name"]
        if len(name) > 32:
            name = name[:29] + "..."
        rows.append(
            [
                InlineKeyboardButton(
                    f"📘 {name} · {quiz['question_count']}",
                    callback_data=f"{prefix}:{quiz['id']}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "🏠 Guruh menyusi" if group_mode else "🏠 Bosh menyu",
                callback_data="g_home" if group_mode else "menu_home",
            )
        ]
    )

    await message.reply_text(
        "📚 Saqlangan testlaringiz\n\nBoshlash uchun testni tanlang:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def quizzes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_saved_quizzes(
        update.message,
        update.effective_user,
        group_mode=is_group_chat(update.effective_chat),
    )


async def quizzes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await show_saved_quizzes(query.message, update.effective_user, group_mode=False)


async def group_saved_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await show_saved_quizzes(query.message, update.effective_user, group_mode=True)


async def private_load_saved_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not db.is_enabled():
        await query.message.reply_text("❌ Database ulanmagan.")
        return

    quiz_id = int(query.data.split(":")[1])
    quiz = await db.load_quiz(update.effective_user.id, quiz_id)
    if not quiz:
        await query.message.reply_text("❌ Test topilmadi yoki sizga tegishli emas.")
        return

    USER_DATA[update.effective_user.id] = {
        "chat_id": update.effective_chat.id,
        "filename": quiz["source_filename"],
        "questions": quiz["questions"],
        "warnings": [],
        "group_size": None,
        "groups": [],
        "active": None,
        "results": {},
        "saved_quiz_id": quiz_id,
    }

    await query.message.reply_text(
        f"📘 {quiz['name']}\n"
        f"✅ {len(quiz['questions'])} ta savol yuklandi.\n\n"
        "Har bir guruhda nechta savol bo‘lsin?",
        reply_markup=group_size_keyboard(group_mode=False),
    )


async def group_load_saved_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not db.is_enabled():
        await query.message.reply_text("❌ Database ulanmagan.")
        return

    quiz_id = int(query.data.split(":")[1])
    quiz = await db.load_quiz(update.effective_user.id, quiz_id)
    if not quiz:
        await query.message.reply_text("❌ Test topilmadi yoki sizga tegishli emas.")
        return

    GROUP_DATA[update.effective_chat.id] = {
        "chat_id": update.effective_chat.id,
        "filename": quiz["source_filename"],
        "questions": quiz["questions"],
        "warnings": [],
        "group_size": None,
        "groups": [],
        "active": None,
        "results": {},
        "controller_id": update.effective_user.id,
        "last_leaderboard_text": None,
        "saved_quiz_id": quiz_id,
    }

    await query.message.reply_text(
        f"✅ {update.effective_user.full_name} o‘z testini tanladi.\n\n"
        f"📘 {quiz['name']}\n"
        f"❓ {len(quiz['questions'])} ta savol\n\n"
        "Har bir bo‘limda nechta savol bo‘lsin?",
        reply_markup=group_size_keyboard(group_mode=True),
    )


async def show_continue(message, user_id: int):
    session = USER_DATA.get(user_id)
    if not session:
        await message.reply_text(
            "▶️ Davom ettirish uchun faol test topilmadi.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📄 Yangi test", callback_data="menu_new")],
                    [InlineKeyboardButton("🏠 Bosh menyu", callback_data="menu_home")],
                ]
            ),
        )
        return

    if session.get("active"):
        active = session["active"]
        await message.reply_text(
            f"▶️ Quiz hozir davom etmoqda.\n\n"
            f"Guruh: {active['group_index'] + 1}\n"
            f"Savol: {active['current'] + 1}/{len(active['questions'])}\n\n"
            "Joriy Telegram polliga javob bering yoki vaqt tugashini kuting.",
            reply_markup=home_button(),
        )
        return

    if session.get("groups"):
        await show_groups(message, user_id)
        return

    await message.reply_text(
        "▶️ Test yuklangan, lekin guruh hajmi hali tanlanmagan.\n\n"
        "Har bir guruhda nechta savol bo‘lsin?",
        reply_markup=group_size_keyboard(),
    )


async def continue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_continue(update.message, update.effective_user.id)


async def continue_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await show_continue(query.message, update.effective_user.id)


async def show_progress(message, user_id: int):
    session = USER_DATA.get(user_id)
    if not session:
        await message.reply_text(
            "📊 Hozircha natijalar yo‘q.",
            reply_markup=home_button(),
        )
        return

    results = session.get("results", {})
    if not results:
        await message.reply_text(
            "📊 Hozircha tugallangan guruh natijalari yo‘q.",
            reply_markup=home_button(),
        )
        return

    lines = ["📊 Natijalar\n"]
    for idx in sorted(results):
        result = results[idx]
        unanswered = result.get("unanswered", 0)
        lines.append(
            f"📘 {idx + 1}-guruh — {result['correct']}/{result['total']} "
            f"({result['percent']}%)"
            + (f" · javobsiz {unanswered}" if unanswered else "")
        )

    await message.reply_text(
        "\n".join(lines),
        reply_markup=home_button(),
    )


async def progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_progress(update.message, update.effective_user.id)


async def progress_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await show_progress(query.message, update.effective_user.id)


async def send_group_mode_info(message, context: ContextTypes.DEFAULT_TYPE):
    chat = message.chat

    if is_group_chat(chat):
        await send_group_home(message)
        return

    me = await context.bot.get_me()
    add_url = f"https://t.me/{me.username}?startgroup=quiz" if me.username else None

    rows = []
    if add_url:
        rows.append([InlineKeyboardButton("➕ Botni guruhga qo‘shish", url=add_url)])
    rows.append([InlineKeyboardButton("🏠 Bosh menyu", callback_data="menu_home")])

    await message.reply_text(
        "👥 Guruh testi\n\n"
        "Botni Telegram guruhiga qo‘shing va guruh ichida /group yoki /start yuboring.\n"
        "So‘ng PDF/DOCX test faylini guruhga yuboring.\n\n"
        "Guruh rejimida:\n"
        "• hamma bir xil savolni ko‘radi;\n"
        "• javob bergan odam keyingi savolni erta boshlatmaydi;\n"
        "• savol tanlangan vaqtning oxirigacha ochiq turadi;\n"
        "• yakunda individual reyting chiqadi.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def group_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_group_mode_info(update.message, context)


async def group_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await send_group_mode_info(query.message, context)


async def group_home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await send_group_home(query.message)


async def group_new_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "📄 PDF yoki DOCX test faylini shu guruhga yuboring.\n\n"
        "Faylni yuborgan foydalanuvchi ushbu quizning boshqaruvchisi bo‘ladi."
    )


async def group_current_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    session = GROUP_DATA.get(chat_id)

    if not session:
        await query.message.reply_text(
            "📚 Bu guruhda hali test yuklanmagan.",
            reply_markup=group_home_keyboard(),
        )
        return

    total = len(session.get("questions", []))
    warnings = len(session.get("warnings", []))
    text = (
        f"📚 Joriy guruh testi\n\n"
        f"📄 {session.get('filename', 'Test')}\n"
        f"✅ Quizga tayyor: {total}\n"
        f"⚠️ Muammoli: {warnings}"
    )

    if session.get("groups"):
        text += f"\n📦 Guruhlar: {len(session['groups'])}"

    await query.message.reply_text(text, reply_markup=group_home_keyboard())


async def group_leaderboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = GROUP_DATA.get(update.effective_chat.id)

    if not session or not session.get("last_leaderboard_text"):
        await query.message.reply_text(
            "🏆 Hozircha yakunlangan guruh testi yo‘q.",
            reply_markup=group_home_keyboard(),
        )
        return

    await query.message.reply_text(
        session["last_leaderboard_text"],
        reply_markup=group_home_keyboard(),
    )


async def group_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "❓ Guruh rejimi\n\n"
        "1) PDF/DOCX fayl yuboring.\n"
        "2) 30 / 40 / 50 / 100 savollik bo‘limni tanlang.\n"
        "3) Quiz bo‘limini tanlang.\n"
        "4) Vaqtni tanlang.\n"
        "5) ▶️ START ni bosing.\n\n"
        "Savol vaqt tugaguncha ochiq turadi. Hamma shu vaqt ichida javob beradi. "
        "Keyingi savol avtomatik ravishda vaqt tugagach chiqadi.\n\n"
        "🛑 /stop — quizni natija bilan tugatish\n"
        "⏭ /skip — joriy savolni o‘tkazish\n"
        f"😴 {GROUP_EMPTY_STOP_THRESHOLD} ta ketma-ket savolga hech kim javob bermasa, bot o‘zi to‘xtaydi.",
        reply_markup=group_home_keyboard(),
    )


async def send_settings(message):
    await message.reply_text(
        "⚙️ Sozlamalar\n\n"
        "Hozir vaqt har bir guruhni boshlashdan oldin tanlanadi:\n"
        "10 / 15 / 20 / 30 / 40 / 60 soniya yoki 2 daqiqa.\n\n"
        "Doimiy standart sozlamalar keyinroq qo‘shiladi.",
        reply_markup=home_button(),
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_settings(update.message)


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await send_settings(query.message)


async def send_help(message):
    await message.reply_text(
        "❓ Yordam\n\n"
        "1) 📄 Yangi test orqali PDF yoki DOCX yuboring.\n"
        "2) Bot savol bloklari va to‘g‘ri javoblarni aniqlaydi.\n"
        "3) 30 / 40 / 50 / 100 savollik guruh hajmini tanlang.\n"
        "4) Guruhni tanlang.\n"
        "5) Har bir savol uchun vaqtni tanlang.\n"
        "6) ▶️ START ni bosing.\n\n"
        "Asosiy buyruqlar:\n"
        "/start /new /quizzes /continue /progress /group /settings /help",
        reply_markup=home_button(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_help(update.message)


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await send_help(query.message)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    group_mode = is_group_chat(update.effective_chat)

    filename = document.file_name or "file"
    lower = filename.lower()

    if not (lower.endswith(".pdf") or lower.endswith(".docx")):
        await update.message.reply_text("❌ Hozircha faqat PDF va DOCX fayllar qabul qilinadi.")
        return

    status = await update.message.reply_text("📥 Fayl o‘qilmoqda...")

    try:
        tg_file = await context.bot.get_file(document.file_id)
        data = await tg_file.download_as_bytearray()
        raw = bytes(data)

        if lower.endswith(".pdf"):
            text = extract_pdf(raw)
        else:
            text = extract_docx(raw)

        if not text.strip():
            await status.edit_text(
                "❌ Fayldan matn topilmadi.\n"
                "Agar bu skanerlangan PDF bo‘lsa, keyingi versiyada OCR qo‘shamiz."
            )
            return

        await status.edit_text("🔎 Test savollari aniqlanmoqda...")
        questions, warnings = parse_questions(text)

        if not questions:
            await status.edit_text(
                "❌ Javobi aniqlangan test savollari topilmadi.\n\n"
                "Prototype hozir quyidagi formatlarni yaxshi taniydi:\n"
                "1. Savol\nA) ...\nB) ...\nC) ...\nD) ...\nJavob: B\n\n"
                "yoki fayl oxirida: Javoblar: 1-B 2-A 3-D ..."
            )
            return

        session_data = {
            "chat_id": chat_id,
            "filename": filename,
            "questions": questions,
            "warnings": warnings,
            "group_size": None,
            "groups": [],
            "active": None,
            "results": {},
            "controller_id": user_id,
            "last_leaderboard_text": None,
        }

        if group_mode:
            GROUP_DATA[chat_id] = session_data
        else:
            USER_DATA[user_id] = session_data

        saved_note = ""
        if db.is_enabled():
            try:
                saved_id = await db.save_quiz(
                    owner_id=user_id,
                    username=update.effective_user.username,
                    full_name=update.effective_user.full_name,
                    filename=filename,
                    questions=questions,
                )
                session_data["saved_quiz_id"] = saved_id
                saved_note = "\n💾 Test doimiy bazaga saqlandi."
            except Exception:
                logging.exception("Could not save quiz to database")
                saved_note = "\n⚠️ Test ishlaydi, lekin bazaga saqlashda xato bo‘ldi."

        total_blocks = len(questions) + len(warnings)

        warning_text = ""
        if warnings:
            preview = "\n".join(f"• {w}" for w in warnings[:8])
            more = ""
            if len(warnings) > 8:
                more = f"\n• ... yana {len(warnings) - 8} ta"
            warning_text = (
                f"\n⚠️ Muammoli savollar: {len(warnings)}\n"
                f"{preview}{more}"
            )

        mode_note = ""
        if group_mode:
            mode_note = (
                f"\n👤 Boshqaruvchi: {update.effective_user.full_name}\n"
                "Faqat shu foydalanuvchi quiz sozlamalarini boshqaradi."
            )

        await status.edit_text(
            f"✅ Fayl tahlil qilindi.\n\n"
            f"📄 {filename}\n"
            f"🧩 Savol bloklari topildi: {total_blocks}\n"
            f"✅ Quizga tayyor: {len(questions)}"
            f"{warning_text}"
            f"{mode_note}"
            f"{saved_note}\n\n"
            f"Har bir guruhda nechta savol bo‘lsin?",
            reply_markup=group_size_keyboard(group_mode=group_mode),
        )

    except Exception as e:
        logging.exception("Document processing failed")
        await status.edit_text(f"❌ Faylni qayta ishlashda xato yuz berdi:\n{type(e).__name__}")


def build_groups(questions: List[dict], size: int) -> List[List[dict]]:
    return [questions[i:i + size] for i in range(0, len(questions), size)]


async def choose_size_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    session = USER_DATA.get(user_id)
    if not session:
        await query.message.reply_text("❌ Sessiya topilmadi. Faylni qayta yuboring.")
        return

    size = int(query.data.split(":")[1])
    groups = build_groups(session["questions"], size)
    session["group_size"] = size
    session["groups"] = groups

    await show_groups(query.message, user_id)


async def show_groups(message, user_id: int):
    session = USER_DATA.get(user_id)
    if not session:
        await message.reply_text("❌ Sessiya topilmadi.")
        return

    rows = []
    for idx, group in enumerate(session["groups"]):
        start_no = idx * session["group_size"] + 1
        end_no = start_no + len(group) - 1

        result = session["results"].get(idx)
        suffix = ""
        if result:
            suffix = f" ✅ {result['percent']}%"

        rows.append(
            [
                InlineKeyboardButton(
                    f"📘 {idx + 1}-guruh · {start_no}-{end_no}{suffix}",
                    callback_data=f"group:{idx}",
                )
            ]
        )

    rows.append([InlineKeyboardButton("📄 Boshqa fayl yuborish", callback_data="help_upload")])
    rows.append([InlineKeyboardButton("🏠 Bosh menyu", callback_data="menu_home")])

    await message.reply_text(
        f"✅ {len(session['groups'])} ta guruh tayyor.\n\n"
        "Guruhni tanlang. Quiz faqat ▶️ Start bosilgandan keyin boshlanadi:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    session = USER_DATA.get(user_id)
    if not session:
        await query.message.reply_text("❌ Sessiya topilmadi.")
        return

    group_index = int(query.data.split(":")[1])
    if group_index < 0 or group_index >= len(session["groups"]):
        return

    group = session["groups"][group_index]
    start_no = group_index * session["group_size"] + 1
    end_no = start_no + len(group) - 1

    previous = session["results"].get(group_index)
    previous_text = ""
    if previous:
        previous_text = (
            f"\nOldingi natija: {previous['correct']}/{previous['total']} "
            f"({previous['percent']}%)"
        )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("10 sec", callback_data=f"timer:{group_index}:10"),
                InlineKeyboardButton("15 sec", callback_data=f"timer:{group_index}:15"),
            ],
            [
                InlineKeyboardButton("20 sec", callback_data=f"timer:{group_index}:20"),
                InlineKeyboardButton("30 sec", callback_data=f"timer:{group_index}:30"),
            ],
            [
                InlineKeyboardButton("40 sec", callback_data=f"timer:{group_index}:40"),
                InlineKeyboardButton("60 sec", callback_data=f"timer:{group_index}:60"),
            ],
            [
                InlineKeyboardButton("2 min", callback_data=f"timer:{group_index}:120"),
            ],
            [InlineKeyboardButton("📚 Guruhlar", callback_data="groups")],
            [InlineKeyboardButton("🏠 Bosh menyu", callback_data="menu_home")],
        ]
    )

    await query.message.reply_text(
        f"📘 {group_index + 1}-guruh\n"
        f"Savollar: {start_no}-{end_no}\n"
        f"Jami: {len(group)}"
        f"{previous_text}\n\n"
        "⏱ Har bir savol uchun vaqtni tanlang:",
        reply_markup=keyboard,
    )


async def timer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    session = USER_DATA.get(user_id)
    if not session:
        await query.message.reply_text("❌ Sessiya topilmadi.")
        return

    _, group_text, seconds_text = query.data.split(":")
    group_index = int(group_text)
    seconds = int(seconds_text)

    if group_index < 0 or group_index >= len(session["groups"]):
        return
    if seconds not in TIMER_CHOICES:
        return

    group = session["groups"][group_index]
    session["selected_timer"] = seconds

    # Maximum duration if the user uses all available time on every question.
    max_seconds = len(group) * seconds
    if max_seconds >= 60:
        max_minutes = max_seconds / 60
        max_time_text = f"{max_minutes:.1f} daqiqagacha"
    else:
        max_time_text = f"{max_seconds} soniyagacha"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ START",
                    callback_data=f"startgroup:{group_index}:{seconds}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⏱ Vaqtni o‘zgartirish",
                    callback_data=f"group:{group_index}",
                )
            ],
            [InlineKeyboardButton("📚 Guruhlar", callback_data="groups")],
        ]
    )

    await query.message.reply_text(
        f"✅ Quiz tayyor.\n\n"
        f"📘 Guruh: {group_index + 1}\n"
        f"❓ Savollar: {len(group)}\n"
        f"⏱ Har bir savol: {format_duration(seconds)}\n"
        f"⌛ Maksimal vaqt: {max_time_text}\n\n"
        "Quiz hali boshlanmadi. Boshlash uchun ▶️ START ni bosing.",
        reply_markup=keyboard,
    )


async def groups_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await show_groups(query.message, update.effective_user.id)


async def start_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    session = USER_DATA.get(user_id)

    if not session:
        await query.message.reply_text("❌ Sessiya topilmadi.")
        return

    parts = query.data.split(":")
    if len(parts) != 3:
        await query.message.reply_text("❌ Vaqt tanlanmagan. Guruhni qayta tanlang.")
        return

    group_index = int(parts[1])
    timer_seconds = int(parts[2])

    if group_index < 0 or group_index >= len(session["groups"]):
        return
    if timer_seconds not in TIMER_CHOICES:
        return

    # A run_id prevents an old timeout task from affecting a restarted quiz.
    session["run_counter"] = session.get("run_counter", 0) + 1
    run_id = session["run_counter"]

    session["active"] = {
        "run_id": run_id,
        "group_index": group_index,
        "questions": session["groups"][group_index],
        "current": 0,
        "correct": 0,
        "wrong": [],
        "unanswered": [],
        "answered_polls": set(),
        "timer_seconds": timer_seconds,
        "current_streak": 0,
        "best_streak": 0,
        "review_mode": False,
    }

    try:
        await context.bot.send_dice(chat_id=chat_id, emoji="🎯")
    except Exception:
        pass

    await query.message.reply_text(
        f"🚀 {group_index + 1}-guruh boshlandi!\n"
        f"Jami {len(session['active']['questions'])} ta savol.\n"
        f"⏱ Har bir savol uchun: {format_duration(timer_seconds)}"
    )
    await send_next_question(chat_id, user_id, context)


def telegram_safe_question(text: str) -> str:
    # Telegram poll question limit is finite; keep it safely short.
    text = re.sub(r"\s+", " ", text).strip()
    return text[:290] if len(text) > 290 else text


def telegram_safe_option(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:95] if len(text) > 95 else text


async def send_next_question(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    session = USER_DATA.get(user_id)
    if not session or not session.get("active"):
        return

    active = session["active"]
    idx = active["current"]
    questions = active["questions"]

    if idx >= len(questions):
        await finish_group(chat_id, user_id, context)
        return

    item = questions[idx]
    options = [telegram_safe_option(x) for x in item["options"]]
    timer_seconds = active["timer_seconds"]

    try:
        msg = await context.bot.send_poll(
            chat_id=chat_id,
            question=telegram_safe_question(
                f"[{idx + 1}/{len(questions)}] {item['question']}"
            ),
            options=options,
            type="quiz",
            correct_option_id=int(item["correct_index"]),
            is_anonymous=False,
            allows_multiple_answers=False,
            open_period=timer_seconds,
        )
    except Exception:
        logging.exception("Could not send poll")
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Bu savol Telegram formatiga sig‘madi. Keyingi savolga o‘tyapman.",
        )
        active["wrong"].append(idx)
        active["current"] += 1
        await send_next_question(chat_id, user_id, context)
        return

    POLL_MAP[msg.poll.id] = {
        "mode": "private",
        "user_id": user_id,
        "chat_id": chat_id,
        "group_index": active["group_index"],
        "question_index": idx,
        "run_id": active["run_id"],
        "handled": False,
    }

    # PollAnswerHandler is not called when the user gives no answer.
    # This task moves the quiz forward once the poll's timer expires.
    asyncio.create_task(
        question_timeout(
            poll_id=msg.poll.id,
            user_id=user_id,
            chat_id=chat_id,
            group_index=active["group_index"],
            question_index=idx,
            run_id=active["run_id"],
            timer_seconds=timer_seconds,
            context=context,
        )
    )


async def question_timeout(
    poll_id: str,
    user_id: int,
    chat_id: int,
    group_index: int,
    question_index: int,
    run_id: int,
    timer_seconds: int,
    context: ContextTypes.DEFAULT_TYPE,
):
    # Small buffer lets Telegram close the poll first.
    await asyncio.sleep(timer_seconds + 0.8)

    meta = POLL_MAP.get(poll_id)
    if not meta or meta.get("handled"):
        return

    session = USER_DATA.get(user_id)
    if not session or not session.get("active"):
        POLL_MAP.pop(poll_id, None)
        return

    active = session["active"]

    # Ignore timeout tasks from an older/restarted quiz.
    if (
        active.get("run_id") != run_id
        or active.get("group_index") != group_index
        or active.get("current") != question_index
    ):
        POLL_MAP.pop(poll_id, None)
        return

    # Mark this poll before awaiting anything else, preventing a late answer
    # from advancing the quiz twice.
    meta["handled"] = True
    active["unanswered"].append(question_index)
    active["current_streak"] = 0
    active["current"] += 1
    POLL_MAP.pop(poll_id, None)

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏱ Vaqt tugadi. {question_index + 1}-savol javobsiz qoldi.",
    )

    await send_next_question(chat_id, user_id, context)


async def poll_answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    meta = POLL_MAP.get(answer.poll_id)
    if not meta:
        return

    if meta.get("mode") == "group":
        await group_poll_answer_handler(update, context, meta)
        return

    # Only count the user who started this private quiz.
    if answer.user.id != meta["user_id"]:
        return

    # If timeout already handled this poll, ignore any late update.
    if meta.get("handled"):
        return

    user_id = meta["user_id"]
    session = USER_DATA.get(user_id)
    if not session or not session.get("active"):
        return

    active = session["active"]

    # Ignore stale answers from an older/restarted quiz.
    if (
        active.get("run_id") != meta.get("run_id")
        or active["group_index"] != meta["group_index"]
    ):
        POLL_MAP.pop(answer.poll_id, None)
        return

    q_idx = meta["question_index"]
    if q_idx != active["current"]:
        return

    if answer.poll_id in active["answered_polls"]:
        return

    # Mark handled BEFORE awaiting the next question. This prevents the timer
    # task from advancing the quiz at the same time.
    meta["handled"] = True
    active["answered_polls"].add(answer.poll_id)

    item = active["questions"][q_idx]
    selected = answer.option_ids[0] if answer.option_ids else None

    if selected == int(item["correct_index"]):
        active["correct"] += 1
        active["current_streak"] = active.get("current_streak", 0) + 1
        active["best_streak"] = max(
            active.get("best_streak", 0),
            active["current_streak"],
        )

        if active["current_streak"] in {5, 10, 20, 30, 50}:
            await context.bot.send_message(
                chat_id=meta["chat_id"],
                text=f"🔥 {active['current_streak']} ta ketma-ket to‘g‘ri!",
            )
    else:
        active["wrong"].append(q_idx)
        active["current_streak"] = 0

    active["current"] += 1
    POLL_MAP.pop(answer.poll_id, None)

    await send_next_question(meta["chat_id"], user_id, context)


async def finish_group(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    session = USER_DATA[user_id]
    active = session["active"]

    total = len(active["questions"])
    correct = active["correct"]
    wrong_answered = len(active["wrong"])
    unanswered = len(active["unanswered"])
    percent = round((correct / total) * 100) if total else 0
    group_index = active["group_index"]
    timer_seconds = active["timer_seconds"]

    best_streak = active.get("best_streak", 0)
    problem_indices = list(dict.fromkeys(active["wrong"] + active["unanswered"]))
    problem_questions = [
        active["questions"][i]
        for i in problem_indices
        if 0 <= i < len(active["questions"])
    ]

    if not active.get("review_mode"):
        session["results"][group_index] = {
            "correct": correct,
            "wrong": wrong_answered,
            "unanswered": unanswered,
            "total": total,
            "percent": percent,
            "timer_seconds": timer_seconds,
            "best_streak": best_streak,
            "problem_questions": problem_questions,
        }

    buttons = [
        [
            InlineKeyboardButton(
                "🔄 Qayta ishlash",
                callback_data=f"startgroup:{group_index}:{timer_seconds}",
            )
        ],
        [
            InlineKeyboardButton(
                "⏱ Boshqa vaqt bilan",
                callback_data=f"group:{group_index}",
            )
        ],
        [InlineKeyboardButton("📚 Guruhlar", callback_data="groups")],
    ]

    if problem_questions:
        buttons.insert(
            0,
            [
                InlineKeyboardButton(
                    f"❌ {len(problem_questions)} ta xatoni mashq qilish",
                    callback_data=f"retrywrong:{group_index}:{timer_seconds}",
                )
            ],
        )

    if group_index + 1 < len(session["groups"]) and not active.get("review_mode"):
        buttons.insert(
            0,
            [
                InlineKeyboardButton(
                    "➡️ Keyingi guruh",
                    callback_data=f"group:{group_index + 1}",
                )
            ],
        )

    was_review = active.get("review_mode", False)
    session["active"] = None

    if percent == 100:
        try:
            await context.bot.send_message(chat_id=chat_id, text="🏆")
        except Exception:
            pass
    elif percent >= 90:
        try:
            await context.bot.send_message(chat_id=chat_id, text="🎉")
        except Exception:
            pass

    title = "🏁 Xatolar mashqi tugadi!" if was_review else f"🏁 {group_index + 1}-guruh tugadi!"

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"{title}\n\n"
            f"✅ To‘g‘ri: {correct}\n"
            f"❌ Noto‘g‘ri: {wrong_answered}\n"
            f"⏱ Javobsiz: {unanswered}\n"
            f"🎯 Natija: {percent}%\n"
            f"🔥 Eng yaxshi seriya: {best_streak}\n"
            f"⏲ Vaqt: {format_duration(timer_seconds)}/savol"
        ),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def group_controller_ok(update: Update, session: dict) -> bool:
    user = update.effective_user
    return bool(user and user.id == session.get("controller_id"))


async def group_choose_size_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    session = GROUP_DATA.get(chat_id)

    if not session:
        await query.answer()
        await query.message.reply_text("❌ Guruh sessiyasi topilmadi. Testni qayta yuboring.")
        return
    if not group_controller_ok(update, session):
        await query.answer("Bu testni faqat uni yuklagan foydalanuvchi boshqaradi.", show_alert=True)
        return
    await query.answer()

    size = int(query.data.split(":")[1])
    session["group_size"] = size
    session["groups"] = build_groups(session["questions"], size)
    await show_group_quiz_groups(query.message, chat_id)


async def show_group_quiz_groups(message, chat_id: int):
    session = GROUP_DATA.get(chat_id)
    if not session:
        await message.reply_text("❌ Guruh sessiyasi topilmadi.")
        return

    rows = []
    for idx, group in enumerate(session.get("groups", [])):
        start_no = idx * session["group_size"] + 1
        end_no = start_no + len(group) - 1
        rows.append(
            [
                InlineKeyboardButton(
                    f"📘 {idx + 1}-bo‘lim · {start_no}-{end_no}",
                    callback_data=f"ggroup:{idx}",
                )
            ]
        )

    rows.append([InlineKeyboardButton("🏠 Guruh menyusi", callback_data="g_home")])

    await message.reply_text(
        f"✅ {len(session['groups'])} ta bo‘lim tayyor.\n\n"
        "Quiz bo‘limini tanlang:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def group_quiz_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    session = GROUP_DATA.get(chat_id)

    if not session:
        await query.answer()
        await query.message.reply_text("❌ Guruh sessiyasi topilmadi.")
        return
    if not group_controller_ok(update, session):
        await query.answer("Bu testni faqat uni yuklagan foydalanuvchi boshqaradi.", show_alert=True)
        return
    await query.answer()

    group_index = int(query.data.split(":")[1])
    if group_index < 0 or group_index >= len(session.get("groups", [])):
        return

    group = session["groups"][group_index]
    start_no = group_index * session["group_size"] + 1
    end_no = start_no + len(group) - 1

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("10 sec", callback_data=f"gtimer:{group_index}:10"),
                InlineKeyboardButton("15 sec", callback_data=f"gtimer:{group_index}:15"),
            ],
            [
                InlineKeyboardButton("20 sec", callback_data=f"gtimer:{group_index}:20"),
                InlineKeyboardButton("30 sec", callback_data=f"gtimer:{group_index}:30"),
            ],
            [
                InlineKeyboardButton("40 sec", callback_data=f"gtimer:{group_index}:40"),
                InlineKeyboardButton("60 sec", callback_data=f"gtimer:{group_index}:60"),
            ],
            [
                InlineKeyboardButton("2 min", callback_data=f"gtimer:{group_index}:120"),
            ],
            [InlineKeyboardButton("📚 Bo‘limlar", callback_data="ggroups")],
        ]
    )

    await query.message.reply_text(
        f"📘 {group_index + 1}-bo‘lim\n"
        f"Savollar: {start_no}-{end_no}\n"
        f"Jami: {len(group)}\n\n"
        "⏱ Har bir savol uchun vaqtni tanlang:",
        reply_markup=keyboard,
    )


async def group_timer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    session = GROUP_DATA.get(chat_id)

    if not session:
        await query.answer()
        await query.message.reply_text("❌ Guruh sessiyasi topilmadi.")
        return
    if not group_controller_ok(update, session):
        await query.answer("Bu testni faqat uni yuklagan foydalanuvchi boshqaradi.", show_alert=True)
        return
    await query.answer()

    _, group_text, seconds_text = query.data.split(":")
    group_index = int(group_text)
    seconds = int(seconds_text)

    if seconds not in TIMER_CHOICES:
        return
    if group_index < 0 or group_index >= len(session.get("groups", [])):
        return

    group = session["groups"][group_index]
    max_seconds = len(group) * seconds
    max_time_text = (
        f"{max_seconds / 60:.1f} daqiqagacha"
        if max_seconds >= 60
        else f"{max_seconds} soniyagacha"
    )

    await query.message.reply_text(
        f"✅ Guruh quizi tayyor.\n\n"
        f"📘 Bo‘lim: {group_index + 1}\n"
        f"❓ Savollar: {len(group)}\n"
        f"⏱ Har bir savol: {format_duration(seconds)}\n"
        f"⌛ Maksimal vaqt: {max_time_text}\n\n"
        "Hamma tayyor bo‘lganda ▶️ START ni bosing.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "▶️ START",
                        callback_data=f"gstart:{group_index}:{seconds}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⏱ Vaqtni o‘zgartirish",
                        callback_data=f"ggroup:{group_index}",
                    )
                ],
                [InlineKeyboardButton("📚 Bo‘limlar", callback_data="ggroups")],
            ]
        ),
    )


async def group_groups_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session = GROUP_DATA.get(update.effective_chat.id)
    if session and not group_controller_ok(update, session):
        await query.answer("Bu testni faqat uni yuklagan foydalanuvchi boshqaradi.", show_alert=True)
        return
    await query.answer()
    await show_group_quiz_groups(query.message, update.effective_chat.id)


async def group_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    chat_id = update.effective_chat.id
    session = GROUP_DATA.get(chat_id)

    if not session:
        await query.answer()
        await query.message.reply_text("❌ Guruh sessiyasi topilmadi.")
        return
    if not group_controller_ok(update, session):
        await query.answer("START ni faqat testni yuklagan foydalanuvchi bosishi mumkin.", show_alert=True)
        return
    await query.answer()

    _, group_text, seconds_text = query.data.split(":")
    group_index = int(group_text)
    timer_seconds = int(seconds_text)

    if timer_seconds not in TIMER_CHOICES:
        return
    if group_index < 0 or group_index >= len(session.get("groups", [])):
        return

    session["run_counter"] = session.get("run_counter", 0) + 1
    run_id = session["run_counter"]

    session["active"] = {
        "run_id": run_id,
        "group_index": group_index,
        "questions": session["groups"][group_index],
        "current": 0,
        "timer_seconds": timer_seconds,
        "participants": {},
        "answered_users": set(),
        "empty_streak": 0,
        "current_poll_id": None,
        "current_poll_message_id": None,
    }

    try:
        await context.bot.send_dice(chat_id=chat_id, emoji="🎯")
    except Exception:
        pass

    await query.message.reply_text(
        f"🚀 Guruh quizi boshlandi!\n"
        f"📘 {group_index + 1}-bo‘lim\n"
        f"❓ {len(session['active']['questions'])} ta savol\n"
        f"⏱ {format_duration(timer_seconds)}/savol\n\n"
        "Javob bergan bo‘lsangiz ham, keyingi savol taymer tugagach chiqadi."
    )

    await send_next_group_question(chat_id, context)


async def send_next_group_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    session = GROUP_DATA.get(chat_id)
    if not session or not session.get("active"):
        return

    active = session["active"]
    idx = active["current"]
    questions = active["questions"]

    if idx >= len(questions):
        await finish_group_quiz(chat_id, context)
        return

    item = questions[idx]
    options = [telegram_safe_option(x) for x in item["options"]]
    timer_seconds = active["timer_seconds"]
    active["answered_users"] = set()

    try:
        msg = await context.bot.send_poll(
            chat_id=chat_id,
            question=telegram_safe_question(
                f"[{idx + 1}/{len(questions)}] {item['question']}"
            ),
            options=options,
            type="quiz",
            correct_option_id=int(item["correct_index"]),
            is_anonymous=False,
            allows_multiple_answers=False,
            open_period=timer_seconds,
        )
    except Exception:
        logging.exception("Could not send group poll")
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Bu savol Telegram formatiga sig‘madi. Keyingi savolga o‘tyapman.",
        )
        active["current"] += 1
        await send_next_group_question(chat_id, context)
        return

    active["current_poll_id"] = msg.poll.id
    active["current_poll_message_id"] = msg.message_id

    POLL_MAP[msg.poll.id] = {
        "mode": "group",
        "chat_id": chat_id,
        "message_id": msg.message_id,
        "group_index": active["group_index"],
        "question_index": idx,
        "run_id": active["run_id"],
        "handled": False,
    }

    asyncio.create_task(
        group_question_timeout(
            poll_id=msg.poll.id,
            chat_id=chat_id,
            group_index=active["group_index"],
            question_index=idx,
            run_id=active["run_id"],
            timer_seconds=timer_seconds,
            context=context,
        )
    )


async def group_poll_answer_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    meta: dict,
):
    answer = update.poll_answer
    chat_id = meta["chat_id"]
    session = GROUP_DATA.get(chat_id)

    if not session or not session.get("active"):
        return

    active = session["active"]
    if (
        active.get("run_id") != meta.get("run_id")
        or active.get("group_index") != meta.get("group_index")
        or active.get("current") != meta.get("question_index")
        or meta.get("handled")
    ):
        return

    user = answer.user
    if not user or user.is_bot:
        return

    # Count only the first submitted answer for this question.
    if user.id in active["answered_users"]:
        return

    selected = answer.option_ids[0] if answer.option_ids else None
    if selected is None:
        return

    active["answered_users"].add(user.id)
    participant = active["participants"].setdefault(
        user.id,
        {
            "name": user.full_name or (f"@{user.username}" if user.username else str(user.id)),
            "correct": 0,
            "wrong": 0,
            "current_streak": 0,
            "best_streak": 0,
        },
    )

    item = active["questions"][meta["question_index"]]
    if selected == int(item["correct_index"]):
        participant["correct"] += 1
        participant["current_streak"] += 1
        participant["best_streak"] = max(
            participant["best_streak"],
            participant["current_streak"],
        )
    else:
        participant["wrong"] += 1
        participant["current_streak"] = 0

    # IMPORTANT: do NOT send the next question here.
    # Everyone waits until group_question_timeout() fires.


async def group_question_timeout(
    poll_id: str,
    chat_id: int,
    group_index: int,
    question_index: int,
    run_id: int,
    timer_seconds: int,
    context: ContextTypes.DEFAULT_TYPE,
):
    await asyncio.sleep(timer_seconds + 0.8)

    meta = POLL_MAP.get(poll_id)
    if not meta or meta.get("handled"):
        return

    session = GROUP_DATA.get(chat_id)
    if not session or not session.get("active"):
        POLL_MAP.pop(poll_id, None)
        return

    active = session["active"]
    if (
        active.get("run_id") != run_id
        or active.get("group_index") != group_index
        or active.get("current") != question_index
    ):
        POLL_MAP.pop(poll_id, None)
        return

    meta["handled"] = True

    answered_count = len(active.get("answered_users", set()))
    if answered_count == 0:
        active["empty_streak"] = active.get("empty_streak", 0) + 1
    else:
        active["empty_streak"] = 0

    active["current"] += 1
    active["current_poll_id"] = None
    active["current_poll_message_id"] = None
    POLL_MAP.pop(poll_id, None)

    if active["empty_streak"] >= GROUP_EMPTY_STOP_THRESHOLD:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"😴 Oxirgi {GROUP_EMPTY_STOP_THRESHOLD} ta savolga hech kim javob bermadi.\n"
                "Quiz avtomatik to‘xtatildi va hozirgacha bo‘lgan natija chiqariladi."
            ),
        )
        await finish_group_quiz(
            chat_id,
            context,
            completed_count=active["current"],
            stopped_reason="no_answers",
        )
        return

    await send_next_group_question(chat_id, context)


async def finish_group_quiz(chat_id: int, context: ContextTypes.DEFAULT_TYPE, completed_count: Optional[int] = None, stopped_reason: Optional[str] = None):
    session = GROUP_DATA.get(chat_id)
    if not session or not session.get("active"):
        return

    active = session["active"]
    total = completed_count if completed_count is not None else len(active["questions"])
    group_index = active["group_index"]
    timer_seconds = active["timer_seconds"]
    participants = active["participants"]

    ranking = []
    for user_id, p in participants.items():
        unanswered = max(0, total - p["correct"] - p["wrong"])
        percent = round((p["correct"] / total) * 100) if total else 0
        ranking.append(
            {
                "user_id": user_id,
                "name": p["name"],
                "correct": p["correct"],
                "wrong": p["wrong"],
                "unanswered": unanswered,
                "percent": percent,
                "best_streak": p["best_streak"],
            }
        )

    ranking.sort(
        key=lambda x: (
            -x["correct"],
            x["wrong"],
            x["unanswered"],
            x["name"].lower(),
        )
    )

    finish_title = "🛑 GURUH QUIZI TO‘XTATILDI" if stopped_reason else "🏆 GURUH QUIZI TUGADI"
    lines = [
        finish_title,
        "",
        f"📘 Bo‘lim: {group_index + 1}",
        f"❓ Savollar: {total}",
        f"👥 Qatnashchilar: {len(ranking)}",
    ]
    if stopped_reason == "manual":
        lines.append("🛑 Boshqaruvchi quizni to‘xtatdi.")
    elif stopped_reason == "no_answers":
        lines.append("😴 Faollik bo‘lmagani uchun avtomatik to‘xtadi.")
    lines.extend(["", "🏅 Reyting:"])

    medals = ["🥇", "🥈", "🥉"]
    for pos, row in enumerate(ranking[:20], start=1):
        medal = medals[pos - 1] if pos <= 3 else f"{pos}."
        lines.append(
            f"{medal} {row['name']} — {row['correct']}/{total} "
            f"({row['percent']}%) · 🔥 {row['best_streak']}"
        )

    if not ranking:
        lines.append("Hech kim javob bermadi.")

    leaderboard_text = "\n".join(lines)
    session["last_leaderboard_text"] = leaderboard_text
    session.setdefault("group_results", {})[group_index] = ranking
    session["active"] = None

    try:
        await context.bot.send_message(chat_id=chat_id, text="🎉")
    except Exception:
        pass

    buttons = [
        [
            InlineKeyboardButton(
                "🔄 Qayta boshlash",
                callback_data=f"gstart:{group_index}:{timer_seconds}",
            )
        ],
        [InlineKeyboardButton("📚 Bo‘limlar", callback_data="ggroups")],
        [InlineKeyboardButton("🏠 Guruh menyusi", callback_data="g_home")],
    ]

    if group_index + 1 < len(session.get("groups", [])):
        buttons.insert(
            0,
            [
                InlineKeyboardButton(
                    "➡️ Keyingi bo‘lim",
                    callback_data=f"ggroup:{group_index + 1}",
                )
            ],
        )

    await context.bot.send_message(
        chat_id=chat_id,
        text=leaderboard_text,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def stop_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group_chat(update.effective_chat):
        await update.message.reply_text("/stop faqat guruh quizida ishlaydi.")
        return

    session = GROUP_DATA.get(update.effective_chat.id)
    if not session or not session.get("active"):
        await update.message.reply_text("🛑 Hozir faol guruh quizi yo‘q.")
        return
    if update.effective_user.id != session.get("controller_id"):
        await update.message.reply_text("⛔ Quizni faqat uni boshlagan boshqaruvchi to‘xtata oladi.")
        return

    await update.message.reply_text(
        "⚠️ Quizni hozir to‘xtatamizmi? Hozirgacha bo‘lgan natija saqlanadi.",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🛑 Ha, to‘xtat", callback_data="gstop_yes")],
                [InlineKeyboardButton("← Bekor qilish", callback_data="gstop_no")],
            ]
        ),
    )


async def group_stop_no_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Davom etadi")
    await query.message.reply_text("▶️ Quiz davom etmoqda.")


async def group_stop_yes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session = GROUP_DATA.get(update.effective_chat.id)
    if not session or not session.get("active"):
        await query.answer("Faol quiz yo‘q", show_alert=True)
        return
    if update.effective_user.id != session.get("controller_id"):
        await query.answer("Faqat quiz boshqaruvchisi to‘xtata oladi.", show_alert=True)
        return
    await query.answer()

    active = session["active"]
    poll_id = active.get("current_poll_id")
    message_id = active.get("current_poll_message_id")

    if poll_id:
        meta = POLL_MAP.get(poll_id)
        if meta:
            meta["handled"] = True
        POLL_MAP.pop(poll_id, None)

    if message_id:
        try:
            await context.bot.stop_poll(update.effective_chat.id, message_id)
        except Exception:
            pass

    # The currently visible question counts as seen.
    completed = min(active["current"] + (1 if message_id else 0), len(active["questions"]))
    active["current_poll_id"] = None
    active["current_poll_message_id"] = None

    await finish_group_quiz(
        update.effective_chat.id,
        context,
        completed_count=completed,
        stopped_reason="manual",
    )


async def skip_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group_chat(update.effective_chat):
        await update.message.reply_text("/skip faqat guruh quizida ishlaydi.")
        return

    session = GROUP_DATA.get(update.effective_chat.id)
    if not session or not session.get("active"):
        await update.message.reply_text("⏭ Hozir faol guruh quizi yo‘q.")
        return
    if update.effective_user.id != session.get("controller_id"):
        await update.message.reply_text("⛔ Savolni faqat quiz boshqaruvchisi o‘tkaza oladi.")
        return

    active = session["active"]
    poll_id = active.get("current_poll_id")
    message_id = active.get("current_poll_message_id")
    if not poll_id or not message_id:
        await update.message.reply_text("⏭ Hozir o‘tkazib yuboriladigan savol yo‘q.")
        return

    meta = POLL_MAP.get(poll_id)
    if meta:
        meta["handled"] = True
    POLL_MAP.pop(poll_id, None)

    try:
        await context.bot.stop_poll(update.effective_chat.id, message_id)
    except Exception:
        pass

    active["current"] += 1
    active["empty_streak"] = 0
    active["current_poll_id"] = None
    active["current_poll_message_id"] = None

    await update.message.reply_text("⏭ Savol o‘tkazib yuborildi.")
    await send_next_group_question(update.effective_chat.id, context)


async def db_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = "✅ Database ulangan" if db.is_enabled() else "❌ Database ulanmagan"
    await update.message.reply_text(status)


async def app_post_init(application: Application):
    try:
        await db.init_pool()
    except Exception:
        logging.exception("Database initialization failed")


async def app_post_shutdown(application: Application):
    try:
        await db.close_pool()
    except Exception:
        logging.exception("Database shutdown failed")


async def retry_wrong_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    session = USER_DATA.get(user_id)

    if not session:
        await query.message.reply_text("❌ Sessiya topilmadi.")
        return

    _, group_text, timer_text = query.data.split(":")
    group_index = int(group_text)
    timer_seconds = int(timer_text)

    result = session.get("results", {}).get(group_index)
    problem_questions = (result or {}).get("problem_questions", [])

    if not problem_questions:
        await query.message.reply_text("✅ Mashq qilish uchun xato savollar qolmagan.")
        return

    session["run_counter"] = session.get("run_counter", 0) + 1
    run_id = session["run_counter"]

    session["active"] = {
        "run_id": run_id,
        "group_index": group_index,
        "questions": problem_questions,
        "current": 0,
        "correct": 0,
        "wrong": [],
        "unanswered": [],
        "answered_polls": set(),
        "timer_seconds": timer_seconds,
        "current_streak": 0,
        "best_streak": 0,
        "review_mode": True,
    }

    try:
        await context.bot.send_dice(chat_id=chat_id, emoji="🎯")
    except Exception:
        pass

    await query.message.reply_text(
        f"🧠 Xatolar mashqi boshlandi!\n"
        f"❓ {len(problem_questions)} ta savol\n"
        f"⏱ {format_duration(timer_seconds)}/savol"
    )
    await send_next_question(chat_id, user_id, context)


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN environment variable is missing.")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(app_post_init)
        .post_shutdown(app_post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new_command))
    app.add_handler(CommandHandler("quizzes", quizzes_command))
    app.add_handler(CommandHandler("continue", continue_command))
    app.add_handler(CommandHandler("progress", progress_command))
    app.add_handler(CommandHandler("group", group_mode_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stop", stop_group_command))
    app.add_handler(CommandHandler("skip", skip_group_command))
    app.add_handler(CommandHandler("dbstatus", db_status_command))
    app.add_handler(
        MessageHandler(
            filters.Document.PDF
            | filters.Document.FileExtension("docx"),
            handle_document,
        )
    )

    app.add_handler(CallbackQueryHandler(home_callback, pattern=r"^menu_home$"))
    app.add_handler(CallbackQueryHandler(group_home_callback, pattern=r"^g_home$"))
    app.add_handler(CallbackQueryHandler(group_saved_callback, pattern=r"^g_saved$"))
    app.add_handler(CallbackQueryHandler(private_load_saved_callback, pattern=r"^pload:\d+$"))
    app.add_handler(CallbackQueryHandler(group_load_saved_callback, pattern=r"^gload:\d+$"))
    app.add_handler(CallbackQueryHandler(group_stop_yes_callback, pattern=r"^gstop_yes$"))
    app.add_handler(CallbackQueryHandler(group_stop_no_callback, pattern=r"^gstop_no$"))
    app.add_handler(CallbackQueryHandler(group_new_callback, pattern=r"^g_new$"))
    app.add_handler(CallbackQueryHandler(group_current_callback, pattern=r"^g_current$"))
    app.add_handler(CallbackQueryHandler(group_leaderboard_callback, pattern=r"^g_leaderboard$"))
    app.add_handler(CallbackQueryHandler(group_help_callback, pattern=r"^g_help$"))
    app.add_handler(CallbackQueryHandler(new_callback, pattern=r"^menu_new$"))
    app.add_handler(CallbackQueryHandler(quizzes_callback, pattern=r"^menu_quizzes$"))
    app.add_handler(CallbackQueryHandler(continue_callback, pattern=r"^menu_continue$"))
    app.add_handler(CallbackQueryHandler(progress_callback, pattern=r"^menu_progress$"))
    app.add_handler(CallbackQueryHandler(group_mode_callback, pattern=r"^menu_group$"))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^menu_settings$"))
    app.add_handler(CallbackQueryHandler(help_callback, pattern=r"^menu_help$"))
    app.add_handler(CallbackQueryHandler(help_upload_callback, pattern=r"^help_upload$"))
    app.add_handler(CallbackQueryHandler(choose_size_callback, pattern=r"^size:\d+$"))
    app.add_handler(CallbackQueryHandler(group_choose_size_callback, pattern=r"^gsize:\d+$"))
    app.add_handler(CallbackQueryHandler(group_quiz_group_callback, pattern=r"^ggroup:\d+$"))
    app.add_handler(CallbackQueryHandler(group_groups_callback, pattern=r"^ggroups$"))
    app.add_handler(CallbackQueryHandler(group_timer_callback, pattern=r"^gtimer:\d+:(?:10|15|20|30|40|60|120)$"))
    app.add_handler(CallbackQueryHandler(group_start_callback, pattern=r"^gstart:\d+:(?:10|15|20|30|40|60|120)$"))
    app.add_handler(CallbackQueryHandler(retry_wrong_callback, pattern=r"^retrywrong:\d+:(?:10|15|20|30|40|60|120)$"))
    app.add_handler(CallbackQueryHandler(group_callback, pattern=r"^group:\d+$"))
    app.add_handler(CallbackQueryHandler(timer_callback, pattern=r"^timer:\d+:(?:10|15|20|30|40|60|120)$"))
    app.add_handler(CallbackQueryHandler(groups_callback, pattern=r"^groups$"))
    app.add_handler(CallbackQueryHandler(start_group_callback, pattern=r"^startgroup:\d+:(?:10|15|20|30|40|60|120)$"))
    app.add_handler(PollAnswerHandler(poll_answer_handler))

    logging.info("Exam Quiz prototype started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
