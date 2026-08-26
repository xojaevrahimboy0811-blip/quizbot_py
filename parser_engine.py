import re
from io import BytesIO
from typing import Dict, List, Optional, Tuple

from docx import Document
from docx.document import Document as _Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from pypdf import PdfReader


OPTION_RE = re.compile(
    r"^\s*[•·▪◦]?\s*([+*✓✔✅☑]?)\s*([A-Ja-j])\s*[\.\)\-:]\s*(.*?)\s*$"
)
ANSWER_LINE_RE = re.compile(
    r"^\s*(?:answer|correct\s*answer|right\s*answer|javob|javobi|to['’`ʻ]?g['’`ʻ]?ri\s*javob|"
    r"ответ|правильный\s*ответ|верный\s*ответ)\s*[:\-]?\s*(.*?)\s*$",
    re.IGNORECASE,
)
ANSWER_KEY_PAIR_RE = re.compile(
    r"(?<!\d)(\d{1,5})\s*[\.\)\-:]?\s*([A-Ja-j])\b"
)
TRAILING_CORRECT_MARKER_RE = re.compile(r"\s*([*✓✔✅☑])\s*$")
INLINE_OPTION_RE = re.compile(r"(?<!\S)([A-Ja-j])\s*[\)\.\-:]\s*")


def normalize_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    text = text.replace("–", "-").replace("—", "-")
    return text


def extract_pdf_text(data: bytes) -> str:
    reader = PdfReader(BytesIO(data))
    parts: List[str] = []
    for page in reader.pages:
        text = ""
        try:
            # Layout mode often preserves teacher-made tests/tables better.
            text = page.extract_text(extraction_mode="layout") or ""
        except Exception:
            text = page.extract_text() or ""
        if text.strip():
            parts.append(text)
    return normalize_text("\n".join(parts))


def _iter_docx_blocks(parent):
    """Yield paragraphs/tables in their actual document order."""
    if isinstance(parent, _Document):
        parent_elm = parent.element.body
    else:
        parent_elm = parent._tc

    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def extract_docx_text(data: bytes) -> str:
    doc = Document(BytesIO(data))
    parts: List[str] = []

    for block in _iter_docx_blocks(doc):
        if isinstance(block, Paragraph):
            value = block.text.strip()
            if value:
                parts.append(value)
            continue

        # Tables are flattened cell-by-cell. This is more parser-friendly than
        # joining an entire row with pipes because question, A/B/C/D and answer
        # are frequently stored in separate cells.
        for row in block.rows:
            seen_cells = set()
            for cell in row.cells:
                # Merged Word cells can appear more than once in row.cells.
                key = id(cell._tc)
                if key in seen_cells:
                    continue
                seen_cells.add(key)
                for inner in _iter_docx_blocks(cell):
                    if isinstance(inner, Paragraph):
                        value = inner.text.strip()
                        if value:
                            parts.append(value)
                    elif isinstance(inner, Table):
                        for inner_row in inner.rows:
                            for inner_cell in inner_row.cells:
                                value = inner_cell.text.strip()
                                if value:
                                    parts.append(value)

    return normalize_text("\n".join(parts))


def _split_inline_options(line: str) -> List[str]:
    """
    Split only lines that visibly contain at least TWO option labels.
    Example: A) one  B) two  C) three  D) four
    Requiring two labels avoids splitting ordinary prose containing "A.".
    """
    matches = list(INLINE_OPTION_RE.finditer(line or ""))
    if len(matches) < 2:
        return [line]

    labels = [m.group(1).upper() for m in matches]
    # A single normal option can contain an author's initial such as
    # "B) I.Ten" or "D) A.Potebnya". Split only when the apparent
    # labels contain a consecutive option sequence (A/B, B/C, ...).
    has_consecutive = any(
        ord(labels[i + 1]) == ord(labels[i]) + 1
        for i in range(len(labels) - 1)
    )
    if not has_consecutive:
        return [line]

    pieces: List[str] = []
    prefix = line[:matches[0].start()].strip()
    if prefix:
        pieces.append(prefix)
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(line)
        segment = line[match.start():end].strip()
        if segment:
            pieces.append(segment)
    return pieces


def preprocess_lines(text: str) -> List[str]:
    result: List[str] = []
    for raw in normalize_text(text).split("\n"):
        stripped = raw.strip()
        if not stripped:
            continue
        result.extend(piece.strip() for piece in _split_inline_options(stripped) if piece.strip())
    return result


def parse_question_start(line: str) -> Optional[Tuple[int, str]]:
    s = (line or "").strip()

    # Savol 12: ..., Question 12 ..., Вопрос 12 ...
    m = re.match(
        r"^(?:savol|question|вопрос)\s*№?\s*(\d{1,5})\s*[\.\)\-:]?\s*(.*)$",
        s,
        re.IGNORECASE,
    )
    if m:
        return int(m.group(1)), m.group(2).strip()

    m = re.match(r"^№\s*(\d{1,5})\s*[\.\)\-:]?\s*(.*)$", s, re.IGNORECASE)
    if m:
        return int(m.group(1)), m.group(2).strip()

    m = re.match(r"^(\d{1,5})\s*([\.\)\-:])\s*(.*)$", s)
    if m:
        return int(m.group(1)), m.group(3).strip()

    m = re.match(r"^(\d{1,5})\s*$", s)
    if m:
        return int(m.group(1)), ""

    return None


def is_source_header(text: str) -> bool:
    cleaned = re.sub(r"^\s*#\s*", "", text or "").strip().lower()
    return cleaned.startswith(("manba", "source", "источник"))


def clean_question_line(line: str) -> str:
    s = (line or "").strip()
    if s in {"#", "=", "*", "+", "-"}:
        return ""
    return re.sub(r"^\s*#\s*", "", s).strip()


def _norm(value: str) -> str:
    value = normalize_text(value).lower()
    value = re.sub(r"^[\s\"'«»“”]+|[\s\"'«»“”]+$", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _answer_key_start(text: str) -> int:
    lower = (text or "").lower()
    anchors = [
        "answer key",
        "answers",
        "javoblar",
        "javob kaliti",
        "to'g'ri javoblar",
        "to‘g‘ri javoblar",
        "ответы",
        "ключ ответов",
        "правильные ответы",
    ]
    starts = [lower.rfind(anchor) for anchor in anchors]
    start = max(starts) if starts else -1
    # Treat it as a global answer-key section only when it is in the latter half.
    return start if start >= max(0, len(text) // 2) else -1


def parse_answer_key(text: str) -> Dict[int, str]:
    answers: Dict[int, str] = {}
    anchors = [
        "answer key",
        "answers",
        "javoblar",
        "javob kaliti",
        "to'g'ri javoblar",
        "to‘g‘ri javoblar",
        "ответы",
        "ключ ответов",
        "правильные ответы",
    ]

    start = _answer_key_start(text)
    if start < 0:
        return answers

    candidate = text[start:]
    for num, letter in ANSWER_KEY_PAIR_RE.findall(candidate):
        answers[int(num)] = letter.upper()
    return answers


def _extract_answer_letters(value: str) -> List[str]:
    # Explicit letter answers: B / B, D / (C)
    letters = [x.upper() for x in re.findall(r"\b([A-Ja-j])\b", value or "")]
    return list(dict.fromkeys(letters))


def parse_questions(text: str) -> Tuple[List[dict], List[str]]:
    """
    Flexible structural parsing with strict correct-answer handling.

    Supported flexibility includes:
      • №1 / 1. / 1) / Savol 1 / Question 1 / Вопрос 1
      • A) / A. / A: / A- and common leading correct markers
      • 2–10 answer choices
      • multi-line questions/options
      • several options on one physical line
      • Uzbek/English/Russian explicit answer lines
      • answer keys near the end of a document
      • exact answer-text lines such as "Javob: Toshkent"

    A correct answer is never inferred from knowledge.
    """
    text = normalize_text(text)
    answer_key = parse_answer_key(text)
    key_start = _answer_key_start(text)
    parse_body = text[:key_start] if key_start >= 0 else text
    lines = preprocess_lines(parse_body)

    questions: List[dict] = []
    warnings: List[str] = []
    current: Optional[dict] = None
    current_option_letter: Optional[str] = None

    def save_current():
        nonlocal current, current_option_letter
        if not current:
            return

        number = current["number"]
        options = [x.strip() for x in current["options"]]
        letter_to_index = current["letter_to_index"]
        answer_letters = list(current.get("answer_letters") or [])
        answer_text = (current.get("answer_text") or "").strip()

        if not answer_letters and not answer_text and number in answer_key:
            answer_letters = [answer_key[number]]

        correct_index = None
        if len(answer_letters) == 1:
            correct_index = letter_to_index.get(answer_letters[0])
        elif not answer_letters and answer_text:
            normalized_answer = _norm(answer_text)
            exact_matches = [i for i, option in enumerate(options) if _norm(option) == normalized_answer]
            if len(exact_matches) == 1:
                correct_index = exact_matches[0]

        reasons: List[str] = []
        if not current["question"].strip():
            reasons.append("question text missing")
        if not (2 <= len(options) <= 10):
            reasons.append(f"{len(options)} options")
        if any(not option for option in options):
            reasons.append("empty option")

        if len(answer_letters) == 0 and not answer_text:
            reasons.append("correct answer not found")
        elif len(answer_letters) > 1:
            reasons.append("multiple correct answers: " + ", ".join(answer_letters))
        elif correct_index is None:
            if answer_letters:
                reasons.append(f"answer {answer_letters[0]} has no matching option")
            else:
                reasons.append("answer text has no unique matching option")

        if not reasons:
            questions.append({
                "number": number,
                "question": current["question"].strip(),
                "options": options,
                "correct_index": int(correct_index),
            })
        else:
            warnings.append(f"Question {number}: " + ", ".join(reasons))

        current = None
        current_option_letter = None

    for line in lines:
        opt_match = OPTION_RE.match(line)
        ans_match = ANSWER_LINE_RE.match(line)
        q_start = parse_question_start(line)

        if q_start and not opt_match:
            number, tail = q_start
            # Do not interpret compact answer-key pairs such as "1. B" as questions.
            if len(tail) == 1 and tail.upper() in "ABCDEFGHIJ":
                continue
            save_current()
            current = {
                "number": number,
                "question": "" if is_source_header(tail) else clean_question_line(tail),
                "options": [],
                "letter_to_index": {},
                "answer_letters": [],
                "answer_text": "",
            }
            current_option_letter = None
            continue

        if current is None:
            continue

        if ans_match:
            raw_answer = ans_match.group(1).strip()
            letters = _extract_answer_letters(raw_answer)
            if letters:
                current["answer_letters"] = letters
            elif raw_answer and raw_answer not in {"-", "—", "–"}:
                # Exact option-text matching only; never semantic guessing.
                current["answer_text"] = raw_answer
            current_option_letter = None
            continue

        if opt_match:
            marker = opt_match.group(1)
            letter = opt_match.group(2).upper()
            option_text = opt_match.group(3).strip()

            if marker and letter not in current["answer_letters"]:
                current["answer_letters"].append(letter)

            trailing = TRAILING_CORRECT_MARKER_RE.search(option_text)
            if trailing:
                option_text = option_text[:trailing.start()].rstrip()
                if letter not in current["answer_letters"]:
                    current["answer_letters"].append(letter)

            current["letter_to_index"][letter] = len(current["options"])
            current["options"].append(option_text)
            current_option_letter = letter
            continue

        # Continuation text after an option belongs to that option. Otherwise it
        # continues the question text.
        if current_option_letter and current["options"]:
            addition = clean_question_line(line)
            if addition:
                if current["options"][-1]:
                    current["options"][-1] += " " + addition
                else:
                    current["options"][-1] = addition
        else:
            q_line = clean_question_line(line)
            if q_line:
                current["question"] = (current["question"] + " " + q_line).strip()

    save_current()
    return questions, warnings


def parse_highlighted_pdf_tables(data: bytes) -> Tuple[List[dict], List[str]]:
    """
    Parse PDF test tables whose correct answers are marked with real PDF
    Highlight annotations. Typical layout: № | Question | A | B | C | D.

    PyMuPDF may split highlighted cells into extra geometric fragments. To stay
    stable, logical table columns are reconstructed from the most frequently
    repeated x-boundaries, then text is clipped directly from those columns.
    """
    try:
        import fitz  # PyMuPDF
        from collections import Counter
    except Exception:
        return [], []

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return [], []

    questions: List[dict] = []
    warnings: List[str] = []

    def intersection_area(a, b) -> float:
        r = fitz.Rect(a) & fitz.Rect(b)
        if r.is_empty:
            return 0.0
        return max(0.0, r.width) * max(0.0, r.height)

    for page in doc:
        highlights = []
        annot = page.first_annot
        while annot:
            try:
                if annot.type[1].lower() == "highlight":
                    highlights.append(fitz.Rect(annot.rect))
            except Exception:
                pass
            annot = annot.next

        if not highlights:
            continue

        try:
            tables = list(page.find_tables().tables)
        except Exception:
            continue

        for table in tables:
            rows = list(table.rows or [])
            if not rows:
                continue

            edge_counts = Counter()
            for row in rows:
                for cell in list(row.cells or []):
                    if cell is None:
                        continue
                    rect = fitz.Rect(cell)
                    edge_counts[round(rect.x0, 1)] += 1
                    edge_counts[round(rect.x1, 1)] += 1

            # True table borders repeat across many rows. Highlight-induced
            # fragments occur in only one or two rows and are filtered out.
            threshold = max(2, len(rows) // 2)
            major_edges = sorted(x for x, count in edge_counts.items() if count >= threshold)
            if len(major_edges) < 5:
                continue

            # Most supported answer tables have: number, question, 2–10 options.
            # If more than 12 logical columns survived, keep the strongest borders.
            if len(major_edges) > 12:
                strongest = sorted(
                    edge_counts.items(), key=lambda item: (-item[1], item[0])
                )[:12]
                major_edges = sorted(x for x, _ in strongest)

            for row in rows:
                row_rect = fitz.Rect(row.bbox)
                values: List[str] = []
                logical_cells: List[fitz.Rect] = []
                for x0, x1 in zip(major_edges, major_edges[1:]):
                    rect = fitz.Rect(x0, row_rect.y0, x1, row_rect.y1)
                    logical_cells.append(rect)
                    text = page.get_text("text", clip=rect) or ""
                    values.append(re.sub(r"\s+", " ", text).strip())

                if len(values) < 4:
                    continue
                m = re.fullmatch(r"№?\s*(\d{1,5})", values[0] or "")
                if not m:
                    continue
                number = int(m.group(1))
                question = values[1].strip()
                options = [x.strip() for x in values[2:] if x.strip()]
                if not question or not (2 <= len(options) <= 10):
                    warnings.append(f"Question {number}: table structure incomplete")
                    continue

                option_cells = logical_cells[2:2 + len(options)]
                marked = set()
                for hi in highlights:
                    scores = [intersection_area(hi, cell) for cell in option_cells]
                    best = max(scores) if scores else 0.0
                    if best <= 0.5:
                        continue
                    marked.add(scores.index(best))

                if len(marked) == 1:
                    questions.append({
                        "number": number,
                        "question": question,
                        "options": options,
                        "correct_index": next(iter(marked)),
                    })
                elif not marked:
                    warnings.append(f"Question {number}: highlighted correct answer not found")
                else:
                    warnings.append(
                        f"Question {number}: multiple highlighted answers: "
                        + ", ".join(chr(65 + i) for i in sorted(marked))
                    )

    try:
        doc.close()
    except Exception:
        pass

    # PyMuPDF can occasionally expose overlapping table detections around a
    # highlighted row. Collapse only near-duplicate rows with the SAME printed
    # number; genuinely different duplicate-number questions are preserved.
    from difflib import SequenceMatcher
    deduped: List[dict] = []
    for item in questions:
        replacement_index = None
        for idx, existing in enumerate(deduped):
            if existing.get("number") != item.get("number"):
                continue
            a = _norm(existing.get("question", ""))
            b = _norm(item.get("question", ""))
            similarity = SequenceMatcher(None, a, b).ratio() if a and b else 0.0
            same_prefix = min(len(a), len(b)) >= 12 and (a.startswith(b) or b.startswith(a))
            if similarity >= 0.55 or same_prefix:
                replacement_index = idx
                break
        if replacement_index is None:
            deduped.append(item)
            continue
        old_item = deduped[replacement_index]
        old_score = len(old_item.get("question", "")) + sum(len(x) for x in old_item.get("options", []))
        new_score = len(item.get("question", "")) + sum(len(x) for x in item.get("options", []))
        if new_score > old_score:
            deduped[replacement_index] = item

    return deduped, warnings

