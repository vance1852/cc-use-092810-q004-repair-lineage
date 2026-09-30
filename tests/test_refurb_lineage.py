from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from refurb_lineage.acceptance import run as acceptance_run
from refurb_lineage.api import JsonApplication
from refurb_lineage.clock import FrozenClock
from refurb_lineage.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from refurb_lineage.service import LineageService


ROOT = Path(__file__).resolve().parents[1]


class LineageServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc))
        self.service = LineageService(self.connection, self.clock)
        for user_id, role in (
            ("intake", "intake"),
            ("tech", "technician"),
            ("qa", "quality"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self._sequence = 0

    def tearDown(self) -> None:
        self.connection.close()

    def _key(self, prefix: str) -> str:
        self._sequence += 1
        return f"{prefix}-{self._sequence}"

    def _register_pack(self, assembly_id: str, mods: tuple[str, ...]) -> None:
        members = [
            {"component_id": component, "kind": "module", "model_name": "LFP-280", "position": f"S{index}"}
            for index, component in enumerate(mods, start=1)
        ]
        self.service.register_assembly("intake", assembly_id, f"LABEL-{assembly_id}", "pack", "external", members)

    def _freeze(self, work_order_id: str, assembly_id: str) -> None:
        self.service.create_work_order("intake", work_order_id, "teardown", assembly_id)
        self.service.freeze_work_order(
            "intake", work_order_id, {"fault_codes": ["SOH-LOW"], "report_sha256": "c" * 64}, 1
        )

    def _inspect(self, component_id: str, result: str = "pass") -> dict:
        return self.service.record_inspection(
            "tech", component_id, result, {"capacity_ah": 260.0}, self._key("insp"), "复测"
        )

    def _latest_inspection_id(self, component_id: str) -> int:
        trace = self.service.component_trace("auditor", component_id)
        return trace["inspections"][-1]["inspection_id"]

    def _scenario_to_stock(self) -> dict[str, int]:
        """两个退役包完成拆解：mod-1/2/5/7 复用，mod-3/8 报废，mod-4 维修完工，mod-6 隔离。"""

        self._register_pack("pack-a", ("mod-1", "mod-2", "mod-3", "mod-4"))
        self._register_pack("pack-b", ("mod-5", "mod-6", "mod-7", "mod-8"))
        self._freeze("wo-a", "pack-a")
        self._freeze("wo-b", "pack-b")
        for component in ("mod-1", "mod-2", "mod-4", "mod-5", "mod-6", "mod-7"):
            self._inspect(component, "pass")
        self._inspect("mod-3", "fail")
        self._inspect("mod-8", "fail")
        for work_order, component, disposition in (
            ("wo-a", "mod-1", "reuse"), ("wo-a", "mod-2", "reuse"), ("wo-a", "mod-3", "scrap"),
            ("wo-a", "mod-4", "repair"), ("wo-b", "mod-5", "reuse"), ("wo-b", "mod-6", "quarantine"),
            ("wo-b", "mod-7", "reuse"), ("wo-b", "mod-8", "scrap"),
        ):
            self.service.record_disassembly("intake", work_order, component, disposition, "处置", self._key("dis"))
        self.service.complete_work_order("intake", "wo-a", 2)
        self.service.complete_work_order("intake", "wo-b", 2)
        repair = self.service.open_repair("tech", "mod-4", "更换采样线束")
        recheck = self._inspect("mod-4", "pass")
        self.service.close_repair("tech", repair["repair_id"], "completed", "复检合格", recheck["inspection_id"])
        return {"repair_id": repair["repair_id"]}

    def _build_device(self) -> dict[str, int]:
        """组包 dev-c：mod-1/2/5 + 维修件 mod-4，工单完工但尚未确认。"""

        context = self._scenario_to_stock()
        self.service.register_assembly("intake", "dev-c", "ECHELON-1", "device", "refurb")
        self.service.create_work_order("intake", "wo-c", "rebuild", "dev-c")
        for component, position in (("mod-1", "M1"), ("mod-2", "M2"), ("mod-5", "M3")):
            self.service.install_component(
                "intake", "wo-c", component, position,
                self._latest_inspection_id(component), None, self._key("inst"),
            )
        self.service.install_component(
            "intake", "wo-c", "mod-4", "M4",
            self._latest_inspection_id("mod-4"), context["repair_id"], self._key("inst"),
        )
        self.service.complete_work_order("intake", "wo-c", 1)
        return context

    def _ship_device(self) -> None:
        self._build_device()
        self.service.confirm_assembly("tech", "dev-c", 1)
        self.service.release_assembly("qa", "dev-c", 2)
        self.service.ship_assembly("intake", "dev-c", 3, "梯次利用示范站")

    # ------------------------------------------------------------------
    # 工单冻结与拆解处置
    # ------------------------------------------------------------------

    def test_disassembly_requires_freeze_and_evidence(self) -> None:
        self._register_pack("pack-a", ("mod-1",))
        self.service.create_work_order("intake", "wo-a", "teardown", "pack-a")
        with self.assertRaises(InvalidState):
            self.service.record_disassembly("intake", "wo-a", "mod-1", "reuse", "合格", self._key("dis"))
        with self.assertRaises(ValidationFailed):
            self.service.freeze_work_order("intake", "wo-a", {}, 1)
        frozen = self.service.freeze_work_order(
            "intake", "wo-a", {"fault_codes": ["E12"], "report_sha256": "a" * 64}, 1
        )
        self.assertEqual(frozen["state"], "frozen")
        self.assertEqual(frozen["fault_evidence"]["fault_codes"], ["E12"])
        self.assertEqual([item["component_id"] for item in frozen["intake_items"]], ["mod-1"])

    def test_disassembly_rejects_component_outside_frozen_configuration(self) -> None:
        self._register_pack("pack-a", ("mod-1",))
        self._freeze("wo-a", "pack-a")
        self.service.register_component("intake", "mod-x", "module", "备件")
        with self.assertRaises(NotFound):
            self.service.record_disassembly("intake", "wo-a", "mod-x", "reuse", "合格", self._key("dis"))

    def test_reuse_disposition_requires_latest_passing_inspection(self) -> None:
        self._register_pack("pack-a", ("mod-1",))
        self._freeze("wo-a", "pack-a")
        with self.assertRaises(ValidationFailed):
            self.service.record_disassembly("intake", "wo-a", "mod-1", "reuse", "无检测", self._key("dis"))
        self._inspect("mod-1", "fail")
        with self.assertRaises(ValidationFailed):
            self.service.record_disassembly("intake", "wo-a", "mod-1", "reuse", "检测不合格", self._key("dis"))
        self._inspect("mod-1", "pass")
        result = self.service.record_disassembly("intake", "wo-a", "mod-1", "reuse", "复测合格", self._key("dis"))
        self.assertEqual(result["disposition"], "reuse")

    def test_disassembly_routes_components_to_distinct_states(self) -> None:
        self._register_pack("pack-a", ("mod-1", "mod-2", "mod-3", "mod-4"))
        self._freeze("wo-a", "pack-a")
        self._inspect("mod-1", "pass")
        self.service.record_disassembly("intake", "wo-a", "mod-1", "reuse", "合格", self._key("dis"))
        self.service.record_disassembly("intake", "wo-a", "mod-2", "repair", "待修", self._key("dis"))
        self.service.record_disassembly("intake", "wo-a", "mod-3", "scrap", "衰减", self._key("dis"))
        self.service.record_disassembly("intake", "wo-a", "mod-4", "quarantine", "待判定", self._key("dis"))
        states = {
            component: self.service.component_trace("auditor", component)["component"]["state"]
            for component in ("mod-1", "mod-2", "mod-3", "mod-4")
        }
        self.assertEqual(
            states, {"mod-1": "reuse", "mod-2": "repair", "mod-3": "scrap", "mod-4": "quarantine"}
        )
        with self.assertRaises(Conflict):
            self.service.record_disassembly("intake", "wo-a", "mod-1", "scrap", "重复", self._key("dis"))

    def test_complete_teardown_dismantles_source(self) -> None:
        self._register_pack("pack-a", ("mod-1",))
        self._freeze("wo-a", "pack-a")
        self._inspect("mod-1", "pass")
        self.service.record_disassembly("intake", "wo-a", "mod-1", "reuse", "合格", self._key("dis"))
        with self.assertRaises(InvalidState):
            self.service.complete_work_order("intake", "wo-a", 99)
        completed = self.service.complete_work_order("intake", "wo-a", 2)
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(self.service.get_assembly("pack-a")["state"], "dismantled")

    def test_partial_failure_keeps_components_accounted(self) -> None:
        self._register_pack("pack-a", ("mod-1", "mod-2"))
        self._freeze("wo-a", "pack-a")
        self._inspect("mod-1", "pass")
        self.service.record_disassembly("intake", "wo-a", "mod-1", "reuse", "合格", self._key("dis"))
        failed = self.service.fail_work_order("intake", "wo-a", "拆解设备故障停工")
        self.assertEqual(failed["state"], "failed")
        location = self.service.component_trace("auditor", "mod-2")["current_location"]
        self.assertEqual(location, {"type": "assembly", "assembly_id": "pack-a",
                                    "label": "LABEL-pack-a", "position": "S2", "assembly_state": "open"})
        # 失败后可重新开工单接续拆解，组件不会失联。
        self._freeze("wo-a2", "pack-a")
        self._inspect("mod-2", "pass")
        self.service.record_disassembly("intake", "wo-a2", "mod-2", "reuse", "合格", self._key("dis"))
        completed = self.service.complete_work_order("intake", "wo-a2", 2)
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(self.service.get_assembly("pack-a")["state"], "dismantled")

    def test_cancel_teardown_preserves_component_states(self) -> None:
        self._register_pack("pack-a", ("mod-1", "mod-2"))
        self._freeze("wo-a", "pack-a")
        self._inspect("mod-1", "pass")
        self.service.record_disassembly("intake", "wo-a", "mod-1", "repair", "待修", self._key("dis"))
        cancelled = self.service.cancel_work_order("intake", "wo-a", "计划变更")
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(self.service.component_trace("auditor", "mod-1")["component"]["state"], "repair")
        self.assertEqual(
            self.service.component_trace("auditor", "mod-2")["current_location"]["assembly_id"], "pack-a"
        )
        with self.assertRaises(InvalidState):
            self.service.record_disassembly("intake", "wo-a", "mod-2", "scrap", "已撤销", self._key("dis"))

    # ------------------------------------------------------------------
    # 检测与维修
    # ------------------------------------------------------------------

    def test_failed_inspection_quarantines_stock_component(self) -> None:
        self.service.register_component("intake", "mod-1", "module", "备件")
        result = self._inspect("mod-1", "fail")
        self.assertEqual(result["component_state"], "quarantine")
        self.assertEqual(self.service.component_trace("auditor", "mod-1")["component"]["state"], "quarantine")

    def test_repair_close_requires_post_repair_passing_inspection(self) -> None:
        self._register_pack("pack-a", ("mod-1",))
        self._freeze("wo-a", "pack-a")
        self._inspect("mod-1", "pass")
        self.service.record_disassembly("intake", "wo-a", "mod-1", "repair", "待修", self._key("dis"))
        stale_inspection = self._latest_inspection_id("mod-1")
        self.clock.advance(hours=1)
        repair = self.service.open_repair("tech", "mod-1", "更换线束")
        with self.assertRaises(Conflict):
            self.service.open_repair("tech", "mod-1", "重复开立")
        with self.assertRaises(ValidationFailed):
            self.service.close_repair("tech", repair["repair_id"], "completed", "无检测")
        with self.assertRaises(ValidationFailed):
            self.service.close_repair("tech", repair["repair_id"], "completed", "维修前检测", stale_inspection)
        recheck = self._inspect("mod-1", "pass")
        closed = self.service.close_repair(
            "tech", repair["repair_id"], "completed", "复检合格", recheck["inspection_id"]
        )
        self.assertEqual(closed["component_state"], "reuse")

    def test_repair_abandon_keeps_component_in_repair(self) -> None:
        self._register_pack("pack-a", ("mod-1",))
        self._freeze("wo-a", "pack-a")
        self.service.record_disassembly("intake", "wo-a", "mod-1", "repair", "待修", self._key("dis"))
        repair = self.service.open_repair("tech", "mod-1", "尝试修复")
        closed = self.service.close_repair("tech", repair["repair_id"], "abandoned", "缺少备件")
        self.assertEqual(closed["component_state"], "repair")
        follow_up = self.service.open_repair("tech", "mod-1", "备件到货再修")
        self.assertEqual(follow_up["version"], 2)

    def test_redisposition_requires_quality_and_respects_open_repair(self) -> None:
        self._register_pack("pack-a", ("mod-1",))
        self._freeze("wo-a", "pack-a")
        self.service.record_disassembly("intake", "wo-a", "mod-1", "quarantine", "待判定", self._key("dis"))
        with self.assertRaises(Forbidden):
            self.service.redisposition_component("intake", "mod-1", "scrap", "越权")
        moved = self.service.redisposition_component("qa", "mod-1", "repair", "转维修")
        self.assertEqual(moved["state"], "repair")
        self.service.open_repair("tech", "mod-1", "检查中")
        with self.assertRaises(InvalidState):
            self.service.redisposition_component("qa", "mod-1", "scrap", "维修中不能判定")

    def test_scrap_is_terminal(self) -> None:
        self._register_pack("pack-a", ("mod-1",))
        self._freeze("wo-a", "pack-a")
        self.service.record_disassembly("intake", "wo-a", "mod-1", "scrap", "衰减", self._key("dis"))
        with self.assertRaises(InvalidState):
            self._inspect("mod-1", "pass")
        with self.assertRaises(InvalidState):
            self.service.redisposition_component("qa", "mod-1", "reuse", "翻盘")
        with self.assertRaises(InvalidState):
            self.service.open_repair("tech", "mod-1", "复活")

    # ------------------------------------------------------------------
    # 组包、确认放行与出场
    # ------------------------------------------------------------------

    def test_install_requires_reuse_state_and_open_work_order(self) -> None:
        self._scenario_to_stock()
        self.service.register_assembly("intake", "dev-c", "ECHELON-1", "device", "refurb")
        self.service.create_work_order("intake", "wo-c", "rebuild", "dev-c")
        with self.assertRaises(InvalidState):
            self.service.install_component(
                "intake", "wo-c", "mod-3", "M1", self._latest_inspection_id("mod-3"), None, self._key("inst")
            )
        self.service.cancel_work_order("intake", "wo-c", "计划调整")
        with self.assertRaises(InvalidState):
            self.service.install_component(
                "intake", "wo-c", "mod-1", "M1", self._latest_inspection_id("mod-1"), None, self._key("inst")
            )

    def test_install_pins_latest_passing_inspection(self) -> None:
        self._scenario_to_stock()
        self.service.register_assembly("intake", "dev-c", "ECHELON-1", "device", "refurb")
        self.service.create_work_order("intake", "wo-c", "rebuild", "dev-c")
        stale = self.service.component_trace("auditor", "mod-1")["inspections"][-1]["inspection_id"]
        self._inspect("mod-1", "pass")
        with self.assertRaises(ValidationFailed):
            self.service.install_component("intake", "wo-c", "mod-1", "M1", stale, None, self._key("inst"))
        latest = self._latest_inspection_id("mod-1")
        installed = self.service.install_component(
            "intake", "wo-c", "mod-1", "M1", latest, None, self._key("inst")
        )
        self.assertEqual(installed["inspection_id"], latest)

    def test_repaired_component_install_requires_repair_reference(self) -> None:
        context = self._scenario_to_stock()
        self.service.register_assembly("intake", "dev-c", "ECHELON-1", "device", "refurb")
        self.service.create_work_order("intake", "wo-c", "rebuild", "dev-c")
        inspection_id = self._latest_inspection_id("mod-4")
        with self.assertRaises(ValidationFailed):
            self.service.install_component("intake", "wo-c", "mod-4", "M1", inspection_id, None, self._key("inst"))
        with self.assertRaises(ValidationFailed):
            self.service.install_component("intake", "wo-c", "mod-4", "M1", inspection_id, 999, self._key("inst"))
        installed = self.service.install_component(
            "intake", "wo-c", "mod-4", "M1", inspection_id, context["repair_id"], self._key("inst")
        )
        self.assertEqual(installed["repair_id"], context["repair_id"])

    def test_component_cannot_be_installed_in_two_assemblies(self) -> None:
        self._scenario_to_stock()
        self.service.register_assembly("intake", "dev-c", "ECHELON-1", "device", "refurb")
        self.service.create_work_order("intake", "wo-c", "rebuild", "dev-c")
        self.service.install_component(
            "intake", "wo-c", "mod-1", "M1", self._latest_inspection_id("mod-1"), None, self._key("inst")
        )
        self.service.register_assembly("intake", "dev-d", "ECHELON-2", "device", "refurb")
        self.service.create_work_order("intake", "wo-d", "rebuild", "dev-d")
        with self.assertRaises(InvalidState):
            self.service.install_component(
                "intake", "wo-d", "mod-1", "M1", self._latest_inspection_id("mod-1"), None, self._key("inst")
            )
        # 数据库部分唯一索引兜底：绕过服务直接写入也会被拒绝。
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO assembly_members(assembly_id,component_id,position,installed_by,installed_at) "
                "VALUES('dev-d','mod-1','M9','intake','2026-09-28T09:00:00Z')"
            )
        self.connection.rollback()

    def test_swap_is_atomic_and_returns_old_component(self) -> None:
        self._scenario_to_stock()
        self.service.register_assembly("intake", "dev-c", "ECHELON-1", "device", "refurb")
        self.service.create_work_order("intake", "wo-c", "rebuild", "dev-c")
        self.service.install_component(
            "intake", "wo-c", "mod-1", "M1", self._latest_inspection_id("mod-1"), None, self._key("inst")
        )
        swapped = self.service.swap_component(
            "intake", "wo-c", "M1", "mod-7", self._latest_inspection_id("mod-7"), None,
            "容量匹配调整", self._key("swap"),
        )
        self.assertEqual(swapped["removed_component_id"], "mod-1")
        self.assertEqual(self.service.component_trace("auditor", "mod-1")["component"]["state"], "reuse")
        members = self.service.get_assembly("dev-c")["members"]
        self.assertEqual([member["component_id"] for member in members], ["mod-7"])
        with self.assertRaises(NotFound):
            self.service.swap_component(
                "intake", "wo-c", "M9", "mod-1", self._latest_inspection_id("mod-1"), None,
                "空槽位", self._key("swap"),
            )

    def test_confirm_and_release_require_distinct_roles(self) -> None:
        self._build_device()
        with self.assertRaises(InvalidState):
            self.service.release_assembly("qa", "dev-c", 1)
        with self.assertRaises(Forbidden):
            self.service.confirm_assembly("intake", "dev-c", 1)
        with self.assertRaises(Forbidden):
            self.service.confirm_assembly("qa", "dev-c", 1)
        confirmed = self.service.confirm_assembly("tech", "dev-c", 1)
        self.assertEqual(confirmed["state"], "confirmed")
        with self.assertRaises(Forbidden):
            self.service.release_assembly("tech", "dev-c", 2)
        released = self.service.release_assembly("qa", "dev-c", 2)
        self.assertEqual(released["state"], "released")
        self.assertNotEqual(released["confirmed_by"], released["released_by"])

    def test_confirm_requires_completed_work_order(self) -> None:
        self._scenario_to_stock()
        self.service.register_assembly("intake", "dev-c", "ECHELON-1", "device", "refurb")
        self.service.create_work_order("intake", "wo-c", "rebuild", "dev-c")
        self.service.install_component(
            "intake", "wo-c", "mod-1", "M1", self._latest_inspection_id("mod-1"), None, self._key("inst")
        )
        with self.assertRaises(InvalidState):
            self.service.confirm_assembly("tech", "dev-c", 1)
        self.service.complete_work_order("intake", "wo-c", 1)
        confirmed = self.service.confirm_assembly("tech", "dev-c", 1)
        self.assertEqual(confirmed["state"], "confirmed")

    def test_shipped_assembly_configuration_is_immutable(self) -> None:
        self._ship_device()
        before = self.service.get_assembly("dev-c")
        self.assertEqual(before["state"], "shipped")
        with self.assertRaises(InvalidState):
            self.service.confirm_assembly("tech", "dev-c", 4)
        with self.assertRaises(InvalidState):
            self.service.rework_assembly("tech", "dev-c", "返工")
        with self.assertRaises(InvalidState):
            self.service.release_assembly("qa", "dev-c", 4)
        with self.assertRaises(InvalidState):
            self.service.void_assembly("intake", "dev-c", "撤销")
        with self.assertRaises(InvalidState):
            self.service.create_work_order("intake", "wo-c2", "rebuild", "dev-c")
        after = self.service.get_assembly("dev-c")
        self.assertEqual(
            [(m["component_id"], m["position"], m["inspection_id"], m["repair_id"]) for m in before["members"]],
            [(m["component_id"], m["position"], m["inspection_id"], m["repair_id"]) for m in after["members"]],
        )

    def test_returned_shipped_assembly_can_be_torn_down_with_history_kept(self) -> None:
        self._ship_device()
        shipped_members = self.connection.execute(
            "SELECT component_id,position,inspection_id,repair_id,installed_at FROM assembly_members "
            "WHERE assembly_id='dev-c' ORDER BY position"
        ).fetchall()
        self._freeze("wo-return", "dev-c")
        for component in ("mod-1", "mod-2", "mod-4", "mod-5"):
            self._inspect(component, "pass")
            self.service.record_disassembly("intake", "wo-return", component, "reuse", "返场复测", self._key("dis"))
        self.service.complete_work_order("intake", "wo-return", 2)
        self.assertEqual(self.service.get_assembly("dev-c")["state"], "dismantled")
        history = self.connection.execute(
            "SELECT component_id,position,inspection_id,repair_id,installed_at FROM assembly_members "
            "WHERE assembly_id='dev-c' ORDER BY position"
        ).fetchall()
        # 历史装机事实（谁、何时、引用哪个检测与维修版本）保持不变，只追加移除事实。
        self.assertEqual(
            [tuple(row) for row in shipped_members], [tuple(row) for row in history]
        )

    def test_void_assembly_returns_components_to_stock(self) -> None:
        self._scenario_to_stock()
        self.service.register_assembly("intake", "dev-c", "ECHELON-1", "device", "refurb")
        self.service.create_work_order("intake", "wo-c", "rebuild", "dev-c")
        self.service.install_component(
            "intake", "wo-c", "mod-1", "M1", self._latest_inspection_id("mod-1"), None, self._key("inst")
        )
        with self.assertRaises(InvalidState):
            self.service.void_assembly("intake", "dev-c", "工单进行中")
        self.service.cancel_work_order("intake", "wo-c", "取消组包")
        voided = self.service.void_assembly("intake", "dev-c", "方案取消")
        self.assertEqual(voided["state"], "void")
        self.assertEqual(self.service.component_trace("auditor", "mod-1")["component"]["state"], "reuse")

    def test_rework_reopens_confirmed_assembly(self) -> None:
        self._build_device()
        self.service.confirm_assembly("tech", "dev-c", 1)
        reworked = self.service.rework_assembly("tech", "dev-c", "抽检发现扭矩异常")
        self.assertEqual(reworked["state"], "open")
        self.service.create_work_order("intake", "wo-c2", "rebuild", "dev-c")
        self.service.swap_component(
            "intake", "wo-c2", "M3", "mod-7", self._latest_inspection_id("mod-7"), None,
            "更换异常模组", self._key("swap"),
        )
        self.service.complete_work_order("intake", "wo-c2", 1)
        self.service.confirm_assembly("tech", "dev-c", 3)
        self.service.release_assembly("qa", "dev-c", 4)
        shipped = self.service.ship_assembly("intake", "dev-c", 5, "梯次利用示范站")
        self.assertEqual(shipped["state"], "shipped")
        members = [m["component_id"] for m in shipped["members"]]
        self.assertEqual(members, ["mod-1", "mod-2", "mod-7", "mod-4"])
        self.assertEqual(self.service.component_trace("auditor", "mod-5")["component"]["state"], "reuse")

    # ------------------------------------------------------------------
    # 幂等、追溯与审计
    # ------------------------------------------------------------------

    def test_idempotent_replay_and_conflict(self) -> None:
        self._register_pack("pack-a", ("mod-1",))
        self._freeze("wo-a", "pack-a")
        first = self.service.record_inspection("tech", "mod-1", "pass", {"capacity_ah": 260.0}, "key-1")
        second = self.service.record_inspection("tech", "mod-1", "pass", {"capacity_ah": 260.0}, "key-1")
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.service.record_inspection("tech", "mod-1", "fail", {"capacity_ah": 100.0}, "key-1")
        disassembly = self.service.record_disassembly("intake", "wo-a", "mod-1", "reuse", "合格", "key-2")
        replay = self.service.record_disassembly("intake", "wo-a", "mod-1", "reuse", "合格", "key-2")
        self.assertEqual(disassembly, replay)
        with self.assertRaises(Conflict):
            self.service.record_disassembly("intake", "wo-a", "mod-1", "scrap", "不同请求", "key-2")
        count = self.connection.execute("SELECT count(*) FROM inspections").fetchone()[0]
        self.assertEqual(count, 1)

    def test_install_idempotent_replay(self) -> None:
        self._scenario_to_stock()
        self.service.register_assembly("intake", "dev-c", "ECHELON-1", "device", "refurb")
        self.service.create_work_order("intake", "wo-c", "rebuild", "dev-c")
        inspection_id = self._latest_inspection_id("mod-1")
        first = self.service.install_component("intake", "wo-c", "mod-1", "M1", inspection_id, None, "key-9")
        replay = self.service.install_component("intake", "wo-c", "mod-1", "M1", inspection_id, None, "key-9")
        self.assertEqual(first, replay)
        members = self.service.get_assembly("dev-c")["members"]
        self.assertEqual(len(members), 1)

    def test_origins_trace_recovers_all_sources(self) -> None:
        context = self._build_device()
        self.service.confirm_assembly("tech", "dev-c", 1)
        self.service.release_assembly("qa", "dev-c", 2)
        self.service.ship_assembly("intake", "dev-c", 3, "梯次利用示范站")
        origins = self.service.assembly_origins("auditor", "dev-c")
        self.assertEqual(set(origins["source_assemblies"]), {"pack-a", "pack-b"})
        self.assertEqual(len(origins["members"]), 4)
        by_component = {member["component_id"]: member for member in origins["members"]}
        self.assertEqual(by_component["mod-1"]["chain"][0]["work_order_id"], "wo-a")
        self.assertEqual(by_component["mod-5"]["chain"][0]["source_assembly_id"], "pack-b")
        self.assertEqual(by_component["mod-4"]["evidence"]["repair_id"], context["repair_id"])
        self.assertEqual(by_component["mod-4"]["evidence"]["inspection_result"], "pass")
        for member in origins["members"]:
            self.assertEqual(member["chain"][-1]["via"], "registered")

    def test_component_trace_reports_destination_pending_and_gaps(self) -> None:
        self._build_device()
        middle = self.service.component_trace("auditor", "mod-1")
        self.assertEqual(middle["final_destination"]["type"], "in_progress")
        self.assertEqual(
            {gap["code"] for gap in middle["evidence_gaps"]},
            {"assembly_not_confirmed", "assembly_not_released"},
        )
        self.service.confirm_assembly("tech", "dev-c", 1)
        confirmed = self.service.component_trace("auditor", "mod-1")
        self.assertEqual([gap["code"] for gap in confirmed["evidence_gaps"]], ["assembly_not_released"])
        self.service.release_assembly("qa", "dev-c", 2)
        self.service.ship_assembly("intake", "dev-c", 3, "梯次利用示范站")
        shipped = self.service.component_trace("auditor", "mod-1")
        self.assertEqual(shipped["evidence_gaps"], [])
        self.assertEqual(shipped["final_destination"]["type"], "shipped")
        self.assertEqual(shipped["final_destination"]["destination"], "梯次利用示范站")
        quarantined = self.service.component_trace("auditor", "mod-6")
        self.assertEqual([gap["code"] for gap in quarantined["evidence_gaps"]], ["quarantine_unresolved"])
        scrapped = self.service.component_trace("auditor", "mod-3")
        self.assertEqual(scrapped["final_destination"]["type"], "scrapped")

    def test_component_trace_lists_pending_repair_and_disposition(self) -> None:
        self._register_pack("pack-a", ("mod-1", "mod-2"))
        self._freeze("wo-a", "pack-a")
        pending = self.service.component_trace("auditor", "mod-1")
        self.assertEqual([action["type"] for action in pending["pending_actions"]], ["pending_disposition"])
        self.service.record_disassembly("intake", "wo-a", "mod-1", "repair", "待修", self._key("dis"))
        repair = self.service.open_repair("tech", "mod-1", "更换线束")
        trace = self.service.component_trace("auditor", "mod-1")
        self.assertEqual(
            [action["type"] for action in trace["pending_actions"]], ["repair_open"]
        )
        self.assertEqual(trace["pending_actions"][0]["repair_id"], repair["repair_id"])
        self.assertEqual([gap["code"] for gap in trace["evidence_gaps"]], ["repair_not_completed"])

    def test_audit_chain_is_valid_and_restricted(self) -> None:
        self._ship_device()
        chain = self.service.audit_chain("auditor")
        self.assertTrue(chain["valid"])
        self.assertGreater(chain["events"], 20)
        with self.assertRaises(Forbidden):
            self.service.audit_chain("intake")

    def test_acceptance_scenario(self) -> None:
        result = acceptance_run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source_assemblies"], ["pack-a", "pack-b"])
        self.assertEqual(result["installed_components"], 4)
        self.assertEqual(result["mod_a1_destination"], "dev-c")
        self.assertEqual(result["mod_a3_destination"], "scrapped")
        self.assertEqual(result["schema"]["missing_tables"], [])


class LineageApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(LineageService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, actor: str | None = "intake", key: str | None = None):
        headers = {}
        if actor is not None:
            headers["X-Actor-Id"] = actor
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.app.handle("POST", path, headers, json.dumps(payload).encode("utf-8"))

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_user_route_and_actor_requirement(self) -> None:
        created = self._post("/users", {"user_id": "u1", "display_name": "受理员", "role": "intake"}, actor=None)
        self.assertEqual(created.status, 201)
        missing_actor = self._post("/components", {"component_id": "m1", "kind": "module", "model_name": "M"}, actor=None)
        self.assertEqual(missing_actor.status, 422)
        self.assertEqual(missing_actor.body["error"]["code"], "validation_failed")

    def test_flow_over_http_and_idempotency_header(self) -> None:
        for user_id, role in (("op", "intake"), ("te", "technician"), ("qa", "quality")):
            self._post("/users", {"user_id": user_id, "display_name": user_id, "role": role}, actor=None)
        pack = self._post("/assemblies", {
            "assembly_id": "pack-a", "label": "PACK-1", "kind": "pack", "origin": "external",
            "members": [{"component_id": "mod-1", "kind": "module", "model_name": "M", "position": "S1"}],
        }, actor="op")
        self.assertEqual(pack.status, 201)
        self._post("/work_orders", {"work_order_id": "wo-a", "kind": "teardown", "assembly_id": "pack-a"}, actor="op")
        frozen = self._post("/work_orders/wo-a/freeze", {"fault_evidence": {"fault_codes": ["E1"]}, "expected_revision": 1}, actor="op")
        self.assertEqual(frozen.status, 200)
        no_key = self._post(
            "/components/mod-1/inspections", {"result": "pass", "metrics": {"capacity_ah": 260.0}}, actor="te"
        )
        self.assertEqual(no_key.status, 422)
        inspected = self._post(
            "/components/mod-1/inspections", {"result": "pass", "metrics": {"capacity_ah": 260.0}},
            actor="te", key="insp-1",
        )
        self.assertEqual(inspected.status, 201)
        disassembled = self._post(
            "/work_orders/wo-a/disassembly", {"component_id": "mod-1", "disposition": "reuse", "reason": "合格"},
            actor="op", key="dis-1",
        )
        self.assertEqual(disassembled.status, 200)
        trace = self.app.handle("GET", "/components/mod-1/trace", {"X-Actor-Id": "qa"})
        self.assertEqual(trace.status, 200)
        self.assertEqual(trace.body["current_location"], {"type": "stock", "state": "reuse"})
        unknown = self.app.handle("GET", "/components/mod-9/trace", {"X-Actor-Id": "qa"})
        self.assertEqual(unknown.status, 404)
        self.assertEqual(unknown.body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
