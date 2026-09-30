"""领域错误码。HTTP 层据此映射状态码。"""
from __future__ import annotations

from typing import Any


class ReconciliationError(Exception):
    """所有可预期领域错误的基类。"""

    code = "domain_error"
    http_status = 400

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}


class ValidationError(ReconciliationError):
    code = "validation_error"
    http_status = 422


class NotFoundError(ReconciliationError):
    code = "not_found"
    http_status = 404


class ConflictError(ReconciliationError):
    code = "conflict"
    http_status = 409


class DuplicateRecordError(ConflictError):
    code = "duplicate_record"


class ReportClosedError(ConflictError):
    """数据落在已封账区间，只能以更正单追加。"""

    code = "report_closed"

    def __init__(self, message: str, report_ids: list[str]) -> None:
        super().__init__(message, {"report_ids": report_ids})
        self.report_ids = report_ids


class ReportAlreadySignedError(ConflictError):
    """多人并发封账时只有一个赢家。"""

    code = "report_already_signed"


class ReportNotDraftError(ConflictError):
    code = "report_not_draft"


class CaliberNotFoundError(NotFoundError):
    code = "caliber_not_found"
