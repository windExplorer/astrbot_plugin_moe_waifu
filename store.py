# -*- coding: utf-8 -*-
"""萌萌今日老婆 · SQLite 存储层（同步 sqlite3，毫秒级小事务；星语同款模式）。

表：draws（当日结果）/ user_char_counts（累计）/ char_stats（全局热度）/
portraits（立绘库存档）/ draw_jobs（生图审计）。
落库位置固定 data/plugin_data/ 下（StarTools.get_data_dir），升级目录不丢数据。
一日一签由 draws 的 (user_id, date) 主键保证：reroll 覆盖更新。
"""
from __future__ import annotations

import functools
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS draws (
  user_id   TEXT NOT NULL,
  date      TEXT NOT NULL,
  char_id   TEXT NOT NULL,
  nonce     INTEGER NOT NULL DEFAULT 0,   -- 换老婆次数：0=首抽
  rerolled  INTEGER NOT NULL DEFAULT 0,
  nickname  TEXT,
  group_id  TEXT,
  group_name TEXT,
  platform  TEXT,
  card_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user_id, date)
);
CREATE TABLE IF NOT EXISTS user_char_counts (
  user_id TEXT NOT NULL,
  char_id TEXT NOT NULL,
  count   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, char_id)
);
CREATE TABLE IF NOT EXISTS char_stats (
  char_id   TEXT PRIMARY KEY,
  total     INTEGER NOT NULL DEFAULT 0,
  last_date TEXT
);
CREATE TABLE IF NOT EXISTS portraits (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  char_id    TEXT NOT NULL,
  file       TEXT NOT NULL,               -- 相对 data 目录的路径（portraits/<id>/xxx.png）
  seed       INTEGER NOT NULL,
  date       TEXT NOT NULL,               -- 生成那天（当日复用按此查询）
  seq        INTEGER NOT NULL,            -- 该角色第几张（0 起）
  strategy   TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_portraits_char ON portraits(char_id, seq);
CREATE TABLE IF NOT EXISTS draw_jobs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  char_id     TEXT NOT NULL,
  user_id     TEXT,
  date        TEXT NOT NULL,
  mode        TEXT NOT NULL,              -- generated / from_pool
  status      TEXT NOT NULL,              -- ok / timeout / error / skipped_no_anima / from_pool
  strategy    TEXT,
  seed        INTEGER,
  error       TEXT,
  duration_ms INTEGER,
  image_path  TEXT,
  created_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _serialized(cls):
    """把 Store 所有公开方法串行化（星语 v1.11.10 同款）。

    单连接 + check_same_thread=False：渲染/决策在 asyncio.to_thread 里跑，
    同一连接会被工作线程与主线程同时使用；sqlite3 要求调用方自己串行化。
    覆盖公开方法、RLock 可重入（方法互调不自锁）、私有方法不包装。
    """
    lock = threading.RLock()
    for name, fn in list(cls.__dict__.items()):
        if name.startswith("_") or not callable(fn):
            continue

        @functools.wraps(fn)
        def wrapper(*args, __fn=fn, **kwargs):
            with lock:
                return __fn(*args, **kwargs)

        setattr(cls, name, wrapper)
    return cls


@_serialized
class Store:
    def __init__(self, db_path):
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    # ---------- draws ----------

    def get_draw(self, user_id: str, date: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM draws WHERE user_id=? AND date=?", (user_id, date)
        ).fetchone()
        return dict(row) if row else None

    def upsert_draw(
        self, user_id: str, date: str, char_id: str, nonce: int, rerolled: bool,
        nickname: str = "", group_id: str = "", group_name: str = "",
        platform: str = "",
    ) -> None:
        now = _now()
        self._conn.execute(
            """
            INSERT INTO draws (user_id, date, char_id, nonce, rerolled, nickname,
                               group_id, group_name, platform, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id, date) DO UPDATE SET
              char_id=excluded.char_id, nonce=excluded.nonce,
              rerolled=excluded.rerolled, nickname=excluded.nickname,
              group_id=excluded.group_id, group_name=excluded.group_name,
              platform=excluded.platform, updated_at=excluded.updated_at
            """,
            (user_id, date, char_id, int(nonce), int(bool(rerolled)), nickname,
             group_id, group_name, platform, now, now),
        )
        self._conn.commit()

    def update_draw_card(self, user_id: str, date: str, card_path: str) -> None:
        self._conn.execute(
            "UPDATE draws SET card_path=?, updated_at=? WHERE user_id=? AND date=?",
            (card_path, _now(), user_id, date),
        )
        self._conn.commit()

    def same_group_today(self, group_id: str, date: str, char_id: str) -> int:
        """今天本群有多少人抽到同一角色（调用方需先 upsert_draw 再查，含自己）。"""
        if not group_id:
            return 1
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM draws WHERE group_id=? AND date=? AND char_id=?",
            (group_id, date, char_id),
        ).fetchone()
        return int(row["n"]) if row else 1

    # ---------- 计数 ----------

    def bump_user_char(self, user_id: str, char_id: str) -> int:
        self._conn.execute(
            """
            INSERT INTO user_char_counts (user_id, char_id, count) VALUES (?,?,1)
            ON CONFLICT(user_id, char_id) DO UPDATE SET count=count+1
            """,
            (user_id, char_id),
        )
        row = self._conn.execute(
            "SELECT count FROM user_char_counts WHERE user_id=? AND char_id=?",
            (user_id, char_id),
        ).fetchone()
        self._conn.commit()
        return int(row["count"]) if row else 1

    def bump_char_stats(self, char_id: str, date: str) -> None:
        self._conn.execute(
            """
            INSERT INTO char_stats (char_id, total, last_date) VALUES (?,?,?)
            ON CONFLICT(char_id) DO UPDATE SET total=total+1, last_date=excluded.last_date
            """,
            (char_id, 1, date),
        )
        self._conn.commit()

    # ---------- portraits ----------

    def add_portrait(self, char_id: str, file: str, seed: int, date: str,
                     seq: int, strategy: str) -> None:
        self._conn.execute(
            "INSERT INTO portraits (char_id, file, seed, date, seq, strategy, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (char_id, file, int(seed), date, int(seq), strategy, _now()),
        )
        self._conn.commit()

    def count_portraits(self, char_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM portraits WHERE char_id=?", (char_id,)
        ).fetchone()
        return int(row["n"]) if row else 0

    def portrait_by_date(self, char_id: str, date: str) -> dict | None:
        """当天已生成的立绘（当日复用）；同天多张取 seq 最大的一张。"""
        row = self._conn.execute(
            "SELECT * FROM portraits WHERE char_id=? AND date=? ORDER BY seq DESC LIMIT 1",
            (char_id, date),
        ).fetchone()
        return dict(row) if row else None

    def list_portraits(self, char_id: str) -> list:
        """按 seq 升序返回该角色全部立绘（满库取图按此排序 + pool_index 取模）。"""
        rows = self._conn.execute(
            "SELECT * FROM portraits WHERE char_id=? ORDER BY seq ASC", (char_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------- draw_jobs ----------

    def log_job(self, char_id: str, date: str, mode: str, status: str,
                user_id: str = "", strategy: str = "", seed: int = None,
                error: str = "", duration_ms: int = 0, image_path: str = "") -> None:
        self._conn.execute(
            "INSERT INTO draw_jobs (char_id, user_id, date, mode, status, strategy,"
            " seed, error, duration_ms, image_path, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (char_id, user_id, date, mode, status, strategy,
             None if seed is None else int(seed), error[:300], int(duration_ms),
             image_path[:500], _now()),
        )
        self._conn.commit()
