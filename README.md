# 非遗活动成效对账

县教育部门非遗活动成效对账后端：把**名册、签到、场次状态、补办关系和数据来源**组成可追溯谱系，
依据**已发布口径**生成可复算统计。每个统计数字都能解释其包含、排除或冲正了哪些记录。

## 解决的问题

年度汇报中"各校覆盖人数 > 学生总数"，根因是三类数据被简单相加：

1. **跨场参与**：同一学生参加多场被重复计人头；
2. **取消后补办**：取消场签到未剔除、补办场又计一遍；
3. **多部门重复报送**：学校、文旅中心、体卫艺股各自报送同一学生。

后端的处理原则：

- 人头指标（覆盖人数、实到人数、学生总数）按**自然人人头跨场去重**；人次指标（有效签到）可多次计数；
- 身份匹配两级指纹：证件号强指纹自动并单；姓名+性别+出生日期弱指纹只进**确认队列**，人工"确认唯一/确认重复"后才影响数字；
- 场次有发布/取消/补办事件谱系，取消场签到**事实保留但不计入**，补办场独立计入并冲正原场；
- 每条记录带来源（批次/部门/原始记录号），`(batch_id, source_record_id)` 唯一保证重复报送幂等拒绝；
- 统计口径冻结为不可变版本（`2026-annual-v1`），报告签发时冻结快照；
- **已签报告只能追加更正单**（delta 冲正、引用具体记录），原快照永不改写；落在已封账区间的写入被拒绝；
- **转学/迟到按活动举办日的学籍归属学校**统计；**多人同时封账由首签者决胜**（条件 UPDATE + 写锁）。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/reconciliation/`：对账后端。
  - `calibers.py`：已发布统计口径（不可变版本）。
  - `identity.py`：身份强/弱指纹。
  - `store.py`：SQLite 仅追加事实存储 + 全局事件日志（谱系）。
  - `engine.py`：纯函数统计引擎，输出指标及逐数字 `includes/excludes/reversals`。
  - `services.py`：应用服务（归集、确认、转学、封账、更正单、复算、谱系查询）。
  - `api.py`：标准库 JSON HTTP 接口。
- `tools/check_contract.py`：契约摘要检查。
- `tools/smoke_demo.py`：复现年度汇报事故的端到端冒烟脚本。
- `tests/`：契约回归 + 业务规则回归（含 16 线程并发封账）。

## 验证

```bash
# 单元/业务回归
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约摘要
python3 tools/check_contract.py domain/contract.json

# 启动服务（默认 127.0.0.1:8080，内存库；--db 指定 SQLite 文件持久化）
PYTHONPATH=src python3 -m reconciliation.api --port 8080 --db data.sqlite

# 端到端冒烟（先启动服务并按需改脚本顶部端口）
python3 tools/smoke_demo.py
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/batches` | 归集部门报送批次（名册/场次/签到 + 来源谱系） |
| POST | `/v1/sessions/{id}/cancel` | 取消场次（事实留痕） |
| POST | `/v1/sessions/reissue` | 取消后补办，建立补办冲正关系 |
| GET | `/v1/identity-queue?status=open` | 疑似重复确认队列 |
| POST | `/v1/identity-queue/{id}/resolve` | 确认唯一 / 确认重复（归并） |
| POST | `/v1/students/transfer` | 转学（关闭旧归属期、追加新区间） |
| POST | `/v1/reports` | 生成报告草稿（冻结口径版本与统计快照） |
| POST | `/v1/reports/{id}/sign` | 封账（首签决胜，并发冲突返回 409） |
| POST | `/v1/reports/{id}/corrections` | 已签报告追加更正单（delta 冲正） |
| GET | `/v1/reports/{id}` | 报告 + 原快照 + 更正单 + 调整后数值 |
| GET | `/v1/reports/{id}/recompute` | 用冻结口径复算并比对签发快照 |
| GET | `/v1/stats/explain?school_code=&from=&to=` | 逐数字解释 |
| GET | `/v1/lineage/{roster|checkin|session}/{id}` | 单记录来源谱系 |
| GET | `/v1/calibers` | 已发布口径清单 |

## 查询结果的解释结构

`/v1/stats/explain` 的每个指标都形如：

```json
{
  "value": 2,
  "includes": [ {"person": "...", "sessions": [...], "records": [...]} ],
  "excludes": [ {"ref_type": "checkin", "ref_id": "...", "reason": "场次已取消…"} ],
  "reversals": [ {"type": "canceled_then_reissued", "canceled_session": "s1", "reissue_session": "s3"} ]
}
```

顶层另有：
- `excluded_ledger`：全部排除记录及原因（取消、状态不在口径、同人同场重复、跨校归属、待确认）；
- `reversals`：取消→补办、跨场去重、重复签到折叠等冲正关系；
- `pending_identity`：疑似重复待确认簇；
- `consistency_checks`：内置一致性检查（如覆盖人数不得超过学生总数）。
