"""本机 SQLite 存储：听写历史、个人词典、应用使用统计。

数据库文件位于用户数据目录，永不同步、永不上传。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

from ..config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    raw_text      TEXT NOT NULL,
    polished_text TEXT NOT NULL,
    app_name      TEXT DEFAULT '',
    bundle_id     TEXT DEFAULT '',
    language      TEXT DEFAULT '',
    mode          TEXT DEFAULT 'dictate',
    duration_ms   INTEGER DEFAULT 0,
    audio_path    TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_history_ts ON history(ts DESC);

CREATE TABLE IF NOT EXISTS dictionary (
    term        TEXT PRIMARY KEY,
    aliases     TEXT DEFAULT '',
    replacement TEXT DEFAULT '',
    enabled     INTEGER DEFAULT 1,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS stats (
    key   TEXT PRIMARY KEY,
    value INTEGER DEFAULT 0
);
"""


class Storage:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------------- 历史 ----------------
    def add_history(self, *, raw: str, polished: str, app_name: str = "",
                    bundle_id: str = "", language: str = "", mode: str = "dictate",
                    duration_ms: int = 0, audio_path: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO history(ts,raw_text,polished_text,app_name,bundle_id,"
                "language,mode,duration_ms,audio_path) VALUES(?,?,?,?,?,?,?,?,?)",
                (int(time.time()), raw, polished, app_name, bundle_id,
                 language, mode, duration_ms, audio_path),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def search_history(self, keyword: str = "", limit: int = 300) -> list[dict]:
        sql = "SELECT * FROM history"
        args: tuple = ()
        if keyword:
            sql += " WHERE raw_text LIKE ? OR polished_text LIKE ?"
            args = (f"%{keyword}%", f"%{keyword}%")
        sql += " ORDER BY ts DESC LIMIT ?"
        args += (limit,)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def update_history_polished(self, row_id: int, polished: str) -> None:
        """重新整理（重试）后回写输出文字。"""
        with self._lock:
            self._conn.execute(
                "UPDATE history SET polished_text=? WHERE id=?", (polished, row_id))
            self._conn.commit()

    def delete_history(self, row_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM history WHERE id=?", (row_id,))
            self._conn.commit()

    def clear_history(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM history")
            self._conn.commit()

    def prune_history(self, retention_days: int) -> int:
        cutoff = int(time.time()) - retention_days * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM history WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    def export_history(self) -> list[dict]:
        return self.search_history(limit=10_000_000)

    # ---------------- 个人词典 ----------------
    def add_term(self, term: str, aliases: Iterable[str] = (), replacement: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO dictionary(term,aliases,replacement,enabled,created_at) "
                "VALUES(?,?,?,1,COALESCE((SELECT created_at FROM dictionary WHERE term=?),?))",
                (term, ",".join(a.strip() for a in aliases if a.strip()),
                 replacement, term, int(time.time())),
            )
            self._conn.commit()

    def remove_term(self, term: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM dictionary WHERE term=?", (term,))
            self._conn.commit()

    def terms(self, enabled_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM dictionary"
        if enabled_only:
            sql += " WHERE enabled=1"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def bump_stat(self, key: str, delta: int = 1) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO stats(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=value+excluded.value",
                (key, delta),
            )
            self._conn.commit()

    def stats(self) -> dict:
        with self._lock:
            return {r["key"]: r["value"] for r in self._conn.execute("SELECT * FROM stats")}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
