"""已发布统计口径（caliber）。

口径一旦发布即不可变：每份报告冻结其生成时使用的口径版本，
从而保证历史结果可以被任何后来者逐记录复算。
新增口径只能发布新版本，不得修改旧版本。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import CaliberNotFoundError


@dataclass(frozen=True)
class Caliber:
    """一个不可变统计口径版本。"""

    version: str
    published_at: str
    description: str
    # 统计口径（跨场去重的人头指标）。True 表示跨场去重，False 表示人次。
    distinct_person_metrics: frozenset[str]
    # 有效签到状态；迟到按显式开关计入。
    valid_attendance_statuses: frozenset[str]
    include_late_checkin: bool
    # 身份确认队列中哪些处置计入统计。
    included_identity_resolutions: frozenset[str]
    rules: tuple[str, ...] = field(default_factory=tuple)

    def is_distinct_person(self, metric: str) -> bool:
        return metric in self.distinct_person_metrics

    def attendance_counts(self, status: str) -> bool:
        if status not in self.valid_attendance_statuses:
            return False
        if status == "迟到":
            return self.include_late_checkin
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "published_at": self.published_at,
            "description": self.description,
            "distinct_person_metrics": sorted(self.distinct_person_metrics),
            "valid_attendance_statuses": sorted(self.valid_attendance_statuses),
            "include_late_checkin": self.include_late_checkin,
            "included_identity_resolutions": sorted(self.included_identity_resolutions),
            "rules": list(self.rules),
        }


# v1：2026 年度汇报采用的已发布口径。
CALIBER_V1 = Caliber(
    version="2026-annual-v1",
    published_at="2026-01-15T00:00:00+08:00",
    description="2026年度非遗活动成效对账口径（县教育部门发布）",
    distinct_person_metrics=frozenset({"covered_students", "attendance_unique"}),
    valid_attendance_statuses=frozenset({"正常", "迟到"}),
    include_late_checkin=True,
    included_identity_resolutions=frozenset({"确认唯一", "确认重复"}),
    rules=(
        "覆盖人数按自然学生人头跨场去重，禁止把跨场参与简单相加",
        "签到状态为正常或迟到均计入（迟到单列可追溯），缺席、无效签到不计",
        "取消的场次及其签到不计入；补办场次独立计入，并通过补办关系冲正原取消场",
        "不同部门重复报送同一学生时，重复身份先进确认队列；确认重复的报送保留主报送",
        "已签发报告不可修改，只能追加更正单；更正单按 delta 冲正，历史快照保留",
        "数据落在已封账区间时追加到更正单，不得改写底层记录",
        "转学学生在活动举办日按所在学校归属；封账由首签者决胜，迟到签到按举办日学校归属",
    ),
)

CALIBERS: dict[str, Caliber] = {CALIBER_V1.version: CALIBER_V1}


def get_caliber(version: str) -> Caliber:
    try:
        return CALIBERS[version]
    except KeyError as exc:
        raise CaliberNotFoundError(f"未发布的口径版本：{version}") from exc


def register_caliber(caliber: Caliber, *, allow_replace: bool = False) -> None:
    """发布新口径版本。默认禁止覆盖已发布版本。"""
    if caliber.version in CALIBERS and not allow_replace:
        raise ValueError(f"口径版本已发布且不可变：{caliber.version}")
    CALIBERS[caliber.version] = caliber
