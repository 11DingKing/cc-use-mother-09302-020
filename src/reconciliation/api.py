"""基于标准库 http.server 的 JSON HTTP 接口。

路由：
- POST /v1/batches                 归集报送批次（名册/场次/签到 + 来源谱系）
- POST /v1/sessions/{id}/cancel   取消场次
- POST /v1/sessions/reissue       取消后补办（建立补办关系）
- GET  /v1/identity-queue         疑似重复确认队列（?status=open）
- POST /v1/identity-queue/{id}/resolve  确认唯一 / 确认重复
- POST /v1/students/transfer      转学归属
- POST /v1/reports                生成报告草稿（冻结口径与快照）
- POST /v1/reports/{id}/sign      封账（首签决胜）
- POST /v1/reports/{id}/corrections  已签报告追加更正单
- GET  /v1/reports/{id}           报告 + 更正单 + 调整后数值
- GET  /v1/reports/{id}/recompute 可复算自证
- GET  /v1/stats/explain          逐数字解释（?school_code=&from=&to=&caliber=）
- GET  /v1/lineage/{type}/{id}    单记录来源谱系
- GET  /v1/calibers               已发布口径清单
- GET  /healthz
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .calibers import CALIBERS
from .errors import ReconciliationError
from .services import ReconciliationService
from .store import open_store


class _Handler(BaseHTTPRequestHandler):
    service: ReconciliationService = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
        return

    # ---- 工具 ----------------------------------------------------------------

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            self._write_error(400, "invalid_json", str(exc))
            return {}
        if not isinstance(value, dict):
            self._write_error(400, "invalid_json", "请求体必须是 JSON 对象")
            return {}
        return value

    def _write_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_error(self, status: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        self._write_json(status, {"error": code, "message": message, "details": details or {}})

    def _query(self) -> dict[str, str]:
        qs = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in qs.items()}

    # ---- 路由 ----------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/healthz":
                self._write_json(200, {"status": "ok"})
            elif path == "/v1/calibers":
                self._write_json(200, {"calibers": [c.to_dict() for c in CALIBERS.values()]})
            elif path == "/v1/identity-queue":
                q = self._query()
                self._write_json(200, {"items": self.service.list_identity_queue(q.get("status", "open"))})
            elif path == "/v1/stats/explain":
                q = self._query()
                self._write_json(200, self.service.explain(
                    school_code=q.get("school_code"), scope_from=q.get("from"),
                    scope_to=q.get("to"), caliber_version=q.get("caliber")))
            else:
                m = re.fullmatch(r"/v1/reports/([^/]+)", path)
                if m:
                    self._write_json(200, self.service.get_report(m.group(1)))
                    return
                m = re.fullmatch(r"/v1/reports/([^/]+)/recompute", path)
                if m:
                    self._write_json(200, self.service.recompute_report(m.group(1)))
                    return
                m = re.fullmatch(r"/v1/lineage/(roster|checkin|session)/([^/]+)", path)
                if m:
                    self._write_json(200, self.service.lineage(m.group(1), m.group(2)))
                    return
                self._write_error(404, "not_found", f"无此路由：{path}")
        except ReconciliationError as exc:
            self._write_error(exc.http_status, exc.code, exc.message, exc.details)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        payload = self._read_json()
        try:
            if path == "/v1/batches":
                self._write_json(201, self.service.ingest_batch(payload))
            elif path == "/v1/sessions/reissue":
                self._write_json(201, self.service.reissue_session(payload))
            elif path == "/v1/students/transfer":
                self._write_json(200, self.service.transfer_student(payload))
            elif path == "/v1/reports":
                self._write_json(201, self.service.create_report(payload))
            else:
                m = re.fullmatch(r"/v1/sessions/([^/]+)/cancel", path)
                if m:
                    self._write_json(200, self.service.cancel_session(
                        m.group(1), at_time=payload["at_time"], actor=payload.get("actor"),
                        reason=payload.get("reason")))
                    return
                m = re.fullmatch(r"/v1/identity-queue/([^/]+)/resolve", path)
                if m:
                    self._write_json(200, self.service.resolve_identity(
                        m.group(1), payload["resolution"],
                        decided_by=payload.get("decided_by", "匿名"),
                        canonical_roster_id=payload.get("canonical_roster_id"),
                        note=payload.get("note")))
                    return
                m = re.fullmatch(r"/v1/reports/([^/]+)/sign", path)
                if m:
                    self._write_json(200, self.service.sign_report(
                        m.group(1), payload.get("operator", "匿名")))
                    return
                m = re.fullmatch(r"/v1/reports/([^/]+)/corrections", path)
                if m:
                    self._write_json(201, self.service.append_correction(m.group(1), payload))
                    return
                self._write_error(404, "not_found", f"无此路由：{path}")
        except ReconciliationError as exc:
            self._write_error(exc.http_status, exc.code, exc.message, exc.details)


def create_server(host: str = "127.0.0.1", port: int = 8080, db_path: str = ":memory:") -> ThreadingHTTPServer:
    service = ReconciliationService(open_store(db_path))
    _Handler.service = service
    server = ThreadingHTTPServer((host, port), _Handler)
    server.service = service  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="非遗活动成效对账后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=":memory:", help="SQLite 文件路径（默认内存库）")
    args = parser.parse_args(argv)
    server = create_server(args.host, args.port, args.db)
    print(f"对账后端监听 http://{args.host}:{args.port}（db={args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
