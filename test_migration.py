import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import CustodyStore


class MigrationTests(unittest.TestCase):
    """验证旧版数据库（无 in_transit、无释放复核表、旧事件类型约束）能平滑升级。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "old.db"
        con = sqlite3.connect(self.db_path)
        con.executescript(
            """
            CREATE TABLE users(id TEXT PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)));
            CREATE TABLE cases(id INTEGER PRIMARY KEY AUTOINCREMENT, case_number TEXT NOT NULL UNIQUE, title TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
            CREATE TABLE case_members(case_id INTEGER NOT NULL REFERENCES cases(id), user_id TEXT NOT NULL REFERENCES users(id), role TEXT NOT NULL CHECK(role IN ('custodian','analyst','auditor')), active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), granted_by TEXT NOT NULL REFERENCES users(id), granted_at TEXT NOT NULL, PRIMARY KEY(case_id,user_id));
            CREATE TABLE evidence(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES cases(id), label TEXT NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'custody' CHECK(status IN ('custody','opened','released','derivative')), current_custodian TEXT NOT NULL, legal_hold INTEGER NOT NULL DEFAULT 0 CHECK(legal_hold IN (0,1)), retention_until TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, UNIQUE(case_id,label));
            CREATE TABLE custody_events(id INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id INTEGER NOT NULL REFERENCES evidence(id), sequence INTEGER NOT NULL, event_type TEXT NOT NULL CHECK(event_type IN ('INGEST','TRANSFER','OPEN','ANALYZE','RELEASE','HOLD_SET','HOLD_CLEARED')), actor_id TEXT NOT NULL REFERENCES users(id), from_person TEXT, to_person TEXT, location TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(evidence_id,sequence));
            CREATE TABLE derivatives(id INTEGER PRIMARY KEY AUTOINCREMENT, parent_evidence_id INTEGER NOT NULL REFERENCES evidence(id), child_evidence_id INTEGER NOT NULL UNIQUE REFERENCES evidence(id), method TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, UNIQUE(parent_evidence_id,child_evidence_id));
            CREATE TABLE audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES cases(id), actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
            INSERT INTO users VALUES ('custodian1','保管员',1),('custodian2','保管员乙',1),('auditor1','审计员',1);
            INSERT INTO cases VALUES (1,'CASE-1','旧库案件','custodian1','2026-01-01T00:00:00+00:00');
            INSERT INTO case_members VALUES (1,'custodian1','custodian',1,'custodian1','2026-01-01T00:00:00+00:00'),(1,'custodian2','custodian',1,'custodian1','2026-01-01T00:00:00+00:00'),(1,'auditor1','auditor',1,'custodian1','2026-01-01T00:00:00+00:00');
            INSERT INTO evidence VALUES (1,1,'E-OLD','old.bin','abc123',4,x'01020304','custody','custodian1',0,'2026-01-01','custodian1','2026-01-01T00:00:00+00:00');
            """
        )
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_db_migrates_and_supports_new_features(self):
        store = CustodyStore(self.db_path)
        store.init_schema()
        ev = store.get_evidence("auditor1", 1)
        self.assertIn("in_transit", ev)
        store.dispatch("custodian1", 1, "custodian2", "旧库法院证物库", "迁移后发出")
        self.assertTrue(store.get_evidence("auditor1", 1)["in_transit"])
        store.accept("custodian2", 1, "接收")
        self.assertFalse(store.get_evidence("auditor1", 1)["in_transit"])
        review = store.create_release_review("auditor1", 1, "迁移后复核")
        self.assertEqual(len(review["items"]), 1)
        # 旧库证据保留期限为 2026-01-01（已届满），可释放
        self.assertEqual(review["items"][0]["eligible"], 1)


if __name__ == "__main__":
    unittest.main()
