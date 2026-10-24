"""python3 -m unittest discover -s code/hark -p "test_*.py"  (stdlib only)"""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ingest import load_activity  # noqa: E402
from multimodal_facts import build  # noqa: E402


class MultimodalFactsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        links = {a["resource_id"] for a in load_activity() if a["event_type"] == "LINK_RESOURCE_IDENTITY"}
        cls.facts, cls.owners, cls.retention, cls.source = build(links)

    def test_every_fact_is_from_extraction(self):
        self.assertTrue(self.facts)
        self.assertEqual(set(self.source.values()), {"extracted"})

    def test_audit_record_003_retained(self):
        fids = sorted(f[0] for f in self.facts["audit_record_003"])
        self.assertEqual(fids, ["evidence_37", "evidence_55"])
        ret = [r for r in self.retention if r["resource_id"] == "audit_record_003"]
        self.assertTrue(ret and all(r["user_id"] == "user_03" for r in ret))
        self.assertEqual(max(r["retain_until"] for r in ret), datetime(2027, 8, 25, 4, 3))

    def test_legal_register_users(self):
        users = sorted(r["user_id"] for r in self.retention if r["evidence_id"] == "evidence_27")
        self.assertEqual(users, ["user_08", "user_12", "user_17", "user_21", "user_24", "user_29"])
        self.assertTrue(all(r["retain_until"].year == 2033 for r in self.retention if r["evidence_id"] == "evidence_27"))
        self.assertNotIn("disputed_orders_tax_register_2026-05", self.facts)

    def test_document_owner_only_without_link(self):
        self.assertEqual(set(self.owners), {"support_001"})
        self.assertEqual(self.owners["support_001"], ("evidence_58", {"priya.raman@gmail.com"}))


if __name__ == "__main__":
    unittest.main()
