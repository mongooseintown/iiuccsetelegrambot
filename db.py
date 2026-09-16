"""
Database layer for Telegram Course Join Bot.
Uses aiosqlite with WAL mode and foreign keys enabled.
"""

import os
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any, AsyncGenerator

import aiosqlite

DEFAULT_DB_PATH = os.getenv("DB_PATH", "data/bot.db")


@asynccontextmanager
async def get_db(db_path: Optional[str] = None) -> AsyncGenerator[aiosqlite.Connection, None]:
    path = db_path or DEFAULT_DB_PATH
    db_dir = os.path.dirname(path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA foreign_keys=ON;")
        yield db


async def init_db(db_path: Optional[str] = None) -> None:
    """Initialize SQLite database tables and indexes."""
    async with get_db(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS semesters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS courses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                semester_id INTEGER NOT NULL REFERENCES semesters(id) ON DELETE CASCADE,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                chat_id INTEGER UNIQUE,
                invite_link TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS students (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                selected_semester_id INTEGER REFERENCES semesters(id) ON DELETE SET NULL,
                last_menu_message_id INTEGER,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS join_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                status TEXT NOT NULL, -- 'pending', 'approved', 'rejected', 'left'
                processed_by INTEGER REFERENCES admins(telegram_id) ON DELETE SET NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, user_id)
            );
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_courses_semester ON courses(semester_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_requests_status ON join_requests(status);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_requests_user ON join_requests(user_id);")
        await db.commit()

    await seed_initial_data(db_path)


SIXTH_SEMESTER_COURSES = [
    ("CSE-3525", "Data Communication"),
    ("CSE-3631", "Operating Systems"),
    ("CSE-3632", "Operating Systems Lab"),
    ("CSE-3635", "Artificial Intelligence"),
    ("CSE-3636", "Artificial Intelligence Lab"),
    ("CSE-3641", "Software Engineering"),
    ("CSE-3642", "Software Engineering Lab"),
    ("ECON-3501", "Principles of Economics"),
    ("GEHE-3601", "History of the Emergence of Bangladesh"),
    ("URED-3604", "Life and Teachings of Prophet Muhammad (SAAS)"),
    ("CSE-4750", "Technical Writing and Presentation"),
]


async def seed_initial_data(db_path: Optional[str] = None) -> None:
    """Pre-seed 6th Semester and its official courses automatically."""
    async with get_db(db_path) as db:
        await db.execute("""
            INSERT INTO semesters (code, name) VALUES ('S6', '6th Semester (CSE)')
            ON CONFLICT(code) DO UPDATE SET name = excluded.name;
        """)
        await db.commit()

        async with db.execute("SELECT id FROM semesters WHERE code = 'S6';") as cursor:
            row = await cursor.fetchone()
            if not row:
                return
            sem_id = row["id"]

        for code, name in SIXTH_SEMESTER_COURSES:
            await db.execute("""
                INSERT INTO courses (semester_id, code, name, is_active)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(code) DO UPDATE SET name = excluded.name;
            """, (sem_id, code, name))
        await db.commit()


# --- Admin Operations ---

async def has_admins(db_path: Optional[str] = None) -> bool:
    async with get_db(db_path) as db:
        async with db.execute("SELECT COUNT(*) AS cnt FROM admins;") as cursor:
            row = await cursor.fetchone()
            return bool(row["cnt"] > 0)


async def claim_admin(telegram_id: int, username: Optional[str], full_name: str, db_path: Optional[str] = None) -> bool:
    """Claim admin role only if no admins exist yet."""
    async with get_db(db_path) as db:
        await db.execute("BEGIN IMMEDIATE;")
        try:
            async with db.execute("SELECT COUNT(*) AS cnt FROM admins;") as cursor:
                row = await cursor.fetchone()
                if row["cnt"] > 0:
                    await db.rollback()
                    return False

            await db.execute(
                "INSERT INTO admins (telegram_id, username, full_name) VALUES (?, ?, ?);",
                (telegram_id, username, full_name)
            )
            await db.commit()
            return True
        except Exception:
            await db.rollback()
            raise


async def is_admin(telegram_id: int, db_path: Optional[str] = None) -> bool:
    async with get_db(db_path) as db:
        async with db.execute("SELECT 1 FROM admins WHERE telegram_id = ?;", (telegram_id,)) as cursor:
            return await cursor.fetchone() is not None


async def get_all_admins(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM admins ORDER BY created_at ASC;") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


# --- Semester Operations ---

async def add_semester(code: str, name: str, db_path: Optional[str] = None) -> int:
    code = code.strip().upper()
    name = name.strip()
    async with get_db(db_path) as db:
        cursor = await db.execute(
            """
            INSERT INTO semesters (code, name) VALUES (?, ?)
            ON CONFLICT(code) DO UPDATE SET name = excluded.name, is_active = 1;
            """,
            (code, name)
        )
        await db.commit()
        return cursor.lastrowid


async def get_semesters(active_only: bool = True, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    query = "SELECT * FROM semesters"
    if active_only:
        query += " WHERE is_active = 1"
    query += " ORDER BY id ASC;"
    async with get_db(db_path) as db:
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_semester_by_code(code: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    code = code.strip().upper()
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM semesters WHERE code = ?;", (code,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_semester_by_id(sem_id: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM semesters WHERE id = ?;", (sem_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


# --- Course Operations ---

async def register_course(
    semester_id: int,
    code: str,
    name: str,
    chat_id: int,
    invite_link: str,
    db_path: Optional[str] = None
) -> Dict[str, Any]:
    code = code.strip().upper()
    name = name.strip()
    async with get_db(db_path) as db:
        # If chat_id was assigned to a different course previously, clear it
        await db.execute(
            "UPDATE courses SET chat_id = NULL, invite_link = NULL WHERE chat_id = ? AND code != ?;",
            (chat_id, code)
        )

        async with db.execute("SELECT id, name FROM courses WHERE code = ?;", (code,)) as cursor:
            existing = await cursor.fetchone()

        final_name = name if name else (existing["name"] if existing else code)

        if existing:
            await db.execute(
                """
                UPDATE courses
                SET semester_id = ?, name = ?, chat_id = ?, invite_link = ?, is_active = 1
                WHERE code = ?;
                """,
                (semester_id, final_name, chat_id, invite_link, code)
            )
        else:
            await db.execute(
                """
                INSERT INTO courses (semester_id, code, name, chat_id, invite_link, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(code) DO UPDATE SET
                    semester_id = excluded.semester_id,
                    name = excluded.name,
                    chat_id = excluded.chat_id,
                    invite_link = excluded.invite_link,
                    is_active = 1;
                """,
                (semester_id, code, final_name, chat_id, invite_link)
            )
        await db.commit()
        async with db.execute("SELECT * FROM courses WHERE code = ?;", (code,)) as cursor:
            row = await cursor.fetchone()
            return dict(row)


async def get_course_by_chat_id(chat_id: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM courses WHERE chat_id = ?;", (chat_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_course_by_code(code: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    code = code.strip().upper()
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM courses WHERE code = ?;", (code,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_course_by_id(course_id: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM courses WHERE id = ?;", (course_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_courses_by_semester(semester_id: int, active_only: bool = True, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    query = "SELECT * FROM courses WHERE semester_id = ?"
    if active_only:
        query += " AND is_active = 1"
    query += " ORDER BY code ASC;"
    async with get_db(db_path) as db:
        async with db.execute(query, (semester_id,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_all_courses(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    async with get_db(db_path) as db:
        async with db.execute("""
            SELECT c.*, s.code AS semester_code, s.name AS semester_name
            FROM courses c
            JOIN semesters s ON c.semester_id = s.id
            ORDER BY s.id ASC, c.code ASC;
        """) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


# --- Student Operations ---

async def upsert_student(
    telegram_id: int,
    username: Optional[str],
    full_name: str,
    selected_semester_id: Optional[int] = None,
    last_menu_message_id: Optional[int] = None,
    db_path: Optional[str] = None
) -> None:
    async with get_db(db_path) as db:
        await db.execute(
            """
            INSERT INTO students (telegram_id, username, full_name, selected_semester_id, last_menu_message_id, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                selected_semester_id = COALESCE(excluded.selected_semester_id, students.selected_semester_id),
                last_menu_message_id = COALESCE(excluded.last_menu_message_id, students.last_menu_message_id),
                updated_at = CURRENT_TIMESTAMP;
            """,
            (telegram_id, username, full_name, selected_semester_id, last_menu_message_id)
        )
        await db.commit()


async def get_student(telegram_id: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM students WHERE telegram_id = ?;", (telegram_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def set_student_semester(telegram_id: int, semester_id: int, db_path: Optional[str] = None) -> None:
    async with get_db(db_path) as db:
        await db.execute(
            "UPDATE students SET selected_semester_id = ?, updated_at = CURRENT_TIMESTAMP WHERE telegram_id = ?;",
            (semester_id, telegram_id)
        )
        await db.commit()


async def set_student_menu_message(telegram_id: int, message_id: int, db_path: Optional[str] = None) -> None:
    async with get_db(db_path) as db:
        await db.execute(
            "UPDATE students SET last_menu_message_id = ?, updated_at = CURRENT_TIMESTAMP WHERE telegram_id = ?;",
            (message_id, telegram_id)
        )
        await db.commit()


# --- Join Request Operations ---

async def record_join_request(
    chat_id: int,
    user_id: int,
    course_id: int,
    status: str = "pending",
    db_path: Optional[str] = None
) -> int:
    """Upsert join request. Transitions back to pending if previously rejected or left."""
    async with get_db(db_path) as db:
        await db.execute(
            """
            INSERT INTO join_requests (chat_id, user_id, course_id, status, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                course_id = excluded.course_id,
                status = excluded.status,
                processed_by = NULL,
                updated_at = CURRENT_TIMESTAMP;
            """,
            (chat_id, user_id, course_id, status)
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM join_requests WHERE chat_id = ? AND user_id = ?;",
            (chat_id, user_id)
        ) as cursor:
            row = await cursor.fetchone()
            return row["id"]


async def get_join_request_by_id(req_id: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    async with get_db(db_path) as db:
        async with db.execute("""
            SELECT r.*, c.code AS course_code, c.name AS course_name, s.code AS semester_code, s.name AS semester_name
            FROM join_requests r
            JOIN courses c ON r.course_id = c.id
            JOIN semesters s ON c.semester_id = s.id
            WHERE r.id = ?;
        """, (req_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_join_request(chat_id: int, user_id: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM join_requests WHERE chat_id = ? AND user_id = ?;", (chat_id, user_id)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_user_course_status(chat_id: int, user_id: int, db_path: Optional[str] = None) -> str:
    """Returns 'available', 'pending', 'approved', or 'rejected'/'left'."""
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT status FROM join_requests WHERE chat_id = ? AND user_id = ?;",
            (chat_id, user_id)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return "available"
            status = row["status"]
            if status in ("rejected", "left"):
                return "available"
            return status


async def update_join_request_status(
    req_id: int,
    status: str,
    processed_by: Optional[int],
    db_path: Optional[str] = None
) -> bool:
    """Update request status atomically; returns True if row was updated."""
    async with get_db(db_path) as db:
        cursor = await db.execute(
            """
            UPDATE join_requests
            SET status = ?, processed_by = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?;
            """,
            (status, processed_by, req_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def set_member_status(
    chat_id: int,
    user_id: int,
    status: str,
    db_path: Optional[str] = None
) -> None:
    """Update status when member leaves or is kicked or rejoins."""
    async with get_db(db_path) as db:
        await db.execute(
            """
            UPDATE join_requests
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE chat_id = ? AND user_id = ?;
            """,
            (status, chat_id, user_id)
        )
        await db.commit()


async def get_pending_requests(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    async with get_db(db_path) as db:
        async with db.execute("""
            SELECT r.*, c.code AS course_code, c.name AS course_name, s.code AS semester_code,
                   st.full_name AS student_name, st.username AS student_username
            FROM join_requests r
            JOIN courses c ON r.course_id = c.id
            JOIN semesters s ON c.semester_id = s.id
            LEFT JOIN students st ON r.user_id = st.telegram_id
            WHERE r.status = 'pending'
            ORDER BY r.created_at ASC;
        """) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
