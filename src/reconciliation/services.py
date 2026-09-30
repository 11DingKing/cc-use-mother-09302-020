"""应用服务：编排写入、身份确认、转学归属、封账与更正。

所有写操作在单个 SQLite 事务内完成（默认隔离级别下 sqlite3 模块的
写操作天然串行），封账采用条件 UPDATE 首签决胜。
"""
from __future__ import annotations

import uuid
from typing import Any

from .calibers import Caliber, get_caliber
from .engine import compute_stats
from .errors import (
    ConflictError,
    ReportClosedError,
    ReportNotDraftError,
    ValidationError,
)
from .identity import Fingerprints
from .store import Store, now_iso

METRIC_KEYS = {
    "enrolled_students", "covered_students", "attendance_unique",
    "checkins_valid", "checkins_late", "sessions_active", "sessions_canceled",
    "pending_identity_persons",
}

# 学籍归属期的最早起点（首次归集建档，早于任何活动日期）。
DATE_MIN = "1900-01-01"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class ReconciliationService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ================= 批次归集（名册/场次/签到/来源谱系） =====================

    def ingest_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """归集一个部门报送批次。

        payload: ``{batch: {...}, rosters: [...], sessions: [...], checkins: [...]}``
        同 (batch_id, record_id) 重复报送幂等拒绝；跨部门重复报送进入身份确认流程。
        """
        batch = payload.get("batch") or {}
        batch_id = batch.get("batch_id") or _new_id("B")
        department = batch.get("department")
        if not department:
            raise ValidationError("批次必须包含 department")
        with self.store.conn:  # 事务
            self.store.register_batch(
                batch_id, department,
                batch.get("submitted_at") or now_iso(), batch.get("note"))

            roster_ids: dict[str, str] = {}
            for item in payload.get("rosters", []):
                saved = self._ingest_roster(item, batch_id, department)
                roster_ids[item.get("client_ref") or saved["roster_id"]] = saved["roster_id"]

            session_ids: dict[str, str] = {}
            for item in payload.get("sessions", []):
                target = item.get("reissues_for_session_id")
                if target and not self._session_exists(target):
                    raise ValidationError(f"补办关系指向不存在的场次：{target}")
                saved = self._ingest_session(item, batch_id, department)
                session_ids[item.get("client_ref") or saved["session_id"]] = saved["session_id"]

            checkin_ids = []
            for item in payload.get("checkins", []):
                checkin_ids.append(self._ingest_checkin(item, batch_id, department, roster_ids, session_ids))

        return {"batch_id": batch_id, "roster_ids": list(roster_ids.values()),
                "session_ids": list(session_ids.values()), "checkin_ids": checkin_ids}

    def _ingest_roster(self, item: dict[str, Any], batch_id: str, department: str) -> dict[str, Any]:
        roster_id = item.get("roster_id") or item.get("client_ref") or _new_id("R")
        if not item.get("name"):
            raise ValidationError("名册记录缺少 name", {"roster_client_ref": item.get("client_ref")})
        if not item.get("school_code") or not item.get("school_name"):
            raise ValidationError("名册记录缺少 school_code/school_name")
        fps = Fingerprints.of(item["name"], item.get("gender"), item.get("birth_date"), item.get("id_number"))

        person_key: str | None = None
        # 强指纹：证件号一致即同一自然人。
        strong = self.store.find_strong_match(fps.strong) if fps.strong else None
        if strong:
            person_key = strong["person_key"]

        # 弱指纹匹配：可能已被“确认重复”归并（直接并入规范自然人），
        # 也可能只是疑似（保持独立自然人，随后开确认队列）。
        weak_matches = self.store.find_weak_matches(fps.weak, roster_id)
        if person_key is None:
            for m in weak_matches:
                alias = self.store.conn.execute(
                    "SELECT person_key FROM person_aliases WHERE roster_id=?", (m["roster_id"],)).fetchone()
                if alias:
                    person_key = alias["person_key"]
                    break

        is_new_person = person_key is None
        if is_new_person:
            person_key = _new_id("P").upper()

        data = {
            "roster_id": roster_id,
            "name": item["name"], "gender": item.get("gender"), "birth_date": item.get("birth_date"),
            "grade": item.get("grade"), "class_name": item.get("class_name"),
            "session_id": item.get("session_id"),
            "school_code": item["school_code"], "school_name": item["school_name"],
            "source_batch_id": batch_id, "source_department": department,
            "source_record_id": item.get("source_record_id") or item.get("client_ref") or roster_id,
        }
        saved = self.store.insert_roster(data, person_key=person_key, strong_fp=fps.strong, weak_fp=fps.weak)
        self.store.ensure_person(person_key, roster_id, saved["seq"])

        if is_new_person:
            # 首见自然人：建立学籍归属期（转学由 transfer 显式关闭/新开）。
            self.store.append_enrollment(
                person_key, item["school_code"], item["school_name"],
                item.get("enrolled_since") or DATE_MIN, "首次归集建档", batch_id)

        # 弱指纹疑似重复：与每个异自然人（未归并、未成队列）配对入队。
        # 强指纹并单或已确认归并时 person_key 相同，自然跳过。
        for m in weak_matches:
            if m["person_key"] == person_key:
                continue
            if self._queue_exists(roster_id, m["roster_id"]):
                continue
            self.store.open_identity_queue(
                _new_id("Q"), saved, m, fps.weak,
                f"弱指纹一致（姓名/性别/出生日期），证件缺失或不一致：{department} 与 "
                f"{m['source_department']} 重复报送疑似")
        return saved

    def _queue_exists(self, roster_a: str, roster_b: str) -> bool:
        row = self.store.conn.execute(
            "SELECT 1 FROM identity_queue WHERE "
            "((roster_id_a=? AND roster_id_b=?) OR (roster_id_a=? AND roster_id_b=?)) LIMIT 1",
            (roster_a, roster_b, roster_b, roster_a)).fetchone()
        return row is not None

    def _ingest_session(self, item: dict[str, Any], batch_id: str, department: str) -> dict[str, Any]:
        session_id = item.get("session_id") or item.get("client_ref") or _new_id("S")
        initial_status = item.get("initial_status") or "发布"
        if initial_status not in ("发布", "补办"):
            raise ValidationError(f"场次初始状态只能是 发布/补办：{initial_status}")
        data = {
            "session_id": session_id, "title": item["title"], "hold_date": item["hold_date"],
            "school_code": item["school_code"], "school_name": item.get("school_name"),
            "initial_status": initial_status,
            "reissues_for_session_id": item.get("reissues_for_session_id"),
            "source_batch_id": batch_id, "source_department": department,
            "source_record_id": item.get("source_record_id") or item.get("client_ref") or session_id,
        }
        return self.store.insert_session(data)

    def _ingest_checkin(self, item: dict[str, Any], batch_id: str, department: str,
                        roster_refs: dict[str, str], session_refs: dict[str, str]) -> str:
        roster_id = (roster_refs.get(item.get("roster_ref")) or item.get("roster_id")
                     or item.get("roster_ref"))
        session_id = (session_refs.get(item.get("session_ref")) or item.get("session_id")
                      or item.get("session_ref"))
        if not roster_id or not self.store.conn.execute(
                "SELECT 1 FROM rosters WHERE roster_id=?", (roster_id,)).fetchone():
            raise ValidationError(f"签到引用的名册不存在：{roster_id}", {"source_record_id": item.get("source_record_id")})
        if not session_id or not self._session_exists(session_id):
            raise ValidationError(f"签到引用的场次不存在：{session_id}")
        status = item.get("status") or "正常"
        if status not in ("正常", "迟到", "缺席", "无效"):
            raise ValidationError(f"非法签到状态：{status}")
        checkin_id = item.get("checkin_id") or item.get("client_ref") or _new_id("C")

        session = self.store.get_session(session_id)
        # 落数前先看封账区间：已封账不得改写，只能追加更正单。
        sealed = self.store.find_sealed_reports(session["school_code"], session["hold_date"])
        if sealed:
            raise ReportClosedError(
                f"场次 {session_id}（{session['hold_date']}）落在已封账区间，"
                "签到不得直接写入，请对已签报告追加更正单",
                [r["report_id"] for r in sealed])

        roster = self.store.get_roster(roster_id)
        data = {
            "checkin_id": checkin_id, "session_id": session_id, "roster_id": roster_id,
            "person_name": roster["name"], "checkin_time": item.get("checkin_time"),
            "status": status, "method": item.get("method"),
            "source_batch_id": batch_id, "source_department": department,
            "source_record_id": item.get("source_record_id") or item.get("client_ref") or checkin_id,
        }
        self.store.insert_checkin(data, person_key_at_ingest=roster["person_key"])
        return checkin_id

    def _session_exists(self, session_id: str) -> bool:
        return self.store.conn.execute(
            "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone() is not None

    # ================= 场次取消与补办 =================

    def cancel_session(self, session_id: str, *, at_time: str, actor: str | None = None,
                       reason: str | None = None) -> dict[str, Any]:
        with self.store.tx():
            return self.store.append_session_event(session_id, "取消", at_time, actor, reason, None)

    def reissue_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        """登记补办场次：建立补办关系，并把原场次状态置为取消（冲正谱系）。"""
        target = payload["reissues_for_session_id"]
        with self.store.tx():
            old = self.store.get_session(target)
            data = {
                "session_id": payload.get("session_id") or _new_id("S"),
                "title": payload.get("title") or old["title"],
                "hold_date": payload["hold_date"],
                "school_code": payload.get("school_code") or old["school_code"],
                "school_name": payload.get("school_name") or old.get("school_name"),
                "initial_status": "补办",
                "reissues_for_session_id": target,
                "source_batch_id": payload.get("source_batch_id") or "system-reissue",
                "source_department": payload.get("department") or "县教育部门",
                "source_record_id": payload.get("source_record_id") or _new_id("reissue"),
            }
            saved = self.store.insert_session(data)
            self.store.append_session_event(
                target, "取消", payload["hold_date"], payload.get("actor"),
                f"取消后补办：{saved['session_id']}", payload.get("source_batch_id"))
            return saved

    # ================= 身份确认队列 =================

    def list_identity_queue(self, status: str | None = "open") -> list[dict[str, Any]]:
        return self.store.list_identity_queue(status)

    def resolve_identity(self, queue_id: str, resolution: str, *, decided_by: str,
                         canonical_roster_id: str | None = None, note: str | None = None) -> dict[str, Any]:
        """处置疑似重复：确认唯一（两个自然人）或确认重复（归并到规范自然人）。"""
        if resolution not in ("确认唯一", "确认重复"):
            raise ValidationError("resolution 只能是 确认唯一 / 确认重复")
        with self.store.tx():
            item = self.store.conn.execute(
                "SELECT * FROM identity_queue WHERE queue_id=?", (queue_id,)).fetchone()
            if not item:
                from .errors import NotFoundError
                raise NotFoundError(f"确认队列事项不存在：{queue_id}")
            canonical_person = ""
            if resolution == "确认重复":
                if canonical_roster_id and canonical_roster_id not in (
                        item["roster_id_a"], item["roster_id_b"]):
                    raise ValidationError("canonical_roster_id 必须是队列中的一名")
                chosen = canonical_roster_id or item["roster_id_a"]
                canonical_person = self.store.get_roster(chosen)["person_key"]
            return self.store.resolve_identity_queue(
                queue_id, resolution, canonical_person, decided_by, note)

    # ================= 转学归属 =================

    def transfer_student(self, payload: dict[str, Any]) -> dict[str, Any]:
        """转学：关闭旧学籍归属期（截至生效日前一日语义由区间表达），追加新区间。

        必传：roster_id 或 person_key、new_school_code/new_school_name、effective_date。
        归属规则：活动举办日落在哪段区间，就归哪所学校；区间半开 [from, to)。
        """
        person_key = payload.get("person_key")
        if not person_key:
            roster = self.store.get_roster(payload["roster_id"])
            person_key = roster["person_key"]
        effective = payload["effective_date"]
        with self.store.tx():
            current = self.store.current_enrollment(person_key)
            if current and current["school_code"] == payload["new_school_code"]:
                raise ConflictError("学生已在该校，无需转学")
            if current:
                self.store.close_enrollment(current["enrollment_id"], effective)
            self.store.append_enrollment(
                person_key, payload["new_school_code"], payload["new_school_name"],
                effective, f"转学自 {current['school_code'] if current else '-'}",
                payload.get("source_batch_id"))
            result = {"person_key": person_key, "enrollments": self.store.list_enrollments(person_key)}
        return result

    # ================= 报告：草稿、封账（首签决胜）、更正单 =================

    def _snapshot(self, school_code: str, scope_from: str, scope_to: str,
                  caliber: Caliber) -> dict[str, Any]:
        records = self._records()
        stats = compute_stats(records, caliber, school_code=school_code,
                              scope_from=scope_from, scope_to=scope_to)
        stats["generated_at_event_seq"] = self.store.last_event_seq()
        return stats

    def _records(self) -> dict[str, Any]:
        sessions = self.store.all_sessions()
        return {
            "rosters": self.store.all_rosters(),
            "enrollments": self.store.all_enrollments(),
            "sessions": sessions,
            "session_events": self.store.session_events_for([s["session_id"] for s in sessions]),
            "checkins": self.store.all_checkins(),
            "identity_queue": self.store.all_identity_queue(),
        }

    def create_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        caliber = get_caliber(payload.get("caliber_version") or "2026-annual-v1")
        report_id = payload.get("report_id") or _new_id("RP")
        with self.store.tx():
            snapshot = self._snapshot(payload["school_code"], payload["scope_from"],
                                      payload["scope_to"], caliber)
            self.store.insert_report({
                "report_id": report_id,
                "school_code": payload["school_code"],
                "scope_from": payload["scope_from"],
                "scope_to": payload["scope_to"],
                "caliber_version": caliber.version,
                "created_by": payload.get("created_by"),
            }, snapshot)
        return self.get_report(report_id)

    def sign_report(self, report_id: str, operator: str) -> dict[str, Any]:
        """多人同时封账：条件 UPDATE，首签者决胜，后来者收到冲突。"""
        with self.store.tx():
            return self.store.seal_report(report_id, operator)

    def append_correction(self, report_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """对已签报告只能**追加**更正单：delta 冲正，引用具体记录，原快照原样保留。"""
        report = self.store.get_report(report_id)
        if report["status"] != "signed":
            raise ReportNotDraftError("仅已签发报告可追加更正单；草稿报告应重新生成")
        items = payload.get("items") or []
        if not items:
            raise ValidationError("更正单至少包含一条 delta 明细")
        for it in items:
            if it.get("metric") not in METRIC_KEYS:
                raise ValidationError(f"更正单引用了未知指标：{it.get('metric')}")
            if not isinstance(it.get("delta"), int) or it["delta"] == 0:
                raise ValidationError("delta 必须是非零整数（冲正方向由正负表达）")
            if not it.get("ref_type") or not it.get("ref_id"):
                raise ValidationError("更正明细必须引用具体记录（ref_type/ref_id）")
        correction_id = payload.get("correction_id") or _new_id("ADJ")
        with self.store.tx():
            self.store.insert_correction(
                correction_id, report_id, payload["reason"], payload.get("operator", "匿名"),
                items)
        return self.get_report(report_id)

    def get_report(self, report_id: str) -> dict[str, Any]:
        report = self.store.get_report(report_id)
        corrections = self.store.list_corrections(report_id)
        # 当前值 = 签发快照 + 全部更正单 delta（逐数字可解释）。
        adjusted: dict[str, int] = {
            k: v["value"] for k, v in report["snapshot"]["metrics"].items()}
        for c in corrections:
            for it in c["items"]:
                adjusted[it["metric"]] = adjusted.get(it["metric"], 0) + it["delta"]
        report["corrections"] = corrections
        report["adjusted_values"] = adjusted
        return report

    def recompute_report(self, report_id: str) -> dict[str, Any]:
        """用当前事实与报告冻结口径重新复算，比对签发快照（可复算性自证）。"""
        report = self.store.get_report(report_id)
        caliber = get_caliber(report["caliber_version"])
        with self.store.tx():
            fresh = compute_stats(
                self._records(), caliber,
                school_code=report["school_code"],
                scope_from=report["scope_from"], scope_to=report["scope_to"])
        frozen = {k: v["value"] for k, v in report["snapshot"]["metrics"].items()}
        current = {k: v["value"] for k, v in fresh["metrics"].items()}
        return {
            "report_id": report_id,
            "caliber_version": report["caliber_version"],
            "frozen_at_signing": frozen,
            "recomputed_now": current,
            "matches": frozen == current,
            "note": "已封账区间的事实写入被拒绝，故复算结果应与签发快照一致；"
                    "数字变化只体现在更正单（adjusted_values）",
        }

    # ================= 即席查询：每个数字都要解释 =================

    def explain(self, *, school_code: str | None = None, scope_from: str | None = None,
                scope_to: str | None = None, caliber_version: str | None = None) -> dict[str, Any]:
        caliber = get_caliber(caliber_version or "2026-annual-v1")
        with self.store.tx():
            stats = compute_stats(
                self._records(), caliber, school_code=school_code,
                scope_from=scope_from, scope_to=scope_to)
        return stats

    def lineage(self, ref_type: str, ref_id: str) -> dict[str, Any]:
        """给出单条记录的来源谱系：批次/部门、场次事件、补办关系、归并队列、更正引用。"""
        with self.store.tx():
            if ref_type == "checkin":
                row = self.store.conn.execute(
                    "SELECT * FROM checkins WHERE checkin_id=?", (ref_id,)).fetchone()
                if not row:
                    from .errors import NotFoundError
                    raise NotFoundError(f"签到不存在：{ref_id}")
                d = dict(row)
                session = self.store.session_current_state(d["session_id"])
                return {
                    "record": d,
                    "source": {"batch_id": d["source_batch_id"], "department": d["source_department"],
                               "record_id": d["source_record_id"]},
                    "roster": self.store.get_roster(d["roster_id"]),
                    "session": session,
                    "referenced_by_corrections": self._corrections_refs(ref_type, ref_id),
                }
            if ref_type == "roster":
                d = self.store.get_roster(ref_id)
                aliases = self.store.conn.execute(
                    "SELECT * FROM person_aliases WHERE roster_id=?", (ref_id,)).fetchall()
                queue_rows = self.store.conn.execute(
                    "SELECT * FROM identity_queue WHERE roster_id_a=? OR roster_id_b=? ORDER BY open_seq",
                    (ref_id, ref_id)).fetchall()
                return {
                    "record": d,
                    "source": {"batch_id": d["source_batch_id"], "department": d["source_department"],
                               "record_id": d["source_record_id"]},
                    "enrollments": self.store.list_enrollments(d["person_key"]),
                    "merge_aliases": [dict(a) for a in aliases],
                    "queue_history": [dict(q) for q in queue_rows],
                }
            if ref_type == "session":
                return self.store.session_current_state(ref_id)
        from .errors import ValidationError
        raise ValidationError(f"不支持的谱系类型：{ref_type}")

    def _corrections_refs(self, ref_type: str, ref_id: str) -> list[dict[str, Any]]:
        rows = self.store.conn.execute(
            "SELECT c.correction_id, c.report_id, i.metric, i.delta, i.note "
            "FROM correction_items i JOIN corrections c ON c.correction_id=i.correction_id "
            "WHERE i.ref_type=? AND i.ref_id=?", (ref_type, ref_id)).fetchall()
        return [dict(r) for r in rows]
