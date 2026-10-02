import base64
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, CustodyStore


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class ReleaseReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.store = CustodyStore(self.db_path)
        self.store.seed()
        self.case = self.store.create_case("custodian1", "CASE-2026-010", "释放复核测试")
        for uid, role in [("custodian2", "custodian"), ("analyst1", "analyst"), ("auditor1", "auditor")]:
            self.store.add_member("custodian1", self.case["id"], uid, role)
        self.past = (date.today() - timedelta(days=1)).isoformat()
        self.future = (date.today() + timedelta(days=365)).isoformat()

    def tearDown(self):
        self.tmp.cleanup()

    def _expire(self, evidence_id):
        """模拟保留期限届满：证据入册时不允许早于今天，这里直接改库模拟时间流逝。"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE evidence SET retention_until=? WHERE id=?", (self.past, evidence_id))
        conn.commit()
        conn.close()

    def _ingest(self, label, retention=None, content=b"bytes"):
        far_future = (date.today() + timedelta(days=3650)).isoformat()
        ev = self.store.ingest_evidence(
            "custodian1", self.case["id"], label, f"{label}.bin", b64(content),
            far_future, "custodian1",
        )
        if retention != "future":
            self._expire(ev["id"])
        return ev

    def test_checklist_classifies_all_states(self):
        # E-001: 正常可释放
        e1 = self._ingest("E-001")
        # E-002: 仍在法律保留
        e2 = self._ingest("E-002")
        self.store.set_hold("auditor1", e2["id"], True, "诉讼保全需要继续保留")
        # E-003: 正在移交（在途）
        e3 = self._ingest("E-003")
        self.store.dispatch("custodian1", e3["id"], "custodian2", "法院证物库", "封存后发出")
        # E-004: 已派生分析件（本件因有派生件被拦截；派生件本身是独立证据，可单独释放）
        e4 = self._ingest("E-004")
        self.store.open_evidence("custodian1", e4["id"], "A 区", "开箱分析")
        child = self.store.derive("analyst1", e4["id"], "提取交易记录", "E-004-D1", "d.json", b64(b"[]"))
        # E-005: 保留期限未届满
        e5 = self._ingest("E-005", retention="future")
        # E-006: 已释放
        e6 = self._ingest("E-006")
        self.store.release("custodian1", e6["id"], "检察机关", "按调取令释放")

        review = self.store.create_release_review("auditor1", self.case["id"], "诉讼结束释放复核")
        by_id = {it["evidence_id"]: it for it in review["items"]}

        self.assertEqual(by_id[e1["id"]]["eligible"], 1)
        self.assertEqual(by_id[child["id"]]["eligible"], 1)
        self.assertEqual(by_id[e2["id"]]["eligible"], 0)
        self.assertIn("法律保留", by_id[e2["id"]]["reason"])
        self.assertEqual(by_id[e3["id"]]["eligible"], 0)
        self.assertIn("移交", by_id[e3["id"]]["reason"])
        self.assertEqual(by_id[e4["id"]]["eligible"], 0)
        self.assertIn("派生", by_id[e4["id"]]["reason"])
        self.assertEqual(by_id[e5["id"]]["eligible"], 0)
        self.assertIn("保留期限", by_id[e5["id"]]["reason"])
        self.assertEqual(by_id[e6["id"]]["eligible"], 0)
        self.assertIn("已释放", by_id[e6["id"]]["reason"])

        # 可释放：E-001 与派生件；其余各有拦截原因
        self.assertEqual(sum(1 for it in review["items"] if it["eligible"]), 2)

    def test_submit_releases_and_optimistic_concurrency_voids(self):
        e1 = self._ingest("E-001")
        e2 = self._ingest("E-002")
        e3 = self._ingest("E-003")
        review = self.store.create_release_review("auditor1", self.case["id"])
        rid = review["review"]["id"]

        # 复核后：E-002 被设置法律保留，E-003 被发出移交
        self.store.set_hold("auditor1", e2["id"], True, "追加法律保留")
        self.store.dispatch("custodian1", e3["id"], "custodian2", "法院证物库", "发出移交")

        summary = self.store.submit_releases("custodian1", rid, "检察机关", "按复核单释放")
        self.assertEqual(len(summary["released"]), 1)
        self.assertEqual(summary["released"][0]["evidence_id"], e1["id"])
        self.assertEqual(len(summary["voided"]), 2)
        void_reasons = {v["evidence_id"]: v["reason"] for v in summary["voided"]}
        self.assertIn("法律保留", void_reasons[e2["id"]])
        self.assertIn("移交", void_reasons[e3["id"]])

        # 复核单状态与实际结果一致
        review_after = self.store.get_release_review("auditor1", rid)
        states = {it["evidence_id"]: it["state"] for it in review_after["items"]}
        self.assertEqual(states[e1["id"]], "released")
        self.assertEqual(states[e2["id"]], "void")
        self.assertEqual(states[e3["id"]], "void")
        ev1 = self.store.get_evidence("auditor1", e1["id"])
        self.assertEqual(ev1["status"], "released")

    def test_crash_recovery_resume_is_idempotent(self):
        e1 = self._ingest("E-001")
        e2 = self._ingest("E-002")
        e3 = self._ingest("E-003")
        review = self.store.create_release_review("auditor1", self.case["id"])
        rid = review["review"]["id"]

        # 第一次提交只处理 E-001（模拟执行到一半）
        s1 = self.store.submit_releases("custodian1", rid, "检察机关", "第一次", evidence_ids=[e1["id"]])
        self.assertEqual(len(s1["released"]), 1)

        # 模拟崩溃重启：新建 store 实例指向同一数据库
        store2 = CustodyStore(self.db_path)
        review_loaded = store2.get_release_review("auditor1", rid)
        self.assertEqual(review_loaded["review"]["status"], "executing")

        # 恢复后继续处理剩余条目
        s2 = store2.submit_releases("custodian1", rid, "检察机关", "第二次")
        self.assertEqual(len(s2["released"]), 2)
        self.assertEqual({r["evidence_id"] for r in s2["released"]}, {e2["id"], e3["id"]})

        # 再次提交：已释放的不能重复写入
        s3 = store2.submit_releases("custodian1", rid, "检察机关", "第三次")
        self.assertEqual(len(s3["released"]), 0)
        self.assertEqual(len(s3["unchanged"]), 3)

        # 报告与实际结果一致
        report = store2.report("auditor1", self.case["id"])
        self.assertTrue(report["release_review_consistent"])
        self.assertEqual(report["release_review_count"], 1)
        states = {it["evidence_id"]: it["state"] for it in report["release_reviews"][0]["items"]}
        self.assertEqual(states, {e1["id"]: "released", e2["id"]: "released", e3["id"]: "released"})
        for eid in (e1["id"], e2["id"], e3["id"]):
            self.assertEqual(store2.get_evidence("auditor1", eid)["status"], "released")

    def test_concurrent_custodians_no_double_release(self):
        e1 = self._ingest("E-001")
        review = self.store.create_release_review("auditor1", self.case["id"])
        rid = review["review"]["id"]
        # 两名保管员同时提交
        s1 = self.store.submit_releases("custodian1", rid, "检察机关", "甲提交")
        s2 = self.store.submit_releases("custodian2", rid, "检察机关", "乙提交")
        self.assertEqual(len(s1["released"]), 1)
        self.assertEqual(len(s2["released"]), 0)
        self.assertEqual(len(s2["unchanged"]), 1)
        ev = self.store.get_evidence("auditor1", e1["id"])
        self.assertEqual(ev["status"], "released")
        release_events = [e for e in ev["events"] if e["event_type"] == "RELEASE"]
        self.assertEqual(len(release_events), 1)

    def test_dispatch_accept_flow(self):
        e1 = self._ingest("E-001")
        self.store.dispatch("custodian1", e1["id"], "custodian2", "法院证物库", "发出")
        ev = self.store.get_evidence("auditor1", e1["id"])
        self.assertTrue(ev["in_transit"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("custodian1", e1["id"], "检察机关")
        self.assertEqual(ctx.exception.code, "in_transit")
        self.store.accept("custodian2", e1["id"], "接收")
        ev = self.store.get_evidence("auditor1", e1["id"])
        self.assertFalse(ev["in_transit"])
        self.assertEqual(ev["current_custodian"], "custodian2")
        # 接收后可正常释放
        self.store.release("custodian2", e1["id"], "检察机关", "释放")
        self.assertEqual(self.store.get_evidence("auditor1", e1["id"])["status"], "released")

    def test_only_auditor_can_create_review(self):
        self._ingest("E-001")
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_release_review("analyst1", self.case["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_release_review("outsider", self.case["id"])
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
