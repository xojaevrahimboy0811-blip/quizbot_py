import os
import re
import logging
import asyncio
from io import BytesIO
from math import ceil
from typing import List, Dict, Optional, Tuple

from docx import Document
from pypdf import PdfReader
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
POLL_MAP: Dict[str, dict] = {}

GROUP_SIZES = [30, 40, 50, 100]

# Telegram supports timed polls. These are the study choices shown before Start.
TIMER_CHOICES = [10, 15, 20, 30, 40, 60, 120]


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


def group_size_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("30", callback_data="size:30"),
                InlineKeyboardButton("40", callback_data="size:40"),
            ],
            [
                InlineKeyboardButton("50", callback_data="size:50"),
                InlineKeyboardButton("100", callback_data="size:100"),
            ],
            [InlineKeyboardButton("🏠 Bosh menyu", callback_data="menu_home")],
        ]
    )


async def send_home(message):
    await message.reply_text(
        "🎓 Exam Quiz Bot\n\n"
        "Kerakli bo‘limni tanlang:",
        reply_markup=main_menu(),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


async def show_current_quiz(message, user_id: int):
    session = USER_DATA.get(user_id)
    if not session:
        await message.reply_text(
            "📚 Hozircha yuklangan test yo‘q.\n\n"
            "Yangi PDF yoki DOCX yuboring.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📄 Yangi test", callback_data="menu_new")],
                    [InlineKeyboardButton("🏠 Bosh menyu", callback_data="menu_home")],
                ]
            ),
        )
        return

    filename = session.get("filename", "Test")
    total = len(session.get("questions", []))
    warnings = len(session.get("warnings", []))

    buttons = []
    if session.get("groups"):
        buttons.append([InlineKeyboardButton("📚 Guruhlarni ochish", callback_data="groups")])
    else:
        buttons.extend(group_size_keyboard().inline_keyboard[:-1])

    buttons.append([InlineKeyboardButton("🏠 Bosh menyu", callback_data="menu_home")])

    await message.reply_text(
        f"📚 Joriy test\n\n"
        f"📄 {filename}\n"
        f"✅ Quizga tayyor: {total}\n"
        f"⚠️ Muammoli: {warnings}\n\n"
        "Eslatma: doimiy saqlash bazasi hali qo‘shilmagan. "
        "Hozircha bu test Render qayta ishga tushmaguncha xotirada turadi.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def quizzes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_current_quiz(update.message, update.effective_user.id)


async def quizzes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await show_current_quiz(query.message, update.effective_user.id)


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


async def send_group_mode_info(message):
    await message.reply_text(
        "👥 Guruh testi\n\n"
        "Bu bo‘lim keyingi bosqichda qo‘shiladi: bir xil savol barcha qatnashchilarga "
        "beriladi, hamma tanlangan vaqt tugaguncha kutadi va yakunda reyting chiqadi.\n\n"
        "Hozir individual quiz rejimi ishlaydi.",
        reply_markup=home_button(),
    )


async def group_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_group_mode_info(update.message)


async def group_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await send_group_mode_info(query.message)


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

        USER_DATA[user_id] = {
            "chat_id": chat_id,
            "filename": filename,
            "questions": questions,
            "warnings": warnings,
            "group_size": None,
            "groups": [],
            "active": None,
            "results": {},
        }


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

        await status.edit_text(
            f"✅ Fayl tahlil qilindi.\n\n"
            f"📄 {filename}\n"
            f"🧩 Savol bloklari topildi: {total_blocks}\n"
            f"✅ Quizga tayyor: {len(questions)}"
            f"{warning_text}\n\n"
            f"Har bir guruhda nechta savol bo‘lsin?",
            reply_markup=group_size_keyboard(),
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
    }

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
    else:
        active["wrong"].append(q_idx)

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

    session["results"][group_index] = {
        "correct": correct,
        "wrong": wrong_answered,
        "unanswered": unanswered,
        "total": total,
        "percent": percent,
        "timer_seconds": timer_seconds,
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

    if group_index + 1 < len(session["groups"]):
        buttons.insert(
            0,
            [
                InlineKeyboardButton(
                    "➡️ Keyingi guruh",
                    callback_data=f"group:{group_index + 1}",
                )
            ],
        )

    session["active"] = None

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🏁 {group_index + 1}-guruh tugadi!\n\n"
            f"✅ To‘g‘ri: {correct}\n"
            f"❌ Noto‘g‘ri: {wrong_answered}\n"
            f"⏱ Javobsiz: {unanswered}\n"
            f"🎯 Natija: {percent}%\n"
            f"⏲ Vaqt: {format_duration(timer_seconds)}/savol"
        ),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN environment variable is missing.")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new_command))
    app.add_handler(CommandHandler("quizzes", quizzes_command))
    app.add_handler(CommandHandler("continue", continue_command))
    app.add_handler(CommandHandler("progress", progress_command))
    app.add_handler(CommandHandler("group", group_mode_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(
        MessageHandler(
            filters.Document.PDF
            | filters.Document.FileExtension("docx"),
            handle_document,
        )
    )

    app.add_handler(CallbackQueryHandler(home_callback, pattern=r"^menu_home$"))
    app.add_handler(CallbackQueryHandler(new_callback, pattern=r"^menu_new$"))
    app.add_handler(CallbackQueryHandler(quizzes_callback, pattern=r"^menu_quizzes$"))
    app.add_handler(CallbackQueryHandler(continue_callback, pattern=r"^menu_continue$"))
    app.add_handler(CallbackQueryHandler(progress_callback, pattern=r"^menu_progress$"))
    app.add_handler(CallbackQueryHandler(group_mode_callback, pattern=r"^menu_group$"))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern=r"^menu_settings$"))
    app.add_handler(CallbackQueryHandler(help_callback, pattern=r"^menu_help$"))
    app.add_handler(CallbackQueryHandler(help_upload_callback, pattern=r"^help_upload$"))
    app.add_handler(CallbackQueryHandler(choose_size_callback, pattern=r"^size:\d+$"))
    app.add_handler(CallbackQueryHandler(group_callback, pattern=r"^group:\d+$"))
    app.add_handler(CallbackQueryHandler(timer_callback, pattern=r"^timer:\d+:(?:10|15|20|30|40|60|120)$"))
    app.add_handler(CallbackQueryHandler(groups_callback, pattern=r"^groups$"))
    app.add_handler(CallbackQueryHandler(start_group_callback, pattern=r"^startgroup:\d+:(?:10|15|20|30|40|60|120)$"))
    app.add_handler(PollAnswerHandler(poll_answer_handler))

    logging.info("Exam Quiz prototype started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
