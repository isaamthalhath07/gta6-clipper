"""SQLite history of clips/posts (dedupe + daily caps) and a JSON token store."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


class State:
    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.dir / "clipper.db", check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS clips (
                id TEXT PRIMARY KEY, channel TEXT, created REAL, event_wall REAL,
                score REAL, mood TEXT, title TEXT, path TEXT, status TEXT, info TEXT
            );
            CREATE TABLE IF NOT EXISTS posts (
                clip_id TEXT, platform TEXT, created REAL, ok INTEGER,
                remote_id TEXT, url TEXT, error TEXT
            );
            CREATE INDEX IF NOT EXISTS posts_platform_created ON posts(platform, created);
            """
        )
        self.tokens_path = self.dir / "tokens.json"

    # ---- clips / posts ---------------------------------------------------------------
    def add_clip(self, clip_id: str, channel: str, event_wall: float, score: float, mood: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO clips(id, channel, created, event_wall, score, mood, status) VALUES (?,?,?,?,?,?,?)",
                (clip_id, channel, time.time(), event_wall, score, mood, "detected"),
            )
            self.db.commit()

    def update_clip(self, clip_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        with self._lock:
            self.db.execute(f"UPDATE clips SET {cols} WHERE id=?", (*vals, clip_id))
            self.db.commit()

    def add_post(self, clip_id: str, platform: str, ok: bool, remote_id: str = "", url: str = "", error: str = "") -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO posts VALUES (?,?,?,?,?,?,?)",
                (clip_id, platform, time.time(), int(ok), remote_id, url, error[:2000]),
            )
            self.db.commit()

    def posts_last_24h(self, platform: str) -> int:
        with self._lock:
            row = self.db.execute(
                "SELECT COUNT(*) FROM posts WHERE platform=? AND ok=1 AND created>?",
                (platform, time.time() - 86400),
            ).fetchone()
        return int(row[0])

    def recent_clips(self, limit: int = 20) -> list[tuple]:
        with self._lock:
            return self.db.execute(
                "SELECT id, channel, score, mood, title, status, path FROM clips ORDER BY created DESC LIMIT ?",
                (limit,),
            ).fetchall()

    # ---- tokens ---------------------------------------------------------------------
    def get_tokens(self, platform: str) -> dict:
        with self._lock:
            if not self.tokens_path.exists():
                return {}
            return json.loads(self.tokens_path.read_text()).get(platform, {})

    def set_tokens(self, platform: str, data: dict) -> None:
        with self._lock:
            allt = json.loads(self.tokens_path.read_text()) if self.tokens_path.exists() else {}
            allt[platform] = {**allt.get(platform, {}), **data}
            tmp = self.tokens_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(allt, indent=2))
            tmp.chmod(0o600)
            tmp.replace(self.tokens_path)
