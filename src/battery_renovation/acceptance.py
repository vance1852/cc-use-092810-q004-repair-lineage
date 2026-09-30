"""维修与翻新谱系的离线验收：两个退役整包重组为一套梯次利用设备。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import Forbidden, InvalidState
from .service import RefurbService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = RefurbService(
        connection, FrozenClock(datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc))
    )
    for user_id, role in (
        ("plan", "planner"), ("tech", "technician"),
        ("eng", "engineer"), ("qual", "quality"), ("aud", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    # 进场：两个退役整包及其模组（纸单时代的既有配置逐件登记）
    for pack, modules in (("pack-A", ("mod-A1", "mod-A2")), ("pack-B", ("mod-B1", "mod-B2"))):
        service.register_item("plan", pack, "pack", "LFP-280", "原厂")
        for module in modules:
            service.register_item("plan", module, "module", "LFP-280-M", "原厂")
            service.record_as_found_membership("plan", pack, module, module[-2:])

    # 故障证据（含内容摘要）冻结在拆解之前
    service.record_fault_evidence("tech", "ev-A", "pack-A", "thermal", "A 包温差告警", "a" * 64)
    service.record_fault_evidence("tech", "ev-B", "pack-B", "capacity", "B 包容量衰减", "b" * 64)

    # 检测定版：A1 直接复用，A2 送修，B1 复用，B2 先隔离
    insp_a1 = service.record_inspection("tech", "mod-A1", "SOH-v2", "reuse", {"soh": 0.91})
    service.record_inspection("tech", "mod-A2", "SOH-v2", "repair", {"soh": 0.72})
    insp_b1 = service.record_inspection("tech", "mod-B1", "SOH-v2", "reuse", {"soh": 0.88})
    service.record_inspection("tech", "mod-B2", "SOH-v2", "quarantine", {"soh": 0.55})

    # 拆解 A：冻结配置与证据后开工，组件分别处置
    service.create_disassembly("plan", "wo-A", "pack-A")
    service.attach_evidence("tech", "wo-A", "ev-A")
    service.freeze_disassembly("plan", "wo-A", 2)
    service.start_disassembly("tech", "wo-A", 3)
    service.execute_disposition("tech", "wo-A", "mod-A1", "reuse", "直接复用")
    service.execute_disposition("tech", "wo-A", "mod-A2", "repair", "均衡线更换后复检")
    service.complete_disassembly("tech", "wo-A")

    # A2 维修：引用确定版本维修动作 + 复检 reuse 才回复用库
    service.open_repair("tech", "rp-A2", "mod-A2")
    action_a2 = service.add_repair_action("tech", "rp-A2", "balance-wire", {"replaced": 4})
    insp_a2 = service.record_inspection("eng", "mod-A2", "SOH-v2", "reuse", {"soh": 0.86})
    service.complete_repair("tech", "rp-A2", insp_a2["inspection_id"])

    # 拆解 B：第一次执行部分失败（B1 已拆，B2 仍在原包），旧单失败后开新单续拆
    service.create_disassembly("plan", "wo-B1", "pack-B")
    service.attach_evidence("tech", "wo-B1", "ev-B")
    service.freeze_disassembly("plan", "wo-B1", 2)
    service.start_disassembly("tech", "wo-B1", 3)
    service.execute_disposition("tech", "wo-B1", "mod-B1", "reuse")
    service.fail_disassembly("tech", "wo-B1", "现场工装故障中止")

    service.create_disassembly("plan", "wo-B2", "pack-B")
    service.attach_evidence("tech", "wo-B2", "ev-B")
    service.freeze_disassembly("plan", "wo-B2", 2)
    service.start_disassembly("tech", "wo-B2", 3)
    service.execute_disposition("tech", "wo-B2", "mod-B2", "quarantine", "等待定级")
    service.complete_disassembly("tech", "wo-B2")

    # B2 依据新检测降级报废，剩余部件去向明确
    scrap_inspection = service.record_inspection("tech", "mod-B2", "SOH-v2", "scrap", {"soh": 0.51})
    service.resolve_component("tech", "mod-B2", scrap_inspection["inspection_id"])

    # 重新组包 pack-R：逐行引用确定版本检测与维修动作
    service.create_reassembly("plan", "wo-R", "pack-R", "LFP-梯次", "翻新中心")
    service.add_reassembly_line("tech", "wo-R", "mod-A1", "M1", insp_a1["inspection_id"])
    service.add_reassembly_line(
        "tech", "wo-R", "mod-A2", "M2", insp_a2["inspection_id"], action_a2["action_id"])
    service.add_reassembly_line("tech", "wo-R", "mod-B1", "M3", insp_b1["inspection_id"])
    service.freeze_reassembly("tech", "wo-R", 4)
    service.start_reassembly("tech", "wo-R", 5)

    # 双签：技术确认与质量放行必须不同人
    service.technical_confirm("eng", "wo-R", 6)
    try:
        service.quality_release("eng", "wo-R", 7)
    except Forbidden:
        pass
    else:  # pragma: no cover - 验收必须捕获越权
        raise AssertionError("同一人不应能完成技术确认与质量放行")
    service.quality_release("qual", "wo-R", 7)
    service.deliver_reassembly("qual", "wo-R")

    # 出场后任何配置变更都必须被拒绝
    try:
        service.record_inspection("tech", "mod-A1", "SOH-v2", "reuse", {"soh": 0.9})
    except InvalidState:
        pass
    else:  # pragma: no cover
        raise AssertionError("出场组件不能再追加检测")

    up = service.lineage_up("aud", "pack-R")
    down_a2 = service.lineage_down("aud", "mod-A2")
    down_b2 = service.lineage_down("aud", "mod-B2")
    blockers = service.delivery_blockers("aud", "wo-R")
    audit = service.audit_chain("aud")

    a2_node = next(node for node in up["sources"] if node["component_id"] == "mod-A2")
    result = {
        "status": "ok",
        "workspace": workspace.name,
        "new_pack_state": up["item"]["state"],
        "source_count": len(up["sources"]),
        "source_origins": {
            node["component_id"]: node["origin"]["from_pack_id"] for node in up["sources"]
        },
        "a2_final_destination": down_a2["final_destination"],
        "a2_repair_orders": len(a2_node["repairs"]),
        "a2_repair_actions": len(a2_node["repairs"][0]["actions"]),
        "b2_final_destination": down_b2["final_destination"],
        "b2_pending_actions": down_b2["pending_actions"],
        "deliverable": blockers["deliverable"],
        "audit": audit,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行维修与翻新谱系离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
