"""身份指纹与重复疑似判定。

报送来自不同学校/部门，同一自然人可能以略有差异的文本出现。
指纹分两级：
- 强指纹：证件号一致 → 直接判为同一自然人，自动归并；
- 弱指纹：姓名+性别+出生日期一致（证件缺失或不一致）→ 疑似重复，进入确认队列。

无证件且弱指纹也不一致的，绝不并单。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_ID_RE = re.compile(r"[0-9Xx]")


def normalize_name(name: str) -> str:
    """姓名归一化：去空白，统一为 NFC。"""
    return re.sub(r"\s+", "", (name or "")).strip()


def normalize_id(id_value: str | None) -> str:
    """证件号归一化：仅保留数字与字母 X，大写。空值返回空串。"""
    if not id_value:
        return ""
    return "".join(_ID_RE.findall(id_value)).upper()


def strong_fingerprint(id_value: str | None) -> str:
    """强指纹：归一化证件号的 SHA-256。空证件无强指纹（返回空串）。"""
    normalized = normalize_id(id_value)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def weak_fingerprint(name: str, gender: str | None, birth_date: str | None) -> str:
    """弱指纹：归一化姓名 + 性别 + 出生日期。"""
    parts = [normalize_name(name), (gender or "").strip(), (birth_date or "").strip()]
    return hashlib.sha256("\u001f".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Fingerprints:
    strong: str
    weak: str

    @classmethod
    def of(cls, name: str, gender: str | None, birth_date: str | None, id_value: str | None) -> "Fingerprints":
        return cls(strong=strong_fingerprint(id_value), weak=weak_fingerprint(name, gender, birth_date))
