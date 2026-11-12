# -*- coding: utf-8 -*-
"""萌萌今日老婆 · SQLite 存储层（同步 sqlite3，毫秒级小事务；星语同款模式）。

表：draws（当日结果，一人一天一行）/ draw_events（每次抽/重抽追加的流水）/
user_char_counts（累计）/ char_stats（全局热度）/ portraits（立绘库存档）/
draw_jobs（生图审计）/ debug_history（调试操作与查询审计）。
落库位置固定 data/plugin_data/ 下（StarTools.get_data_dir），升级目录不丢数据。

口径（v0.9.0）：draws 是「当前状态」，reroll 覆盖更新；draw_events 是「事件流」，
只追加不覆盖——换老婆前抽到谁、当时的种子与立绘来源都留得住。
统计一律以 draws 为准（一人一天只计一次），重抽率等从 events 推导，
两张表不混算。老库通过 PRAGMA table_info + ALTER TABLE 就地补列，不重建表。
"""
from __future__ import annotations

import functools
import sqlite3
import threading
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
-- v0.9.0：抽签流水。draws 覆盖更新会抹掉「换老婆前是谁」，这里只追加。
CREATE TABLE IF NOT EXISTS draw_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       TEXT NOT NULL,
  date          TEXT NOT NULL,
  char_id       TEXT NOT NULL,
  char_name     TEXT NOT NULL DEFAULT '',
  series        TEXT NOT NULL DEFAULT '',
  nonce         INTEGER NOT NULL DEFAULT 0,
  kind          TEXT NOT NULL DEFAULT 'first',   -- first / reroll
  seed_hex      TEXT NOT NULL DEFAULT '',
  decision      TEXT NOT NULL DEFAULT '',        -- reuse_today / generate / from_pool
  portrait_file TEXT NOT NULL DEFAULT '',
  card_path     TEXT NOT NULL DEFAULT '',
  group_id      TEXT NOT NULL DEFAULT '',
  group_name    TEXT NOT NULL DEFAULT '',
  platform      TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL
);
-- v0.9.0：调试审计 + 最近查询 uid（面板无用户态，一律记 webui）。
-- 环形保留 200 条，避免调试动作把库撑大。
CREATE TABLE IF NOT EXISTS debug_history (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL,              -- query / simulate / rerender / reset / ...
  target     TEXT NOT NULL DEFAULT '',
  detail     TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_draws_date ON draws(date);
CREATE INDEX IF NOT EXISTS idx_draws_char_date ON draws(char_id, date);
CREATE INDEX IF NOT EXISTS idx_draws_user_date ON draws(user_id, date);
CREATE INDEX IF NOT EXISTS idx_draw_jobs_date ON draw_jobs(date);
CREATE INDEX IF NOT EXISTS idx_draw_jobs_user_date ON draw_jobs(user_id, date);
CREATE INDEX IF NOT EXISTS idx_draw_jobs_status ON draw_jobs(status);
CREATE INDEX IF NOT EXISTS idx_draw_events_user_date ON draw_events(user_id, date, id);
CREATE INDEX IF NOT EXISTS idx_draw_events_date ON draw_events(date);
CREATE INDEX IF NOT EXISTS idx_debug_history_kind ON debug_history(kind, id);
"""

# 老库补列（PRAGMA 探测 → ALTER TABLE，绝不重建已有表）。
# draws 早期只有 12 列：角色名/种子/立绘来源都得靠角色库反查，角色删了就显示 id。
_MIGRATIONS = {
    "draws": {
        "seed_hex": "TEXT NOT NULL DEFAULT ''",
        "char_name": "TEXT NOT NULL DEFAULT ''",
        "series": "TEXT NOT NULL DEFAULT ''",
        "portrait_file": "TEXT NOT NULL DEFAULT ''",
        "decision": "TEXT NOT NULL DEFAULT ''",
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _streak_stats(dates, today: str = "") -> tuple[int, int]:
    """(当前连签, 最长连签)，输入为 YYYY-MM-DD 字符串的可迭代对象（纯函数，可直测）。

    当前连签允许「今天还没抽」时从昨天起算——早上打开面板不该显示断签。
    纯字符串比较不够（跨月/跨年），这里用 date.fromisoformat 逐日推进。
    """
    ds = {str(d)[:10] for d in dates if d}
    if not ds:
        return (0, 0)

    def parse(s):
        try:
            return _date.fromisoformat(s)
        except (ValueError, TypeError):
            return None

    anchor = parse(today) or max((x for x in (parse(s) for s in ds) if x), default=None)
    current = 0
    if anchor:
        probe = anchor if anchor.isoformat() in ds else anchor - timedelta(days=1)
        while probe.isoformat() in ds:
            current += 1
            probe -= timedelta(days=1)

    ordered = sorted(x for x in (parse(s) for s in ds) if x)
    longest = run = 0
    prev = None
    for cur in ordered:
        run = run + 1 if prev is not None and (cur - prev).days == 1 else 1
        longest = max(longest, run)
        prev = cur
    return (current, longest)


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
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """老库就地补列：只加缺的列，不改已有数据、不重建表。"""
        for table, cols in _MIGRATIONS.items():
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for col, ddl in cols.items():
                if col not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

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
        platform: str = "", seed_hex: str = "", char_name: str = "",
        series: str = "", portrait_file: str = "", decision: str = "",
    ) -> None:
        """写当日结果 + 追加一条抽签流水。

        v0.9.0 起角色名 / 作品 / 种子 / 立绘来源 / 立绘决策都随签快照落库：
        角色条目日后被删或被改名，历史记录仍读得懂。
        """
        now = _now()
        nonce = int(nonce)
        self._conn.execute(
            """
            INSERT INTO draws (user_id, date, char_id, nonce, rerolled, nickname,
                               group_id, group_name, platform, seed_hex, char_name,
                               series, portrait_file, decision, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id, date) DO UPDATE SET
              char_id=excluded.char_id, nonce=excluded.nonce,
              rerolled=excluded.rerolled, nickname=excluded.nickname,
              group_id=excluded.group_id, group_name=excluded.group_name,
              platform=excluded.platform, seed_hex=excluded.seed_hex,
              char_name=excluded.char_name, series=excluded.series,
              portrait_file=excluded.portrait_file, decision=excluded.decision,
              updated_at=excluded.updated_at
            """,
            (user_id, date, char_id, nonce, int(bool(rerolled)), nickname,
             group_id, group_name, platform, seed_hex, char_name, series,
             portrait_file, decision, now, now),
        )
        self._conn.execute(
            """
            INSERT INTO draw_events (user_id, date, char_id, char_name, series,
                                     nonce, kind, seed_hex, decision, portrait_file,
                                     group_id, group_name, platform, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (user_id, date, char_id, char_name, series, nonce,
             "reroll" if nonce > 0 else "first", seed_hex, decision,
             portrait_file, group_id, group_name, platform, now),
        )
        self._conn.commit()

    def update_draw_card(self, user_id: str, date: str, card_path: str) -> None:
        self._conn.execute(
            "UPDATE draws SET card_path=?, updated_at=? WHERE user_id=? AND date=?",
            (card_path, _now(), user_id, date),
        )
        # 流水补上成卡路径（只写该人当天最后一条事件）
        self._conn.execute(
            "UPDATE draw_events SET card_path=? WHERE id=("
            "  SELECT id FROM draw_events WHERE user_id=? AND date=? "
            "  ORDER BY id DESC LIMIT 1)",
            (card_path, user_id, date),
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

    def list_portraits_all(self) -> list:
        """全部立绘行（工坊孤立图发现用）。"""
        rows = self._conn.execute("SELECT * FROM portraits ORDER BY char_id, seq ASC").fetchall()
        return [dict(r) for r in rows]

    def delete_portrait(self, file: str) -> None:
        self._conn.execute("DELETE FROM portraits WHERE file=?", (file,))
        self._conn.commit()

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

    # ---------- 工坊「记录 · 调试」查询（v0.8.0） ----------

    def list_draws(self, limit: int = 200, offset: int = 0,
                   user_id: str = "", date: str = "") -> tuple[list, int]:
        """抽取记录分页（updated_at 倒序）。返回 (rows, total)。"""
        conds, args = [], []
        if user_id:
            conds.append("user_id=?")
            args.append(user_id)
        if date:
            conds.append("date=?")
            args.append(date)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        total = int(self._conn.execute(
            f"SELECT COUNT(*) AS n FROM draws{where}", args).fetchone()["n"])
        rows = self._conn.execute(
            f"SELECT * FROM draws{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (*args, max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
        return [dict(r) for r in rows], total

    def draw_stats(self, today: str) -> dict:
        """抽取侧汇总：总签数 / 今日签数 / 累计用户 / 重抽次数 / 打过照面的角色数。"""
        def one(sql, args=()):
            row = self._conn.execute(sql, args).fetchone()
            return row[0] if row else 0
        return {
            "total_draws": int(one("SELECT COUNT(*) FROM draws")),
            "today_draws": int(one("SELECT COUNT(*) FROM draws WHERE date=?", (today,))),
            "users": int(one("SELECT COUNT(DISTINCT user_id) FROM draws")),
            "rerolls": int(one("SELECT COALESCE(SUM(nonce),0) FROM draws")),
            "today_rerolls": int(one(
                "SELECT COALESCE(SUM(nonce),0) FROM draws WHERE date=?", (today,))),
            "chars_seen": int(one("SELECT COUNT(*) FROM char_stats")),
            "portraits_total": int(one("SELECT COUNT(*) FROM portraits")),
        }

    def top_users(self, limit: int = 10) -> list:
        """按抽签次数排的用户榜（含重抽次数与抽过的不同角色数）。"""
        rows = self._conn.execute(
            """
            SELECT user_id, MAX(nickname) AS nickname,
                   COUNT(*) AS draws, COUNT(DISTINCT char_id) AS chars,
                   COALESCE(SUM(nonce),0) AS rerolls, MAX(date) AS last_date
            FROM draws GROUP BY user_id ORDER BY draws DESC, user_id LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(r) for r in rows]

    def top_chars(self, limit: int = 10) -> list:
        """全局热度榜（char_stats：total = 被抽中总次数）。"""
        rows = self._conn.execute(
            "SELECT char_id, total, last_date FROM char_stats"
            " ORDER BY total DESC, char_id LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_jobs(self, limit: int = 200, offset: int = 0,
                  char_id: str = "", status: str = "", mode: str = "",
                  user_id: str = "") -> tuple[list, int]:
        """生图审计分页（created_at 倒序）。mode/status 为空串 = 不过滤。"""
        conds, args = [], []
        if char_id:
            conds.append("char_id=?")
            args.append(char_id)
        if status:
            conds.append("status=?")
            args.append(status)
        if mode:
            conds.append("mode=?")
            args.append(mode)
        if user_id:
            conds.append("user_id=?")
            args.append(user_id)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        total = int(self._conn.execute(
            f"SELECT COUNT(*) AS n FROM draw_jobs{where}", args).fetchone()["n"])
        rows = self._conn.execute(
            f"SELECT * FROM draw_jobs{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*args, max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
        return [dict(r) for r in rows], total

    def job_stats(self, today: str) -> dict:
        """生成侧汇总：总数 / 成功 / 失败 / 今日数 / 平均耗时（成功项）。"""
        def one(sql, args=()):
            row = self._conn.execute(sql, args).fetchone()
            return row[0] if row is not None else 0
        return {
            "jobs_total": int(one("SELECT COUNT(*) FROM draw_jobs")),
            "jobs_ok": int(one("SELECT COUNT(*) FROM draw_jobs WHERE status='ok'")),
            "jobs_error": int(one(
                "SELECT COUNT(*) FROM draw_jobs WHERE status IN ('error','timeout')")),
            "jobs_today": int(one("SELECT COUNT(*) FROM draw_jobs WHERE date=?", (today,))),
            "avg_ms": int(one(
                "SELECT COALESCE(AVG(duration_ms),0) FROM draw_jobs WHERE status='ok'")),
        }

    # ---------- 调试 / 重置（v0.8.0） ----------
    # 文件删除一律由调用方（webui 层）做：这里只动库并返回受影响路径清单。

    def clear_draws(self, user_id: str = "", date: str = "") -> list[dict]:
        """删签（可按 user/date 过滤，两者都空 = 全部）。返回被删行（含 card_path）。"""
        conds, args = [], []
        if user_id:
            conds.append("user_id=?")
            args.append(user_id)
        if date:
            conds.append("date=?")
            args.append(date)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = self._conn.execute(f"SELECT * FROM draws{where}", args).fetchall()
        out = [dict(r) for r in rows]
        self._conn.execute(f"DELETE FROM draws{where}", args)
        # 流水跟着删，避免「签没了、事件还在」的假历史（两张表列名兼容同一 where）
        self._conn.execute(f"DELETE FROM draw_events{where}", args)
        self._conn.commit()
        return out

    def force_reroll(self, user_id: str, date: str) -> dict | None:
        """调试版「换老婆」：nonce+1 重派种子并清卡图，下次抽取即出新角色。

        直接改库不走 reroll_daily_limit（调试语义）；无签返回 None。
        """
        row = self._conn.execute(
            "SELECT * FROM draws WHERE user_id=? AND date=?", (user_id, date)
        ).fetchone()
        if row is None:
            return None
        nonce = int(row["nonce"]) + 1
        self._conn.execute(
            "UPDATE draws SET nonce=?, rerolled=1, card_path='', updated_at=?"
            " WHERE user_id=? AND date=?",
            (nonce, _now(), user_id, date),
        )
        self._conn.commit()
        return dict(self._conn.execute(
            "SELECT * FROM draws WHERE user_id=? AND date=?", (user_id, date)
        ).fetchone())

    def update_draw_char(self, user_id: str, date: str, char_id: str,
                         char_name: str = "", series: str = "",
                         seed_hex: str = "") -> None:
        """强制换签重派角色后更新签行：char_id 与随签快照一并刷新。

        portrait_file/decision 清空——旧立绘属于旧角色，下次抽取按新角色
        重新做立绘决策（出图或取库存）。
        """
        self._conn.execute(
            "UPDATE draws SET char_id=?, char_name=?, series=?, seed_hex=?,"
            " portrait_file='', decision='', updated_at=? WHERE user_id=? AND date=?",
            (char_id, char_name, series, seed_hex, _now(), user_id, date),
        )
        self._conn.commit()

    def reset_char_day(self, char_id: str, date: str) -> dict:
        """重试某角色当日生成：删当日立绘行 + 清指向它的签卡路径。

        返回 {removed_files, cleared_draws}（文件由调用方删）。
        """
        rows = self._conn.execute(
            "SELECT file FROM portraits WHERE char_id=? AND date=?", (char_id, date)
        ).fetchall()
        removed = [r["file"] for r in rows]
        if removed:
            self._conn.execute(
                "DELETE FROM portraits WHERE char_id=? AND date=?", (char_id, date))
        cur = self._conn.execute(
            "UPDATE draws SET card_path='', updated_at=? WHERE char_id=? AND date=?",
            (_now(), char_id, date),
        )
        self._conn.commit()
        return {"removed_files": removed, "cleared_draws": int(cur.rowcount or 0)}

    def clear_char_portraits(self, char_id: str) -> list[str]:
        """清空某角色全部立绘行。返回被删文件相对路径清单（调用方删文件）。"""
        rows = self._conn.execute(
            "SELECT file FROM portraits WHERE char_id=?", (char_id,)
        ).fetchall()
        out = [r["file"] for r in rows]
        self._conn.execute("DELETE FROM portraits WHERE char_id=?", (char_id,))
        self._conn.commit()
        return out

    # ---------- 抽签记录 / 用户查询 / 区间统计（v0.9.0） ----------
    # 口径：draws = 当前状态（一人一天一行），draw_events = 只追加的事件流。
    # 所有列表统一返回 {total,page,size,rows}，区间一律「含两端」的字符串比较。

    def _record_filters(self, date_from="", date_to="", q="", char_id="",
                        group_id="", platform="", rerolled="") -> tuple[str, list]:
        conds, args = [], []
        if date_from:
            conds.append("date>=?")
            args.append(date_from)
        if date_to:
            conds.append("date<=?")
            args.append(date_to)
        q = str(q or "").strip()
        if q:
            conds.append("(user_id LIKE ? OR nickname LIKE ? OR char_name LIKE ?)")
            args.extend([f"%{q}%"] * 3)
        if char_id:
            conds.append("char_id=?")
            args.append(char_id)
        if group_id == "__private__":
            conds.append("(group_id='' OR group_id IS NULL)")
        elif group_id:
            conds.append("group_id=?")
            args.append(group_id)
        if platform:
            conds.append("platform=?")
            args.append(platform)
        if rerolled in ("yes", "no"):
            conds.append("rerolled=?")
            args.append(1 if rerolled == "yes" else 0)
        return ((" WHERE " + " AND ".join(conds)) if conds else ""), args

    def list_records(self, page: int = 1, size: int = 20, date_from: str = "",
                     date_to: str = "", q: str = "", char_id: str = "",
                     group_id: str = "", platform: str = "",
                     rerolled: str = "") -> dict:
        """抽签记录分页：日期区间 + 关键词（QQ/昵称/角色名）+ 角色/群/平台/重抽筛选。"""
        page, size = max(1, int(page)), max(1, min(100, int(size)))
        where, args = self._record_filters(date_from, date_to, q, char_id,
                                           group_id, platform, rerolled)
        total = int(self._conn.execute(
            f"SELECT COUNT(*) AS n FROM draws{where}", args).fetchone()["n"])
        rows = self._conn.execute(
            f"SELECT * FROM draws{where} ORDER BY date DESC, updated_at DESC"
            " LIMIT ? OFFSET ?", (*args, size, (page - 1) * size),
        ).fetchall()
        return {"total": total, "page": page, "size": size,
                "rows": [dict(r) for r in rows]}

    def record_detail(self, user_id: str, date: str) -> dict | None:
        """单条签的完整上下文：当前行 + 当天抽签流水 + 相关出图任务。"""
        row = self._conn.execute(
            "SELECT * FROM draws WHERE user_id=? AND date=?", (user_id, date)
        ).fetchone()
        if row is None:
            return None
        events = self._conn.execute(
            "SELECT * FROM draw_events WHERE user_id=? AND date=? ORDER BY id ASC",
            (user_id, date),
        ).fetchall()
        jobs = self._conn.execute(
            "SELECT * FROM draw_jobs WHERE date=? AND (user_id=? OR char_id=?)"
            " ORDER BY id DESC LIMIT 20", (date, user_id, row["char_id"]),
        ).fetchall()
        return {"draw": dict(row), "events": [dict(r) for r in events],
                "jobs": [dict(r) for r in jobs]}

    def list_events(self, limit: int = 50, offset: int = 0, user_id: str = "",
                    date: str = "", kind: str = "") -> tuple[list, int]:
        """抽签流水（含被覆盖掉的重抽前角色）。返回 (rows, total)。"""
        conds, args = [], []
        if user_id:
            conds.append("user_id=?")
            args.append(user_id)
        if date:
            conds.append("date=?")
            args.append(date)
        if kind in ("first", "reroll"):
            conds.append("kind=?")
            args.append(kind)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        total = int(self._conn.execute(
            f"SELECT COUNT(*) AS n FROM draw_events{where}", args).fetchone()["n"])
        rows = self._conn.execute(
            f"SELECT * FROM draw_events{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*args, max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
        return [dict(r) for r in rows], total

    def list_groups(self) -> list:
        """记录里出现过的群（含私聊伪群 `__private__`），群名取最近一次非空快照。"""
        rows = self._conn.execute(
            """
            SELECT group_id, COUNT(*) AS n,
              (SELECT d2.group_name FROM draws d2
               WHERE d2.group_id=d.group_id AND d2.group_name<>''
               ORDER BY d2.updated_at DESC LIMIT 1) AS group_name
            FROM draws d WHERE d.group_id<>'' AND d.group_id IS NOT NULL
            GROUP BY group_id ORDER BY n DESC
            """
        ).fetchall()
        out = [{"group_id": r["group_id"],
                "group_name": r["group_name"] or r["group_id"],
                "n": int(r["n"])} for r in rows]
        private = int(self._conn.execute(
            "SELECT COUNT(*) AS n FROM draws WHERE group_id='' OR group_id IS NULL"
        ).fetchone()["n"])
        out.append({"group_id": "__private__", "group_name": "私聊", "n": private})
        return out

    def list_platforms(self) -> list:
        """记录里出现过的平台（筛选下拉用）。"""
        rows = self._conn.execute(
            "SELECT DISTINCT platform FROM draws WHERE platform<>'' AND platform IS NOT NULL"
            " ORDER BY platform"
        ).fetchall()
        return [r["platform"] for r in rows]

    def list_users(self, page: int = 1, size: int = 24, q: str = "",
                   order: str = "last", today: str = "") -> dict:
        """用户列表：分页 + QQ/昵称搜索 + 连签天数（Python 侧按日期集合算）。"""
        page, size = max(1, int(page)), max(1, min(100, int(size)))
        conds, args = [], []
        q = str(q or "").strip()
        if q:
            conds.append("(user_id LIKE ? OR nickname LIKE ?)")
            args.extend([f"%{q}%"] * 2)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        order_sql = {
            "last": "last_date DESC", "draws": "draws DESC",
            "chars": "chars DESC", "rerolls": "rerolls DESC",
        }.get(order, "last_date DESC")
        total = int(self._conn.execute(
            f"SELECT COUNT(*) AS n FROM (SELECT user_id FROM draws{where} GROUP BY user_id)",
            args).fetchone()["n"])
        rows = self._conn.execute(
            f"""
            SELECT user_id, MAX(nickname) AS nickname, COUNT(*) AS draws,
                   COUNT(DISTINCT char_id) AS chars,
                   COALESCE(SUM(nonce),0) AS rerolls,
                   MIN(date) AS first_date, MAX(date) AS last_date
            FROM draws{where} GROUP BY user_id
            ORDER BY {order_sql}, user_id LIMIT ? OFFSET ?
            """, (*args, size, (page - 1) * size),
        ).fetchall()
        dates_by_user: dict = {}
        for r in self._conn.execute("SELECT user_id, date FROM draws"):
            dates_by_user.setdefault(r["user_id"], []).append(r["date"])
        out = []
        for r in rows:
            item = dict(r)
            cur, longest = _streak_stats(dates_by_user.get(item["user_id"], []), today)
            item["streak"], item["longest_streak"] = cur, longest
            out.append(item)
        return {"total": total, "page": page, "size": size, "rows": out}

    def user_profile(self, user_id: str, today: str = "",
                     calendar_months: int = 3) -> dict | None:
        """单用户档案：汇总 / 今日签 / 角色偏好 / 历史签 / 抽签日历 / 流水 / 出图任务。"""
        base = self._conn.execute(
            """
            SELECT MAX(nickname) AS nickname, COUNT(*) AS draws,
                   COUNT(DISTINCT char_id) AS chars,
                   COALESCE(SUM(nonce),0) AS rerolls,
                   MIN(date) AS first_date, MAX(date) AS last_date
            FROM draws WHERE user_id=?
            """, (user_id,),
        ).fetchone()
        if base is None or not base["draws"]:
            return None
        all_dates = [r["date"] for r in self._conn.execute(
            "SELECT date FROM draws WHERE user_id=?", (user_id,))]
        cur, longest = _streak_stats(all_dates, today)
        months = max(1, min(12, int(calendar_months)))
        cal_from = ""
        try:
            anchor = _date.fromisoformat(today) if today else _date.fromisoformat(max(all_dates))
            cal_from = (anchor - timedelta(days=months * 31)).isoformat()
        except (ValueError, TypeError):
            cal_from = ""
        top_chars = self._conn.execute(
            "SELECT char_id, MAX(char_name) AS char_name, COUNT(*) AS n"
            " FROM draws WHERE user_id=? GROUP BY char_id"
            " ORDER BY n DESC, char_id LIMIT 12", (user_id,),
        ).fetchall()
        history = self._conn.execute(
            "SELECT * FROM draws WHERE user_id=? ORDER BY date DESC LIMIT 60", (user_id,)
        ).fetchall()
        events = self._conn.execute(
            "SELECT * FROM draw_events WHERE user_id=? ORDER BY id DESC LIMIT 40", (user_id,)
        ).fetchall()
        jobs = self._conn.execute(
            "SELECT * FROM draw_jobs WHERE user_id=? ORDER BY id DESC LIMIT 20", (user_id,)
        ).fetchall()
        calendar = self._conn.execute(
            "SELECT date, char_id, char_name, rerolled, card_path FROM draws"
            " WHERE user_id=? AND date>=? ORDER BY date", (user_id, cal_from),
        ).fetchall()
        today_row = self.get_draw(user_id, today) if today else None
        return {
            "user_id": user_id,
            "nickname": base["nickname"] or "",
            "today": dict(today_row) if today_row else None,
            "summary": {
                "draws": int(base["draws"]), "chars": int(base["chars"]),
                "rerolls": int(base["rerolls"]), "first_date": base["first_date"],
                "last_date": base["last_date"], "streak": cur,
                "longest_streak": longest,
            },
            "top_chars": [dict(r) for r in top_chars],
            "history": [dict(r) for r in history],
            "events": [dict(r) for r in events],
            "jobs": [dict(r) for r in jobs],
            "calendar": [dict(r) for r in calendar],
        }

    def stats_range(self, date_from: str, date_to: str,
                    tz_name: str = "Asia/Shanghai") -> dict:
        """区间统计：趋势 / 时段 / 角色 / 群 / 立绘决策 / 出图质量。

        绝大多数维度走 SQL 聚合；只有「时段」需要 UTC→配置时区换算，在 Python 侧
        逐行做（区间上限 180 天，数千行足够快，也不依赖 SQLite 的 JSON1 扩展）。
        """
        lo, hi = str(date_from or "0000-01-01"), str(date_to or "9999-12-31")
        span = (lo, hi)

        def one(sql, args=()):
            row = self._conn.execute(sql, args).fetchone()
            return row[0] if row is not None else 0

        draws = int(one("SELECT COUNT(*) FROM draws WHERE date>=? AND date<=?", span))
        users = int(one("SELECT COUNT(DISTINCT user_id) FROM draws WHERE date>=? AND date<=?", span))
        rerolls = int(one("SELECT COALESCE(SUM(nonce),0) FROM draws WHERE date>=? AND date<=?", span))
        chars = int(one("SELECT COUNT(DISTINCT char_id) FROM draws WHERE date>=? AND date<=?", span))
        new_users = int(one(
            "SELECT COUNT(*) FROM (SELECT user_id, MIN(date) AS first FROM draws"
            " GROUP BY user_id) WHERE first>=? AND first<=?", span))
        events_rerolls = int(one(
            "SELECT COUNT(*) FROM draw_events WHERE kind='reroll' AND date>=? AND date<=?", span))
        portraits_added = int(one(
            "SELECT COUNT(*) FROM portraits WHERE date>=? AND date<=?", span))
        jobs_total = int(one("SELECT COUNT(*) FROM draw_jobs WHERE date>=? AND date<=?", span))
        jobs_ok = int(one(
            "SELECT COUNT(*) FROM draw_jobs WHERE status='ok' AND date>=? AND date<=?", span))
        jobs_fail = int(one(
            "SELECT COUNT(*) FROM draw_jobs WHERE status IN ('error','timeout','skipped_no_anima')"
            " AND date>=? AND date<=?", span))
        avg_ms = int(one(
            "SELECT COALESCE(AVG(duration_ms),0) FROM draw_jobs"
            " WHERE status='ok' AND duration_ms>0 AND date>=? AND date<=?", span))

        trend_map: dict = {}
        for r in self._conn.execute(
            "SELECT date, COUNT(*) AS n, COUNT(DISTINCT user_id) AS u,"
            " COALESCE(SUM(nonce),0) AS r FROM draws WHERE date>=? AND date<=?"
            " GROUP BY date", span,
        ):
            trend_map[r["date"]] = {"date": r["date"], "draws": int(r["n"]),
                                    "users": int(r["u"]), "rerolls": int(r["r"]),
                                    "jobs_ok": 0, "jobs_fail": 0}
        for r in self._conn.execute(
            "SELECT date, status, COUNT(*) AS n FROM draw_jobs WHERE date>=? AND date<=?"
            " GROUP BY date, status", span,
        ):
            item = trend_map.setdefault(r["date"], {
                "date": r["date"], "draws": 0, "users": 0, "rerolls": 0,
                "jobs_ok": 0, "jobs_fail": 0})
            if r["status"] == "ok":
                item["jobs_ok"] += int(r["n"])
            elif r["status"] in ("error", "timeout", "skipped_no_anima"):
                item["jobs_fail"] += int(r["n"])
        # 补齐没有数据的日子，前端折线才不会把 7 天画成 2 个点
        try:
            start, end = _date.fromisoformat(lo), _date.fromisoformat(hi)
            if 0 <= (end - start).days <= 366:
                for i in range((end - start).days + 1):
                    d = (start + timedelta(days=i)).isoformat()
                    trend_map.setdefault(d, {"date": d, "draws": 0, "users": 0,
                                             "rerolls": 0, "jobs_ok": 0, "jobs_fail": 0})
        except (ValueError, TypeError):
            pass
        trend = [trend_map[k] for k in sorted(trend_map)]

        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        hours = [0] * 24
        for r in self._conn.execute(
            "SELECT created_at FROM draws WHERE date>=? AND date<=?", span
        ):
            try:
                dt = datetime.strptime(str(r["created_at"]), "%Y-%m-%dT%H:%M:%SZ")
                hours[dt.replace(tzinfo=timezone.utc).astimezone(tz).hour] += 1
            except (ValueError, TypeError):
                continue

        chars_top = [dict(r) for r in self._conn.execute(
            "SELECT char_id, MAX(char_name) AS char_name, COUNT(*) AS n"
            " FROM draws WHERE date>=? AND date<=? GROUP BY char_id"
            " ORDER BY n DESC, char_id LIMIT 12", span)]
        decisions = [dict(r) for r in self._conn.execute(
            "SELECT decision, COUNT(*) AS n FROM draws WHERE date>=? AND date<=?"
            " GROUP BY decision ORDER BY n DESC", span)]
        groups = []
        group_names: dict = {}
        for r in self._conn.execute(
            "SELECT group_id, group_name, date FROM draws WHERE date>=? AND date<=?"
            " ORDER BY date", span,
        ):
            gid = str(r["group_id"] or "") or "__private__"
            if r["group_name"]:
                group_names[gid] = r["group_name"]
        for r in self._conn.execute(
            "SELECT COALESCE(group_id,'') AS gid, COUNT(*) AS n FROM draws"
            " WHERE date>=? AND date<=? GROUP BY gid ORDER BY n DESC", span,
        ):
            gid = str(r["gid"] or "") or "__private__"
            groups.append({"group_id": gid,
                           "group_name": group_names.get(gid) or ("私聊" if gid == "__private__" else gid),
                           "n": int(r["n"])})

        return {
            "range": {"from": lo, "to": hi},
            "totals": {
                "draws": draws, "users": users, "new_users": new_users,
                "rerolls": rerolls, "events_rerolls": events_rerolls,
                "reroll_rate": round(rerolls / draws * 100, 1) if draws else 0.0,
                "chars": chars, "avg_per_user": round(draws / users, 2) if users else 0.0,
                "portraits_added": portraits_added,
                "jobs_total": jobs_total, "jobs_ok": jobs_ok, "jobs_fail": jobs_fail,
                "success_rate": round(jobs_ok / jobs_total * 100, 1) if jobs_total else 0.0,
                "avg_ms": avg_ms,
            },
            "trend": trend,
            "hours": [{"hour": h, "n": hours[h]} for h in range(24)],
            "chars": chars_top,
            "decisions": decisions,
            "groups": groups,
            "sample_days": len(trend),
        }

    # ---------- 调试审计（v0.9.0） ----------

    def debug_log(self, kind: str, target: str = "", detail: str = "") -> None:
        """记一条调试动作；环形保留最近 200 条（不让它变成第二个日志库）。"""
        self._conn.execute(
            "INSERT INTO debug_history (kind, target, detail, created_at) VALUES (?,?,?,?)",
            (str(kind)[:32], str(target)[:120], str(detail)[:500], _now()),
        )
        self._conn.execute(
            "DELETE FROM debug_history WHERE id NOT IN"
            " (SELECT id FROM debug_history ORDER BY id DESC LIMIT 200)"
        )
        self._conn.commit()

    def debug_recent(self, limit: int = 8) -> list:
        """最近查询过的 uid（供调试页快捷标签）。"""
        rows = self._conn.execute(
            "SELECT target FROM debug_history WHERE kind='query' AND target<>''"
            " GROUP BY target ORDER BY MAX(id) DESC LIMIT ?",
            (max(1, min(30, int(limit))),),
        ).fetchall()
        return [r["target"] for r in rows]

    def debug_audit(self, limit: int = 40) -> list:
        rows = self._conn.execute(
            "SELECT * FROM debug_history WHERE kind<>'query'"
            " ORDER BY id DESC LIMIT ?", (max(1, min(200, int(limit))),),
        ).fetchall()
        return [dict(r) for r in rows]
