import os
import re
import logging
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
QUESTION_RE = re.compile(r"^\s*(\d{1,4})\s*[\.\)\-:]\s*(.+?)\s*$")
OPTION_RE = re.compile(
    r"^\s*([A-Ha-h])\s*[\.\)\-:]\s*(.+?)\s*$"
)
INLINE_ANSWER_RE = re.compile(
    r"^\s*(?:answer|correct\s*answer|javob|to['’`ʻ]?g['’`ʻ]?ri\s*javob)\s*[:\-]?\s*([A-Ha-h])\b",
    re.IGNORECASE,
)
ANSWER_KEY_PAIR_RE = re.compile(
    r"(?<!\d)(\d{1,4})\s*[\.\)\-:]?\s*([A-Ha-h])\b"
)


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    text = text.replace("–", "-").replace("—", "-")
    # Split some common cases where PDF extraction joins items on one line.
    text = re.sub(r"\s+(?=\d{1,4}[\.\)]\s+)", "\n", text)
    text = re.sub(r"\s+(?=[A-Ha-h][\.\)]\s+)", "\n", text)
    return text


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

    candidate = text[start:] if start >= 0 else text

    # Avoid accidentally interpreting normal question lines as answer keys
    # unless the file contains a recognizable answer-key heading.
    if start < 0:
        return answers

    for num, letter in ANSWER_KEY_PAIR_RE.findall(candidate):
        answers[int(num)] = letter.upper()

    return answers


def parse_questions(text: str) -> Tuple[List[dict], List[str]]:
    """
    Parses common test formats:
      1. Question
      A) option
      B) option
      C) option
      D) option
      Answer: B

    or an answer key at the end:
      Javoblar:
      1-B 2-A 3-D ...
    """
    text = normalize_text(text)
    answer_key = parse_answer_key(text)
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    questions: List[dict] = []
    warnings: List[str] = []

    current: Optional[dict] = None
    current_option_letter: Optional[str] = None

    def save_current():
        nonlocal current
        if not current:
            return

        number = current["number"]
        options = current["options"]
        letter_to_index = current["letter_to_index"]

        answer_letter = current.get("answer_letter") or answer_key.get(number)
        correct_index = None
        if answer_letter:
            correct_index = letter_to_index.get(answer_letter.upper())

        # Only accept Telegram-compatible multiple-choice questions.
        if current["question"].strip() and 2 <= len(options) <= 10 and correct_index is not None:
            questions.append(
                {
                    "number": number,
                    "question": current["question"].strip(),
                    "options": [x.strip() for x in options],
                    "correct_index": correct_index,
                }
            )
        else:
            reason = []
            if not current["question"].strip():
                reason.append("question text missing")
            if not (2 <= len(options) <= 10):
                reason.append(f"{len(options)} options")
            if correct_index is None:
                reason.append("correct answer not found")
            warnings.append(f"Question {number}: " + ", ".join(reason))

        current = None

    for line in lines:
        q_match = QUESTION_RE.match(line)
        opt_match = OPTION_RE.match(line)
        ans_match = INLINE_ANSWER_RE.match(line)

        if q_match:
            # If this looks like an answer-key line (e.g. "1. B"), skip it.
            if len(q_match.group(2).strip()) == 1 and q_match.group(2).strip().upper() in "ABCDEFGH":
                continue

            save_current()
            current = {
                "number": int(q_match.group(1)),
                "question": q_match.group(2).strip(),
                "options": [],
                "letter_to_index": {},
                "answer_letter": None,
            }
            current_option_letter = None
            continue

        if current is None:
            continue

        if ans_match:
            current["answer_letter"] = ans_match.group(1).upper()
            current_option_letter = None
            continue

        if opt_match:
            letter = opt_match.group(1).upper()
            option_text = opt_match.group(2).strip()

            # Support a trailing * as a correct-answer marker:
            # A) Paris *
            is_marked_correct = option_text.endswith("*")
            if is_marked_correct:
                option_text = option_text[:-1].rstrip()
                current["answer_letter"] = letter

            current["letter_to_index"][letter] = len(current["options"])
            current["options"].append(option_text)
            current_option_letter = letter
            continue

        # Continuation lines:
        if current_option_letter and current["options"]:
            current["options"][-1] += " " + line
        else:
            current["question"] += " " + line

    save_current()
    return questions, warnings


# -----------------------------
# TELEGRAM UI
# -----------------------------
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📄 Upload PDF/DOCX", callback_data="help_upload")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Xush kelibsiz!\n\n"
        "PDF yoki Word (.docx) test faylini yuboring.\n"
        "Men savollarni topaman, keyin siz 30 / 40 / 50 / 100 savoldan guruhlarga ajratasiz.\n\n"
        "Quiz avtomatik boshlanmaydi — tayyor bo‘lgach, siz ▶️ Start tugmasini bosasiz.",
        reply_markup=main_menu(),
    )


async def help_upload_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("📎 Endi PDF yoki DOCX faylingizni shu chatga yuboring.")


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

        keyboard = [
            [
                InlineKeyboardButton("30", callback_data="size:30"),
                InlineKeyboardButton("40", callback_data="size:40"),
            ],
            [
                InlineKeyboardButton("50", callback_data="size:50"),
                InlineKeyboardButton("100", callback_data="size:100"),
            ],
        ]

        warning_text = ""
        if warnings:
            warning_text = f"\n⚠️ {len(warnings)} ta savol to‘liq aniqlanmadi va o‘tkazib yuborildi."

        await status.edit_text(
            f"✅ Fayl tahlil qilindi.\n\n"
            f"📄 {filename}\n"
            f"🧩 To‘liq aniqlangan savollar: {len(questions)}"
            f"{warning_text}\n\n"
            f"Har bir guruhda nechta savol bo‘lsin?",
            reply_markup=InlineKeyboardMarkup(keyboard),
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
            [InlineKeyboardButton("▶️ Start", callback_data=f"startgroup:{group_index}")],
            [InlineKeyboardButton("📚 Guruhlar", callback_data="groups")],
        ]
    )

    await query.message.reply_text(
        f"📘 {group_index + 1}-guruh\n"
        f"Savollar: {start_no}-{end_no}\n"
        f"Jami: {len(group)}"
        f"{previous_text}\n\n"
        f"Quiz hali boshlanmadi.",
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

    group_index = int(query.data.split(":")[1])
    if group_index < 0 or group_index >= len(session["groups"]):
        return

    session["active"] = {
        "group_index": group_index,
        "questions": session["groups"][group_index],
        "current": 0,
        "correct": 0,
        "wrong": [],
        "answered_polls": set(),
    }

    await query.message.reply_text(
        f"🚀 {group_index + 1}-guruh boshlandi!\n"
        f"Jami {len(session['active']['questions'])} ta savol."
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
    }


async def poll_answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    meta = POLL_MAP.get(answer.poll_id)
    if not meta:
        return

    # Only count the user who started this private quiz.
    if answer.user.id != meta["user_id"]:
        return

    user_id = meta["user_id"]
    session = USER_DATA.get(user_id)
    if not session or not session.get("active"):
        return

    active = session["active"]

    # Ignore stale answers from an older quiz/group.
    if active["group_index"] != meta["group_index"]:
        return

    # Prevent the same poll from being counted twice.
    if answer.poll_id in active["answered_polls"]:
        return
    active["answered_polls"].add(answer.poll_id)

    q_idx = meta["question_index"]
    if q_idx != active["current"]:
        return

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
    wrong = total - correct
    percent = round((correct / total) * 100) if total else 0
    group_index = active["group_index"]

    session["results"][group_index] = {
        "correct": correct,
        "wrong": wrong,
        "total": total,
        "percent": percent,
    }

    buttons = [
        [InlineKeyboardButton("🔄 Qayta ishlash", callback_data=f"startgroup:{group_index}")],
        [InlineKeyboardButton("📚 Guruhlar", callback_data="groups")],
    ]

    if group_index + 1 < len(session["groups"]):
        buttons.insert(
            0,
            [InlineKeyboardButton("➡️ Keyingi guruh", callback_data=f"group:{group_index + 1}")],
        )

    session["active"] = None

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🏁 {group_index + 1}-guruh tugadi!\n\n"
            f"✅ To‘g‘ri: {correct}\n"
            f"❌ Noto‘g‘ri: {wrong}\n"
            f"🎯 Natija: {percent}%"
        ),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN environment variable is missing.")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(
            filters.Document.PDF
            | filters.Document.FileExtension("docx"),
            handle_document,
        )
    )

    app.add_handler(CallbackQueryHandler(help_upload_callback, pattern=r"^help_upload$"))
    app.add_handler(CallbackQueryHandler(choose_size_callback, pattern=r"^size:\d+$"))
    app.add_handler(CallbackQueryHandler(group_callback, pattern=r"^group:\d+$"))
    app.add_handler(CallbackQueryHandler(groups_callback, pattern=r"^groups$"))
    app.add_handler(CallbackQueryHandler(start_group_callback, pattern=r"^startgroup:\d+$"))
    app.add_handler(PollAnswerHandler(poll_answer_handler))

    logging.info("Exam Quiz prototype started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

if __name__ == "__main__":
    main()

