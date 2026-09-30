"""维修与翻新谱系的输入校验与领域常量。"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

COMPONENT_KINDS = {"module", "cell", "bms", "other"}
ASSEMBLY_KINDS = {"pack", "device"}
ASSEMBLY_ORIGINS = {"external", "refurb"}
DISPOSITIONS = {"reuse", "repair", "scrap", "quarantine"}
INSPECTION_RESULTS = {"pass", "fail"}
WORK_ORDER_KINDS = {"teardown", "rebuild"}
STOCK_STATES = {"reuse", "repair", "quarantine", "scrap"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def choice(value: object, field: str, allowed: set[str]) -> str:
    result = required_text(value, field, 32)
    if result not in allowed:
        raise ValidationFailed(f"{field} 必须是 {'、'.join(sorted(allowed))} 之一")
    return result


def metrics_map(value: object, field: str = "metrics") -> dict[str, Any]:
    """检测指标必须是非空对象，数值为有限数字。"""

    if not isinstance(value, Mapping) or not value:
        raise ValidationFailed(f"{field} 必须是非空对象")
    parsed: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValidationFailed(f"{field} 的键不能为空")
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise ValidationFailed(f"{field}.{key} 必须是有限数值")
        parsed[key.strip()] = item
    return parsed


def fault_evidence(value: object) -> dict[str, Any]:
    """冻结的故障证据必须是非空对象，且可规范化序列化。"""

    if not isinstance(value, Mapping) or not value:
        raise ValidationFailed("fault_evidence 必须是非空对象")
    from .jsonutil import canonical_json

    try:
        text = canonical_json(dict(value))
    except TypeError as exc:
        raise ValidationFailed("fault_evidence 必须是可 JSON 序列化的对象") from exc
    if len(text) > 8192:
        raise ValidationFailed("fault_evidence 不能超过 8192 个字符")
    return dict(value)
