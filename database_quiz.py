import os
import json
import logging
from typing import Optional, List, Dict, Any, Tuple

try:
    import asyncpg
except ImportError:
    asyncpg = None

DATABASE_URL = os.environ.get("DATABASE_URL")
_POOL = None


async def init_pool() -> bool:
    global _POOL
    if not DATABASE_URL:
        logging.warning("DATABASE_URL is not set. Persistent quiz storage is disabled.")
        return False
    if asyncpg is None:
        logging.error("asyncpg is not installed. Add it to requirements.txt.")
        return False

    _POOL = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=60,
    )

    async with _POOL.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                plan TEXT NOT NULL DEFAULT 'free',
                pro_expires_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS quizzes (
                id BIGSERIAL PRIMARY KEY,
                owner_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                source_filename TEXT NOT NULL,
                question_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(owner_id, source_filename)
            );

            CREATE TABLE IF NOT EXISTS questions (
                id BIGSERIAL PRIMARY KEY,
                quiz_id BIGINT NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                source_number INTEGER,
                question TEXT NOT NULL,
                options JSONB NOT NULL,
                correct_index INTEGER NOT NULL,
                UNIQUE(quiz_id, position)
            );

            CREATE TABLE IF NOT EXISTS monthly_imports (
                user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                month_start DATE NOT NULL,
                import_count INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, month_start)
            );

            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id BIGINT PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
                shuffle_questions BOOLEAN NOT NULL DEFAULT FALSE,
                shuffle_options BOOLEAN NOT NULL DEFAULT FALSE,
                quiz_mode TEXT NOT NULL DEFAULT 'practice',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS ai_monthly_usage (
                user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                month_start DATE NOT NULL,
                import_count INTEGER NOT NULL DEFAULT 0,
                recovered_questions INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, month_start)
            );

            CREATE TABLE IF NOT EXISTS attempts (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                quiz_id BIGINT REFERENCES quizzes(id) ON DELETE SET NULL,
                mode TEXT NOT NULL DEFAULT 'private',
                chat_id BIGINT,
                section_index INTEGER,
                total INTEGER NOT NULL,
                correct INTEGER NOT NULL,
                wrong INTEGER NOT NULL,
                unanswered INTEGER NOT NULL DEFAULT 0,
                percent INTEGER NOT NULL,
                best_streak INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_quizzes_owner
                ON quizzes(owner_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_questions_quiz
                ON questions(quiz_id, position);
            CREATE INDEX IF NOT EXISTS idx_attempts_user
                ON attempts(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_attempts_quiz
                ON attempts(quiz_id, created_at DESC);
            """
        )
    logging.info("Persistent quiz database initialized.")
    return True


async def close_pool() -> None:
    global _POOL
    if _POOL is not None:
        await _POOL.close()
        _POOL = None


def is_enabled() -> bool:
    return _POOL is not None


async def ensure_user(user_id: int, username: Optional[str], full_name: Optional[str]) -> None:
    if not _POOL:
        return
    async with _POOL.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (telegram_id, username, full_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id) DO UPDATE SET
                username = COALESCE(EXCLUDED.username, users.username),
                full_name = COALESCE(EXCLUDED.full_name, users.full_name),
                updated_at = NOW()
            """,
            user_id,
            username,
            full_name,
        )


async def get_plan_status(user_id: int, free_limit: int = 1) -> Dict[str, Any]:
    if not _POOL:
        return {
            "plan": "free",
            "is_pro": False,
            "imports_used": 0,
            "imports_limit": free_limit,
            "imports_remaining": free_limit,
        }

    async with _POOL.acquire() as conn:
        user = await conn.fetchrow(
            """
            SELECT plan, pro_expires_at,
                   (plan = 'pro' AND (pro_expires_at IS NULL OR pro_expires_at > NOW())) AS is_pro
            FROM users
            WHERE telegram_id=$1
            """,
            user_id,
        )
        used = await conn.fetchval(
            """
            SELECT COALESCE(import_count, 0)
            FROM monthly_imports
            WHERE user_id=$1 AND month_start=date_trunc('month', CURRENT_DATE)::date
            """,
            user_id,
        )

    used = int(used or 0)
    is_pro = bool(user and user["is_pro"])
    plan = user["plan"] if user else "free"
    return {
        "plan": plan,
        "is_pro": is_pro,
        "imports_used": used,
        "imports_limit": None if is_pro else free_limit,
        "imports_remaining": None if is_pro else max(0, free_limit - used),
    }


async def quiz_exists_by_filename(owner_id: int, filename: str) -> bool:
    if not _POOL:
        return False
    async with _POOL.acquire() as conn:
        return bool(
            await conn.fetchval(
                "SELECT 1 FROM quizzes WHERE owner_id=$1 AND source_filename=$2",
                owner_id,
                filename,
            )
        )


async def can_import_new_quiz(owner_id: int, filename: str, free_limit: int = 1) -> Tuple[bool, str]:
    """Allow replacing an already-saved filename without consuming a new monthly slot."""
    if not _POOL:
        return True, "database_disabled"

    if await quiz_exists_by_filename(owner_id, filename):
        return True, "existing_quiz_update"

    status = await get_plan_status(owner_id, free_limit=free_limit)
    if status["is_pro"]:
        return True, "pro"
    if status["imports_used"] < free_limit:
        return True, "free_slot"
    return False, "monthly_limit"


async def record_new_import(user_id: int) -> None:
    if not _POOL:
        return
    async with _POOL.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO monthly_imports (user_id, month_start, import_count)
            VALUES ($1, date_trunc('month', CURRENT_DATE)::date, 1)
            ON CONFLICT (user_id, month_start) DO UPDATE SET
                import_count = monthly_imports.import_count + 1,
                updated_at = NOW()
            """,
            user_id,
        )


async def save_quiz(
    owner_id: int,
    username: Optional[str],
    full_name: Optional[str],
    filename: str,
    questions: List[dict],
    display_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not _POOL:
        return None

    await ensure_user(owner_id, username, full_name)
    name = (display_name or filename.rsplit(".", 1)[0]).strip() or "Quiz"

    async with _POOL.acquire() as conn:
        async with conn.transaction():
            quiz_id = await conn.fetchval(
                "SELECT id FROM quizzes WHERE owner_id=$1 AND source_filename=$2",
                owner_id,
                filename,
            )
            created_new = quiz_id is None

            if quiz_id:
                await conn.execute(
                    """
                    UPDATE quizzes
                    SET name=$1, question_count=$2, updated_at=NOW()
                    WHERE id=$3
                    """,
                    name,
                    len(questions),
                    quiz_id,
                )
                await conn.execute("DELETE FROM questions WHERE quiz_id=$1", quiz_id)
            else:
                quiz_id = await conn.fetchval(
                    """
                    INSERT INTO quizzes (owner_id, name, source_filename, question_count)
                    VALUES ($1, $2, $3, $4)
                    RETURNING id
                    """,
                    owner_id,
                    name,
                    filename,
                    len(questions),
                )

            rows = []
            for pos, item in enumerate(questions, start=1):
                rows.append(
                    (
                        quiz_id,
                        pos,
                        item.get("number"),
                        item["question"],
                        json.dumps(item["options"], ensure_ascii=False),
                        int(item["correct_index"]),
                    )
                )

            if rows:
                await conn.executemany(
                    """
                    INSERT INTO questions
                        (quiz_id, position, source_number, question, options, correct_index)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                    """,
                    rows,
                )

    if created_new:
        await record_new_import(owner_id)

    return {"quiz_id": int(quiz_id), "created_new": bool(created_new)}


async def list_quizzes(owner_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    if not _POOL:
        return []
    async with _POOL.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, source_filename, question_count, created_at, updated_at
            FROM quizzes
            WHERE owner_id=$1
            ORDER BY updated_at DESC
            LIMIT $2
            """,
            owner_id,
            limit,
        )
    return [dict(r) for r in rows]


async def load_quiz(owner_id: int, quiz_id: int) -> Optional[Dict[str, Any]]:
    if not _POOL:
        return None

    async with _POOL.acquire() as conn:
        quiz = await conn.fetchrow(
            """
            SELECT id, name, source_filename, question_count, created_at, updated_at
            FROM quizzes
            WHERE id=$1 AND owner_id=$2
            """,
            quiz_id,
            owner_id,
        )
        if not quiz:
            return None

        qrows = await conn.fetch(
            """
            SELECT position, source_number, question, options, correct_index
            FROM questions
            WHERE quiz_id=$1
            ORDER BY position
            """,
            quiz_id,
        )

    questions = []
    for row in qrows:
        opts = row["options"]
        if isinstance(opts, str):
            opts = json.loads(opts)
        questions.append(
            {
                "number": row["source_number"] or row["position"],
                "question": row["question"],
                "options": list(opts),
                "correct_index": int(row["correct_index"]),
            }
        )

    result = dict(quiz)
    result["questions"] = questions
    return result


async def delete_quiz(owner_id: int, quiz_id: int) -> bool:
    if not _POOL:
        return False
    async with _POOL.acquire() as conn:
        status = await conn.execute(
            "DELETE FROM quizzes WHERE id=$1 AND owner_id=$2",
            quiz_id,
            owner_id,
        )
    return status.endswith("1")


async def save_attempt(
    user_id: int,
    username: Optional[str],
    full_name: Optional[str],
    quiz_id: Optional[int],
    mode: str,
    chat_id: Optional[int],
    section_index: Optional[int],
    total: int,
    correct: int,
    wrong: int,
    unanswered: int,
    percent: int,
    best_streak: int,
) -> Optional[int]:
    if not _POOL:
        return None

    await ensure_user(user_id, username, full_name)
    async with _POOL.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO attempts
                (user_id, quiz_id, mode, chat_id, section_index,
                 total, correct, wrong, unanswered, percent, best_streak)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            RETURNING id
            """,
            user_id,
            quiz_id,
            mode,
            chat_id,
            section_index,
            total,
            correct,
            wrong,
            unanswered,
            percent,
            best_streak,
        )


async def list_recent_attempts(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    if not _POOL:
        return []
    async with _POOL.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.id, a.mode, a.section_index, a.total, a.correct, a.wrong,
                   a.unanswered, a.percent, a.best_streak, a.created_at,
                   q.name AS quiz_name
            FROM attempts a
            LEFT JOIN quizzes q ON q.id=a.quiz_id
            WHERE a.user_id=$1
            ORDER BY a.created_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )
    return [dict(r) for r in rows]


DEFAULT_PREFERENCES = {
    "shuffle_questions": False,
    "shuffle_options": False,
    "quiz_mode": "practice",
}


async def get_user_preferences(user_id: int) -> Dict[str, Any]:
    if not _POOL:
        return dict(DEFAULT_PREFERENCES)
    async with _POOL.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT shuffle_questions, shuffle_options, quiz_mode
            FROM user_preferences
            WHERE user_id=$1
            """,
            user_id,
        )
    if not row:
        return dict(DEFAULT_PREFERENCES)
    result = dict(DEFAULT_PREFERENCES)
    result.update(dict(row))
    if result.get("quiz_mode") not in ("practice", "exam"):
        result["quiz_mode"] = "practice"
    return result


async def update_user_preferences(
    user_id: int,
    username: Optional[str] = None,
    full_name: Optional[str] = None,
    **changes,
) -> Dict[str, Any]:
    if not _POOL:
        result = dict(DEFAULT_PREFERENCES)
        result.update(changes)
        return result

    await ensure_user(user_id, username, full_name)
    current = await get_user_preferences(user_id)
    current.update(changes)
    mode = current.get("quiz_mode", "practice")
    if mode not in ("practice", "exam"):
        mode = "practice"

    async with _POOL.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_preferences
                (user_id, shuffle_questions, shuffle_options, quiz_mode)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (user_id) DO UPDATE SET
                shuffle_questions=EXCLUDED.shuffle_questions,
                shuffle_options=EXCLUDED.shuffle_options,
                quiz_mode=EXCLUDED.quiz_mode,
                updated_at=NOW()
            """,
            user_id,
            bool(current.get("shuffle_questions", False)),
            bool(current.get("shuffle_options", False)),
            mode,
        )
    return {
        "shuffle_questions": bool(current.get("shuffle_questions", False)),
        "shuffle_options": bool(current.get("shuffle_options", False)),
        "quiz_mode": mode,
    }


async def rename_quiz(owner_id: int, quiz_id: int, new_name: str) -> bool:
    if not _POOL:
        return False
    new_name = (new_name or "").strip()
    if not new_name:
        return False
    async with _POOL.acquire() as conn:
        status = await conn.execute(
            """
            UPDATE quizzes SET name=$1, updated_at=NOW()
            WHERE id=$2 AND owner_id=$3
            """,
            new_name[:120], quiz_id, owner_id,
        )
    return status.endswith("1")


async def list_quiz_attempts(owner_id: int, quiz_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    if not _POOL:
        return []
    async with _POOL.acquire() as conn:
        allowed = await conn.fetchval(
            "SELECT 1 FROM quizzes WHERE id=$1 AND owner_id=$2",
            quiz_id, owner_id,
        )
        if not allowed:
            return []
        rows = await conn.fetch(
            """
            SELECT id, mode, section_index, total, correct, wrong,
                   unanswered, percent, best_streak, created_at
            FROM attempts
            WHERE user_id=$1 AND quiz_id=$2
            ORDER BY created_at DESC
            LIMIT $3
            """,
            owner_id, quiz_id, limit,
        )
    return [dict(r) for r in rows]
async def get_ai_usage(user_id: int) -> Dict[str, int]:
    if not _POOL:
        return {"imports_used": 0, "recovered_questions": 0}

    async with _POOL.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT import_count, recovered_questions
            FROM ai_monthly_usage
            WHERE user_id=$1
              AND month_start=date_trunc('month', CURRENT_DATE)::date
            """,
            user_id,
        )

    if not row:
        return {"imports_used": 0, "recovered_questions": 0}
    return {
        "imports_used": int(row["import_count"] or 0),
        "recovered_questions": int(row["recovered_questions"] or 0),
    }


async def can_use_ai_import(user_id: int, monthly_limit: int) -> Tuple[bool, Dict[str, int]]:
    usage = await get_ai_usage(user_id)
    return usage["imports_used"] < monthly_limit, usage


async def record_ai_import(user_id: int, recovered_questions: int) -> None:
    if not _POOL:
        return

    async with _POOL.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ai_monthly_usage
                (user_id, month_start, import_count, recovered_questions)
            VALUES (
                $1,
                date_trunc('month', CURRENT_DATE)::date,
                1,
                $2
            )
            ON CONFLICT (user_id, month_start) DO UPDATE SET
                import_count = ai_monthly_usage.import_count + 1,
                recovered_questions = ai_monthly_usage.recovered_questions + EXCLUDED.recovered_questions,
                updated_at = NOW()
            """,
            user_id,
            max(0, int(recovered_questions)),
        )


async def grant_pro(
    user_id: int,
    days: int,
    username: Optional[str] = None,
    full_name: Optional[str] = None,
) -> None:
    """Grant/extend Pro from now for manual testing/activation."""
    if not _POOL:
        raise RuntimeError("Database is disabled")

    days = max(1, int(days))
    await ensure_user(user_id, username, full_name)

    async with _POOL.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET plan='pro',
                pro_expires_at=GREATEST(
                    COALESCE(pro_expires_at, NOW()),
                    NOW()
                ) + ($2::text || ' days')::interval,
                updated_at=NOW()
            WHERE telegram_id=$1
            """,
            user_id,
            days,
        )


async def revoke_pro(user_id: int) -> None:
    if not _POOL:
        raise RuntimeError("Database is disabled")

    async with _POOL.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET plan='free',
                pro_expires_at=NULL,
                updated_at=NOW()
            WHERE telegram_id=$1
            """,
            user_id,
        )
