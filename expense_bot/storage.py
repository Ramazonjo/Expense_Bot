import asyncio
import json
import time
from pathlib import Path

import aiosqlite


class QueueFull(RuntimeError):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.db = None
        self.lock = asyncio.Lock()

    async def open(self, binding: str):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                author TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                text TEXT NOT NULL,
                photo BLOB,
                mime TEXT,
                rows_json TEXT,
                start_row INTEGER,
                state TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0,
                error TEXT,
                notified INTEGER NOT NULL DEFAULT 0,
                next_notice REAL NOT NULL DEFAULT 0,
                UNIQUE(chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS job_queue ON jobs(state, next_try);
            CREATE TABLE IF NOT EXISTS reports (
                report_date TEXT NOT NULL, chat_id INTEGER NOT NULL,
                PRIMARY KEY(report_date, chat_id)
            );
        """)
        existing = await self._one("SELECT value FROM meta WHERE key='binding'")
        if existing and existing["value"] != binding:
            raise RuntimeError("SQLite belongs to a different bot/sheet/timezone/currency")
        await self._write("INSERT OR IGNORE INTO meta VALUES ('binding', ?)", (binding,))

    async def _one(self, sql: str, args=()):
        async with self.db.execute(sql, args) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def _all(self, sql: str, args=()):
        async with self.db.execute(sql, args) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def _write(self, sql: str, args=()):
        async with self.lock:
            cursor = await self.db.execute(sql, args)
            await self.db.commit()
            return cursor.rowcount

    async def enqueue(
        self, *, chat_id, message_id, author, occurred_at, text, photo, mime, limit: int
    ):
        async with self.lock:
            old = await self._one(
                "SELECT id FROM jobs WHERE chat_id=? AND message_id=?", (chat_id, message_id)
            )
            if old:
                return old["id"], False
            count = await self._one(
                "SELECT COUNT(*) AS n FROM jobs WHERE state IN ('queued','ready')"
            )
            if count["n"] >= limit:
                raise QueueFull("Очередь заполнена")
            cursor = await self.db.execute(
                """INSERT INTO jobs(chat_id,message_id,author,occurred_at,text,photo,mime)
                   VALUES (?,?,?,?,?,?,?)""",
                (chat_id, message_id, author, occurred_at, text, photo, mime),
            )
            await self.db.commit()
            return cursor.lastrowid, True

    async def next_job(self):
        return await self._one(
            """SELECT * FROM jobs WHERE state IN ('queued','ready') AND next_try<=?
               ORDER BY id LIMIT 1""",
            (time.time(),),
        )

    async def save_rows(self, job_id: int, rows: list[list]):
        await self._write(
            "UPDATE jobs SET rows_json=?, state='ready' WHERE id=?",
            (json.dumps(rows, ensure_ascii=False), job_id),
        )

    async def reserve(self, job_id: int, observed_next: int, count: int) -> int:
        async with self.lock:
            job = await self._one("SELECT start_row FROM jobs WHERE id=?", (job_id,))
            if job["start_row"] is not None:
                return job["start_row"]
            cursor = await self._one("SELECT value FROM meta WHERE key='next_row'")
            start = max(observed_next, int(cursor["value"]) if cursor else 2)
            await self.db.execute("UPDATE jobs SET start_row=? WHERE id=?", (start, job_id))
            await self.db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('next_row', ?)", (str(start + count),)
            )
            await self.db.commit()
            return start

    async def complete(self, job_id: int):
        await self._write(
            "UPDATE jobs SET state='done', error=NULL, photo=NULL, text='' WHERE id=?", (job_id,)
        )

    async def failure(self, job: dict, explanation: str, retry: bool, limit: int):
        attempts = job["attempts"] + 1
        state = (
            ("ready" if job["rows_json"] else "queued") if retry and attempts < limit else "failed"
        )
        delay = min(300, 10 * 2 ** (attempts - 1))
        await self._write(
            "UPDATE jobs SET state=?,attempts=?,next_try=?,error=?,notified=0 WHERE id=?",
            (state, attempts, time.time() + delay, explanation, job["id"]),
        )

    async def retry(self, chat_id: int, job_id: int) -> bool:
        changed = await self._write(
            """UPDATE jobs SET state=CASE WHEN rows_json IS NULL THEN 'queued' ELSE 'ready' END,
               attempts=0,next_try=0,error=NULL,notified=0,next_notice=0
               WHERE id=? AND chat_id=? AND state='failed'""",
            (job_id, chat_id),
        )
        return bool(changed)

    async def notices(self):
        return await self._all(
            """SELECT * FROM jobs WHERE state IN ('done','failed') AND notified=0
               AND next_notice<=? ORDER BY id LIMIT 5""",
            (time.time(),),
        )

    async def notice_sent(self, job_id: int):
        await self._write("UPDATE jobs SET notified=1 WHERE id=?", (job_id,))

    async def notice_later(self, job_id: int):
        await self._write("UPDATE jobs SET next_notice=? WHERE id=?", (time.time() + 60, job_id))

    async def recent(self, chat_id: int):
        return await self._all(
            """SELECT id,state,error FROM jobs WHERE chat_id=? ORDER BY id DESC LIMIT 10""",
            (chat_id,),
        )

    async def unfinished(self):
        return await self._all("SELECT occurred_at FROM jobs WHERE state!='done'")

    async def report_sent(self, day: str, chat_id: int) -> bool:
        return bool(
            await self._one(
                "SELECT 1 FROM reports WHERE report_date=? AND chat_id=?", (day, chat_id)
            )
        )

    async def mark_report(self, day: str, chat_id: int):
        await self._write("INSERT OR IGNORE INTO reports VALUES (?,?)", (day, chat_id))

    async def close(self):
        if self.db:
            await self.db.close()
