from __future__ import annotations

import json
import sqlite3
import unittest

from battery_renovation.api import JsonApplication
from battery_renovation.service import RefurbService


def body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def headers(actor: str) -> dict[str, str]:
    return {"X-Actor-Id": actor}


class RefurbApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(RefurbService(self.connection))
        for user_id, role in (
            ("plan", "planner"), ("tech", "technician"),
            ("eng", "engineer"), ("qual", "quality"), ("aud", "auditor"),
        ):
            self.app.handle("POST", "/users", body=body(
                {"user_id": user_id, "display_name": user_id, "role": role}))

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["service"], "battery-renovation")

    def test_missing_actor(self) -> None:
        response = self.app.handle("POST", "/items", body=body({
            "item_id": "p1", "item_kind": "pack", "model_name": "m", "vendor": "v"}))
        self.assertEqual(response.status, 422)

    def test_full_flow_via_http_and_lineage_routes(self) -> None:
        self.app.handle("POST", "/items", headers("plan"), body({"item_id": "pack-A",
            "item_kind": "pack", "model_name": "LFP", "vendor": "原厂"}))
        self.app.handle("POST", "/items", headers("plan"), body({"item_id": "m1",
            "item_kind": "module", "model_name": "LFP-M", "vendor": "原厂"}))
        self.app.handle("POST", "/items/pack-A/memberships", headers("plan"),
                        body({"child_item_id": "m1", "position": "M1"}))
        self.app.handle("POST", "/fault_evidences", headers("tech"), body({
            "evidence_id": "ev-A", "item_id": "pack-A", "evidence_kind": "thermal",
            "summary": "温差告警", "content_sha256": "a" * 64}))
        inspection = self.app.handle("POST", "/inspections", headers("tech"), body({
            "component_id": "m1", "protocol_id": "SOH-v2", "verdict": "reuse",
            "metrics": {"soh": 0.9}}))
        self.assertEqual(inspection.status, 201)
        self.app.handle("POST", "/work_orders/disassembly", headers("plan"),
                        body({"work_order_id": "wo-A", "pack_id": "pack-A"}))
        self.app.handle("POST", "/work_orders/wo-A/evidence", headers("tech"),
                        body({"evidence_id": "ev-A"}))
        self.app.handle("POST", "/work_orders/wo-A/freeze-disassembly", headers("plan"),
                        body({"expected_revision": 2}))
        self.app.handle("POST", "/work_orders/wo-A/start", headers("tech"),
                        body({"expected_revision": 3}))
        disposition = self.app.handle("POST", "/work_orders/wo-A/dispositions", headers("tech"), body({
            "component_id": "m1", "disposition": "reuse"}))
        self.assertEqual(disposition.body["item_state"], "available")
        self.app.handle("POST", "/work_orders/wo-A/complete-disassembly", headers("tech"))

        self.app.handle("POST", "/work_orders/reassembly", headers("plan"), body({
            "work_order_id": "wo-R", "new_pack_id": "pack-R",
            "model_name": "梯次", "vendor": "翻新中心"}))
        self.app.handle("POST", "/work_orders/wo-R/lines", headers("tech"), body({
            "component_id": "m1", "position": "M1",
            "inspection_id": inspection.body["inspection_id"]}))
        self.app.handle("POST", "/work_orders/wo-R/freeze-reassembly", headers("tech"),
                        body({"expected_revision": 2}))
        self.app.handle("POST", "/work_orders/wo-R/start-reassembly", headers("tech"),
                        body({"expected_revision": 3}))
        self.app.handle("POST", "/work_orders/wo-R/technical-confirm", headers("eng"),
                        body({"expected_revision": 4}))
        same_person = self.app.handle("POST", "/work_orders/wo-R/quality-release",
                                      headers("eng"), body({"expected_revision": 5}))
        self.assertEqual(same_person.status, 403)
        self.app.handle("POST", "/work_orders/wo-R/quality-release", headers("qual"),
                        body({"expected_revision": 5}))
        self.app.handle("POST", "/work_orders/wo-R/deliver", headers("qual"))

        up = self.app.handle("GET", "/items/pack-R/lineage/up", headers("aud"))
        self.assertEqual(up.status, 200)
        self.assertEqual(up.body["sources"][0]["origin"]["from_pack_id"], "pack-A")
        down = self.app.handle("GET", "/items/m1/lineage/down", headers("aud"))
        self.assertEqual(down.body["final_destination"],
                         {"kind": "delivered_pack", "pack_id": "pack-R"})
        blockers = self.app.handle("GET", "/work_orders/wo-R/blockers", headers("aud"))
        self.assertTrue(blockers.body["deliverable"])
        audit = self.app.handle("GET", "/audit/chain", headers("aud"))
        self.assertTrue(audit.body["valid"])

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope", headers("aud"))
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
