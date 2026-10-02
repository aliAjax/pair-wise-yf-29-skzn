import base64
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, CustodyStore


class CustodyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CustodyStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.case = self.store.create_case("custodian1", "CASE-2026-001", "跨境资金调查")
        self.store.add_member("custodian1", self.case["id"], "custodian2", "custodian")
        self.store.add_member("custodian1", self.case["id"], "analyst1", "analyst")
        self.store.add_member("custodian1", self.case["id"], "auditor1", "auditor")
        self.retention = (date.today() + timedelta(days=3650)).isoformat()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_custody_analysis_release_and_integrity_report(self):
        raw = b"bank statement original bytes"
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-001", "statement.csv",
            base64.b64encode(raw).decode(), self.retention, "custodian1",
        )
        opened = self.store.open_evidence("custodian1", item["id"], "A 区证物室", "两名人员在场开箱")
        self.assertEqual(opened["status"], "opened")
        child = self.store.derive(
            "analyst1", item["id"], "CSV 提取交易记录", "E-001-D1", "transactions.json",
            base64.b64encode(b'[{"amount": 100}]').decode(),
        )
        self.store.transfer("custodian2", item["id"], "custodian2", "法院证物库", "封存后移交")
        self.store.release("custodian2", item["id"], "检察机关", "按调取令释放原件")
        report = self.store.report("auditor1", self.case["id"])
        self.assertTrue(report["overall_integrity_valid"])
        self.assertEqual(report["evidence_count"], 2)
        original = next(x for x in report["evidence"] if x["id"] == item["id"])
        self.assertEqual(original["status"], "released")
        self.assertTrue(original["chain_valid"])
        self.assertEqual(child["parent_id"], item["id"])

    def test_permissions_and_legal_hold_block_release(self):
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-002", "raw.bin",
            base64.b64encode(b"evidence").decode(), self.retention,
        )
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_evidence("outsider", item["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("analyst1", item["id"], "外部机构")
        self.assertEqual(ctx.exception.status, 403)
        self.store.set_hold("auditor1", item["id"], True, "诉讼保全要求")
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("custodian1", item["id"], "外部机构")
        self.assertEqual(ctx.exception.code, "legal_hold_active")


class ReleaseBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.store = CustodyStore(self.db_path)
        self.store.seed()
        self.case = self.store.create_case("custodian1", "CASE-2026-101", "合同诉讼证据")
        self.store.add_member("custodian1", self.case["id"], "custodian2", "custodian")
        self.store.add_member("custodian1", self.case["id"], "analyst1", "analyst")
        self.store.add_member("custodian1", self.case["id"], "auditor1", "auditor")
        self.expired = date.today().isoformat()
        self.future = (date.today() + timedelta(days=365)).isoformat()

    def tearDown(self):
        self.tmp.cleanup()

    def _ingest(self, label, retention=None, custodian="custodian1"):
        return self.store.ingest_evidence(
            "custodian1", self.case["id"], label, f"{label}.bin",
            base64.b64encode(f"{label} bytes".encode()).decode(), retention or self.expired, custodian)

    def _release_events(self, evidence_id):
        with self.store.connect() as conn:
            return conn.execute(
                "SELECT * FROM custody_events WHERE evidence_id=? AND event_type='RELEASE'", (evidence_id,)).fetchall()

    def test_review_execute_and_report_reconciliation(self):
        e1 = self._ingest("E-1")  # 期限届满，可释放
        e2 = self._ingest("E-2")  # 仍在法律保留
        e3 = self._ingest("E-3")  # 复核后被移交
        e4 = self._ingest("E-4")  # 开箱并派生分析件
        self.store.set_hold("auditor1", e2["id"], True, "上诉期内继续保全")
        self.store.open_evidence("custodian1", e4["id"], "A 区证物室")
        child = self.store.derive("analyst1", e4["id"], "关键词检索提取", "E-4-D1", "hits.json",
                                  base64.b64encode(b"{}").decode())
        batch = self.store.create_release_batch("auditor1", self.case["id"], "案件当事人")
        by_ev = {i["evidence_id"]: i for i in batch["items"]}
        self.assertEqual(by_ev[e1["id"]]["review_status"], "pending")
        self.assertEqual(by_ev[e2["id"]]["review_status"], "ineligible")
        self.assertIn("法律保留", by_ev[e2["id"]]["ineligible_reason"])
        self.assertEqual(by_ev[child["id"]]["review_status"], "pending")
        # 复核后 E-3 被移交：保管人和保管链都变了
        self.store.transfer("custodian1", e3["id"], "custodian2", "法院证物库")
        result = self.store.execute_release_batch("custodian2", batch["id"])
        self.assertEqual(sorted(result["released"]), sorted([e1["id"], e4["id"], child["id"]]))
        self.assertEqual([v["evidence_id"] for v in result["voided"]], [e3["id"]])
        self.assertIn("保管人", result["voided"][0]["reason"])
        self.assertEqual(len(self._release_events(e3["id"])), 0)
        sheet = self.store.get_release_batch("auditor1", batch["id"])
        self.assertEqual(sheet["status"], "completed")
        self.assertEqual(sheet["counts"], {"pending": 0, "ineligible": 1, "released": 3, "voided": 1})
        report = self.store.report("auditor1", self.case["id"])
        self.assertTrue(report["release_reconciliation_valid"])
        rb = report["release_batches"][0]
        self.assertTrue(rb["reconciled"])
        self.assertTrue(all(i["matches_actual"] for i in rb["items"]))
        # 已写入的不能重复
        again = self.store.execute_release_batch("custodian1", batch["id"])
        self.assertEqual(again["released"], [])
        self.assertEqual(len(self._release_events(e1["id"])), 1)

    def test_hold_change_after_review_voids_item(self):
        e1 = self._ingest("E-H1")
        e2 = self._ingest("E-H2", retention=self.future)
        batch = self.store.create_release_batch("auditor1", self.case["id"], "案件当事人",
                                                evidence_ids=[e1["id"], e2["id"]])
        by_ev = {i["evidence_id"]: i for i in batch["items"]}
        self.assertEqual(by_ev[e2["id"]]["review_status"], "ineligible")
        self.assertIn("保留期限", by_ev[e2["id"]]["ineligible_reason"])
        self.store.set_hold("auditor1", e1["id"], True, "新线索需继续保留")
        result = self.store.execute_release_batch("custodian1", batch["id"])
        self.assertEqual(result["released"], [])
        self.assertEqual(len(result["voided"]), 1)
        self.assertIn("保留", result["voided"][0]["reason"])
        ev = self.store.get_evidence("custodian1", e1["id"])
        self.assertEqual(ev["status"], "custody")
        self.assertEqual(len(self._release_events(e1["id"])), 0)

    def test_concurrent_executors_release_exactly_once(self):
        items = [self._ingest(f"E-C{i}") for i in range(6)]
        batch = self.store.create_release_batch("auditor1", self.case["id"], "检察机关")
        errors, results = [], []
        def run(user):
            try:
                results.append(self.store.execute_release_batch(user, batch["id"]))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run, args=(u,)) for u in ("custodian1", "custodian2")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        released = sorted(results[0]["released"] + results[1]["released"])
        self.assertEqual(released, sorted(i["id"] for i in items))
        for i in items:
            ev = self.store.get_evidence("auditor1", i["id"])
            self.assertEqual(ev["status"], "released")
            self.assertEqual(len([e for e in ev["events"] if e["event_type"] == "RELEASE"]), 1)
        sheet = self.store.get_release_batch("auditor1", batch["id"])
        self.assertEqual(sheet["counts"]["released"], len(items))
        report = self.store.report("auditor1", self.case["id"])
        self.assertTrue(report["release_reconciliation_valid"])

    def test_crash_resume_finds_batch_without_duplicates(self):
        items = [self._ingest(f"E-R{i}") for i in range(3)]
        batch = self.store.create_release_batch("auditor1", self.case["id"], "案件当事人")
        original = CustodyStore._append_event
        calls = {"n": 0}
        def flaky(conn, evidence_id, event_type, *args, **kwargs):
            if event_type == "RELEASE":
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("模拟执行中崩溃")
            return original(self.store, conn, evidence_id, event_type, *args, **kwargs)
        self.store._append_event = flaky
        with self.assertRaises(RuntimeError):
            self.store.execute_release_batch("custodian1", batch["id"])
        # 重启：新实例能找回未完成的批次
        restarted = CustodyStore(self.db_path)
        batches = restarted.list_release_batches("auditor1", self.case["id"])["batches"]
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["status"], "open")
        self.assertEqual(batches[0]["counts"]["pending"], 2)
        result = restarted.execute_release_batch("custodian2", batch["id"])
        self.assertEqual(sorted(result["released"]), sorted(i["id"] for i in items[1:]))
        for i in items:
            self.assertEqual(len(self._release_events(i["id"])), 1)
        sheet = restarted.get_release_batch("auditor1", batch["id"])
        self.assertEqual(sheet["status"], "completed")
        report = restarted.report("auditor1", self.case["id"])
        self.assertTrue(report["release_reconciliation_valid"])

    def test_batch_permissions(self):
        self._ingest("E-P1")
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_release_batch("outsider", self.case["id"], "案件当事人")
        self.assertEqual(ctx.exception.status, 403)
        batch = self.store.create_release_batch("auditor1", self.case["id"], "案件当事人")
        with self.assertRaises(BusinessError) as ctx:
            self.store.execute_release_batch("analyst1", batch["id"])
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
