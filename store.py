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
import hashlib
import json
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
-- v0.23.1：立绘 seq 单调分配器。seq 是「这张图的唯一序列」，不是可复用的
-- 展示编号——彻底删除（硬删行）后也不能把号还回去，否则同角色同日会算出
-- 同一个 portrait_seed_int、归档文件名也可能撞。只增不减，删行不重置。
CREATE TABLE IF NOT EXISTS portrait_seq (
  char_id  TEXT PRIMARY KEY,
  next_seq INTEGER NOT NULL DEFAULT 0
);
-- v0.24.0 关系模型改造 阶段 1：主键 / 业务键分离。自增 id 是唯一编号，
-- char_id / series / name 等语义化字符串保留为 UNIQUE 自然键（抽签种子、
-- 候选排序、日志、工坊 API 全靠它们，不换成数字）。此阶段只建表 + 迁移，
-- 现有读写路径仍走 JSON，行为不变。
CREATE TABLE IF NOT EXISTS assets (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL DEFAULT '',        -- portrait/ref/cover/card/try
  path       TEXT NOT NULL UNIQUE,           -- 相对 data 目录，正斜杠
  sha256     TEXT NOT NULL DEFAULT '',       -- 内容寻址（同内容不同路径也能对上）
  width      INTEGER NOT NULL DEFAULT 0,
  height     INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packs (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  name    TEXT NOT NULL UNIQUE,
  title   TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS works (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  name           TEXT NOT NULL UNIQUE,       -- 作品名（series）
  category       TEXT NOT NULL DEFAULT 'other',
  color          TEXT NOT NULL DEFAULT '',
  cover_asset_id INTEGER,
  created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS characters (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  char_id       TEXT NOT NULL UNIQUE,        -- 语义化自然键，如 hatsune_miku
  pack_id       INTEGER,
  work_id       INTEGER,
  name          TEXT NOT NULL DEFAULT '',
  tags_en       TEXT NOT NULL DEFAULT '',
  verified      INTEGER NOT NULL DEFAULT 0,
  strategy_json TEXT NOT NULL DEFAULT '{}',
  cover_path    TEXT NOT NULL DEFAULT '',
  -- v0.25.0 切读路径：条目全量字段，读出来的结构与 pools/*.json 同构
  category      TEXT NOT NULL DEFAULT 'other',
  tier          TEXT NOT NULL DEFAULT '',
  quote         TEXT NOT NULL DEFAULT '',
  quotes_json   TEXT NOT NULL DEFAULT '[]',
  color         TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_characters_work ON characters(work_id);
CREATE INDEX IF NOT EXISTS idx_assets_kind ON assets(kind);
-- v0.20.0：参考图生成信息（转存/转入参考图时随文件记录；手动上传的图没有）
CREATE TABLE IF NOT EXISTS ref_meta (
  file       TEXT PRIMARY KEY,            -- refs/ 相对路径（与 strategy.refs 同口径）
  prompt     TEXT,
  theme      TEXT NOT NULL DEFAULT '',
  seed       INTEGER,
  source     TEXT NOT NULL DEFAULT '',    -- try（试画转入）/ moved（立绘转入）
  width      INTEGER NOT NULL DEFAULT 0,
  height     INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
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
  decision_note TEXT NOT NULL DEFAULT '',        -- v0.19.4：这次为什么这么出图（可读原因）
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
-- v0.9.5：用户「重置轮次」。调试清签（clear_draws）时对受影响用户 +1，
-- 抽签种子据此整体重派——清签重抽不再回到同一个人（用户明确要求）。
CREATE TABLE IF NOT EXISTS user_epochs (
  user_id TEXT PRIMARY KEY,
  epoch   INTEGER NOT NULL DEFAULT 0
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
    "portraits": {
        # v0.15.0：立绘记录生成时的主题与提示词（详情弹窗查看）；
        # prompt 可空（参考图转立绘的行没有提示词）
        "prompt": "TEXT",
        "theme": "TEXT NOT NULL DEFAULT ''",
        # v0.20.0：变化维度（JSON 数组字符串 "[[label, zh], ...]"，空 = 旧行未记录）
        # + 图片尺寸（归档时读文件；读不到 = 0）
        "dims": "TEXT",
        "width": "INTEGER NOT NULL DEFAULT 0",
        "height": "INTEGER NOT NULL DEFAULT 0",
        # v0.21.0：软删除（停用）。已抽到这张的用户仍可溯源（draws/draw_events
        # 里的 portrait_file 指向的文件与行都还在），只是不再参与抽卡/配额
        "active": "INTEGER NOT NULL DEFAULT 1",
        # v0.24.0：关系模型阶段 1，指向 assets.id（全部图片统一编号）
        "asset_id": "INTEGER",
        # v0.30.0：这张图的萌绘生成耗时（毫秒，成功生成才 >0；参考图转入/
        # 手动恢复等非生成路径为 0）——立绘详情弹窗展示 + 统计逐日趋势
        "duration_ms": "INTEGER NOT NULL DEFAULT 0",
    },
    # v0.25.0：v0.24.0 建的 characters 表只有基础列，切读路径需要完整条目字段
    # （工坊角色卡会显示分类 / 稀有度 / 印象语 / 颜色，缺了就全空）。
    "characters": {
        "category": "TEXT NOT NULL DEFAULT 'other'",
        "tier": "TEXT NOT NULL DEFAULT ''",
        "quote": "TEXT NOT NULL DEFAULT ''",
        "quotes_json": "TEXT NOT NULL DEFAULT '[]'",
        "color": "TEXT NOT NULL DEFAULT ''",
    },
    "draws": {
        "seed_hex": "TEXT NOT NULL DEFAULT ''",
        "char_name": "TEXT NOT NULL DEFAULT ''",
        "series": "TEXT NOT NULL DEFAULT ''",
        "portrait_file": "TEXT NOT NULL DEFAULT ''",
        "decision": "TEXT NOT NULL DEFAULT ''",
        # v0.19.4：决策原因（draw.explain_decision 产出的中文说明）——
        # 只有 decision 一个词说明不了「为什么回退取立绘」，工坊记录要能直接看
        "decision_note": "TEXT NOT NULL DEFAULT ''",
    },
    "draw_events": {
        "decision_note": "TEXT NOT NULL DEFAULT ''",
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
        # v0.23.1：seq 分配是「读 → +1 → 提交」，进程内必须串行，否则抽卡与
        # 工坊补图并发时会拿到同一个 seq（唯一序列 ≠ 连续展示编号）。
        self._seq_lock = threading.RLock()
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """老库就地补列：只加缺的列，不改已有数据、不重建表。"""
        for table, cols in _MIGRATIONS.items():
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for col, ddl in cols.items():
                if col not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        # v0.23.1：老库升级时按现存行初始化计数器（只插不覆盖）。此后即使
        # 彻底删掉最大 seq 的行，next_seq 也不会跟着 MAX(seq) 回退。
        self._conn.execute(
            "INSERT OR IGNORE INTO portrait_seq(char_id, next_seq) "
            "SELECT char_id, COALESCE(MAX(seq), -1) + 1 FROM portraits GROUP BY char_id")

        # v0.33.1：为 v0.30.0 之前生成的历史立绘回填耗时（幂等，见方法 docstring）
        self._backfill_portrait_durations()

    def _backfill_portrait_durations(self) -> None:
        """为 v0.30.0 之前生成的历史立绘回填耗时。

        draw_jobs（生成任务流水）一直记录每张的 duration_ms 且带 char_id +
        seed，与 portraits 的 (char_id, seed) 匹配回填；同键多条成功任务取
        最小值（决定论）。只回填 duration_ms = 0 的行（参考图转入/恢复等非
        生成路径没有对应任务，自然保持 0）；幂等：回填过的行 >0 不再进入
        WHERE。种子不含 epoch，重置轮次后同键可能对应多次生成，取 MIN 为
        可接受的展示近似。
        """
        self._conn.execute(
            "UPDATE portraits SET duration_ms = ("
            "  SELECT MIN(j.duration_ms) FROM draw_jobs j"
            "  WHERE j.char_id = portraits.char_id"
            "    AND j.seed = portraits.seed"
            "    AND j.status = 'ok' AND j.duration_ms > 0)"
            " WHERE duration_ms = 0 AND EXISTS ("
            "  SELECT 1 FROM draw_jobs j2"
            "  WHERE j2.char_id = portraits.char_id"
            "    AND j2.seed = portraits.seed"
            "    AND j2.status = 'ok' AND j2.duration_ms > 0)")

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
        decision_note: str = "",
    ) -> None:
        """写当日结果 + 追加一条抽签流水。

        v0.9.0 起角色名 / 作品 / 种子 / 立绘来源 / 立绘决策都随签快照落库：
        角色条目日后被删或被改名，历史记录仍读得懂。
        v0.19.4：`decision_note` 是这次决策的可读原因（draw.explain_decision），
        同时写进 draws（当前状态）与 draw_events（流水），换老婆前的签也留得住。
        """
        now = _now()
        nonce = int(nonce)
        self._conn.execute(
            """
            INSERT INTO draws (user_id, date, char_id, nonce, rerolled, nickname,
                               group_id, group_name, platform, seed_hex, char_name,
                               series, portrait_file, decision, decision_note,
                               created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id, date) DO UPDATE SET
              char_id=excluded.char_id, nonce=excluded.nonce,
              rerolled=excluded.rerolled, nickname=excluded.nickname,
              group_id=excluded.group_id, group_name=excluded.group_name,
              platform=excluded.platform, seed_hex=excluded.seed_hex,
              char_name=excluded.char_name, series=excluded.series,
              portrait_file=excluded.portrait_file, decision=excluded.decision,
              decision_note=excluded.decision_note,
              updated_at=excluded.updated_at
            """,
            (user_id, date, char_id, nonce, int(bool(rerolled)), nickname,
             group_id, group_name, platform, seed_hex, char_name, series,
             portrait_file, decision, decision_note, now, now),
        )
        self._conn.execute(
            """
            INSERT INTO draw_events (user_id, date, char_id, char_name, series,
                                     nonce, kind, seed_hex, decision, decision_note,
                                     portrait_file, group_id, group_name, platform,
                                     created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (user_id, date, char_id, char_name, series, nonce,
             "reroll" if nonce > 0 else "first", seed_hex, decision,
             decision_note, portrait_file, group_id, group_name, platform, now),
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
                     seq: int, strategy: str,
                     prompt: str | None = None, theme: str = "",
                     dims: str = "", width: int = 0, height: int = 0,
                     duration_ms: int = 0) -> None:
        # v0.15.0：prompt/theme 随档记录（立绘详情弹窗展示）；prompt 可空。
        # v0.20.0：dims（变化维度 JSON）/ 图片尺寸随档记录。
        # v0.30.0：生成耗时（毫秒）随档记录——参考图转入/回收站恢复等非生成
        # 路径不传（0），详情弹窗按 >0 显示。
        self._conn.execute(
            "INSERT INTO portraits (char_id, file, seed, date, seq, strategy, created_at,"
            " prompt, theme, dims, width, height, duration_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (char_id, file, int(seed), date, int(seq), strategy, _now(),
             prompt, str(theme or ""), str(dims or ""),
             int(width or 0), int(height or 0), max(0, int(duration_ms or 0))),
        )
        # v0.24.0：每张立绘登记进 assets（全局唯一编号）并回填 asset_id。
        # 同一路径被复用时 register_asset 只补哈希 / 尺寸，不重复建记录。
        aid = self.register_asset("portrait", file,
                                  width=int(width or 0), height=int(height or 0))
        self._conn.execute(
            "UPDATE portraits SET asset_id=? WHERE file=?", (aid, file))
        # v0.23.1：任何写入都把计数器推到 max(当前, 本次 seq+1)。外部直接
        # 指定 seq 的路径（参考图转立绘、老数据、测试）也不会让后续分配撞号。
        with self._seq_lock:
            self._conn.execute(
                "INSERT INTO portrait_seq(char_id, next_seq) VALUES(?, ?) "
                "ON CONFLICT(char_id) DO UPDATE SET "
                "next_seq = MAX(next_seq, excluded.next_seq)",
                (char_id, int(seq) + 1))
        self._conn.commit()

    # ---------- 关系模型（v0.24.0 阶段 1） ----------

    @staticmethod
    def _sha256_of(path) -> str:
        try:
            h = hashlib.sha256()
            with Path(path).open("rb") as fp:
                for chunk in iter(lambda: fp.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return ""

    def register_asset(self, kind: str, path: str, sha256: str = "",
                       width: int = 0, height: int = 0) -> int:
        """登记一张图片，返回全局唯一 `assets.id`；同路径复用不重复建记录。

        覆盖立绘 / 参考图 / 封面 / 卡片 / 试画产物。封面若与已有图片同路径，
        复用同一个 asset_id ——「一张图一个编号」，不按引用次数重复发号。
        """
        rel = str(path or "").strip().replace("\\", "/")
        if not rel:
            return 0
        row = self._conn.execute(
            "SELECT id, sha256, width, height FROM assets WHERE path=?",
            (rel,)).fetchone()
        if row is not None:
            if (sha256 and not row["sha256"]) or (width and not row["width"]):
                self._conn.execute(
                    "UPDATE assets SET sha256=COALESCE(NULLIF(?, ''), sha256), "
                    "width=CASE WHEN ? > 0 THEN ? ELSE width END, "
                    "height=CASE WHEN ? > 0 THEN ? ELSE height END WHERE id=?",
                    (sha256, width, width, height, height, row["id"]))
                self._conn.commit()
            return int(row["id"])
        cur = self._conn.execute(
            "INSERT INTO assets(kind, path, sha256, width, height, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (str(kind or ""), rel, str(sha256 or ""), int(width or 0),
             int(height or 0), _now()))
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def relational_status(self) -> dict:
        """各表数量（工坊只读状态接口用）。"""
        out = {}
        for table in ("assets", "packs", "works", "characters", "portraits"):
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            out[table] = int(row["n"]) if row else 0
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM portraits WHERE asset_id > 0").fetchone()
        out["portraits_with_asset"] = int(row["n"]) if row else 0
        return out

    def migrate_relational(self, pools_dir, data_dir,
                           series_scope=None, logger=None) -> dict:
        """幂等迁移：卷 JSON → packs/works/characters，图片 → assets。

        只纳入 `series ∈ series_scope`（默认原神 / 鸣潮 / 异环），其余作品留在
        JSON 里不删，等稳定后再纳入。重复启动不产生重复行，新表可随时重建。
        阶段 1 不改任何现有读写路径 —— JSON 仍是真源，抽卡 / 工坊行为不变。
        """
        scope = self.series_scope(series_scope)
        pools_dir, data_dir = Path(pools_dir), Path(data_dir)
        stats = {"scope": sorted(scope), "skipped": 0}
        # 1) 卷 / 作品 / 角色：统一走 sync_pack_file（同一套 upsert + 清理逻辑，
        #    确保「启动迁移」与「工坊写完同步」不会出现两套口径）
        for path in (sorted(pools_dir.glob("*.json"))
                     if pools_dir.is_dir() else []):
            res = self.sync_pack_file(path, series_scope=scope)
            stats["skipped"] += int(res.get("skipped") or 0)
        self._conn.commit()
        # 2) 立绘行：登记并回填 asset_id
        for row in self._conn.execute(
                "SELECT file, width, height FROM portraits").fetchall():
            rel = str(row["file"] or "").replace("\\", "/")
            aid = self.register_asset(
                "portrait", rel, sha256=self._sha256_of(data_dir / rel),
                width=int(row["width"] or 0), height=int(row["height"] or 0))
            self._conn.execute("UPDATE portraits SET asset_id=? WHERE file=?",
                               (aid, row["file"]))
        self._conn.commit()
        # 3) data 目录下其它图片（参考图 / 卡片 / 试画缓存）
        exts = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        for kind, sub in (("ref", "refs"), ("card", "cards"),
                          ("try", "try_cache")):
            folder = data_dir / sub
            if not folder.is_dir():
                continue
            for f in sorted(folder.iterdir()):
                if not f.is_file() or f.suffix.lower() not in exts:
                    continue
                self.register_asset(kind, f"{sub}/{f.name}",
                                    sha256=self._sha256_of(f))
        # 4) 角色封面（可能落在 portraits/ 与 refs/ 之外）
        for row in self._conn.execute(
                "SELECT cover_path FROM characters WHERE cover_path <> ''").fetchall():
            rel = str(row["cover_path"] or "").replace("\\", "/")
            if rel:
                self.register_asset("cover", rel)
        for table in ("assets", "packs", "works", "characters"):
            stats[table] = int(self._conn.execute(
                f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
        if logger is not None:
            logger.info(f"[moe_waifu] 关系模型迁移（阶段 1）：{stats}")
        return stats

    _SERIES_SCOPE = ("原神", "鸣潮", "异环")

    @classmethod
    def series_scope(cls, scope=None) -> set:
        """首版纳入新模型的作品集合（用户口径：先只做这三个，稳定后再加）。"""
        return {str(s).strip() for s in
                (scope or cls._SERIES_SCOPE) if str(s).strip()}

    def _upsert_character(self, raw, pack_id: int, scope: set) -> str:
        """upsert 单条角色（含作品），返回 char_id；不合法 / 不在 scope → ""。"""
        if not isinstance(raw, dict):
            return ""
        cid = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()
        series = str(raw.get("series") or "").strip()
        if not cid or not name or series not in scope:
            return ""
        self._conn.execute(
            "INSERT OR IGNORE INTO works(name, category, color, created_at) "
            "VALUES(?,?,?,?)",
            (series, str(raw.get("category") or "other"),
             str(raw.get("color") or ""), _now()))
        wrow = self._conn.execute(
            "SELECT id FROM works WHERE name=?", (series,)).fetchone()
        work_id = int(wrow["id"]) if wrow else 0
        st = raw.get("strategy") if isinstance(raw.get("strategy"), dict) else {}
        quotes = raw.get("quotes") if isinstance(raw.get("quotes"), list) else []
        quotes = [str(q).strip() for q in quotes if str(q).strip()]
        single = str(raw.get("quote") or "").strip()
        if single and single not in quotes:
            quotes.insert(0, single)
        self._conn.execute(
            "INSERT INTO characters(char_id, pack_id, work_id, name, tags_en,"
            " verified, strategy_json, cover_path, category, tier, quote,"
            " quotes_json, color, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            # 跨卷重复 id：先同步到的卷胜。game 卷按文件名排在 genshin /
            # wuthering_waves 等遗留卷之前，后到的旧卷不能把角色抢过去，
            # 否则异环 20 位会挂在遗留卷上、工坊卷筛选错乱（v0.25.1）。
            "ON CONFLICT(char_id) DO UPDATE SET pack_id=characters.pack_id,"
            " work_id=excluded.work_id, name=excluded.name,"
            " tags_en=excluded.tags_en, verified=excluded.verified,"
            " strategy_json=excluded.strategy_json,"
            " cover_path=excluded.cover_path, category=excluded.category,"
            " tier=excluded.tier, quote=excluded.quote,"
            " quotes_json=excluded.quotes_json, color=excluded.color",
            (cid, pack_id, work_id, name, str(raw.get("tags_en") or ""),
             1 if raw.get("verified") else 0,
             json.dumps(st, ensure_ascii=False),
             str(raw.get("cover") or ""),
             str(raw.get("category") or "other"),
             str(raw.get("tier") or ""),
             single or (quotes[0] if quotes else ""),
             json.dumps(quotes, ensure_ascii=False),
             str(raw.get("color") or ""), _now()))
        return cid

    def sync_pack_file(self, path, series_scope=None) -> dict:
        """单个卷 JSON → 新表（upsert + 删除该卷里已消失的角色），幂等。

        v0.25.0 切读路径后，工坊所有写操作仍落 JSON（真源不动），写完经
        `webui_api._json_save` 钩子调这里，读路径立刻能看到改动。
        非 scope 角色不纳入（首版只有三游戏），也**不会删除其它卷的数据**。
        """
        scope = self.series_scope(series_scope)
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"error": "json"}
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            return {"error": "shape"}
        pack_name = str(data.get("pack") or path.stem).strip()
        if not pack_name:
            return {"error": "pack"}
        title = str(data.get("title") or pack_name)
        enabled = 1 if data.get("enabled", True) else 0
        self._conn.execute(
            "INSERT OR IGNORE INTO packs(name, title, enabled) VALUES(?,?,?)",
            (pack_name, title, enabled))
        self._conn.execute(
            "UPDATE packs SET title=?, enabled=? WHERE name=?",
            (title, enabled, pack_name))
        prow = self._conn.execute(
            "SELECT id FROM packs WHERE name=?", (pack_name,)).fetchone()
        pack_id = int(prow["id"]) if prow else 0
        kept, skipped = set(), 0
        for raw in data["entries"]:
            cid = self._upsert_character(raw, pack_id, scope)
            if cid:
                kept.add(cid)
            else:
                skipped += 1
        existing = {r["char_id"] for r in self._conn.execute(
            "SELECT char_id FROM characters WHERE pack_id=?", (pack_id,))}
        gone = existing - kept
        for cid in gone:
            self._conn.execute("DELETE FROM characters WHERE char_id=?", (cid,))
        # v0.25.2：该卷没有任何「已纳入」角色（v0.22.0 遗留的 genshin/nte/
        # wuthering_waves 旧卷、或只有非 scope 角色的卷）→ 删掉 packs 记录，
        # 前端卷筛选不再出现一堆 0 条目的空卷。**卷 JSON 文件保持不动**，
        # 将来把作品加回 series_scope 后重新迁移就会自动恢复。
        if not kept:
            self._conn.execute("DELETE FROM packs WHERE id=?", (pack_id,))
        self._conn.commit()
        return {"pack": pack_name, "kept": len(kept), "skipped": skipped,
                "removed": len(gone), "pack_dropped": not kept}

    def load_packs(self) -> list:
        """按 `pool.load_packs` 同构返回：卷 → 条目（原始形状，供 normalize_entry）。

        只含已纳入新表的角色（首版 = 原神 / 鸣潮 / 异环）；空库返回 []，调用方
        据此回退 JSON。字段与卷 JSON 一一对应，工坊 / 抽卡代码零改动。
        """
        out = []
        prows = self._conn.execute(
            "SELECT id, name, title, enabled FROM packs ORDER BY name").fetchall()
        for p in prows:
            rows = self._conn.execute(
                "SELECT c.*, w.name AS work_name FROM characters c "
                "LEFT JOIN works w ON w.id = c.work_id "
                "WHERE c.pack_id=? ORDER BY c.char_id", (p["id"],)).fetchall()
            entries = []
            for r in rows:
                try:
                    st = json.loads(r["strategy_json"] or "{}")
                except Exception:
                    st = {}
                if not isinstance(st, dict):
                    st = {}
                try:
                    quotes = json.loads(r["quotes_json"] or "[]")
                except Exception:
                    quotes = []
                if not isinstance(quotes, list):
                    quotes = []
                e = {"id": r["char_id"], "name": r["name"],
                     "series": str(r["work_name"] or ""),
                     "pack": p["name"],
                     "category": r["category"] or "other",
                     "tier": r["tier"] or "",
                     "tags_en": r["tags_en"] or "",
                     "verified": bool(r["verified"]),
                     "strategy": st,
                     "color": r["color"] or "#E8B4C8"}
                if r["quote"]:
                    e["quote"] = r["quote"]
                if quotes:
                    e["quotes"] = quotes
                if r["cover_path"]:
                    e["cover"] = r["cover_path"]
                entries.append(e)
            if not entries:
                # v0.25.2 兜底：历史版本可能留下 0 条目的卷记录（JSON 已删 /
                # 角色全被合并到别的卷），不进筛选，避免界面上一排空卷。
                continue
            out.append({"pack": p["name"], "title": p["title"] or p["name"],
                        "enabled": bool(p["enabled"]), "entries": entries})
        return out

    def portrait_by_file(self, file: str) -> dict | None:
        """按相对路径取立绘行（转参考图时把 prompt/theme/seed 带走）。"""
        row = self._conn.execute(
            "SELECT * FROM portraits WHERE file=?", (str(file or ""),),
        ).fetchone()
        return dict(row) if row else None

    # ---------- ref_meta（v0.20.0：参考图的生成信息） ----------

    def set_ref_meta(self, file: str, prompt: str | None = None, theme: str = "",
                     seed: int | None = None, source: str = "",
                     width: int = 0, height: int = 0) -> None:
        """随文件记录参考图的生成信息；同路径重复转入时覆盖（内容寻址同文件）。"""
        self._conn.execute(
            "INSERT INTO ref_meta (file, prompt, theme, seed, source, width, height,"
            " created_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(file) DO UPDATE SET prompt=excluded.prompt,"
            " theme=excluded.theme, seed=excluded.seed, source=excluded.source,"
            " width=excluded.width, height=excluded.height",
            (str(file or ""), prompt, str(theme or ""),
             None if seed is None else int(seed), str(source or ""),
             int(width or 0), int(height or 0), _now()),
        )
        self._conn.commit()

    def get_ref_meta(self, files) -> dict:
        """批量取 ref_meta：{file: 行 dict}；不在表里的路径不出现在结果中。"""
        fs = [str(f) for f in (files or []) if str(f or "").strip()]
        out: dict = {}
        for i in range(0, len(fs), 200):
            chunk = fs[i:i + 200]
            q = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT * FROM ref_meta WHERE file IN ({q})", chunk).fetchall()
            out.update({r["file"]: dict(r) for r in rows})
        return out

    def next_portrait_seq(self, char_id: str) -> int:
        """查询下一个可用 seq（**只读、不消耗**）：预览与实际生成同口径。

        v0.23.1：优先读持久化计数器 `portrait_seq.next_seq`。旧口径
        `MAX(seq)+1` 在「彻底删除最大 seq 那张」后会回退，导致同一天算出
        相同的 portrait_seed_int、归档文件名也可能撞；计数器只增不减，
        删行 / 停用都不回收号。计数器缺行（全新角色）时回退 MAX(seq)+1。
        真正要占用一个号请用 alloc_portrait_seq()。
        """
        with self._seq_lock:
            row = self._conn.execute(
                "SELECT next_seq FROM portrait_seq WHERE char_id=?",
                (str(char_id),)).fetchone()
            if row is not None:
                return int(row["next_seq"])
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM portraits "
                "WHERE char_id=?", (str(char_id),)).fetchone()
            return int(row["n"]) if row is not None else 0

    def alloc_portrait_seq(self, char_id: str) -> int:
        """原子分配一个 seq（实际生成路径用），返回后该号即被占用、永不复用。

        RLock 串行化「读计数器 → +1 写回 → commit」，避免抽卡与工坊补图
        并发时两张图拿到同一号。允许跳号（生成失败 / 取消也消耗一个号）——
        seq 是 ID 而不是展示序号，界面上的连续编号由接口另给 display_no。
        """
        with self._seq_lock:
            row = self._conn.execute(
                "SELECT next_seq FROM portrait_seq WHERE char_id=?",
                (str(char_id),)).fetchone()
            if row is None:
                base = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM portraits "
                    "WHERE char_id=?", (str(char_id),)).fetchone()
                n = int(base["n"]) if base is not None else 0
                self._conn.execute(
                    "INSERT INTO portrait_seq(char_id, next_seq) VALUES(?, ?)",
                    (str(char_id), n + 1))
            else:
                n = int(row["next_seq"])
                self._conn.execute(
                    "UPDATE portrait_seq SET next_seq=? WHERE char_id=?",
                    (n + 1, str(char_id)))
            self._conn.commit()
            return n

    def count_portraits(self, char_id: str) -> int:
        # v0.21.0：只数启用行——停用的立绘不占配额（清库/满库判定随之上移）
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM portraits WHERE char_id=? AND active=1",
            (char_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def portrait_by_date(self, char_id: str, date: str) -> dict | None:
        """当天已生成的立绘（当日复用）；同天多张取 seq 最大的一张。
        v0.21.0：停用的行不参与当日复用。"""
        row = self._conn.execute(
            "SELECT * FROM portraits WHERE char_id=? AND date=? AND active=1"
            " ORDER BY seq DESC LIMIT 1",
            (char_id, date),
        ).fetchone()
        return dict(row) if row else None

    def list_portraits(self, char_id: str, include_disabled: bool = False) -> list:
        """按 seq 升序返回该角色立绘（满库取图按此排序 + pool_index 取模）。

        v0.21.0：默认只返回启用行（抽卡/配额语义）；工坊立绘墙传
        include_disabled=True 连停用行一起拿（前端默认隐藏、可切换查看）。
        """
        sql = "SELECT * FROM portraits WHERE char_id=?"
        if not include_disabled:
            sql += " AND active=1"
        sql += " ORDER BY seq ASC, created_at ASC, id ASC"
        rows = self._conn.execute(sql, (char_id,)).fetchall()
        return [dict(r) for r in rows]

    def list_portraits_all(self) -> list:
        """全部立绘行（工坊孤立图发现用）。"""
        rows = self._conn.execute(
            "SELECT * FROM portraits ORDER BY char_id, seq ASC, created_at ASC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def reassign_portraits(self, from_id: str, to_id: str) -> int:
        """v0.23.0 去重：旧 id 的立绘行整体改挂到保留 id（不删行、不动文件）。

        portraits 只有 (char_id, seq) 普通索引、无 UNIQUE 约束，两个 id 的
        seq 撞号不会冲突；取图按 (seq, created_at, id) 排序由后两列决胜。
        返回迁移行数。删掉重复角色条目后，这些立绘才不会变成孤儿。
        """
        if not from_id or not to_id or from_id == to_id:
            return 0
        cur = self._conn.execute(
            "UPDATE portraits SET char_id=? WHERE char_id=?", (to_id, from_id))
        self._conn.commit()
        return int(cur.rowcount or 0)

    def delete_portrait(self, file: str) -> None:
        self._conn.execute("DELETE FROM portraits WHERE file=?", (file,))
        self._conn.commit()

    def set_portrait_active(self, file: str, active: bool) -> int:
        """v0.21.0：软删除（停用）/ 恢复。返回受影响行数（0 = 路径不存在）。"""
        cur = self._conn.execute(
            "UPDATE portraits SET active=? WHERE file=?",
            (1 if active else 0, str(file or "")),
        )
        self._conn.commit()
        return cur.rowcount if cur.rowcount > 0 else 0

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

    def char_summary(self, char_id: str) -> dict:
        """角色速览（v0.31.0）：首次/最近被抽日期、被抽次数、立绘数、平均生成耗时。

        draws 表是抽签流水（含重抽覆盖前的历史），COUNT = 累计被抽次数；
        portraits 只统计现存行；耗时只算成功生成路径（duration_ms>0）。
        """
        def one(sql, args=()):
            row = self._conn.execute(sql, args).fetchone()
            return row[0] if row is not None else None
        return {
            "first_draw": str(one(
                "SELECT MIN(date) FROM draws WHERE char_id=?", (char_id,)) or ""),
            "last_draw": str(one(
                "SELECT MAX(date) FROM draws WHERE char_id=?", (char_id,)) or ""),
            "draw_count": int(one(
                "SELECT COUNT(*) FROM draws WHERE char_id=?", (char_id,)) or 0),
            "portrait_count": int(one(
                "SELECT COUNT(*) FROM portraits WHERE char_id=?", (char_id,)) or 0),
            "portrait_avg_ms": int(one(
                "SELECT COALESCE(AVG(duration_ms),0) FROM portraits"
                " WHERE char_id=? AND duration_ms>0", (char_id,)) or 0),
        }

    # ---------- 调试 / 重置（v0.8.0） ----------
    # 文件删除一律由调用方（webui 层）做：这里只动库并返回受影响路径清单。

    def clear_draws(self, user_id: str = "", date: str = "") -> list[dict]:
        """删签（可按 user/date 过滤，两者都空 = 全部）。返回被删行（含 card_path）。

        v0.9.5 起删签会做三件连带事：
        1. 按 draw_events 流水扣减 user_char_counts / char_stats（抽到的次数
           也减回去，统计不虚高）——次数是逐次 bump 的，流水与之一一对应；
        2. char_stats.last_date 从剩余 draws 重算；
        3. 受影响用户的「重置轮次」epoch +1——下次抽取种子整体重派，
           重置后再抽不再是同一个人。
        """
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
        # 扣减次数：以 draw_events 流水为准（每次抽/换老婆都有一条，与 bump 一一对应）
        for r in self._conn.execute(
            f"SELECT user_id, char_id, COUNT(*) AS n FROM draw_events{where}"
            " GROUP BY user_id, char_id", args,
        ):
            self._conn.execute(
                "UPDATE user_char_counts SET count=count-? WHERE user_id=? AND char_id=?",
                (int(r["n"]), r["user_id"], r["char_id"]),
            )
        for r in self._conn.execute(
            f"SELECT char_id, COUNT(*) AS n FROM draw_events{where} GROUP BY char_id", args,
        ):
            self._conn.execute(
                "UPDATE char_stats SET total=total-? WHERE char_id=?",
                (int(r["n"]), r["char_id"]),
            )
        # 清零的计数行直接删掉（免得列表里全是 0）；last_date 从剩余 draws 重算
        self._conn.execute("DELETE FROM user_char_counts WHERE count<=0")
        self._conn.execute("DELETE FROM char_stats WHERE total<=0")
        for r in self._conn.execute(
            "SELECT char_id, MAX(date) AS last FROM draws GROUP BY char_id"
        ):
            self._conn.execute(
                "UPDATE char_stats SET last_date=? WHERE char_id=?",
                (r["last"], r["char_id"]),
            )
        self._conn.execute(f"DELETE FROM draws{where}", args)
        # 流水跟着删，避免「签没了、事件还在」的假历史（两张表列名兼容同一 where）
        self._conn.execute(f"DELETE FROM draw_events{where}", args)
        # 重置轮次 +1：受影响用户下次抽卡种子重派（换个人）
        if out:
            affected = list({r["user_id"] for r in out if r.get("user_id")})
            if not affected and user_id:
                affected = [user_id]
            for uid in affected:
                self._conn.execute(
                    "INSERT INTO user_epochs (user_id, epoch) VALUES (?, 1)"
                    " ON CONFLICT(user_id) DO UPDATE SET epoch=epoch+1",
                    (uid,),
                )
        self._conn.commit()
        return out

    def get_epoch(self, user_id: str) -> int:
        """用户重置轮次（抽签种子维度之一，见 derive_seed）。"""
        row = self._conn.execute(
            "SELECT epoch FROM user_epochs WHERE user_id=?", (user_id,)
        ).fetchone()
        return int(row["epoch"]) if row else 0

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
                                    "jobs_ok": 0, "jobs_fail": 0, "avg_ms": 0}
        for r in self._conn.execute(
            "SELECT date, status, COUNT(*) AS n FROM draw_jobs WHERE date>=? AND date<=?"
            " GROUP BY date, status", span,
        ):
            item = trend_map.setdefault(r["date"], {
                "date": r["date"], "draws": 0, "users": 0, "rerolls": 0,
                "jobs_ok": 0, "jobs_fail": 0, "avg_ms": 0})
            if r["status"] == "ok":
                item["jobs_ok"] += int(r["n"])
            elif r["status"] in ("error", "timeout", "skipped_no_anima"):
                item["jobs_fail"] += int(r["n"])
        # v0.30.0：逐日平均生成耗时（成功项，毫秒；当天无成功项 = 0，前端跳过）
        for r in self._conn.execute(
            "SELECT date, COALESCE(AVG(duration_ms),0) AS avg_ms FROM draw_jobs"
            " WHERE date>=? AND date<=? AND status='ok' AND duration_ms>0"
            " GROUP BY date", span,
        ):
            trend_map.setdefault(r["date"], {
                "date": r["date"], "draws": 0, "users": 0, "rerolls": 0,
                "jobs_ok": 0, "jobs_fail": 0, "avg_ms": 0})
            trend_map[r["date"]]["avg_ms"] = int(r["avg_ms"])
        # 补齐没有数据的日子，前端折线才不会把 7 天画成 2 个点
        try:
            start, end = _date.fromisoformat(lo), _date.fromisoformat(hi)
            if 0 <= (end - start).days <= 366:
                for i in range((end - start).days + 1):
                    d = (start + timedelta(days=i)).isoformat()
                    trend_map.setdefault(d, {"date": d, "draws": 0, "users": 0,
                                             "rerolls": 0, "jobs_ok": 0, "jobs_fail": 0,
                                             "avg_ms": 0})
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
