"""端到端业务规则回归测试。

覆盖：跨场去重、取消补办冲正、多部门重复报送与确认队列、
封账后只能更正、转学/迟到归属、多人并发首签决胜、逐数字解释与可复算。
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
from urllib.parse import quote
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reconciliation import ReconciliationService, open_store
from reconciliation.errors import (
    DuplicateRecordError,
    ReportAlreadySignedError,
    ReportClosedError,
)

S1, S2 = "S1-县一小", "S2-县二小"
DEPT_SCHOOL, DEPT_CULTURE, DEPT_SPORTS = "县一小统计员", "县文旅中心", "县体卫艺股"


def roster(client_ref, name, *, dept=None, school=S1, id_number=None, gender="男",
          birth="2014-03-03", session_ref=None, grade="四年级"):
    return {"client_ref": client_ref, "name": name, "gender": gender, "birth_date": birth,
            "id_number": id_number, "grade": grade,
            "school_code": school, "school_name": school,
            "session_ref": session_ref}


def session(ref, title, date, *, school=S1, dept=DEPT_SCHOOL):
    return {"client_ref": ref, "title": title, "hold_date": date,
            "school_code": school, "school_name": school}


def checkin(ref, roster_ref, session_ref, *, dept, status="正常", time=None):
    return {"client_ref": ref, "roster_ref": roster_ref, "session_ref": session_ref,
            "status": status, "checkin_time": time or "2026-03-10T09:00:00+08:00"}


def batch(bid, dept, rosters=(), sessions=(), checkins=()):
    return {"batch": {"batch_id": bid, "department": dept, "submitted_at": "2026-03-20T10:00:00+08:00"},
            "rosters": list(rosters), "sessions": list(sessions), "checkins": list(checkins)}


class ReconciliationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = ReconciliationService(open_store(":memory:"))

    def metric(self, stats, key):
        return stats["metrics"][key]

    # ---- 1. 跨场参与不得简单相加 ---------------------------------------------

    def test_cross_session_counts_person_once(self):
        self.svc.ingest_batch(batch(
            "B1", DEPT_SCHOOL,
            rosters=[roster("r1", "李雷", id_number="110101201403031234")],
            sessions=[session("sA", "皮影戏体验", "2026-03-10"),
                      session("sB", "剪纸体验", "2026-04-10")],
            checkins=[checkin("c1", "r1", "sA", dept=DEPT_SCHOOL),
                      checkin("c2", "r1", "sB", dept=DEPT_SCHOOL)]))
        stats = self.svc.explain(school_code=S1, scope_from="2026-01-01", scope_to="2026-12-31")
        self.assertEqual(self.metric(stats, "covered_students")["value"], 1)
        self.assertEqual(self.metric(stats, "attendance_unique")["value"], 1)
        self.assertEqual(self.metric(stats, "checkins_valid")["value"], 2)
        reversals = self.metric(stats, "covered_students")["reversals"]
        self.assertIn("cross_session_dedup", {r["type"] for r in reversals})
        # 解释：人头的两条记录都在 includes 中。
        inc = self.metric(stats, "covered_students")["includes"][0]
        self.assertEqual(len(inc["records"]), 2)
        self.assertIn("跨 2 个场次", inc["dedup_note"])

    def test_same_person_same_session_duplicate_collapsed(self):
        # 学校与文旅中心就同一场次重复报送同一人签到（证件号一致）。
        self.svc.ingest_batch(batch(
            "B1", DEPT_SCHOOL,
            rosters=[roster("r1", "李雷", id_number="110101201403031234")],
            sessions=[session("sA", "皮影戏", "2026-03-10")],
            checkins=[checkin("c1", "r1", "sA", dept=DEPT_SCHOOL)]))
        self.svc.ingest_batch(batch(
            "B2", DEPT_CULTURE,
            rosters=[roster("r2", "李雷", id_number="110101201403031234", dept=DEPT_CULTURE)],
            checkins=[checkin("c2", "r2", "sA", dept=DEPT_CULTURE)]))
        stats = self.svc.explain(school_code=S1, scope_from="2026-01-01", scope_to="2026-12-31")
        self.assertEqual(self.metric(stats, "checkins_valid")["value"], 1)
        self.assertEqual(self.metric(stats, "covered_students")["value"], 1)
        self.assertTrue(any("重复签到" in e["reason"] for e in self.metric(stats, "checkins_valid")["excludes"]))
        self.assertTrue(any(r["type"] == "duplicate_checkin_collapsed"
                            for r in self.metric(stats, "checkins_valid")["reversals"]))

    # ---- 2. 取消后补办：原场排除、补办计入、冲正可追溯 --------------------------

    def test_canceled_then_reissued_reversal(self):
        self.svc.ingest_batch(batch(
            "B1", DEPT_SCHOOL,
            rosters=[roster("r1", "韩梅梅", gender="女", id_number="110101201404041234")],
            sessions=[session("sA", "昆曲讲座", "2026-03-10")],
            checkins=[checkin("c1", "r1", "sA", dept=DEPT_SCHOOL)]))
        self.svc.reissue_session({
            "session_id": "sA2", "reissues_for_session_id": "sA",
            "hold_date": "2026-03-20", "actor": "县教育部门",
            "source_record_id": "reissue-1"})
        # 补办场签到（sA2 已由补办接口建立）。
        self.svc.ingest_batch({
            "batch": {"batch_id": "B3", "department": DEPT_SCHOOL,
                      "submitted_at": "2026-03-21T10:00:00+08:00"},
            "checkins": [checkin("c2", "r1", "sA2", dept=DEPT_SCHOOL, time="2026-03-20T09:00:00+08:00")]})
        stats = self.svc.explain(school_code=S1, scope_from="2026-01-01", scope_to="2026-12-31")
        self.assertEqual(self.metric(stats, "sessions_canceled")["value"], 1)
        self.assertEqual(self.metric(stats, "sessions_active")["value"], 1)
        self.assertEqual(self.metric(stats, "covered_students")["value"], 1)
        self.assertEqual(self.metric(stats, "checkins_valid")["value"], 1)
        # c1 必须以“场次取消”出现在排除账中。
        self.assertTrue(any("取消" in e["reason"] for e in self.metric(stats, "checkins_valid")["excludes"]))
        rev = [r for r in stats["reversals"] if r["type"] == "canceled_then_reissued"]
        self.assertEqual(rev[0]["canceled_session"], "sA")
        self.assertEqual(rev[0]["reissue_session"], "sA2")
        self.assertEqual(len(rev[0]["excluded_records"]), 1)
        self.assertEqual(len(rev[0]["included_records"]), 1)

    # ---- 3. 多部门弱重复 → 确认队列 → 归并 ------------------------------------

    def test_weak_duplicate_enters_queue_then_merges(self):
        self.svc.ingest_batch(batch(
            "B1", DEPT_SCHOOL,
            rosters=[roster("w1", "王芳", gender="女", birth="2014-05-05")]))
        self.svc.ingest_batch(batch(
            "B2", DEPT_CULTURE,
            rosters=[roster("w2", "王芳", gender="女", birth="2014-05-05", dept=DEPT_CULTURE)]))
        pending = self.svc.list_identity_queue("open")
        self.assertEqual(len(pending), 1)
        stats = self.svc.explain(school_code=S1, scope_from="2026-01-01", scope_to="2026-12-31")
        self.assertEqual(self.metric(stats, "pending_identity_persons")["value"], 1)
        # 未确认前，正式人头为 0 且排除账说明原因。
        self.assertEqual(self.metric(stats, "enrolled_students")["value"], 0)
        self.assertTrue(any("待确认" in e["reason"] for e in stats["excluded_ledger"]))
        # 确认重复 → 归并为一个自然人。
        self.svc.resolve_identity(pending[0]["queue_id"], "确认重复",
                                  decided_by="审计人员", canonical_roster_id=pending[0]["roster_id_a"])
        stats2 = self.svc.explain(school_code=S1, scope_from="2026-01-01", scope_to="2026-12-31")
        self.assertEqual(self.metric(stats2, "pending_identity_persons")["value"], 0)
        self.assertEqual(self.metric(stats2, "enrolled_students")["value"], 1)

    def test_weak_match_confirmed_distinct_counts_two(self):
        self.svc.ingest_batch(batch("B1", DEPT_SCHOOL,
                                    rosters=[roster("w1", "王芳", gender="女", birth="2014-05-05")]))
        self.svc.ingest_batch(batch("B2", DEPT_CULTURE,
                                    rosters=[roster("w2", "王芳", gender="女", birth="2014-05-05",
                                                    dept=DEPT_CULTURE)]))
        q = self.svc.list_identity_queue("open")[0]
        self.svc.resolve_identity(q["queue_id"], "确认唯一", decided_by="审计人员")
        stats = self.svc.explain(school_code=S1, scope_from="2026-01-01", scope_to="2026-12-31")
        self.assertEqual(self.metric(stats, "enrolled_students")["value"], 2)

    # ---- 4. 迟到计入、缺席无效排除 ---------------------------------------------

    def test_late_included_and_invalid_excluded(self):
        self.svc.ingest_batch(batch(
            "B1", DEPT_SCHOOL,
            rosters=[roster("r1", "林涛", id_number="110101201406061234"),
                     roster("r2", "赵敏", gender="女", id_number="110101201407071234")],
            sessions=[session("sA", "竹编", "2026-03-10")],
            checkins=[checkin("c1", "r1", "sA", dept=DEPT_SCHOOL, status="迟到",
                              time="2026-03-10T09:25:00+08:00"),
                      checkin("c2", "r2", "sA", dept=DEPT_SCHOOL, status="缺席")]))
        stats = self.svc.explain(school_code=S1, scope_from="2026-01-01", scope_to="2026-12-31")
        self.assertEqual(self.metric(stats, "checkins_valid")["value"], 1)
        self.assertEqual(self.metric(stats, "checkins_late")["value"], 1)
        self.assertTrue(any("缺席" in e["reason"] for e in self.metric(stats, "checkins_valid")["excludes"]))
        self.assertEqual(self.metric(stats, "covered_students")["value"], 1)

    # ---- 5. 转学按举办日归属 ---------------------------------------------------

    def test_transfer_attribution_by_hold_date(self):
        self.svc.ingest_batch(batch(
            "B1", DEPT_SCHOOL,
            rosters=[roster("r1", "钱转", id_number="110101201408081234")],
            sessions=[session("sA", "老学校活动", "2026-03-01", school=S1),
                      session("sB", "新学校活动", "2026-05-01", school=S2)],
            checkins=[checkin("c1", "r1", "sA", dept=DEPT_SCHOOL, time="2026-03-01T09:00:00+08:00"),
                      checkin("c2", "r1", "sB", dept=DEPT_SPORTS, time="2026-05-01T09:00:00+08:00")]))
        # 2026-04-01 转入 S2。
        self.svc.transfer_student({"roster_id": "r1", "new_school_code": S2,
                                   "new_school_name": S2, "effective_date": "2026-04-01"})
        s1 = self.svc.explain(school_code=S1, scope_from="2026-01-01", scope_to="2026-12-31")
        s2 = self.svc.explain(school_code=S2, scope_from="2026-01-01", scope_to="2026-12-31")
        self.assertEqual(self.metric(s1, "covered_students")["value"], 1)
        self.assertEqual(self.metric(s2, "covered_students")["value"], 1)
        # S1 视图里新学校那条签到必须作为跨校排除可解释。
        self.assertTrue(any("举办日学籍归属" in e["reason"] and "S2" in e["reason"]
                            for e in s1["excluded_ledger"]))
        # 学年底学生总数归 S2，不归 S1。
        self.assertEqual(self.metric(s2, "enrolled_students")["value"], 1)
        self.assertEqual(self.metric(s1, "enrolled_students")["value"], 0)

    # ---- 6. 封账后写入被拒、只能追加更正单、首签决胜 ----------------------------

    def _sealed_report(self):
        self.svc.ingest_batch(batch(
            "B1", DEPT_SCHOOL,
            rosters=[roster("r1", "孙封", id_number="110101201409091234")],
            sessions=[session("sA", "封账场", "2026-03-10")],
            checkins=[checkin("c1", "r1", "sA", dept=DEPT_SCHOOL)]))
        self.svc.create_report({"report_id": "RP1", "school_code": S1,
                                "scope_from": "2026-01-01", "scope_to": "2026-06-30",
                                "created_by": "统计员甲"})
        self.svc.sign_report("RP1", "统计员甲")

    def test_writes_rejected_after_sealing(self):
        self._sealed_report()
        with self.assertRaises(ReportClosedError) as ctx:
            self.svc.ingest_batch({
                "batch": {"batch_id": "B9", "department": DEPT_CULTURE,
                          "submitted_at": "2026-07-01T10:00:00+08:00"},
                "checkins": [checkin("cx", "r1", "sA", dept=DEPT_CULTURE)]})
        self.assertEqual(ctx.exception.report_ids, ["RP1"])

    def test_signed_report_append_only_correction_and_recompute(self):
        self._sealed_report()
        rep = self.svc.get_report("RP1")
        frozen_valid = rep["snapshot"]["metrics"]["checkins_valid"]["value"]
        real_checkin_id = rep["snapshot"]["metrics"]["checkins_valid"]["includes"][0]["ref_id"]
        self.svc.append_correction("RP1", {
            "reason": "审计发现 1 条代签，冲正", "operator": "审计人员",
            "items": [{"metric": "checkins_valid", "delta": -1, "ref_type": "checkin",
                       "ref_id": real_checkin_id, "note": "代签冲正"}]})
        rep2 = self.svc.get_report("RP1")
        self.assertEqual(rep2["adjusted_values"]["checkins_valid"], frozen_valid - 1)
        # 原快照保持不变（追加，不改写）。
        self.assertEqual(rep2["snapshot"]["metrics"]["checkins_valid"]["value"], frozen_valid)
        check = self.svc.recompute_report("RP1")
        self.assertTrue(check["matches"])

    def test_concurrent_sign_first_wins(self):
        self._sealed_report_draft_only()
        outcomes: list[str] = []
        lock = threading.Lock()

        def sign(who: str):
            try:
                self.svc.sign_report("RP1", who)
                with lock:
                    outcomes.append(f"ok:{who}")
            except ReportAlreadySignedError:
                with lock:
                    outcomes.append(f"lost:{who}")

        threads = [threading.Thread(target=sign, args=(f"封账人{i}",)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len([o for o in outcomes if o.startswith("ok")]), 1)
        self.assertEqual(len([o for o in outcomes if o.startswith("lost")]), 15)

    def _sealed_report_draft_only(self):
        self.svc.ingest_batch(batch(
            "B1", DEPT_SCHOOL,
            rosters=[roster("r1", "孙封", id_number="110101201409091234")],
            sessions=[session("sA", "封账场", "2026-03-10")],
            checkins=[checkin("c1", "r1", "sA", dept=DEPT_SCHOOL)]))
        self.svc.create_report({"report_id": "RP1", "school_code": S1,
                                "scope_from": "2026-01-01", "scope_to": "2026-06-30"})

    # ---- 7. 幂等与一致性 ------------------------------------------------------

    def test_duplicate_batch_is_idempotent_rejection(self):
        payload = batch("B1", DEPT_SCHOOL, rosters=[roster("r1", "周幂", id_number="110101201410101234")])
        self.svc.ingest_batch(payload)
        with self.assertRaises(DuplicateRecordError):
            self.svc.ingest_batch(payload)

    def test_covered_never_exceeds_enrolled(self):
        self.svc.ingest_batch(batch(
            "B1", DEPT_SCHOOL,
            rosters=[roster("r1", "吴一", id_number="110101201411111234")],
            sessions=[session("sA", "A", "2026-03-10"), session("sB", "B", "2026-04-10")],
            checkins=[checkin("c1", "r1", "sA", dept=DEPT_SCHOOL),
                      checkin("c2", "r1", "sB", dept=DEPT_SCHOOL)]))
        stats = self.svc.explain(school_code=S1, scope_from="2026-01-01", scope_to="2026-12-31")
        self.assertLessEqual(self.metric(stats, "covered_students")["value"],
                             self.metric(stats, "enrolled_students")["value"])
        self.assertTrue(all(c["passed"] for c in stats["consistency_checks"]))

    # ---- 8. 单记录谱系 --------------------------------------------------------

    def test_lineage_explains_record(self):
        r = self.svc.ingest_batch(batch(
            "B1", DEPT_SCHOOL,
            rosters=[roster("r1", "郑谱", id_number="110101201412121234")],
            sessions=[session("sA", "谱系场", "2026-03-10")],
            checkins=[checkin("c1", "r1", "sA", dept=DEPT_SCHOOL)]))
        cid = r["checkin_ids"][0]
        lin = self.svc.lineage("checkin", cid)
        self.assertEqual(lin["source"]["batch_id"], "B1")
        self.assertEqual(lin["source"]["department"], DEPT_SCHOOL)
        self.assertEqual(lin["session"]["current_status"], "发布")
        self.assertEqual(lin["roster"]["name"], "郑谱")


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        from reconciliation.api import create_server
        self.server = create_server("127.0.0.1", 0, ":memory:")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def call(self, method: str, path: str, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     headers={"Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_http_flow(self):
        status, body = self.call("GET", "/healthz")
        self.assertEqual(status, 200)
        payload = batch("B1", DEPT_SCHOOL,
                        rosters=[roster("r1", "李雷", id_number="110101201403031234")],
                        sessions=[session("sA", "皮影", "2026-03-10")],
                        checkins=[checkin("c1", "r1", "sA", dept=DEPT_SCHOOL)])
        status, body = self.call("POST", "/v1/batches", payload)
        self.assertEqual(status, 201)
        status, stats = self.call("GET", f"/v1/stats/explain?school_code={quote(S1)}&from=2026-01-01&to=2026-12-31")
        self.assertEqual(status, 200)
        self.assertEqual(stats["metrics"]["covered_students"]["value"], 1)
        status, calibers = self.call("GET", "/v1/calibers")
        self.assertEqual(status, 200)
        self.assertIn("2026-annual-v1", [c["version"] for c in calibers["calibers"]])


if __name__ == "__main__":
    unittest.main()
