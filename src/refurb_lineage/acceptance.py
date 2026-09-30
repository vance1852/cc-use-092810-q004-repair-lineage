"""维修与翻新谱系的离线验收：两个退役包重组一套梯次利用设备并双向追溯。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import LineageService
from .storage import inspect_schema


def _modules(prefix: str, indexes: range) -> list[dict[str, str]]:
    return [
        {
            "component_id": f"mod-{prefix}{index}",
            "kind": "module",
            "model_name": "LFP-280 梯次模组",
            "position": f"S{index}",
        }
        for index in indexes
    ]


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = LineageService(connection, FrozenClock(datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)))
    try:
        service.create_user("intake-1", "翻新受理员", "intake")
        service.create_user("tech-1", "维修技师", "technician")
        service.create_user("qa-1", "质量工程师", "quality")
        service.create_user("audit-1", "审计人员", "auditor")

        # 两个退役电池包进场，登记进场配置。
        service.register_assembly(
            "intake-1", "pack-a", "PACK-2024-0001", "pack", "external", _modules("a", range(1, 5))
        )
        service.register_assembly(
            "intake-1", "pack-b", "PACK-2024-0002", "pack", "external", _modules("b", range(1, 5))
        )

        # 拆解工单冻结进场配置与故障证据。
        for pack, work_order, codes in (
            ("pack-a", "wo-teardown-a", ["BMS-E12", "CELL-DELTA-HIGH"]),
            ("pack-b", "wo-teardown-b", ["PACK-SOH-LOW"]),
        ):
            service.create_work_order("intake-1", work_order, "teardown", pack)
            service.freeze_work_order(
                "intake-1", work_order,
                {"fault_codes": codes, "report_sha256": "c" * 64, "notes": "退役进场容量复测"},
                1,
            )

        # 进场检测：mod-a3 与 mod-b4 不合格，其余合格。
        for component in ("mod-a1", "mod-a2", "mod-a4", "mod-b1", "mod-b2", "mod-b3"):
            service.record_inspection(
                "tech-1", component, "pass",
                {"capacity_ah": 262.5, "internal_resistance_mohm": 0.31},
                f"insp-{component}", "进场复测",
            )
        for component in ("mod-a3", "mod-b4"):
            service.record_inspection(
                "tech-1", component, "fail",
                {"capacity_ah": 198.0, "internal_resistance_mohm": 0.9},
                f"insp-{component}", "进场复测",
            )

        # 拆解处置：复用、维修、报废、隔离各归其位。
        service.record_disassembly("intake-1", "wo-teardown-a", "mod-a1", "reuse", "复测合格", "dis-a1")
        service.record_disassembly("intake-1", "wo-teardown-a", "mod-a2", "reuse", "复测合格", "dis-a2")
        service.record_disassembly("intake-1", "wo-teardown-a", "mod-a3", "scrap", "容量严重衰减", "dis-a3")
        service.record_disassembly("intake-1", "wo-teardown-a", "mod-a4", "repair", "采样线束老化", "dis-a4")
        service.record_disassembly("intake-1", "wo-teardown-b", "mod-b1", "reuse", "复测合格", "dis-b1")
        service.record_disassembly("intake-1", "wo-teardown-b", "mod-b2", "quarantine", "外观待判定", "dis-b2")
        service.record_disassembly("intake-1", "wo-teardown-b", "mod-b3", "reuse", "复测合格", "dis-b3")
        service.record_disassembly("intake-1", "wo-teardown-b", "mod-b4", "scrap", "绝缘失效", "dis-b4")
        service.complete_work_order("intake-1", "wo-teardown-a", 2)
        service.complete_work_order("intake-1", "wo-teardown-b", 2)

        # 维修 mod-a4 并复检完工；隔离件 mod-b2 由质量判定复用。
        repair = service.open_repair("tech-1", "mod-a4", "更换采样线束")
        recheck = service.record_inspection(
            "tech-1", "mod-a4", "pass",
            {"capacity_ah": 261.0, "internal_resistance_mohm": 0.33},
            "insp-mod-a4-recheck", "维修后复检",
        )
        service.close_repair("tech-1", repair["repair_id"], "completed", "复检合格", recheck["inspection_id"])
        service.redisposition_component("qa-1", "mod-b2", "reuse", "外观复检合格")

        # 组包：引用确定版本的检测与维修动作装机。
        service.register_assembly("intake-1", "dev-c", "ECHELON-2026-0001", "device", "refurb")
        service.create_work_order("intake-1", "wo-build-c", "rebuild", "dev-c")
        plan = (
            ("mod-a1", "M1", None),
            ("mod-a2", "M2", None),
            ("mod-b1", "M3", None),
            ("mod-a4", "M4", repair["repair_id"]),
        )
        for component, position, repair_id in plan:
            inspection = service.component_trace("audit-1", component)["inspections"][-1]
            service.install_component(
                "intake-1", "wo-build-c", component, position,
                inspection["inspection_id"], repair_id, f"inst-{component}",
            )
        service.complete_work_order("intake-1", "wo-build-c", 1)

        # 技术确认与质量放行由不同职责人员完成，然后出场。
        service.confirm_assembly("tech-1", "dev-c", 1, "结构与绝缘确认")
        service.release_assembly("qa-1", "dev-c", 2, "放行检验合格")
        service.ship_assembly("intake-1", "dev-c", 3, "梯次利用示范站")

        origins = service.assembly_origins("audit-1", "dev-c")
        trace_installed = service.component_trace("audit-1", "mod-a1")
        trace_stock = service.component_trace("audit-1", "mod-b2")
        trace_scrap = service.component_trace("audit-1", "mod-a3")
        audit = service.audit_chain("audit-1")
        schema = inspect_schema(connection)
    finally:
        connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 谱系结构检查失败")
    if not audit["valid"]:
        raise RuntimeError("审计哈希链校验失败")
    if set(origins["source_assemblies"]) != {"pack-a", "pack-b"}:
        raise RuntimeError("向上追溯未还原全部来源装配")
    if trace_installed["final_destination"]["type"] != "shipped":
        raise RuntimeError("向下追溯未定位最终去向")
    return {
        "status": "ok",
        "device": origins["assembly_id"],
        "device_state": origins["state"],
        "source_assemblies": origins["source_assemblies"],
        "installed_components": len(origins["members"]),
        "mod_a1_destination": trace_installed["final_destination"]["assembly_id"],
        "mod_b2_location": trace_stock["current_location"],
        "mod_a3_destination": trace_scrap["final_destination"]["type"],
        "audit_events": audit["events"],
        "schema": schema,
        "workspace": workspace.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行维修与翻新谱系的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
