import re
from typing import Dict, Optional, Tuple


def parse_custom_range(value: str, total_questions: int) -> Optional[Tuple[int, int]]:
    """Parse 1-50, 1–50, 1:50 or '1 50' into inclusive 1-based bounds."""
    text = (value or "").strip().replace("–", "-").replace("—", "-")
    match = re.fullmatch(r"\s*(\d{1,5})\s*(?:-|:|\.\.|\s+)\s*(\d{1,5})\s*", text)
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    if start < 1 or end < start or end > int(total_questions):
        return None
    return start, end


def weak_bucket(attempts: int, correct: int, wrong: int, unanswered: int, last_result: str = "") -> Optional[str]:
    """Return a stable, explainable weakness bucket or None when the item is not weak."""
    attempts = max(0, int(attempts or 0))
    correct = max(0, int(correct or 0))
    wrong = max(0, int(wrong or 0))
    unanswered = max(0, int(unanswered or 0))
    if attempts <= 0 or (wrong + unanswered) <= 0:
        return None
    accuracy = correct / attempts
    # A recent failure remains review-worthy even when long-run accuracy is high.
    if accuracy >= 0.80 and (last_result or "") == "correct":
        return None
    if attempts >= 2 and accuracy <= 0.40:
        return "red"
    if accuracy <= 0.65:
        return "orange"
    return "yellow"


def weak_bucket_label(bucket: str) -> str:
    return {
        "red": "🔴 Juda qiyin",
        "orange": "🟠 Qiyin",
        "yellow": "🟡 Takrorlash kerak",
    }.get(bucket, "🟡 Takrorlash kerak")


def range_label(start: int, end: int) -> str:
    return f"{int(start)}–{int(end)}"

def parse_random_count(value: str, total_questions: int) -> Optional[int]:
    """Parse a requested random question count."""
    text = (value or "").strip()
    if not re.fullmatch(r"\d{1,5}", text):
        return None
    count = int(text)
    if count < 1 or count > int(total_questions):
        return None
    return count

