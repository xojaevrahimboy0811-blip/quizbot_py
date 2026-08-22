import os
import json
import logging
from typing import Optional, List, Dict, Any

try:
    import asyncpg
except ImportError:  # lets the bot still start if requirements were not updated yet
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

            CREATE INDEX IF NOT EXISTS idx_quizzes_owner
                ON quizzes(owner_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_questions_quiz
                ON questions(quiz_id, position);
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
                username = EXCLUDED.username,
                full_name = EXCLUDED.full_name,
                updated_at = NOW()
            """,
            user_id,
            username,
            full_name,
        )


async def save_quiz(
    owner_id: int,
    username: Optional[str],
    full_name: Optional[str],
    filename: str,
    questions: List[dict],
    display_name: Optional[str] = None,
) -> Optional[int]:
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

            await conn.executemany(
                """
                INSERT INTO questions
                    (quiz_id, position, source_number, question, options, correct_index)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                """,
                rows,
            )

    return int(quiz_id)


async def list_quizzes(owner_id: int, limit: int = 20) -> List[Dict[str, Any]]:
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
