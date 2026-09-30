"""维修与翻新谱系的领域规则、事务边界与双向追溯测试。"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from battery_renovation.clock import FrozenClock
from battery_renovation.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from battery_renovation.service import RefurbService


class RefurbCase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc))
        self.service = RefurbService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"), ("tech", "technician"),
            ("eng", "engineer"), ("qual", "quality"), ("aud", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def seed_pack(self, pack: str, modules: tuple[str, ...]) -> None:
        self.service.register_item("plan", pack, "pack", "LFP", "原厂")
        for index, module in enumerate(modules, start=1):
            self.service.register_item("plan", module, "module", "LFP-M", "原厂")
            self.service.record_as_found_membership("plan", pack, module, f"M{index}")

    def inspect(self, module: str, verdict: str, soh: float = 0.9) -> dict:
        return self.service.record_inspection("tech", module, "SOH-v2", verdict, {"soh": soh})

    def evidence(self, evidence_id: str, pack: str, digest_char: str) -> None:
        self.service.record_fault_evidence(
            "tech", evidence_id, pack, "thermal", "故障记录", digest_char * 64)

    def disassemble(self, wo: str, pack: str, evidence_id: str,
                    dispositions: dict[str, str]) -> None:
        self.service.create_disassembly("plan", wo, pack)
        self.service.attach_evidence("tech", wo, evidence_id)
        frozen = self.service.freeze_disassembly("plan", wo, 2)
        self.assertEqual(frozen["state"], "frozen")
        self.service.start_disassembly("tech", wo, 3)
        for module, disposition in dispositions.items():
            self.service.execute_disposition("tech", wo, module, disposition)
        self.service.complete_disassembly("tech", wo)


class FreezeGateTests(RefurbCase):
    def test_freeze_requires_evidence_and_nonempty_config(self) -> None:
        self.seed_pack("pack-A", ("mod-A1",))
        self.service.create_disassembly("plan", "wo-A", "pack-A")
        with self.assertRaises(InvalidState):
            self.service.freeze_disassembly("plan", "wo-A", 1)
        self.evidence("ev-A", "pack-A", "a")
        self.service.attach_evidence("tech", "wo-A", "ev-A")
        with self.assertRaises(InvalidState):
            self.service.start_disassembly("tech", "wo-A", 2)
        self.service.freeze_disassembly("plan", "wo-A", 2)

    def test_disassembly_before_freeze_rejected(self) -> None:
        self.seed_pack("pack-A", ("mod-A1",))
        self.service.create_disassembly("plan", "wo-A", "pack-A")
        with self.assertRaises(InvalidState):
            self.service.execute_disposition("tech", "wo-A", "mod-A1", "reuse")

    def test_stale_revision_rejected(self) -> None:
        self.seed_pack("pack-A", ("mod-A1",))
        self.service.create_disassembly("plan", "wo-A", "pack-A")
        self.evidence("ev-A", "pack-A", "a")
        self.service.attach_evidence("tech", "wo-A", "ev-A")
        with self.assertRaises(InvalidState):
            self.service.freeze_disassembly("plan", "wo-A", 1)

    def test_evidence_must_belong_to_pack_or_mounted_component(self) -> None:
        self.seed_pack("pack-A", ("mod-A1",))
        self.seed_pack("pack-B", ("mod-B1",))
        self.service.create_disassembly("plan", "wo-A", "pack-A")
        self.evidence("ev-B", "pack-B", "b")
        with self.assertRaises(ValidationFailed):
            self.service.attach_evidence("tech", "wo-A", "ev-B")

    def test_role_separation(self) -> None:
        self.seed_pack("pack-A", ("mod-A1",))
        self.service.create_disassembly("plan", "wo-A", "pack-A")
        with self.assertRaises(Forbidden):
            self.service.attach_evidence("plan", "wo-A", "ev")
        with self.assertRaises(Forbidden):
            self.service.start_disassembly("plan", "wo-A", 1)


class DispositionTests(RefurbCase):
    def test_four_dispositions_land_distinct_states(self) -> None:
        self.seed_pack("pack-A", ("m1", "m2", "m3", "m4"))
        self.evidence("ev-A", "pack-A", "a")
        self.inspect("m1", "reuse")
        self.inspect("m2", "repair", 0.7)
        self.inspect("m3", "scrap", 0.4)
        self.inspect("m4", "quarantine", 0.5)
        self.service.create_disassembly("plan", "wo-A", "pack-A")
        self.service.attach_evidence("tech", "wo-A", "ev-A")
        self.service.freeze_disassembly("plan", "wo-A", 2)
        self.service.start_disassembly("tech", "wo-A", 3)
        self.service.execute_disposition("tech", "wo-A", "m1", "reuse")
        self.service.execute_disposition("tech", "wo-A", "m2", "repair")
        self.service.execute_disposition("tech", "wo-A", "m3", "scrap")
        self.service.execute_disposition("tech", "wo-A", "m4", "quarantine")
        self.service.complete_disassembly("tech", "wo-A")
        self.assertEqual(self.service.get_item("m1")["state"], "available")
        self.assertEqual(self.service.get_item("m2")["state"], "repair")
        self.assertEqual(self.service.get_item("m3")["state"], "scrapped")
        self.assertEqual(self.service.get_item("m4")["state"], "quarantined")
        self.assertEqual(self.service.get_item("pack-A")["state"], "retired")

    def test_reuse_requires_reuse_inspection(self) -> None:
        self.seed_pack("pack-A", ("m1",))
        self.evidence("ev-A", "pack-A", "a")
        self.inspect("m1", "repair", 0.7)
        self.service.create_disassembly("plan", "wo-A", "pack-A")
        self.service.attach_evidence("tech", "wo-A", "ev-A")
        self.service.freeze_disassembly("plan", "wo-A", 2)
        self.service.start_disassembly("tech", "wo-A", 3)
        with self.assertRaises(InvalidState):
            self.service.execute_disposition("tech", "wo-A", "m1", "reuse")
        # 失败事务回滚：组件仍在原装配、未被处置
        self.assertEqual(self.service.get_item("m1")["state"], "in_service")
        active = self.connection.execute(
            "SELECT count(*) FROM assembly_memberships WHERE child_item_id='m1' AND state='installed'"
        ).fetchone()[0]
        self.assertEqual(active, 1)

    def test_cannot_disposition_same_component_twice(self) -> None:
        self.seed_pack("pack-A", ("m1",))
        self.evidence("ev-A", "pack-A", "a")
        self.inspect("m1", "reuse")
        self.service.create_disassembly("plan", "wo-A", "pack-A")
        self.service.attach_evidence("tech", "wo-A", "ev-A")
        self.service.freeze_disassembly("plan", "wo-A", 2)
        self.service.start_disassembly("tech", "wo-A", 3)
        self.service.execute_disposition("tech", "wo-A", "m1", "reuse")
        with self.assertRaises(InvalidState):
            self.service.execute_disposition("tech", "wo-A", "m1", "scrap")

    def test_complete_blocked_until_every_component_decided(self) -> None:
        self.seed_pack("pack-A", ("m1", "m2"))
        self.evidence("ev-A", "pack-A", "a")
        for module in ("m1", "m2"):
            self.inspect(module, "reuse")
        self.service.create_disassembly("plan", "wo-A", "pack-A")
        self.service.attach_evidence("tech", "wo-A", "ev-A")
        self.service.freeze_disassembly("plan", "wo-A", 2)
        self.service.start_disassembly("tech", "wo-A", 3)
        self.service.execute_disposition("tech", "wo-A", "m1", "reuse")
        with self.assertRaises(InvalidState):
            self.service.complete_disassembly("tech", "wo-A")
        self.assertEqual(self.service.get_item("pack-A")["state"], "in_service")

    def test_partial_failure_keeps_everything_accountable(self) -> None:
        self.seed_pack("pack-A", ("m1", "m2"))
        self.evidence("ev-A", "pack-A", "a")
        for module in ("m1", "m2"):
            self.inspect(module, "reuse")
        self.service.create_disassembly("plan", "wo-A", "pack-A")
        self.service.attach_evidence("tech", "wo-A", "ev-A")
        self.service.freeze_disassembly("plan", "wo-A", 2)
        self.service.start_disassembly("tech", "wo-A", 3)
        self.service.execute_disposition("tech", "wo-A", "m1", "reuse")
        self.service.fail_disassembly("tech", "wo-A", "工装故障")
        # 已拆组件在复用库，未拆组件仍挂在原包，无失联、无一物多装
        self.assertEqual(self.service.get_item("m1")["state"], "available")
        self.assertEqual(self.service.get_item("m2")["state"], "in_service")
        active = self.connection.execute(
            "SELECT count(*) FROM assembly_memberships WHERE child_item_id='m2' AND state='installed'"
        ).fetchone()[0]
        self.assertEqual(active, 1)
        # 失败单不能再处置，需开新单续拆
        with self.assertRaises(InvalidState):
            self.service.execute_disposition("tech", "wo-A", "m2", "reuse")
        self.service.create_disassembly("plan", "wo-A2", "pack-A")
        self.service.attach_evidence("tech", "wo-A2", "ev-A")
        self.service.freeze_disassembly("plan", "wo-A2", 2)
        self.service.start_disassembly("tech", "wo-A2", 3)
        self.service.execute_disposition("tech", "wo-A2", "m2", "reuse")
        self.service.complete_disassembly("tech", "wo-A2")
        self.assertEqual(self.service.get_item("pack-A")["state"], "retired")

    def test_pre_start_cancel_leaves_config_intact(self) -> None:
        self.seed_pack("pack-A", ("m1",))
        self.evidence("ev-A", "pack-A", "a")
        self.service.create_disassembly("plan", "wo-A", "pack-A")
        self.service.attach_evidence("tech", "wo-A", "ev-A")
        self.service.freeze_disassembly("plan", "wo-A", 2)
        self.service.cancel_disassembly("plan", "wo-A", "取消")
        self.assertEqual(self.service.get_item("pack-A")["state"], "in_service")
        self.assertEqual(self.service.get_item("m1")["state"], "in_service")


    def test_disposition_must_match_latest_inspection_verdict(self) -> None:
        self.seed_pack("pack-A", ("m1",))
        self.evidence("ev-A", "pack-A", "a")
        self.inspect("m1", "reuse", 0.9)
        self.inspect("m1", "scrap", 0.3)
        self.service.create_disassembly("plan", "wo-A", "pack-A")
        self.service.attach_evidence("tech", "wo-A", "ev-A")
        self.service.freeze_disassembly("plan", "wo-A", 2)
        self.service.start_disassembly("tech", "wo-A", 3)
        with self.assertRaises(InvalidState):
            self.service.execute_disposition("tech", "wo-A", "m1", "reuse")
        self.service.execute_disposition("tech", "wo-A", "m1", "scrap")
        self.assertEqual(self.service.get_item("m1")["state"], "scrapped")

    def test_retired_pack_lineage_reconstructed_from_frozen_snapshot(self) -> None:
        self.seed_pack("pack-A", ("m1",))
        self.evidence("ev-A", "pack-A", "a")
        self.inspect("m1", "reuse", 0.9)
        self.disassemble("wo-A", "pack-A", "ev-A", {"m1": "reuse"})
        up = self.service.lineage_up("aud", "pack-A")
        self.assertEqual([n["component_id"] for n in up["sources"]], ["m1"])
        origin = self.service.lineage_down("aud", "m1")
        self.assertEqual(origin["mount_history"][0]["state"], "removed")


class MembershipIntegrityTests(RefurbCase):
    def test_child_can_only_hold_one_active_membership(self) -> None:
        self.seed_pack("pack-A", ("m1",))
        self.service.register_item("plan", "pack-B", "pack", "LFP", "原厂")
        with self.assertRaises(Conflict):
            self.service.record_as_found_membership("plan", "pack-B", "m1", "M1")

    def test_position_has_single_occupant(self) -> None:
        self.seed_pack("pack-A", ("m1", "m2"))
        with self.assertRaises(Conflict):
            self.service.record_as_found_membership("plan", "pack-A", "m2", "M1")


class RepairTests(RefurbCase):
    def prepare_repair_component(self) -> None:
        self.seed_pack("pack-A", ("m1",))
        self.evidence("ev-A", "pack-A", "a")
        self.inspect("m1", "repair", 0.7)
        self.disassemble("wo-A", "pack-A", "ev-A", {"m1": "repair"})

    def test_repair_flow_returns_component_to_pool(self) -> None:
        self.prepare_repair_component()
        self.service.open_repair("tech", "rp-1", "m1")
        with self.assertRaises(InvalidState):
            self.service.complete_repair("tech", "rp-1", 1)
        action = self.service.add_repair_action("tech", "rp-1", "balance-wire", {"n": 2})
        with self.assertRaises(InvalidState):
            bad = self.inspect("m1", "repair", 0.71)
            self.service.complete_repair("tech", "rp-1", bad["inspection_id"])
        recheck = self.inspect("m1", "reuse", 0.86)
        self.service.complete_repair("tech", "rp-1", recheck["inspection_id"])
        self.assertEqual(self.service.get_item("m1")["state"], "available")
        self.assertEqual(len(action["content_sha256"]), 64)

    def test_open_repair_requires_repair_state_and_is_unique(self) -> None:
        self.prepare_repair_component()
        self.service.open_repair("tech", "rp-1", "m1")
        with self.assertRaises(Conflict):
            self.service.open_repair("tech", "rp-2", "m1")

    def test_repair_cancel_moves_component_to_quarantine_not_limbo(self) -> None:
        self.prepare_repair_component()
        self.service.open_repair("tech", "rp-1", "m1")
        self.service.add_repair_action("tech", "rp-1", "x", {"k": 1})
        self.service.cancel_repair("tech", "rp-1", "无备件")
        self.assertEqual(self.service.get_item("m1")["state"], "quarantined")
        down = self.service.lineage_down("aud", "m1")
        self.assertIn({"type": "quarantine_decision_required"}, down["pending_actions"])

    def test_resolve_quarantine_to_scrap(self) -> None:
        self.seed_pack("pack-A", ("m1",))
        self.evidence("ev-A", "pack-A", "a")
        self.inspect("m1", "quarantine", 0.5)
        self.disassemble("wo-A", "pack-A", "ev-A", {"m1": "quarantine"})
        verdict = self.inspect("m1", "scrap", 0.45)
        self.service.resolve_component("tech", "m1", verdict["inspection_id"])
        self.assertEqual(self.service.get_item("m1")["state"], "scrapped")

    def test_resolve_repair_verdict_reopens_repair_path(self) -> None:
        self.seed_pack("pack-A", ("m1",))
        self.evidence("ev-A", "pack-A", "a")
        self.inspect("m1", "quarantine", 0.5)
        self.disassemble("wo-A", "pack-A", "ev-A", {"m1": "quarantine"})
        verdict = self.inspect("m1", "repair", 0.68)
        self.service.resolve_component("tech", "m1", verdict["inspection_id"])
        self.assertEqual(self.service.get_item("m1")["state"], "repair")


class ReassemblyTests(RefurbCase):
    def prepare_components(self) -> tuple[dict, dict, int]:
        self.seed_pack("pack-A", ("a1", "a2"))
        self.seed_pack("pack-B", ("b1",))
        self.evidence("ev-A", "pack-A", "a")
        self.evidence("ev-B", "pack-B", "b")
        self.inspect("a1", "reuse", 0.9)
        self.inspect("a2", "repair", 0.7)
        b1 = self.inspect("b1", "reuse", 0.88)
        self.disassemble("wo-A", "pack-A", "ev-A", {"a1": "reuse", "a2": "repair"})
        self.disassemble("wo-B", "pack-B", "ev-B", {"b1": "reuse"})
        self.service.open_repair("tech", "rp-a2", "a2")
        action = self.service.add_repair_action("tech", "rp-a2", "fix", {"n": 1})
        recheck = self.inspect("a2", "reuse", 0.85)
        self.service.complete_repair("tech", "rp-a2", recheck["inspection_id"])
        return recheck, b1, action["action_id"]

    def build_pack(self, wo: str = "wo-R", pack: str = "pack-R") -> dict:
        recheck, b1, action_id = self.prepare_components()
        self.service.create_reassembly("plan", wo, pack, "梯次", "翻新中心")
        self.service.add_reassembly_line("tech", wo, "a1", "M1",
                                         self.inspect("a1", "reuse", 0.91)["inspection_id"])
        self.service.add_reassembly_line("tech", wo, "a2", "M2",
                                         recheck["inspection_id"], action_id)
        self.service.add_reassembly_line("tech", wo, "b1", "M3", b1["inspection_id"])
        return self.service.freeze_reassembly("tech", wo, 4)

    def test_line_must_reference_component_reuse_inspection(self) -> None:
        self.prepare_components()
        self.service.create_reassembly("plan", "wo-R", "pack-R", "梯次", "翻新中心")
        other = self.inspect("a1", "reuse", 0.92)
        with self.assertRaises(ValidationFailed):
            self.service.add_reassembly_line("tech", "wo-R", "a2", "M1", other["inspection_id"])

    def test_repaired_component_requires_repair_action_on_line(self) -> None:
        recheck, _b1, _action = self.prepare_components()
        self.service.create_reassembly("plan", "wo-R", "pack-R", "梯次", "翻新中心")
        with self.assertRaises(InvalidState):
            self.service.add_reassembly_line("tech", "wo-R", "a2", "M2", recheck["inspection_id"])

    def test_line_rejects_incomplete_repair_action(self) -> None:
        self.seed_pack("pack-A", ("a1",))
        self.evidence("ev-A", "pack-A", "a")
        self.inspect("a1", "repair", 0.7)
        self.disassemble("wo-A", "pack-A", "ev-A", {"a1": "repair"})
        self.service.open_repair("tech", "rp", "a1")
        action = self.service.add_repair_action("tech", "rp", "fix", {"n": 1})
        reuse = self.inspect("a1", "reuse", 0.85)
        self.service.create_reassembly("plan", "wo-R", "pack-R", "梯次", "翻新中心")
        with self.assertRaises(InvalidState):
            self.service.add_reassembly_line("tech", "wo-R", "a1", "M1",
                                             reuse["inspection_id"], action["action_id"])

    def test_dual_signoff_and_delivery(self) -> None:
        self.build_pack()
        self.service.start_reassembly("tech", "wo-R", 5)
        self.service.technical_confirm("eng", "wo-R", 6)
        with self.assertRaises(Forbidden):
            self.service.quality_release("eng", "wo-R", 7)
        with self.assertRaises(Forbidden):
            self.service.technical_confirm("qual", "wo-R", 7)
        self.service.quality_release("qual", "wo-R", 7)
        self.assertEqual(self.service.get_item("pack-R")["state"], "released")
        self.service.deliver_reassembly("qual", "wo-R")
        self.assertEqual(self.service.get_item("pack-R")["state"], "delivered")
        for module in ("a1", "a2", "b1"):
            self.assertEqual(self.service.get_item(module)["state"], "delivered")

    def test_superseded_reuse_inspection_blocks_delivery(self) -> None:
        self.build_pack()
        self.service.start_reassembly("tech", "wo-R", 5)
        # a1 已在新包中，却追加出结论更差的新版本检测
        self.service.record_inspection(
            "eng", "a1", "SOH-v3", "quarantine", {"soh": 0.4})
        blockers = self.service.delivery_blockers("aud", "wo-R")
        codes = {gap["code"] for gap in blockers["blockers"]}
        self.assertIn("inspection_superseded", codes)
        self.assertFalse(blockers["deliverable"])
        with self.assertRaises(InvalidState):
            self.service.technical_confirm("eng", "wo-R", 6)

    def test_quality_release_requires_technical_confirmation(self) -> None:
        self.build_pack()
        self.service.start_reassembly("tech", "wo-R", 5)
        with self.assertRaises(InvalidState):
            self.service.quality_release("qual", "wo-R", 6)

    def test_delivery_blockers_listed_before_signoff(self) -> None:
        self.build_pack()
        self.service.start_reassembly("tech", "wo-R", 5)
        blockers = self.service.delivery_blockers("aud", "wo-R")
        codes = {gap["code"] for gap in blockers["blockers"]}
        self.assertIn("missing_technical_confirmation", codes)
        self.assertIn("missing_quality_release", codes)
        self.assertFalse(blockers["deliverable"])

    def test_component_cannot_be_mounted_twice(self) -> None:
        recheck, b1, _action_id = self.prepare_components()
        a1_extra = self.inspect("a1", "reuse", 0.93)
        # 两份草稿清单可以同时引用同一复用库组件，但实际装配只能有一个赢家
        self.service.create_reassembly("plan", "wo-R", "pack-R", "梯次", "翻新中心")
        self.service.add_reassembly_line("tech", "wo-R", "a1", "M1", a1_extra["inspection_id"])
        self.service.create_reassembly("plan", "wo-S", "pack-S", "梯次", "翻新中心")
        self.service.add_reassembly_line("tech", "wo-S", "a1", "M1", a1_extra["inspection_id"])
        self.service.freeze_reassembly("tech", "wo-R", 2)
        self.service.freeze_reassembly("tech", "wo-S", 2)
        self.service.start_reassembly("tech", "wo-R", 3)
        with self.assertRaises((Conflict, InvalidState)):
            self.service.start_reassembly("tech", "wo-S", 3)
        # 失败回滚：pack-S 仍冻结，a1 仍只属于 pack-R
        self.assertEqual(self.service.get_work_order("wo-S")["state"], "frozen")
        active = self.connection.execute(
            "SELECT count(*) FROM assembly_memberships WHERE child_item_id='a1' AND state='installed'"
        ).fetchone()[0]
        self.assertEqual(active, 1)
        self.assertEqual(self.service.get_item("a1")["state"], "building")

    def test_replace_resets_signoffs_and_returns_old_component(self) -> None:
        self.build_pack()
        # 额外准备一个替换件 c1
        self.seed_pack("pack-C", ("c1",))
        self.evidence("ev-C", "pack-C", "c")
        self.inspect("c1", "reuse", 0.89)
        self.disassemble("wo-C", "pack-C", "ev-C", {"c1": "reuse"})
        c1 = self.inspect("c1", "reuse", 0.9)

        self.service.start_reassembly("tech", "wo-R", 5)
        self.service.technical_confirm("eng", "wo-R", 6)
        self.service.replace_reassembly_component(
            "tech", "wo-R", "M3", "c1", c1["inspection_id"], "B1 复检异常")
        self.assertEqual(self.service.get_item("b1")["state"], "available")
        self.assertEqual(self.service.get_item("c1")["state"], "building")
        # 双签被重置，旧 revision 已失效
        with self.assertRaises(InvalidState):
            self.service.quality_release("qual", "wo-R", 7)
        self.service.technical_confirm("eng", "wo-R", 8)
        self.service.quality_release("qual", "wo-R", 9)
        self.service.deliver_reassembly("qual", "wo-R")
        # 旧清单行保留为 superseded，谱系仍可还原换件历史
        rows = self.connection.execute(
            "SELECT component_id,state FROM reassembly_lines WHERE work_order_id='wo-R' ORDER BY line_id"
        ).fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows][2], ("b1", "superseded"))

    def test_cancel_after_start_returns_components_to_pool(self) -> None:
        self.build_pack()
        self.service.start_reassembly("tech", "wo-R", 5)
        self.service.cancel_reassembly("plan", "wo-R", "客户撤单")
        for module in ("a1", "a2", "b1"):
            self.assertEqual(self.service.get_item(module)["state"], "available")
        self.assertEqual(self.service.get_item("pack-R")["state"], "cancelled")
        active = self.connection.execute(
            "SELECT count(*) FROM assembly_memberships WHERE parent_item_id='pack-R' AND state='installed'"
        ).fetchone()[0]
        self.assertEqual(active, 0)

    def test_delivered_configuration_is_immutable(self) -> None:
        self.build_pack()
        self.service.start_reassembly("tech", "wo-R", 5)
        self.service.technical_confirm("eng", "wo-R", 6)
        self.service.quality_release("qual", "wo-R", 7)
        self.service.deliver_reassembly("qual", "wo-R")
        # 已出场工单不能撤销、换件
        with self.assertRaises(InvalidState):
            self.service.cancel_reassembly("plan", "wo-R", "x")
        with self.assertRaises(InvalidState):
            self.service.replace_reassembly_component(
                "tech", "wo-R", "M1", "a1", 1, "x")
        # 出场序列件终态，不能再登记检测
        with self.assertRaises(InvalidState):
            self.inspect("a1", "reuse", 0.96)
        # 出场组件不能进入任何新组包清单，历史配置不受影响
        self.service.create_reassembly("plan", "wo-T", "pack-T", "梯次", "翻新中心")
        prior_inspection = self.connection.execute(
            "SELECT inspection_id FROM inspections WHERE component_id='a1' ORDER BY version DESC LIMIT 1"
        ).fetchone()[0]
        with self.assertRaises(InvalidState):
            self.service.add_reassembly_line(
                "tech", "wo-T", "a1", "M1", prior_inspection)
        up = self.service.lineage_up("aud", "pack-R")
        self.assertEqual({n["component_id"] for n in up["sources"]}, {"a1", "a2", "b1"})


class LineageQueryTests(RefurbCase):
    def test_up_and_down_lineage(self) -> None:
        case = ReassemblyTests()
        case.setUp()
        try:
            case.build_pack()
            case.service.start_reassembly("tech", "wo-R", 5)
            case.service.technical_confirm("eng", "wo-R", 6)
            case.service.quality_release("qual", "wo-R", 7)
            case.service.deliver_reassembly("qual", "wo-R")

            up = case.service.lineage_up("aud", "pack-R")
            self.assertEqual({n["component_id"] for n in up["sources"]}, {"a1", "a2", "b1"})
            origins = {n["component_id"]: n["origin"]["from_pack_id"] for n in up["sources"]}
            self.assertEqual(origins, {"a1": "pack-A", "a2": "pack-A", "b1": "pack-B"})
            a2 = next(n for n in up["sources"] if n["component_id"] == "a2")
            self.assertEqual(a2["inspections"][-1]["verdict"], "reuse")
            self.assertEqual(len(a2["repairs"][0]["actions"]), 1)

            down = case.service.lineage_down("aud", "a2")
            self.assertEqual(down["final_destination"],
                             {"kind": "delivered_pack", "pack_id": "pack-R"})
            self.assertEqual(down["root_pack_id"], "pack-R")
            self.assertEqual(down["delivery_blockers"], [])
            self.assertEqual(down["pending_actions"], [])
            self.assertGreaterEqual(len(down["mount_history"]), 2)

            audit = case.service.audit_chain("aud")
            self.assertTrue(audit["valid"])
            self.assertGreater(audit["events"], 0)
        finally:
            case.tearDown()

    def test_down_for_scrapped_component(self) -> None:
        case = DispositionTests()
        case.setUp()
        try:
            case.test_four_dispositions_land_distinct_states()
            down = case.service.lineage_down("aud", "m3")
            self.assertEqual(down["final_destination"], {"kind": "scrapped"})
        finally:
            case.tearDown()

    def test_auditor_is_read_only(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.register_item("aud", "x", "pack", "m", "v")


if __name__ == "__main__":
    unittest.main()
