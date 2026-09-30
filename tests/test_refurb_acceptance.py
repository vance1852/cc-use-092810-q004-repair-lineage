from __future__ import annotations

import unittest
from pathlib import Path

from battery_renovation.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class RefurbAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["new_pack_state"], "delivered")
        self.assertEqual(result["source_count"], 3)
        self.assertEqual(result["source_origins"],
                         {"mod-A1": "pack-A", "mod-A2": "pack-A", "mod-B1": "pack-B"})
        self.assertEqual(result["a2_final_destination"],
                         {"kind": "delivered_pack", "pack_id": "pack-R"})
        self.assertEqual(result["a2_repair_orders"], 1)
        self.assertEqual(result["a2_repair_actions"], 1)
        self.assertEqual(result["b2_final_destination"], {"kind": "scrapped"})
        self.assertEqual(result["b2_pending_actions"], [])
        self.assertTrue(result["deliverable"])
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
