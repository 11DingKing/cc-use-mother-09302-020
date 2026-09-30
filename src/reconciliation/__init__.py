"""非遗活动成效对账后端。

层次：
- ``calibers``：已发布统计口径（不可变版本）。
- ``store``：SQLite 仅追加谱系存储（名册、签到、场次事件、身份队列、报告、更正单）。
- ``engine``：纯函数统计引擎，输出可复算结果与逐数字解释。
- ``services``：应用服务，编排写入、封账、更正与归属规则。
- ``api``：基于标准库的 JSON HTTP 接口。
"""
from __future__ import annotations

from .calibers import CALIBERS, Caliber, get_caliber
from .services import ReconciliationService
from .store import open_store

__all__ = [
    "ReconciliationService",
    "open_store",
    "Caliber",
    "CALIBERS",
    "get_caliber",
]
