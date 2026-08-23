import os
import re
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx


GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()
GEMINI_MODEL = (os.environ.get("GEMINI_MODEL") or "").strip()
AI_CHUNK_CHARS = int(os.environ.get("AI_CHUNK_CHARS", "12000"))
AI_TIMEOUT_SECONDS = float(os.environ.get("AI_TIMEOUT_SECONDS", "70"))

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

# These are tried only when GEMINI_MODEL is not explicitly configured.
# If both ever disappear, the module can discover another generateContent-capable
# Gemini model automatically from the API, so the bot file does not need editing.
DEFAULT_MODEL_CANDIDATES = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]

QUESTION_START_RE = re.compile(
    r"(?m)^\s*(?:№\s*)?(\d{1,5})\s*(?:[\.\)\-:]|\s)\s*(.*?)\s*$"
)
WARNING_NUMBER_RE = re.compile(r"Question\s+(\d+)\s*:", re.IGNORECASE)


class AIParserError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(GEMINI_API_KEY)


def _normalize_for_match(value: str) -> str:
    value = (value or "").replace("\u00a0", " ")
    value = value.replace("–", "-").replace("—", "-")
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


def _question_fingerprint(question: Dict[str, Any]) -> str:
    text = _normalize_for_match(question.get("question", ""))
    opts = "|".join(_normalize_for_match(x) for x in question.get("options", []))
    return f"{text}::{opts}"


def merge_questions(existing: List[dict], recovered: List[dict]) -> List[dict]:
    """Merge without overwriting deterministic-parser questions."""
    merged = list(existing)
    seen = {_question_fingerprint(q) for q in existing}
    for q in recovered:
        fp = _question_fingerprint(q)
        if fp and fp not in seen:
            merged.append(q)
            seen.add(fp)
    return merged


def _warning_numbers(warnings: List[str]) -> List[int]:
    nums: List[int] = []
    for warning in warnings:
        match = WARNING_NUMBER_RE.search(warning or "")
        if match:
            nums.append(int(match.group(1)))
    return nums


def _extract_numbered_blocks(text: str) -> List[Tuple[Optional[int], str]]:
    """
    Best-effort block extraction for unusual but still numbered tests.
    This is intentionally broader than the deterministic quiz parser.
    """
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    starts: List[Tuple[int, int]] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        # Match styles such as:
        # 1. ...
        # 1) ...
        # №1 ...
        # № 1. ...
        m = re.match(r"^\s*№\s*(\d{1,5})\s*[\.\)\-:]?\s*(.*)$", stripped, re.I)
        if not m:
            m = re.match(r"^\s*(\d{1,5})\s*[\.\)\-:]\s*(.*)$", stripped)
        if m:
            starts.append((i, int(m.group(1))))

    blocks: List[Tuple[Optional[int], str]] = []
    for pos, (start_i, number) in enumerate(starts):
        end_i = starts[pos + 1][0] if pos + 1 < len(starts) else len(lines)
        block = "\n".join(lines[start_i:end_i]).strip()
        if block:
            blocks.append((number, block))
    return blocks


def _select_recovery_text(
    text: str,
    warnings: List[str],
    existing_questions: List[dict],
) -> Tuple[str, str]:
    """
    Prefer sending only deterministic-parser problem blocks.
    Fall back to the complete extracted document only when block targeting
    is impossible or when the normal parser found nothing at all.
    """
    if not existing_questions:
        return text, "full_document"

    targets = set(_warning_numbers(warnings))
    if not targets:
        # There are no explicit unresolved blocks to target.
        return "", "nothing_to_recover"

    numbered = _extract_numbered_blocks(text)
    selected = [block for number, block in numbered if number in targets]

    if selected:
        return "\n\n".join(selected), "unresolved_blocks"

    # We know something was unresolved but could not isolate it safely.
    return text, "full_document_fallback"


def _split_text(text: str, max_chars: int = AI_CHUNK_CHARS) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []

    # Split first around apparent question starts to reduce the chance of cutting
    # a question in half.
    blocks = _extract_numbered_blocks(text)
    if blocks:
        pieces = [block for _, block in blocks]
    else:
        # Paragraph fallback for very unusual structures.
        pieces = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]

    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    def flush():
        nonlocal current, current_len
        if current:
            chunks.append("\n\n".join(current).strip())
            current = []
            current_len = 0

    for piece in pieces:
        if len(piece) > max_chars:
            flush()
            # Last-resort split for a huge single block.
            for start in range(0, len(piece), max_chars):
                part = piece[start:start + max_chars].strip()
                if part:
                    chunks.append(part)
            continue

        projected = current_len + len(piece) + (2 if current else 0)
        if current and projected > max_chars:
            flush()

        current.append(piece)
        current_len += len(piece) + (2 if len(current) > 1 else 0)

    flush()
    return chunks


def _extract_json_text(raw_text: str) -> str:
    value = (raw_text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    first = value.find("{")
    last = value.rfind("}")
    if first >= 0 and last > first:
        return value[first:last + 1]
    return value


def _build_prompt(chunk: str) -> str:
    return f"""
You are a STRICT TEST-STRUCTURE EXTRACTOR.

TASK:
Extract multiple-choice test questions from the SOURCE below.

CRITICAL RULES:
1. NEVER solve a question using your own knowledge.
2. NEVER infer or invent a correct answer.
3. A correct answer is allowed ONLY when the SOURCE explicitly identifies it
   using a marker, answer line, answer key, highlight represented in extracted
   text, star/check/plus marker, or another explicit textual indication.
4. If the source does not explicitly prove the correct answer, use:
   "status": "unknown", "correct_label": null, "answer_evidence": null
5. If more than one answer is explicitly marked and it is not clearly a
   multi-select question, use:
   "status": "ambiguous", "correct_label": null
6. Preserve the question and option wording. Only normalize whitespace and
   remove numbering/option-label punctuation from visible text.
7. "answer_evidence" MUST be a SHORT, EXACT substring copied from SOURCE that
   proves the marked answer (examples: "Javob: B", "+B)", "Answer: C").
   Do not paraphrase evidence.
8. Do not return explanatory prose outside JSON.

RETURN ONLY THIS JSON SHAPE:
{{
  "questions": [
    {{
      "source_number": 1,
      "question": "question text",
      "options": [
        {{"label": "A", "text": "option text"}},
        {{"label": "B", "text": "option text"}}
      ],
      "correct_label": "B",
      "answer_evidence": "Javob: B",
      "status": "ready"
    }}
  ]
}}

Allowed status values: "ready", "unknown", "ambiguous".

SOURCE:
---BEGIN SOURCE---
{chunk}
---END SOURCE---
""".strip()


async def _list_generate_models(client: httpx.AsyncClient) -> List[str]:
    if not GEMINI_API_KEY:
        return []

    try:
        response = await client.get(
            f"{API_ROOT}/models",
            params={"key": GEMINI_API_KEY},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        logging.exception("Gemini model discovery failed")
        return []

    candidates: List[str] = []
    for item in payload.get("models", []):
        methods = item.get("supportedGenerationMethods") or []
        name = (item.get("name") or "").replace("models/", "")
        lname = name.lower()

        if "generateContent" not in methods or not name:
            continue
        if any(bad in lname for bad in ("embedding", "image", "live", "tts")):
            continue
        candidates.append(name)

    # Prefer Flash-family text models.
    candidates.sort(key=lambda x: (0 if "flash" in x.lower() else 1, x))
    return candidates


async def _candidate_models(client: httpx.AsyncClient) -> List[str]:
    if GEMINI_MODEL:
        return [GEMINI_MODEL]

    result: List[str] = []
    for item in DEFAULT_MODEL_CANDIDATES:
        if item not in result:
            result.append(item)

    for item in await _list_generate_models(client):
        if item not in result:
            result.append(item)

    return result


async def _call_model(
    client: httpx.AsyncClient,
    model: str,
    chunk: str,
) -> Dict[str, Any]:
    url = f"{API_ROOT}/models/{model}:generateContent"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": _build_prompt(chunk)}],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }

    response = await client.post(
        url,
        params={"key": GEMINI_API_KEY},
        json=payload,
    )

    # Let model selection retry on missing/retired model.
    if response.status_code in (400, 404):
        raise AIParserError(f"MODEL_UNAVAILABLE:{model}:{response.status_code}")

    if response.status_code == 429:
        raise AIParserError("AI_QUOTA")

    if response.status_code >= 500:
        raise AIParserError(f"AI_PROVIDER_{response.status_code}")

    response.raise_for_status()
    data = response.json()

    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts)
    except Exception as exc:
        raise AIParserError("AI_EMPTY_RESPONSE") from exc

    try:
        return json.loads(_extract_json_text(text))
    except Exception as exc:
        logging.warning("Gemini returned invalid JSON: %s", text[:1000])
        raise AIParserError("AI_INVALID_JSON") from exc


def _validate_item(item: Dict[str, Any], source_chunk: str) -> Tuple[Optional[dict], Optional[str]]:
    number = item.get("source_number")
    try:
        if number is not None:
            number = int(number)
    except Exception:
        number = None

    question = str(item.get("question") or "").strip()
    raw_options = item.get("options") or []
    status = str(item.get("status") or "").strip().lower()
    correct_label = item.get("correct_label")
    evidence = item.get("answer_evidence")

    if not question:
        return None, f"AI: savol matni topilmadi{f' (№{number})' if number else ''}"

    if not isinstance(raw_options, list) or not (2 <= len(raw_options) <= 10):
        return None, f"AI: variantlar soni noto‘g‘ri{f' (№{number})' if number else ''}"

    labels: List[str] = []
    options: List[str] = []

    for opt in raw_options:
        if not isinstance(opt, dict):
            return None, f"AI: variant tuzilishi noto‘g‘ri{f' (№{number})' if number else ''}"

        label = str(opt.get("label") or "").strip().upper()
        text = str(opt.get("text") or "").strip()

        if not label or len(label) > 3 or not text:
            return None, f"AI: variant ma’lumoti yetarli emas{f' (№{number})' if number else ''}"
        if label in labels:
            return None, f"AI: variant harfi takrorlangan{f' (№{number})' if number else ''}"

        labels.append(label)
        options.append(text)

    if status != "ready":
        return None, (
            f"AI: to‘g‘ri javob aniqlanmadi"
            f"{f' (№{number})' if number else ''}"
        )

    correct_label = str(correct_label or "").strip().upper()
    if correct_label not in labels:
        return None, f"AI: to‘g‘ri javob varianti mos emas{f' (№{number})' if number else ''}"

    # Strong hallucination guard:
    # The model must quote exact source evidence for the answer.
    evidence = str(evidence or "").strip()
    if not evidence:
        return None, f"AI: javob dalili yo‘q{f' (№{number})' if number else ''}"

    if _normalize_for_match(evidence) not in _normalize_for_match(source_chunk):
        return None, f"AI: javob dalili manbada tasdiqlanmadi{f' (№{number})' if number else ''}"

    return {
        "number": number,
        "question": question,
        "options": options,
        "correct_index": labels.index(correct_label),
    }, None


async def recover_questions(
    text: str,
    existing_questions: Optional[List[dict]] = None,
    parser_warnings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Recover questions the deterministic parser could not safely use.

    Result:
      questions: validated recovered ready questions
      warnings: unresolved AI findings
      source_mode: unresolved_blocks/full_document/...
      model: Gemini model used
      batches: number of successful AI calls
      ai_called: whether a provider request was made
    """
    existing_questions = existing_questions or []
    parser_warnings = parser_warnings or []

    if not GEMINI_API_KEY:
        raise AIParserError("AI_NOT_CONFIGURED")

    recovery_text, source_mode = _select_recovery_text(
        text,
        parser_warnings,
        existing_questions,
    )
    if not recovery_text.strip():
        return {
            "questions": [],
            "warnings": [],
            "source_mode": source_mode,
            "model": None,
            "batches": 0,
            "ai_called": False,
        }

    chunks = _split_text(recovery_text)
    if not chunks:
        return {
            "questions": [],
            "warnings": ["AI: tahlil qilinadigan matn topilmadi"],
            "source_mode": source_mode,
            "model": None,
            "batches": 0,
            "ai_called": False,
        }

    timeout = httpx.Timeout(AI_TIMEOUT_SECONDS)
    recovered: List[dict] = []
    unresolved: List[str] = []
    model_used: Optional[str] = None
    successful_batches = 0
    ai_called = False

    async with httpx.AsyncClient(timeout=timeout) as client:
        models = await _candidate_models(client)
        if not models:
            raise AIParserError("AI_MODEL_NOT_FOUND")

        active_model: Optional[str] = None

        for chunk_index, chunk in enumerate(chunks, start=1):
            payload: Optional[Dict[str, Any]] = None
            last_error: Optional[Exception] = None

            # Once one model succeeds, keep using it for later chunks.
            try_models = [active_model] if active_model else models

            for model in [m for m in try_models if m]:
                try:
                    ai_called = True
                    payload = await _call_model(client, model, chunk)
                    active_model = model
                    model_used = model
                    break
                except AIParserError as exc:
                    last_error = exc
                    if str(exc).startswith("MODEL_UNAVAILABLE:"):
                        continue
                    raise

            if payload is None and not active_model:
                # Candidate defaults may have become stale; try discovered models
                # not already attempted.
                discovered = await _list_generate_models(client)
                for model in discovered:
                    if model in models:
                        continue
                    try:
                        ai_called = True
                        payload = await _call_model(client, model, chunk)
                        active_model = model
                        model_used = model
                        break
                    except AIParserError as exc:
                        last_error = exc
                        if str(exc).startswith("MODEL_UNAVAILABLE:"):
                            continue
                        raise

            if payload is None:
                if last_error:
                    raise last_error
                raise AIParserError("AI_MODEL_NOT_FOUND")

            successful_batches += 1
            raw_questions = payload.get("questions")
            if not isinstance(raw_questions, list):
                unresolved.append(f"AI: {chunk_index}-bo‘lakdan savol ro‘yxati olinmadi")
                continue

            for item in raw_questions:
                if not isinstance(item, dict):
                    unresolved.append("AI: noto‘g‘ri savol tuzilishi")
                    continue
                ready, warning = _validate_item(item, chunk)
                if ready:
                    recovered.append(ready)
                elif warning:
                    unresolved.append(warning)

    # Remove duplicates created by chunk overlap or repeated source numbering.
    unique: List[dict] = []
    seen = set()
    for item in recovered:
        fp = _question_fingerprint(item)
        if fp not in seen:
            unique.append(item)
            seen.add(fp)

    return {
        "questions": unique,
        "warnings": unresolved,
        "source_mode": source_mode,
        "model": model_used,
        "batches": successful_batches,
        "ai_called": ai_called,
    }
