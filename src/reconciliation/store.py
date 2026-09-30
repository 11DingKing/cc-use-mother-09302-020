"""SQLite 仅追加谱系存储。

设计原则：
- 名册、签到、场次事件、补办关系、转学归属、身份确认、报告、更正单均为**不可变事实行**；
- 全局 ``event_log`` 给出统一先后顺序（seq），支撑谱系追溯与多人封账决胜；
- 仅有的状态迁移（报告 draft→signed、确认队列 open→resolved、转学关闭旧归属期）
  通过条件 UPDATE 原子完成，同时向 event_log 留痕；
- (source_batch_id, source_record_id) 唯一约束保证重复报送幂等。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .errors import ConflictError, DuplicateRecordError, NotFoundError

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT,
    event_type TEXT NOT NULL,
    aggregate_id TEXT,
    payload TEXT NOT NULL,
    source_batch_id TEXT
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    department TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    note TEXT,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persons (
    person_key TEXT PRIMARY KEY,
    first_roster_id TEXT NOT NULL,
    created_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rosters (
    roster_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    person_key TEXT NOT NULL,
    name TEXT NOT NULL,
    gender TEXT,
    birth_date TEXT,
    id_number_fp TEXT,
    weak_fp TEXT NOT NULL,
    grade TEXT,
    class_name TEXT,
    session_id TEXT,                          -- 报名/点名所针对的场次（一般学籍名册可空）
    school_code TEXT NOT NULL,
    school_name TEXT NOT NULL,
    source_batch_id TEXT NOT NULL,
    source_department TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    UNIQUE(source_batch_id, source_record_id)
);

-- 身份确认“确认重复”后产生的归并别名（仅追加）。
CREATE TABLE IF NOT EXISTS person_aliases (
    roster_id TEXT PRIMARY KEY,
    person_key TEXT NOT NULL,        -- 归并后的规范自然人
    queue_id TEXT NOT NULL,
    decided_at TEXT NOT NULL
);

-- 按人保存的学校归属期，转学通过关闭旧区间+追加新区间表达。
CREATE TABLE IF NOT EXISTS enrollments (
    enrollment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_key TEXT NOT NULL,
    school_code TEXT NOT NULL,
    school_name TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    reason TEXT,
    source_batch_id TEXT,
    seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    hold_date TEXT NOT NULL,
    school_code TEXT NOT NULL,
    school_name TEXT,
    initial_status TEXT NOT NULL,            -- 发布 / 补办
    reissues_for_session_id TEXT,            -- 补办关系
    source_batch_id TEXT NOT NULL,
    source_department TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    created_seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_batch_id, source_record_id)
);

CREATE TABLE IF NOT EXISTS session_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,                -- 发布 / 取消 / 补办
    at_time TEXT NOT NULL,
    actor TEXT,
    reason TEXT,
    source_batch_id TEXT
);

CREATE TABLE IF NOT EXISTS checkins (
    checkin_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    roster_id TEXT NOT NULL,
    person_key_at_ingest TEXT NOT NULL,
    person_name TEXT NOT NULL,
    checkin_time TEXT,
    status TEXT NOT NULL,                    -- 正常 / 迟到 / 缺席 / 无效
    method TEXT,
    source_batch_id TEXT NOT NULL,
    source_department TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    UNIQUE(source_batch_id, source_record_id)
);

CREATE TABLE IF NOT EXISTS identity_queue (
    queue_id TEXT PRIMARY KEY,
    open_seq INTEGER NOT NULL,
    roster_id_a TEXT NOT NULL,
    roster_id_b TEXT NOT NULL,
    weak_fp TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',    -- open / resolved
    resolution TEXT,                         -- 确认唯一 / 确认重复
    canonical_person_key TEXT,
    decided_by TEXT,
    decided_at TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    school_code TEXT NOT NULL,
    scope_from TEXT NOT NULL,
    scope_to TEXT NOT NULL,
    caliber_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',   -- draft / signed
    created_by TEXT,
    created_at TEXT NOT NULL,
    signed_by TEXT,
    signed_at TEXT,
    snapshot_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corrections (
    correction_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    reason TEXT NOT NULL,
    operator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '生效'
);

CREATE TABLE IF NOT EXISTS correction_items (
    correction_id TEXT NOT NULL,
    item_no INTEGER NOT NULL,
    metric TEXT NOT NULL,
    delta INTEGER NOT NULL,
    ref_type TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (correction_id, item_no)
);

CREATE INDEX IF NOT EXISTS idx_rosters_strong ON rosters(id_number_fp);
CREATE INDEX IF NOT EXISTS idx_rosters_weak ON rosters(weak_fp);
CREATE INDEX IF NOT EXISTS idx_checkins_session ON checkins(session_id);
CREATE INDEX IF NOT EXISTS idx_enroll_person ON enrollments(person_key);
CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(hold_date);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        # HTTP 为多线程服务，全部写事务经同一把锁串行化；
        # 配合条件 UPDATE，保证多人同时封账时首签决胜。
        self._tx_lock = threading.RLock()
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def tx(self):
        with self._tx_lock:
            with self.conn:
                yield

    # ---- 基础 ----------------------------------------------------------------

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def log_event(self, event_type: str, aggregate_id: str, payload: dict[str, Any],
                  *, actor: str | None = None, source_batch_id: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO event_log(ts, actor, event_type, aggregate_id, payload, source_batch_id) "
            "VALUES (?,?,?,?,?,?)",
            (now_iso(), actor, event_type, aggregate_id,
             json.dumps(payload, ensure_ascii=False, sort_keys=True), source_batch_id),
        )
        return int(cur.lastrowid)

    def register_batch(self, batch_id: str, department: str, submitted_at: str, note: str | None) -> None:
        try:
            self.conn.execute(
                "INSERT INTO batches(batch_id, department, submitted_at, note, ingested_at) "
                "VALUES (?,?,?,?,?)",
                (batch_id, department, submitted_at, note, now_iso()),
            )
            self.log_event("batch_registered", batch_id,
                           {"department": department, "submitted_at": submitted_at, "note": note})
        except sqlite3.IntegrityError as exc:
            raise DuplicateRecordError(f"报送批次已存在：{batch_id}") from exc

    # ---- 名册 ----------------------------------------------------------------

    def insert_roster(self, data: dict[str, Any], *, person_key: str, strong_fp: str, weak_fp: str) -> dict[str, Any]:
        sql = (
            "INSERT INTO rosters(roster_id, seq, person_key, name, gender, birth_date, id_number_fp, weak_fp, "
            "grade, class_name, session_id, school_code, school_name, source_batch_id, source_department, "
            "source_record_id, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        seq = self.log_event("roster_ingested", data["roster_id"],
                             {"person_key": person_key, "school_code": data["school_code"],
                              "source_record_id": data["source_record_id"]},
                             source_batch_id=data["source_batch_id"])
        try:
            self.conn.execute(sql, (
                data["roster_id"], seq, person_key, data["name"], data.get("gender"), data.get("birth_date"),
                strong_fp or None, weak_fp, data.get("grade"), data.get("class_name"), data.get("session_id"),
                data["school_code"], data["school_name"], data["source_batch_id"],
                data["source_department"], data["source_record_id"], now_iso(),
            ))
        except sqlite3.IntegrityError as exc:
            raise DuplicateRecordError(
                f"名册记录重复：{data['source_batch_id']}/{data['source_record_id']}") from exc
        return self.get_roster(data["roster_id"])

    def ensure_person(self, person_key: str, roster_id: str, seq: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO persons(person_key, first_roster_id, created_seq) VALUES (?,?,?)",
            (person_key, roster_id, seq),
        )

    def find_strong_match(self, strong_fp: str) -> dict[str, Any] | None:
        if not strong_fp:
            return None
        row = self.conn.execute(
            "SELECT * FROM rosters WHERE id_number_fp = ? ORDER BY seq LIMIT 1", (strong_fp,)).fetchone()
        return dict(row) if row else None

    def find_weak_matches(self, weak_fp: str, exclude_roster_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM rosters WHERE weak_fp = ? AND roster_id <> ? ORDER BY seq",
            (weak_fp, exclude_roster_id)).fetchall()
        return [dict(r) for r in rows]

    def open_identity_queue(self, queue_id: str, roster_a: dict[str, Any], roster_b: dict[str, Any],
                            weak_fp: str, note: str | None) -> None:
        seq = self.log_event("identity_suspected", queue_id,
                             {"roster_a": roster_a["roster_id"], "roster_b": roster_b["roster_id"],
                              "a_person": roster_a["person_key"], "b_person": roster_b["person_key"],
                              "note": note})
        self.conn.execute(
            "INSERT INTO identity_queue(queue_id, open_seq, roster_id_a, roster_id_b, weak_fp, note) "
            "VALUES (?,?,?,?,?,?)",
            (queue_id, seq, roster_a["roster_id"], roster_b["roster_id"], weak_fp, note),
        )

    def resolve_identity_queue(self, queue_id: str, resolution: str, canonical_person_key: str,
                               decided_by: str, note: str | None) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM identity_queue WHERE queue_id = ?", (queue_id,)).fetchone()
        if not row:
            raise NotFoundError(f"确认队列事项不存在：{queue_id}")
        if row["status"] != "open":
            raise ConflictError(f"确认队列事项已处置：{queue_id}")
        seq = self.log_event("identity_resolved", queue_id,
                             {"resolution": resolution, "canonical_person_key": canonical_person_key,
                              "note": note}, actor=decided_by)
        self.conn.execute(
            "UPDATE identity_queue SET status='resolved', resolution=?, canonical_person_key=?, "
            "decided_by=?, decided_at=? WHERE queue_id=? AND status='open'",
            (resolution, canonical_person_key if resolution == "确认重复" else None,
             decided_by, now_iso(), queue_id),
        )
        if resolution == "确认重复":
            # 两名册归并到规范自然人：双方都写别名（含规范方自身），引擎统一经别名映射。
            for roster_id in (row["roster_id_a"], row["roster_id_b"]):
                self.conn.execute(
                    "INSERT OR REPLACE INTO person_aliases(roster_id, person_key, queue_id, decided_at) "
                    "VALUES (?,?,?,?)",
                    (roster_id, canonical_person_key, queue_id, now_iso()),
                )
        return dict(self.conn.execute(
            "SELECT * FROM identity_queue WHERE queue_id = ?", (queue_id,)).fetchone())

    def list_identity_queue(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM identity_queue WHERE status=? ORDER BY open_seq", (status,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM identity_queue ORDER BY open_seq").fetchall()
        return [dict(r) for r in rows]

    def get_roster(self, roster_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM rosters WHERE roster_id = ?", (roster_id,)).fetchone()
        if not row:
            raise NotFoundError(f"名册记录不存在：{roster_id}")
        return dict(row)

    # ---- 归属期（转学）-------------------------------------------------------

    def append_enrollment(self, person_key: str, school_code: str, school_name: str,
                          valid_from: str, reason: str | None, source_batch_id: str | None) -> int:
        seq = self.log_event("enrollment_opened", person_key,
                             {"school_code": school_code, "valid_from": valid_from, "reason": reason},
                             source_batch_id=source_batch_id)
        self.conn.execute(
            "INSERT INTO enrollments(person_key, school_code, school_name, valid_from, reason, source_batch_id, seq) "
            "VALUES (?,?,?,?,?,?,?)",
            (person_key, school_code, school_name, valid_from, reason, source_batch_id, seq),
        )
        return seq

    def close_enrollment(self, enrollment_id: int, valid_to: str) -> None:
        self.conn.execute("UPDATE enrollments SET valid_to=? WHERE enrollment_id=? AND valid_to IS NULL",
                          (valid_to, enrollment_id))

    def current_enrollment(self, person_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM enrollments WHERE person_key=? AND valid_to IS NULL ORDER BY seq DESC LIMIT 1",
            (person_key,)).fetchone()
        return dict(row) if row else None

    def list_enrollments(self, person_key: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM enrollments WHERE person_key=? ORDER BY seq", (person_key,)).fetchall()
        return [dict(r) for r in rows]

    # ---- 场次与签到 -----------------------------------------------------------

    def insert_session(self, data: dict[str, Any]) -> dict[str, Any]:
        seq = self.log_event("session_created", data["session_id"],
                             {"title": data["title"], "hold_date": data["hold_date"],
                              "school_code": data["school_code"], "initial_status": data["initial_status"],
                              "reissues_for_session_id": data.get("reissues_for_session_id")},
                             source_batch_id=data["source_batch_id"])
        try:
            self.conn.execute(
                "INSERT INTO sessions(session_id, title, hold_date, school_code, school_name, initial_status, "
                "reissues_for_session_id, source_batch_id, source_department, source_record_id, created_seq, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (data["session_id"], data["title"], data["hold_date"], data["school_code"], data.get("school_name"),
                 data["initial_status"], data.get("reissues_for_session_id"), data["source_batch_id"],
                 data["source_department"], data["source_record_id"], seq, now_iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateRecordError(
                f"场次记录重复：{data['source_batch_id']}/{data['source_record_id']}") from exc
        ev_type = data["initial_status"]
        ev_seq = self.log_event(f"session_{ev_type}", data["session_id"],
                                {"at_time": data["hold_date"]}, source_batch_id=data["source_batch_id"])
        self.conn.execute(
            "INSERT INTO session_events(seq, session_id, event_type, at_time, source_batch_id) "
            "VALUES (?,?,?,?,?)",
            (ev_seq, data["session_id"], ev_type, data["hold_date"], data["source_batch_id"]),
        )
        return self.get_session(data["session_id"])

    def get_session(self, session_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            raise NotFoundError(f"场次不存在：{session_id}")
        return dict(row)

    def append_session_event(self, session_id: str, event_type: str, at_time: str,
                             actor: str | None, reason: str | None, source_batch_id: str | None) -> dict[str, Any]:
        self.get_session(session_id)
        if event_type not in ("发布", "取消", "补办"):
            raise ValueError(f"非法场次事件：{event_type}")
        seq = self.log_event("session_event", session_id,
                             {"event_type": event_type, "at_time": at_time, "reason": reason},
                             actor=actor, source_batch_id=source_batch_id)
        self.conn.execute(
            "INSERT INTO session_events(seq, session_id, event_type, at_time, actor, reason, source_batch_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (seq, session_id, event_type, at_time, actor, reason, source_batch_id),
        )
        return self.session_current_state(session_id)

    def session_current_state(self, session_id: str) -> dict[str, Any]:
        """当前状态由事件日志中该场次最后一个事件决定（取消可被补办/重新发布覆盖）。"""
        session = self.get_session(session_id)
        events = self.conn.execute(
            "SELECT * FROM session_events WHERE session_id=? ORDER BY seq", (session_id,)).fetchall()
        status = events[-1]["event_type"] if events else session["initial_status"]
        session["current_status"] = status
        session["events"] = [dict(e) for e in events]
        return session

    def insert_checkin(self, data: dict[str, Any], *, person_key_at_ingest: str) -> dict[str, Any]:
        seq = self.log_event("checkin_ingested", data["checkin_id"],
                             {"session_id": data["session_id"], "roster_id": data["roster_id"],
                              "status": data["status"], "source_record_id": data["source_record_id"]},
                             source_batch_id=data["source_batch_id"])
        try:
            self.conn.execute(
                "INSERT INTO checkins(checkin_id, seq, session_id, roster_id, person_key_at_ingest, person_name, "
                "checkin_time, status, method, source_batch_id, source_department, source_record_id, ingested_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (data["checkin_id"], seq, data["session_id"], data["roster_id"], person_key_at_ingest,
                 data["person_name"], data.get("checkin_time"), data["status"], data.get("method"),
                 data["source_batch_id"], data["source_department"], data["source_record_id"], now_iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateRecordError(
                f"签到记录重复：{data['source_batch_id']}/{data['source_record_id']}") from exc
        return dict(self.conn.execute(
            "SELECT * FROM checkins WHERE checkin_id = ?", (data["checkin_id"],)).fetchone())

    # ---- 报告与更正单 ---------------------------------------------------------

    def insert_report(self, report: dict[str, Any], snapshot: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO reports(report_id, school_code, scope_from, scope_to, caliber_version, status, "
            "created_by, created_at, snapshot_json) VALUES (?,?,?,?,?,'draft',?,?,?)",
            (report["report_id"], report["school_code"], report["scope_from"], report["scope_to"],
             report["caliber_version"], report.get("created_by"), now_iso(),
             json.dumps(snapshot, ensure_ascii=False, sort_keys=True)),
        )
        self.log_event("report_created", report["report_id"],
                       {"school_code": report["school_code"], "scope": [report["scope_from"], report["scope_to"]],
                        "caliber_version": report["caliber_version"]}, actor=report.get("created_by"))

    def get_report(self, report_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,)).fetchone()
        if not row:
            raise NotFoundError(f"报告不存在：{report_id}")
        result = dict(row)
        result["snapshot"] = json.loads(result.pop("snapshot_json"))
        return result

    def seal_report(self, report_id: str, operator: str) -> dict[str, Any]:
        """条件更新实现首签决胜：并发封账只有一人成功。"""
        cur = self.conn.execute(
            "UPDATE reports SET status='signed', signed_by=?, signed_at=? "
            "WHERE report_id=? AND status='draft'",
            (operator, now_iso(), report_id),
        )
        if cur.rowcount == 0:
            row = self.conn.execute("SELECT status, signed_by FROM reports WHERE report_id=?",
                                    (report_id,)).fetchone()
            from .errors import ReportAlreadySignedError
            if row is None:
                raise NotFoundError(f"报告不存在：{report_id}")
            raise ReportAlreadySignedError(
                f"报告已由 {row['signed_by']} 签发，封账以首签者为准",
                {"report_id": report_id, "signed_by": row["signed_by"]})
        self.log_event("report_signed", report_id, {"operator": operator}, actor=operator)
        return self.get_report(report_id)

    def find_sealed_reports(self, school_code: str, hold_date: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT report_id, scope_from, scope_to, status FROM reports "
            "WHERE school_code=? AND status='signed' AND scope_from<=? AND scope_to>=?",
            (school_code, hold_date, hold_date)).fetchall()
        return [dict(r) for r in rows]

    def insert_correction(self, correction_id: str, report_id: str, reason: str,
                          operator: str, items: list[dict[str, Any]]) -> int:
        seq = self.log_event("correction_appended", correction_id,
                             {"report_id": report_id, "reason": reason,
                              "items": [{"metric": i["metric"], "delta": i["delta"]} for i in items]},
                             actor=operator)
        self.conn.execute(
            "INSERT INTO corrections(correction_id, report_id, seq, reason, operator, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (correction_id, report_id, seq, reason, operator, now_iso()),
        )
        for no, item in enumerate(items, start=1):
            self.conn.execute(
                "INSERT INTO correction_items(correction_id, item_no, metric, delta, ref_type, ref_id, note) "
                "VALUES (?,?,?,?,?,?,?)",
                (correction_id, no, item["metric"], item["delta"], item["ref_type"], item["ref_id"],
                 item.get("note")),
            )
        return seq

    def list_corrections(self, report_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM corrections WHERE report_id=? ORDER BY seq", (report_id,)).fetchall()
        result = []
        for r in rows:
            item_rows = self.conn.execute(
                "SELECT * FROM correction_items WHERE correction_id=? ORDER BY item_no",
                (r["correction_id"],)).fetchall()
            d = dict(r)
            d["items"] = [dict(i) for i in item_rows]
            result.append(d)
        return result

    # ---- 只读全量取数（引擎使用）----------------------------------------------

    def all_rosters(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM rosters ORDER BY seq")]

    def all_aliases(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM person_aliases")]

    def all_enrollments(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM enrollments ORDER BY seq")]

    def all_sessions(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM sessions ORDER BY created_seq")]

    def session_events_for(self, session_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for r in self.conn.execute("SELECT * FROM session_events ORDER BY seq"):
            result.setdefault(r["session_id"], []).append(dict(r))
        return {sid: result.get(sid, []) for sid in session_ids}

    def all_checkins(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM checkins ORDER BY seq")]

    def all_identity_queue(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM identity_queue ORDER BY open_seq")]

    def last_event_seq(self) -> int:
        row = self.conn.execute("SELECT MAX(seq) AS m FROM event_log").fetchone()
        return int(row["m"] or 0)


def open_store(path: str | Path = ":memory:") -> Store:
    # check_same_thread=False：HTTP 多线程共享连接，写操作由 _tx_lock 串行化。
    conn = sqlite3.connect(str(path), check_same_thread=False)
    store = Store(conn)
    store.init_schema()
    return store
