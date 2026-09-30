"""端到端冒烟：复现年度汇报事故并验证修复（通过真实 HTTP）。"""
import json
import urllib.request
from urllib.parse import quote

BASE = "http://127.0.0.1:8101"


def call(method, path, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def roster(ref, name, **kw):
    d = {"client_ref": ref, "name": name, "gender": kw.pop("gender", "男"),
         "birth_date": kw.pop("birth", "2014-05-05"),
         "school_code": kw.pop("school", "县一小"), "school_name": kw.pop("school_name", "县一小")}
    d.update(kw)
    return d


def session(ref, title, date, school="县一小"):
    return {"client_ref": ref, "title": title, "hold_date": date,
            "school_code": school, "school_name": school}


def checkin(ref, r, s, status="正常", t="2026-03-10T09:00:00+08:00"):
    return {"client_ref": ref, "roster_ref": r, "session_ref": s, "status": status, "checkin_time": t}


def batch(bid, dept, rosters=(), sessions=(), checkins=()):
    return {"batch": {"batch_id": bid, "department": dept, "submitted_at": "2026-03-20T10:00:00+08:00"},
            "rosters": list(rosters), "sessions": list(sessions), "checkins": list(checkins)}


# 1) 学校上报：3 名学生，其中李雷参加 2 个场次（跨场）
call("POST", "/v1/batches", batch("B-school", "县一小统计员",
    rosters=[roster("lilei", "李雷", id_number="110101201403031234"),
             roster("hanmeimei", "韩梅梅", gender="女", id_number="110101201404041234"),
             roster("wangfang", "王芳", gender="女")],
    sessions=[session("s1", "皮影戏", "2026-03-10"), session("s2", "剪纸", "2026-04-10")],
    checkins=[checkin("c1", "lilei", "s1"), checkin("c2", "lilei", "s2", t="2026-04-10T09:00:00+08:00"),
              checkin("c3", "hanmeimei", "s1", status="迟到", t="2026-03-10T09:22:00+08:00")]))

# 2) 文旅中心重复报送李雷同场签到（强指纹并单，同人同场重复应折叠）
call("POST", "/v1/batches", batch("B-culture", "县文旅中心",
    rosters=[roster("lilei2", "李雷", id_number="110101201403031234")],
    checkins=[checkin("c4", "lilei2", "s1")]))

# 3) 体卫艺股也报了一个王芳（无证件，弱指纹疑似重复 → 确认队列）
call("POST", "/v1/batches", batch("B-sports", "县体卫艺股",
    rosters=[roster("wangfang2", "王芳", gender="女")]))

# 4) 场次 s1 取消后补办为 s3；s1 签到排除，s3 签到计入并保留冲正关系
call("POST", "/v1/sessions/reissue", {"session_id": "s3", "reissues_for_session_id": "s1",
      "hold_date": "2026-03-25", "actor": "县教育部门", "source_record_id": "rj-1"})
call("POST", "/v1/batches", {"batch": {"batch_id": "B-r3", "department": "县一小统计员",
      "submitted_at": "2026-03-26T10:00:00+08:00"},
      "checkins": [checkin("c5", "lilei", "s3", t="2026-03-25T09:00:00+08:00"),
                   checkin("c6", "hanmeimei", "s3", status="迟到", t="2026-03-25T09:18:00+08:00")]})

# 5) 确认队列：两个王芳确认为同一人（归并）
q = call("GET", "/v1/identity-queue?status=open")[1]["items"][0]
call("POST", f"/v1/identity-queue/{q['queue_id']}/resolve",
     {"resolution": "确认重复", "decided_by": "审计人员", "canonical_roster_id": q["roster_id_a"]})

# 6) 王芳 2026-05-01 转入县二小；在县二小的活动按举办日归属
call("POST", "/v1/batches", batch("B-s2-school", "县二小统计员",
    sessions=[session("s4", "竹编", "2026-06-01", school="县二小")]))
call("POST", "/v1/students/transfer", {"person_key": None, "roster_id": "wangfang",
     "new_school_code": "县二小", "new_school_name": "县二小", "effective_date": "2026-05-01"})
call("POST", "/v1/batches", {"batch": {"batch_id": "B-s4", "department": "县二小统计员",
      "submitted_at": "2026-06-02T10:00:00+08:00"},
      "checkins": [checkin("c7", "wangfang", "s4", t="2026-06-01T09:00:00+08:00")]})

# 7) 生成报告并封账
call("POST", "/v1/reports", {"report_id": "RP-2026-H1", "school_code": "县一小",
     "scope_from": "2026-01-01", "scope_to": "2026-06-30", "created_by": "统计员甲"})
st, body = call("POST", "/v1/reports/RP-2026-H1/sign", {"operator": "统计员甲"})
print("封账:", st, body["status"], body["signed_by"])

# 8) 封账后补写被拒绝（只能更正单）
st, body = call("POST", "/v1/batches", {"batch": {"batch_id": "B-late", "department": "县文旅中心",
      "submitted_at": "2026-07-10T10:00:00+08:00"},
      "checkins": [checkin("cx", "lilei2", "s1")]})
print("封账后写入:", st, body["error"], "→", body["details"])

# 9) 追加更正单
st, body = call("POST", "/v1/reports/RP-2026-H1/corrections",
    {"reason": "审计认定补办场 1 条代签，冲正", "operator": "审计人员",
     "items": [{"metric": "checkins_valid", "delta": -1, "ref_type": "checkin",
                "ref_id": "c5", "note": "补办场代签冲正"}]})
print("更正单追加:", st, "调整后有效人次 =", body["adjusted_values"]["checkins_valid"])

# 10) 可复算自证
st, body = call("GET", "/v1/reports/RP-2026-H1/recompute")
print("复算一致:", body["matches"])

# 11) 逐数字解释
st, stats = call("GET", f"/v1/stats/explain?school_code={quote('县一小')}&from=2026-01-01&to=2026-06-30")
print("\n=== 县一小 2026H1 指标解释 ===")
for k, m in stats["metrics"].items():
    print(f"{m['label']}: {m['value']}（计入 {len(m['includes'])}，排除 {len(m['excludes'])}，冲正 {len(m['reversals'])}）")
print("一致性检查:", stats["consistency_checks"])

# 12) 单记录谱系
st, lin = call("GET", "/v1/lineage/checkin/c3")
print("\n=== c3（韩梅梅迟到签到）谱系 ===")
print("来源:", lin["source"]["department"], lin["source"]["batch_id"], "/", lin["source"]["record_id"])
print("场次状态:", lin["session"]["current_status"], "事件序列:", [e["event_type"] for e in lin["session"]["events"]])
