"""可复算统计引擎（纯函数）。

输入存储层的不可变事实快照与一个已发布口径，输出：
- 指标值；
- 每个指标 ``includes``（计入了哪些记录）、``excludes``（排除了哪些记录及原因）、
  ``reversals``（冲正关系：取消→补办、归并去重）；
- ``pending_identity``（疑似重复待确认）与 ``consistency_checks``（如覆盖人数>学生总数）。

引擎不访问数据库、不依赖当前时间，给定相同输入与口径必然得到相同输出。
"""
from __future__ import annotations

from typing import Any

from .calibers import Caliber


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, x: str) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: str) -> str:
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> str:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # 字典序较小者为稳定规范根，保证复算确定。
            root, other = (ra, rb) if ra <= rb else (rb, ra)
            self.parent[other] = root
        return self.find(a)


def _source_of(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "batch_id": row.get("source_batch_id"),
        "department": row.get("source_department"),
        "record_id": row.get("source_record_id"),
    }


def _current_session_status(session: dict[str, Any], events: list[dict[str, Any]]) -> str:
    return events[-1]["event_type"] if events else session["initial_status"]


def compute_stats(  # noqa: PLR0915 - 解释型统计天然较长，保持单遍可读
    records: dict[str, Any],
    caliber: Caliber,
    *,
    school_code: str | None = None,
    scope_from: str | None = None,
    scope_to: str | None = None,
) -> dict[str, Any]:
    rosters: list[dict[str, Any]] = records["rosters"]
    enrollments: list[dict[str, Any]] = records["enrollments"]
    sessions: list[dict[str, Any]] = records["sessions"]
    events_by_session: dict[str, list[dict[str, Any]]] = records.get("session_events", {})
    checkins: list[dict[str, Any]] = records["checkins"]
    queue: list[dict[str, Any]] = records.get("identity_queue", [])

    # ---- 1. 自然人归并（强指纹 + 确认队列“确认重复”的传递闭包）----------------
    uf = _UnionFind()
    for r in rosters:
        uf.add(r["person_key"])
    roster_by_id = {r["roster_id"]: r for r in rosters}
    for q in queue:
        if q["status"] == "resolved" and q["resolution"] == "确认重复":
            a = roster_by_id[q["roster_id_a"]]["person_key"]
            b = roster_by_id[q["roster_id_b"]]["person_key"]
            uf.add(q["canonical_person_key"])
            uf.union(q["canonical_person_key"], a)
            uf.union(q["canonical_person_key"], b)

    def canonical(roster: dict[str, Any]) -> str:
        return uf.find(roster["person_key"])

    # ---- 2. 场次状态与范围 -----------------------------------------------------
    in_range = lambda d: (scope_from is None or d >= scope_from) and (scope_to is None or d <= scope_to)
    ref_date = scope_to or "9999-12-31"

    # 待确认自然人：开放队列连接出的连通簇（一对疑似重复 = 1 个待确认簇）。
    pending_uf = _UnionFind()
    pending_evidence: dict[str, list[str]] = {}
    for q in queue:
        if q["status"] != "open":
            continue
        a = uf.find(roster_by_id[q["roster_id_a"]]["person_key"])
        b = uf.find(roster_by_id[q["roster_id_b"]]["person_key"])
        pending_uf.add(a)
        pending_uf.add(b)
        pending_uf.union(a, b)
        pending_evidence.setdefault(a, []).append(q["queue_id"])
        pending_evidence.setdefault(b, []).append(q["queue_id"])
    pending_members: set[str] = set(pending_uf.parent)

    session_view: dict[str, dict[str, Any]] = {}
    for s in sessions:
        events = events_by_session.get(s["session_id"], [])
        status = _current_session_status(s, events)
        view = dict(s)
        view["current_status"] = status
        view["events"] = events
        session_view[s["session_id"]] = view

    def date_in_scope(hold_date: str) -> bool:
        return in_range(hold_date)

    def session_in_scope(s: dict[str, Any]) -> bool:
        # 场次类指标按场次所属学校；参与类（签到/覆盖）另按学生举办日学籍归属。
        return in_range(s["hold_date"]) and (school_code is None or s["school_code"] == school_code)

    # ---- 3. 转学归属：举办日所在学校 -------------------------------------------
    enroll_by_person: dict[str, list[dict[str, Any]]] = {}
    for e in enrollments:
        enroll_by_person.setdefault(e["person_key"], []).append(e)
    for evs in enroll_by_person.values():
        evs.sort(key=lambda e: (e["valid_from"], e["seq"]))

    def owning_school(person: str, on_date: str) -> tuple[str, str]:
        """返回 (school_code, school_name)。区间半开：[valid_from, valid_to)。"""
        intervals = enroll_by_person.get(person, [])
        covering = [e for e in intervals if e["valid_from"] <= on_date
                    and (e["valid_to"] is None or on_date < e["valid_to"])]
        if covering:
            e = max(covering, key=lambda x: (x["valid_from"], x["seq"]))
            return e["school_code"], e["school_name"]
        future = [e for e in intervals if e["valid_from"] > on_date]
        if future:
            e = min(future, key=lambda x: (x["valid_from"], x["seq"]))
            return e["school_code"], e["school_name"]
        # 无归属期：以该自然人最新一条名册的学校为准（确定性回退）。
        rs = [r for r in rosters if uf.find(r["person_key"]) == person]
        if rs:
            r = max(rs, key=lambda x: x["seq"])
            return r["school_code"], r["school_name"]
        return "", ""

    def pending_clusters() -> list[dict[str, Any]]:
        groups: dict[str, list[str]] = {}
        for p in pending_members:
            groups.setdefault(pending_uf.find(p), []).append(p)
        clusters: list[dict[str, Any]] = []
        for root, members in groups.items():
            refs = [r["roster_id"] for r in rosters if uf.find(r["person_key"]) in members]
            qitems = sorted({q for p in members for q in pending_evidence.get(p, [])})
            schools = {owning_school(p, ref_date)[0] for p in members}
            clusters.append({"cluster": root, "persons": sorted(members),
                             "roster_records": refs, "queue_items": qitems,
                             "schools": sorted(s for s in schools if s)})
        return sorted(clusters, key=lambda c: c["cluster"])

    # ---- 4. 指标累加器 ---------------------------------------------------------
    metrics: dict[str, dict[str, Any]] = {
        "enrolled_students": {"label": "学生总数（归并去重后）", "value": 0, "includes": [], "excludes": [], "reversals": []},
        "covered_students": {"label": "覆盖学生人数（跨场去重）", "value": 0, "includes": [], "excludes": [], "reversals": []},
        "attendance_unique": {"label": "实到学生人数（跨场去重）", "value": 0, "includes": [], "excludes": [], "reversals": []},
        "checkins_valid": {"label": "有效签到人次", "value": 0, "includes": [], "excludes": [], "reversals": []},
        "checkins_late": {"label": "其中：迟到签到人次", "value": 0, "includes": [], "excludes": [], "reversals": []},
        "sessions_active": {"label": "实际举办场次（含补办）", "value": 0, "includes": [], "excludes": [], "reversals": []},
        "sessions_canceled": {"label": "取消场次（不计入）", "value": 0, "includes": [], "excludes": [], "reversals": []},
        "pending_identity_persons": {"label": "疑似重复待确认簇（每簇涉及人数见 includes）", "value": 0, "includes": [], "excludes": [], "reversals": []},
    }
    excluded_ledger: list[dict[str, Any]] = []

    def exclude(metric: str | None, ref_type: str, ref_id: str, reason: str, **extra: Any) -> None:
        entry = {"ref_type": ref_type, "ref_id": ref_id, "reason": reason}
        entry.update(extra)
        excluded_ledger.append(entry)
        if metric is not None:
            metrics[metric]["excludes"].append({"ref_type": ref_type, "ref_id": ref_id, "reason": reason})

    # ---- 5. 场次指标 ------------------------------------------------------------
    reissue_links: list[dict[str, Any]] = []
    for s in session_view.values():
        if not session_in_scope(s):
            continue
        if s["current_status"] == "取消":
            metrics["sessions_canceled"]["value"] += 1
            metrics["sessions_canceled"]["includes"].append({
                "ref_type": "session", "ref_id": s["session_id"], "title": s["title"],
                "hold_date": s["hold_date"], "current_status": "取消",
                "events": [e["event_type"] for e in s["events"]],
            })
            note = "补办自 " + s["reissues_for_session_id"] if s.get("reissues_for_session_id") else None
            if note:
                metrics["sessions_canceled"]["includes"][-1]["note"] = note
        elif s["current_status"] in ("发布", "补办"):
            metrics["sessions_active"]["value"] += 1
            entry = {"ref_type": "session", "ref_id": s["session_id"], "title": s["title"],
                     "hold_date": s["hold_date"], "current_status": s["current_status"]}
            if s.get("reissues_for_session_id"):
                entry["reissues_for"] = s["reissues_for_session_id"]
                reissue_links.append({"type": "reissue", "in_scope": True,
                                      "canceled_session": s["reissues_for_session_id"],
                                      "reissue_session": s["session_id"]})
            metrics["sessions_active"]["includes"].append(entry)

    # ---- 6. 参与证据：报名名册 + 签到 ------------------------------------------
    # person -> 参与证据（跨场去重的关键：按规范自然人聚合）。
    coverage_evidence: dict[str, dict[str, Any]] = {}
    attendance_evidence: dict[str, dict[str, Any]] = {}

    def record_participation(bucket: dict[str, dict[str, Any]], person: str, kind: str,
                             session: dict[str, Any], ref_type: str, ref_id: str,
                             source: dict[str, Any], status: str | None = None) -> None:
        ev = bucket.setdefault(person, {"sessions": [], "records": [], "schools": set()})
        school_code, school_name = owning_school(person, session["hold_date"])
        ev["schools"].add(school_code)
        if session["session_id"] not in ev["sessions"]:
            ev["sessions"].append(session["session_id"])
        ev["records"].append({"kind": kind, "ref_type": ref_type, "ref_id": ref_id,
                              "session": session["session_id"], "status": status,
                              "owned_school": school_code, "source": source})

    # 6.1 报名/点名册（带 session_id）
    for r in sorted(rosters, key=lambda x: x["seq"]):
        sid = r.get("session_id")
        if not sid or sid not in session_view:
            continue
        s = session_view[sid]
        if not date_in_scope(s["hold_date"]):
            continue
        person = canonical(r)
        if s["current_status"] == "取消":
            exclude("covered_students", "roster", r["roster_id"],
                    "场次取消，报名记录不计入覆盖", session=sid, source=_source_of(r))
            continue
        record_participation(coverage_evidence, person, "报名名册", s, "roster",
                             r["roster_id"], _source_of(r))

    # 6.2 签到：状态口径 + 取消排除 + 同人同场重复 + 跨校
    seen_person_session: dict[tuple[str, str], dict[str, Any]] = {}
    for c in sorted(checkins, key=lambda x: x["seq"]):
        s = session_view.get(c["session_id"])
        if s is None or not date_in_scope(s["hold_date"]):
            continue
        roster = roster_by_id.get(c["roster_id"])
        person = uf.find(c["person_key_at_ingest"]) if c.get("person_key_at_ingest") else canonical(roster)
        src = _source_of(c)
        if s["current_status"] == "取消":
            reversal = None
            if s.get("reissues_for_session_id") or any(
                    l["canceled_session"] == s["session_id"] for l in reissue_links):
                reversal = s["session_id"]
            exclude("checkins_valid", "checkin", c["checkin_id"],
                    "场次已取消，签到事实保留但不计入；以补办场签到为准"
                    if reversal else "场次已取消，签到不计入",
                    session=s["session_id"], status=c["status"], reversal=reversal, source=src)
            continue
        if not caliber.attendance_counts(c["status"]):
            exclude("checkins_valid", "checkin", c["checkin_id"],
                    f"签到状态“{c['status']}”不在口径 {sorted(caliber.valid_attendance_statuses)} 内",
                    session=s["session_id"], status=c["status"], source=src)
            continue
        key = (person, s["session_id"])
        if key in seen_person_session:
            first = seen_person_session[key]
            exclude("checkins_valid", "checkin", c["checkin_id"],
                    "同一自然人同一场次重复签到（多部门重复报送），保留最早一条",
                    session=s["session_id"], status=c["status"],
                    duplicate_of=first["checkin_id"], source=src)
            metrics["checkins_valid"]["reversals"].append({
                "type": "duplicate_checkin_collapsed", "kept": first["checkin_id"],
                "dropped": c["checkin_id"], "session": s["session_id"], "person": person})
            continue
        seen_person_session[key] = c
        # 归属是否跨校延迟到提交指标时按“每条证据的举办日学校”判定，
        # 转学学生因此获得明确归属，证据本身不丢弃。
        record_participation(coverage_evidence, person, "签到", s, "checkin",
                             c["checkin_id"], src, status=c["status"])
        record_participation(attendance_evidence, person, "签到", s, "checkin",
                             c["checkin_id"], src, status=c["status"])

    # ---- 7. 学生总数（参考日 = 区间末日）---------------------------------------
    enrolled_persons: dict[str, dict[str, Any]] = {}
    for person in {uf.find(r["person_key"]) for r in rosters}:
        owned, owned_name = owning_school(person, ref_date)
        if school_code and owned != school_code:
            continue
        roster_refs = [r["roster_id"] for r in rosters if uf.find(r["person_key"]) == person]
        enrolled_persons[person] = {"person": person, "school": owned, "rosters": roster_refs}

    # ---- 8. 待确认队列拆分（疑似重复先排队，不进正式人头）-----------------------
    for cluster in pending_clusters():
        if school_code and school_code not in cluster["schools"]:
            continue
        metrics["pending_identity_persons"]["value"] += 1
        metrics["pending_identity_persons"]["includes"].append(cluster)

    def commit_person_metric(metric_name: str, evidence: dict[str, dict[str, Any]]) -> None:
        for person in sorted(evidence):
            ev = evidence[person]
            in_school = [rec for rec in ev["records"]
                         if not school_code or not rec.get("owned_school")
                         or rec["owned_school"] == school_code]
            out_school = [rec for rec in ev["records"] if rec not in in_school]
            for rec in out_school:
                exclude(metric_name, rec["ref_type"], rec["ref_id"],
                        f"举办日学籍归属 {rec['owned_school']}，不计入本校 {school_code}（转学/跨校按举办日归属）",
                        session=rec.get("session"), person=person, source=rec.get("source"))
            if person in pending_members:
                for rec in in_school:
                    exclude(metric_name, rec["ref_type"], rec["ref_id"],
                            "身份疑似重复待确认，确认前不计入正式人头",
                            session=rec.get("session"), person=person,
                            queue_items=sorted(set(pending_evidence.get(person, []))),
                            source=rec.get("source"))
                continue
            if school_code and not in_school:
                continue
            chosen = in_school if in_school else ev["records"]
            sess = sorted({rec["session"] for rec in chosen})
            entry = {"person": person, "sessions": sess, "records": chosen}
            if len(sess) > 1:
                entry["dedup_note"] = f"跨 {len(sess)} 个场次参与，按自然人人头只计 1 人"
                metrics[metric_name]["reversals"].append({
                    "type": "cross_session_dedup", "person": person, "sessions": sess})
            metrics[metric_name]["includes"].append(entry)
            metrics[metric_name]["value"] += 1

    commit_person_metric("covered_students", coverage_evidence)
    commit_person_metric("attendance_unique", attendance_evidence)

    for person, info in sorted(enrolled_persons.items()):
        if person in pending_members:
            exclude("enrolled_students", "person", person,
                    "身份疑似重复待确认，暂不计入学生总数",
                    roster_records=info["rosters"],
                    queue_items=sorted(set(pending_evidence.get(person, []))))
            continue
        metrics["enrolled_students"]["includes"].append(
            {"person": person, "school": info["school"], "roster_records": info["rosters"]})
        metrics["enrolled_students"]["value"] += 1

    # ---- 9. 有效/迟到签到人次（逐条列示：计入、排除、跨校归属）------------------
    for person, ev in attendance_evidence.items():
        for rec in ev["records"]:
            if school_code and rec.get("owned_school") and rec["owned_school"] != school_code:
                exclude("checkins_valid", rec["ref_type"], rec["ref_id"],
                        f"举办日学籍归属 {rec['owned_school']}，人次归属该校",
                        session=rec.get("session"), status=rec.get("status"),
                        owned_school=rec["owned_school"], person=person, source=rec.get("source"))
                continue
            if person in pending_members:
                exclude("checkins_valid", rec["ref_type"], rec["ref_id"],
                        "身份疑似重复待确认，签到事实保留但暂不计入人次",
                        session=rec.get("session"), status=rec.get("status"), person=person,
                        queue_items=sorted(set(pending_evidence.get(person, []))),
                        source=rec.get("source"))
                continue
            metrics["checkins_valid"]["value"] += 1
            metrics["checkins_valid"]["includes"].append(rec)
            if rec.get("status") == "迟到":
                metrics["checkins_late"]["value"] += 1
                metrics["checkins_late"]["includes"].append(rec)

    # ---- 10. 冲正：取消→补办谱系 ------------------------------------------------
    reversals: list[dict[str, Any]] = []
    checkins_by_session: dict[str, list[dict[str, Any]]] = {}
    for c in checkins:
        checkins_by_session.setdefault(c["session_id"], []).append(c)
    for s in session_view.values():
        target = s.get("reissues_for_session_id")
        if not target:
            continue
        old = session_view.get(target)
        reversals.append({
            "type": "canceled_then_reissued",
            "canceled_session": target,
            "reissue_session": s["session_id"],
            "reissue_hold_date": s["hold_date"],
            "in_scope": session_in_scope(s) and (old is None or session_in_scope(old)),
            "excluded_records": [
                {"ref_type": "checkin", "ref_id": c["checkin_id"], "source": _source_of(c)}
                for c in checkins_by_session.get(target, [])],
            "included_records": [
                {"ref_type": "checkin", "ref_id": c["checkin_id"], "source": _source_of(c)}
                for c in checkins_by_session.get(s["session_id"], [])],
        })
    if reversals:
        metrics["covered_students"]["reversals"].extend(reversals)
        metrics["checkins_valid"]["reversals"].extend(reversals)

    # ---- 11. 一致性检查 ---------------------------------------------------------
    consistency: list[dict[str, Any]] = []
    if metrics["covered_students"]["value"] > metrics["enrolled_students"]["value"]:
        consistency.append({
            "check": "covered_not_exceed_enrolled",
            "passed": False,
            "detail": "覆盖人数高于学生总数，通常意味着仍有未归并身份或归属错误",
            "covered": metrics["covered_students"]["value"],
            "enrolled": metrics["enrolled_students"]["value"],
        })
    else:
        consistency.append({"check": "covered_not_exceed_enrolled", "passed": True})

    return {
        "scope": {"school_code": school_code, "from": scope_from, "to": scope_to},
        "caliber_version": caliber.version,
        "metrics": metrics,
        "excluded_ledger": excluded_ledger,
        "reversals": reversals,
        "pending_identity": [c for c in pending_clusters()
                             if not school_code or school_code in c["schools"]],
        "consistency_checks": consistency,
    }
