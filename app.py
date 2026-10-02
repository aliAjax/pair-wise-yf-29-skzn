"""法律证据保管与流转后台。"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "custody.db"
MEMBER_ROLES = {"custodian", "analyst", "auditor"}


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CustodyStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def init_schema(self):
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS cases(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, case_number TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS case_members(
                    case_id INTEGER NOT NULL REFERENCES cases(id), user_id TEXT NOT NULL REFERENCES users(id),
                    role TEXT NOT NULL CHECK(role IN ('custodian','analyst','auditor')),
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                    granted_by TEXT NOT NULL REFERENCES users(id), granted_at TEXT NOT NULL,
                    PRIMARY KEY(case_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS evidence(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases(id), label TEXT NOT NULL,
                    filename TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                    content BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'custody'
                        CHECK(status IN ('custody','opened','released','derivative')),
                    current_custodian TEXT NOT NULL, legal_hold INTEGER NOT NULL DEFAULT 0 CHECK(legal_hold IN (0,1)),
                    retention_until TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, UNIQUE(case_id,label)
                );
                CREATE TABLE IF NOT EXISTS custody_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id INTEGER NOT NULL REFERENCES evidence(id), sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL CHECK(event_type IN ('INGEST','TRANSFER','OPEN','ANALYZE','RELEASE','HOLD_SET','HOLD_CLEARED')),
                    actor_id TEXT NOT NULL REFERENCES users(id), from_person TEXT,
                    to_person TEXT, location TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
                    previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(evidence_id,sequence)
                );
                CREATE TABLE IF NOT EXISTS derivatives(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_evidence_id INTEGER NOT NULL REFERENCES evidence(id),
                    child_evidence_id INTEGER NOT NULL UNIQUE REFERENCES evidence(id),
                    method TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, UNIQUE(parent_evidence_id,child_evidence_id)
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases(id), actor_id TEXT NOT NULL REFERENCES users(id),
                    action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS release_reviews(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases(id),
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','executing','completed','cancelled')),
                    created_by TEXT NOT NULL REFERENCES users(id), note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS release_review_items(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_id INTEGER NOT NULL REFERENCES release_reviews(id),
                    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
                    eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
                    reason TEXT NOT NULL DEFAULT '',
                    snap_sha256 TEXT NOT NULL, snap_status TEXT NOT NULL,
                    snap_legal_hold INTEGER NOT NULL, snap_in_transit INTEGER NOT NULL DEFAULT 0,
                    snap_current_custodian TEXT NOT NULL, snap_retention_until TEXT NOT NULL,
                    snap_event_count INTEGER NOT NULL, snap_last_event_hash TEXT NOT NULL,
                    snap_derivative_count INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','released','void','skipped')),
                    recipient TEXT NOT NULL DEFAULT '', release_note TEXT NOT NULL DEFAULT '',
                    actor_id TEXT REFERENCES users(id), idempotency_key TEXT NOT NULL DEFAULT '',
                    result_reason TEXT NOT NULL DEFAULT '', processed_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(review_id, evidence_id)
                );
                """
            )
            self._migrate(conn)

    def _migrate(self, conn):
        """兼容旧库：补齐 in_transit 列，并扩展 custody_events 的事件类型约束。"""
        cols = [r[1] for r in conn.execute("PRAGMA table_info(evidence)").fetchall()]
        if "in_transit" not in cols:
            conn.execute("ALTER TABLE evidence ADD COLUMN in_transit INTEGER NOT NULL DEFAULT 0 CHECK(in_transit IN (0,1))")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='custody_events'").fetchone()
        ddl = row[0] if row else ""
        if "DISPATCH" not in ddl:
            conn.execute(
                """
                CREATE TABLE custody_events_new(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id INTEGER NOT NULL REFERENCES evidence(id), sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL CHECK(event_type IN ('INGEST','TRANSFER','OPEN','ANALYZE','RELEASE','HOLD_SET','HOLD_CLEARED','DISPATCH','ACCEPT')),
                    actor_id TEXT NOT NULL REFERENCES users(id), from_person TEXT,
                    to_person TEXT, location TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
                    previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(evidence_id,sequence)
                )
                """
            )
            conn.execute(
                "INSERT INTO custody_events_new(id,evidence_id,sequence,event_type,actor_id,from_person,to_person,location,note,previous_hash,event_hash,created_at) "
                "SELECT id,evidence_id,sequence,event_type,actor_id,from_person,to_person,location,note,previous_hash,event_hash,created_at FROM custody_events"
            )
            conn.execute("DROP TABLE custody_events")
            conn.execute("ALTER TABLE custody_events_new RENAME TO custody_events")

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name) VALUES(?,?)",
                [
                    ("custodian1", "证据保管员甲"), ("custodian2", "证据保管员乙"),
                    ("analyst1", "电子数据分析员"), ("auditor1", "案件审计员"), ("outsider", "外部人员"),
                ],
            )

    def _user(self, conn, user_id):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在或已停用", 401, "unknown_user")
        return user

    def _case(self, conn, case_id):
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise BusinessError("案件不存在", 404, "not_found")
        return row

    def _member(self, conn, case_id, user_id, roles=None):
        user = self._user(conn, user_id)
        self._case(conn, case_id)
        row = conn.execute(
            "SELECT * FROM case_members WHERE case_id=? AND user_id=? AND active=1", (case_id, user_id)
        ).fetchone()
        if not row:
            raise BusinessError("不是案件有效成员", 403, "forbidden")
        if roles and row["role"] not in roles:
            raise BusinessError("当前案件角色无权执行此操作", 403, "forbidden")
        return user, row

    def _audit(self, conn, case_id, actor_id, action, detail):
        conn.execute(
            "INSERT INTO audit_log(case_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (case_id, actor_id, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def create_case(self, user_id, case_number, title):
        if not case_number.strip() or len(title.strip()) < 2:
            raise BusinessError("案件编号和标题不能为空", 422, "invalid_case")
        with self.connect() as conn:
            self._user(conn, user_id)
            try:
                cur = conn.execute(
                    "INSERT INTO cases(case_number,title,created_by,created_at) VALUES(?,?,?,?)",
                    (case_number.strip(), title.strip(), user_id, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("案件编号已存在", 409, "case_exists")
            case_id = cur.lastrowid
            conn.execute(
                "INSERT INTO case_members(case_id,user_id,role,granted_by,granted_at) VALUES(?,?,?,?,?)",
                (case_id, user_id, "custodian", user_id, now()),
            )
            self._audit(conn, case_id, user_id, "case.create", {"case_number": case_number.strip()})
            return {"id": case_id, "case_number": case_number.strip(), "title": title.strip()}

    def add_member(self, user_id, case_id, member_id, role):
        if role not in MEMBER_ROLES:
            raise BusinessError("案件角色必须是 custodian、analyst 或 auditor", 422, "invalid_role")
        with self.connect() as conn:
            case = self._case(conn, case_id)
            if case["created_by"] != user_id:
                raise BusinessError("只有案件创建人可以授权成员", 403, "forbidden")
            self._user(conn, member_id)
            conn.execute(
                """INSERT INTO case_members(case_id,user_id,role,active,granted_by,granted_at) VALUES(?,?,?,1,?,?)
                   ON CONFLICT(case_id,user_id) DO UPDATE SET role=excluded.role,active=1,granted_by=excluded.granted_by,granted_at=excluded.granted_at""",
                (case_id, member_id, role, user_id, now()),
            )
            self._audit(conn, case_id, user_id, "member.grant", {"member_id": member_id, "role": role})
            return {"case_id": case_id, "member_id": member_id, "role": role}

    @staticmethod
    def _event_hash(event):
        canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()

    def _append_event(self, conn, evidence_id, event_type, actor, from_person="", to_person="", location="", note=""):
        previous = conn.execute(
            "SELECT event_hash,sequence FROM custody_events WHERE evidence_id=? ORDER BY sequence DESC LIMIT 1", (evidence_id,)
        ).fetchone()
        sequence = (previous["sequence"] + 1) if previous else 1
        previous_hash = previous["event_hash"] if previous else "GENESIS"
        payload = {
            "evidence_id": evidence_id, "sequence": sequence, "event_type": event_type,
            "actor_id": actor, "from_person": from_person or None, "to_person": to_person or None,
            "location": location, "note": note, "previous_hash": previous_hash, "created_at": now(),
        }
        digest = self._event_hash(payload)
        cur = conn.execute(
            """INSERT INTO custody_events(evidence_id,sequence,event_type,actor_id,from_person,to_person,location,note,previous_hash,event_hash,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (evidence_id, sequence, event_type, actor, from_person or None, to_person or None, location, note, previous_hash, digest, payload["created_at"]),
        )
        return cur.lastrowid, digest

    def ingest_evidence(self, user_id, case_id, label, filename, content_b64, retention_until, custodian=None):
        label, filename = label.strip(), filename.strip()
        if not label or not filename:
            raise BusinessError("证据标签和文件名不能为空", 422, "invalid_evidence")
        try:
            content = base64.b64decode(content_b64, validate=True)
            deadline = date.fromisoformat(retention_until)
        except (binascii.Error, ValueError, TypeError):
            raise BusinessError("证据内容 Base64 或保留期限格式错误", 422, "invalid_evidence")
        if deadline < date.today():
            raise BusinessError("保留期限不能早于今天", 422, "invalid_retention")
        digest = hashlib.sha256(content).hexdigest()
        custodian = (custodian or user_id).strip()
        with self.connect() as conn:
            _, member = self._member(conn, case_id, user_id, {"custodian"})
            if not custodian:
                raise BusinessError("保管人不能为空", 422, "invalid_custodian")
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    """INSERT INTO evidence(case_id,label,filename,sha256,size,content,current_custodian,retention_until,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (case_id, label, filename, digest, len(content), content, custodian, retention_until, user_id, now()),
                )
                evidence_id = cur.lastrowid
                self._append_event(conn, evidence_id, "INGEST", user_id, to_person=custodian, note=f"入册 SHA-256 {digest}")
                self._audit(conn, case_id, user_id, "evidence.ingest", {"evidence_id": evidence_id, "sha256": digest, "label": label})
                return {"id": evidence_id, "label": label, "sha256": digest, "size": len(content), "status": "custody", "current_custodian": custodian}
            except sqlite3.IntegrityError:
                conn.rollback()
                raise BusinessError("该案件中的证据标签已存在", 409, "label_exists")
            except Exception:
                conn.rollback()
                raise

    def _evidence(self, conn, evidence_id):
        row = conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        if not row:
            raise BusinessError("证据不存在", 404, "not_found")
        return row

    def get_evidence(self, user_id, evidence_id, include_content=False):
        with self.connect() as conn:
            row = self._evidence(conn, evidence_id)
            self._member(conn, row["case_id"], user_id)
            result = {k: row[k] for k in row.keys() if k != "content"}
            result["legal_hold"] = bool(row["legal_hold"])
            result["integrity_valid"] = hashlib.sha256(row["content"]).hexdigest() == row["sha256"]
            result["events"] = [dict(x) for x in conn.execute("SELECT * FROM custody_events WHERE evidence_id=? ORDER BY sequence", (evidence_id,)).fetchall()]
            result["derived_children"] = [dict(x) for x in conn.execute("SELECT * FROM derivatives WHERE parent_evidence_id=? ORDER BY id", (evidence_id,)).fetchall()]
            if include_content:
                result["content_b64"] = base64.b64encode(row["content"]).decode()
            return result

    def transfer(self, user_id, evidence_id, to_person, location, note=""):
        if not to_person.strip() or not location.strip():
            raise BusinessError("接收人和保管位置不能为空", 422, "invalid_transfer")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._evidence(conn, evidence_id)
                _, member = self._member(conn, row["case_id"], user_id, {"custodian"})
                if row["status"] == "released":
                    raise BusinessError("已释放证据不能再移交", 409, "evidence_released")
                self._append_event(conn, evidence_id, "TRANSFER", user_id, from_person=row["current_custodian"], to_person=to_person.strip(), location=location.strip(), note=note.strip())
                conn.execute("UPDATE evidence SET current_custodian=? WHERE id=?", (to_person.strip(), evidence_id))
                self._audit(conn, row["case_id"], user_id, "custody.transfer", {"evidence_id": evidence_id, "to": to_person.strip(), "location": location.strip()})
                return {"id": evidence_id, "current_custodian": to_person.strip(), "location": location.strip()}
            except Exception:
                conn.rollback()
                raise

    def open_evidence(self, user_id, evidence_id, location, note=""):
        if not location.strip():
            raise BusinessError("开箱地点不能为空", 422, "location_required")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._evidence(conn, evidence_id)
                _, member = self._member(conn, row["case_id"], user_id, {"custodian"})
                if row["status"] != "custody":
                    raise BusinessError("只有处于封存保管状态的证据可以开箱", 409, "invalid_status")
                self._append_event(conn, evidence_id, "OPEN", user_id, from_person=row["current_custodian"], location=location.strip(), note=note.strip())
                conn.execute("UPDATE evidence SET status='opened' WHERE id=?", (evidence_id,))
                self._audit(conn, row["case_id"], user_id, "evidence.open", {"evidence_id": evidence_id, "location": location.strip()})
                return {"id": evidence_id, "status": "opened", "location": location.strip()}
            except Exception:
                conn.rollback()
                raise

    def derive(self, user_id, evidence_id, method, label, filename, content_b64):
        if len(method.strip()) < 3 or not label.strip() or not filename.strip():
            raise BusinessError("分析方法、子证据标签和文件名不能为空", 422, "invalid_derivative")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise BusinessError("content_b64 不是合法 Base64", 422, "invalid_base64")
        digest = hashlib.sha256(content).hexdigest()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                parent = self._evidence(conn, evidence_id)
                _, member = self._member(conn, parent["case_id"], user_id, {"analyst"})
                if parent["status"] != "opened":
                    raise BusinessError("原始证据必须先开箱才能分析", 409, "evidence_not_opened")
                cur = conn.execute(
                    """INSERT INTO evidence(case_id,label,filename,sha256,size,content,status,current_custodian,legal_hold,retention_until,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (parent["case_id"], label.strip(), filename.strip(), digest, len(content), content, "derivative", user_id, 0, parent["retention_until"], user_id, now()),
                )
                child_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO derivatives(parent_evidence_id,child_evidence_id,method,actor_id,created_at) VALUES(?,?,?,?,?)",
                    (evidence_id, child_id, method.strip(), user_id, now()),
                )
                self._append_event(conn, evidence_id, "ANALYZE", user_id, from_person=parent["current_custodian"], note=f"生成衍生证据 #{child_id}: {method.strip()}")
                self._append_event(conn, child_id, "INGEST", user_id, from_person=parent["current_custodian"], to_person=user_id, note=f"由证据 #{evidence_id} 派生，SHA-256 {digest}")
                self._audit(conn, parent["case_id"], user_id, "evidence.derive", {"parent_id": evidence_id, "child_id": child_id, "method": method.strip(), "sha256": digest})
                return {"id": child_id, "parent_id": evidence_id, "label": label.strip(), "sha256": digest, "status": "derivative"}
            except sqlite3.IntegrityError:
                conn.rollback()
                raise BusinessError("衍生证据标签已存在", 409, "label_exists")
            except Exception:
                conn.rollback()
                raise

    def set_hold(self, user_id, evidence_id, hold, reason):
        if len(reason.strip()) < 5:
            raise BusinessError("法律保留原因至少 5 字", 422, "reason_required")
        with self.connect() as conn:
            row = self._evidence(conn, evidence_id)
            case = self._case(conn, row["case_id"])
            if user_id != case["created_by"]:
                self._member(conn, row["case_id"], user_id, {"auditor"})
            conn.execute("UPDATE evidence SET legal_hold=? WHERE id=?", (int(bool(hold)), evidence_id))
            event = "HOLD_SET" if hold else "HOLD_CLEARED"
            self._append_event(conn, evidence_id, event, user_id, note=reason.strip())
            self._audit(conn, row["case_id"], user_id, "evidence.hold", {"evidence_id": evidence_id, "hold": bool(hold), "reason": reason.strip()})
            return {"id": evidence_id, "legal_hold": bool(hold)}

    def _release_in_tx(self, conn, user_id, evidence_id, recipient, note):
        """在已开启的事务内执行释放校验与落库，供 release 与释放复核共用。"""
        row = self._evidence(conn, evidence_id)
        if row["legal_hold"]:
            raise BusinessError("存在法律保留，禁止释放证据", 409, "legal_hold_active")
        if row["status"] == "released":
            raise BusinessError("证据已经释放", 409, "already_released")
        if row["in_transit"]:
            raise BusinessError("证据正在移交（在途），不能释放", 409, "in_transit")
        self._append_event(conn, evidence_id, "RELEASE", user_id, from_person=row["current_custodian"], to_person=recipient.strip(), note=note.strip())
        conn.execute("UPDATE evidence SET status='released' WHERE id=?", (evidence_id,))
        self._audit(conn, row["case_id"], user_id, "evidence.release", {"evidence_id": evidence_id, "recipient": recipient.strip()})
        return row

    def release(self, user_id, evidence_id, recipient, note=""):
        if not recipient.strip():
            raise BusinessError("接收方不能为空", 422, "recipient_required")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._evidence(conn, evidence_id)
                self._member(conn, row["case_id"], user_id, {"custodian"})
                self._release_in_tx(conn, user_id, evidence_id, recipient, note)
                return {"id": evidence_id, "status": "released", "recipient": recipient.strip()}
            except Exception:
                conn.rollback()
                raise

    def dispatch(self, user_id, evidence_id, to_person, location, note=""):
        """发出移交：证据进入在途状态，复核与释放均会拦截。"""
        to_person, location = to_person.strip(), location.strip()
        if not to_person or not location:
            raise BusinessError("接收人和移交地点不能为空", 422, "invalid_dispatch")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._evidence(conn, evidence_id)
                self._member(conn, row["case_id"], user_id, {"custodian"})
                if row["status"] == "released":
                    raise BusinessError("已释放证据不能移交", 409, "evidence_released")
                if row["in_transit"]:
                    raise BusinessError("证据已在移交途中", 409, "already_in_transit")
                self._append_event(conn, evidence_id, "DISPATCH", user_id, from_person=row["current_custodian"],
                                   to_person=to_person, location=location, note=note.strip() or "发出移交，在途")
                conn.execute("UPDATE evidence SET in_transit=1 WHERE id=?", (evidence_id,))
                self._audit(conn, row["case_id"], user_id, "custody.dispatch", {"evidence_id": evidence_id, "to": to_person, "location": location})
                return {"id": evidence_id, "in_transit": True, "to_person": to_person, "location": location}
            except Exception:
                conn.rollback()
                raise

    def accept(self, user_id, evidence_id, note=""):
        """接收入库：在途移交完成，保管人变更为接收人。"""
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._evidence(conn, evidence_id)
                self._member(conn, row["case_id"], user_id, {"custodian"})
                if row["status"] == "released":
                    raise BusinessError("已释放证据无需接收", 409, "evidence_released")
                if not row["in_transit"]:
                    raise BusinessError("证据不在移交途中", 409, "not_in_transit")
                disp = conn.execute(
                    "SELECT * FROM custody_events WHERE evidence_id=? AND event_type='DISPATCH' ORDER BY sequence DESC LIMIT 1",
                    (evidence_id,),
                ).fetchone()
                to_person = disp["to_person"] if disp else row["current_custodian"]
                location = disp["location"] if disp else ""
                self._append_event(conn, evidence_id, "ACCEPT", user_id, from_person=row["current_custodian"],
                                   to_person=to_person, location=location, note=note.strip() or "移交完成，接收入库")
                conn.execute("UPDATE evidence SET in_transit=0, current_custodian=? WHERE id=?", (to_person, evidence_id))
                self._audit(conn, row["case_id"], user_id, "custody.accept", {"evidence_id": evidence_id, "custodian": to_person})
                return {"id": evidence_id, "in_transit": False, "current_custodian": to_person}
            except Exception:
                conn.rollback()
                raise

    # ---- 释放复核单 ----

    def _snapshot(self, conn, ev):
        event_count = conn.execute("SELECT COUNT(*) AS c FROM custody_events WHERE evidence_id=?", (ev["id"],)).fetchone()["c"]
        last = conn.execute(
            "SELECT event_hash FROM custody_events WHERE evidence_id=? ORDER BY sequence DESC LIMIT 1", (ev["id"],)
        ).fetchone()
        deriv_count = conn.execute("SELECT COUNT(*) AS c FROM derivatives WHERE parent_evidence_id=?", (ev["id"],)).fetchone()["c"]
        return {
            "snap_sha256": ev["sha256"], "snap_status": ev["status"],
            "snap_legal_hold": ev["legal_hold"], "snap_in_transit": ev["in_transit"],
            "snap_current_custodian": ev["current_custodian"], "snap_retention_until": ev["retention_until"],
            "snap_event_count": event_count, "snap_last_event_hash": last["event_hash"] if last else "GENESIS",
            "snap_derivative_count": deriv_count,
        }

    def _eligibility(self, conn, ev, today):
        """复核时点的可释放判定，返回 (eligible, reason)。"""
        if ev["status"] == "released":
            return 0, "证据已释放，无需重复释放"
        if ev["legal_hold"]:
            return 0, "仍在法律保留（法律保留中），禁止释放"
        if ev["in_transit"]:
            return 0, "证据正在移交（在途），保管状态未结清"
        if date.fromisoformat(ev["retention_until"]) > today:
            return 0, f"保留期限未届满（{ev['retention_until']}），暂不可释放"
        deriv_count = conn.execute("SELECT COUNT(*) AS c FROM derivatives WHERE parent_evidence_id=?", (ev["id"],)).fetchone()["c"]
        if deriv_count > 0:
            return 0, f"存在 {deriv_count} 件派生分析件，需先对派生件完成处置再释放本件"
        return 1, "符合释放条件：保留期限已届满，无法律保留，无在途移交，无未处置派生件"

    def _review_dict(self, conn, review_id):
        review = conn.execute("SELECT * FROM release_reviews WHERE id=?", (review_id,)).fetchone()
        if not review:
            return None
        items = []
        for it in conn.execute("SELECT * FROM release_review_items WHERE review_id=? ORDER BY id", (review_id,)).fetchall():
            d = dict(it)
            d["evidence"] = dict(conn.execute(
                "SELECT id,label,filename,status,current_custodian,legal_hold,in_transit,retention_until FROM evidence WHERE id=?",
                (it["evidence_id"],),
            ).fetchone())
            items.append(d)
        return {"review": dict(review), "items": items}

    def create_release_review(self, user_id, case_id, note=""):
        with self.connect() as conn:
            _, member = self._member(conn, case_id, user_id)
            case = self._case(conn, case_id)
            if member["role"] != "auditor" and case["created_by"] != user_id:
                raise BusinessError("只有审计员或案件创建人可以创建释放复核单", 403, "forbidden")
            today = date.today()
            cur = conn.execute(
                "INSERT INTO release_reviews(case_id,status,created_by,note,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (case_id, "open", user_id, note.strip(), now(), now()),
            )
            review_id = cur.lastrowid
            for ev in conn.execute("SELECT * FROM evidence WHERE case_id=? ORDER BY id", (case_id,)).fetchall():
                eligible, reason = self._eligibility(conn, ev, today)
                snap = self._snapshot(conn, ev)
                conn.execute(
                    """INSERT INTO release_review_items(review_id,evidence_id,eligible,reason,state,
                       snap_sha256,snap_status,snap_legal_hold,snap_in_transit,snap_current_custodian,snap_retention_until,
                       snap_event_count,snap_last_event_hash,snap_derivative_count,created_at,updated_at)
                       VALUES(?,?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?)""",
                    (review_id, ev["id"], eligible, reason, snap["snap_sha256"], snap["snap_status"],
                     snap["snap_legal_hold"], snap["snap_in_transit"], snap["snap_current_custodian"],
                     snap["snap_retention_until"], snap["snap_event_count"], snap["snap_last_event_hash"],
                     snap["snap_derivative_count"], now(), now()),
                )
            self._audit(conn, case_id, user_id, "release_review.create", {"review_id": review_id})
            return self._review_dict(conn, review_id)

    def get_release_review(self, user_id, review_id):
        with self.connect() as conn:
            review = conn.execute("SELECT * FROM release_reviews WHERE id=?", (review_id,)).fetchone()
            if not review:
                raise BusinessError("释放复核单不存在", 404, "not_found")
            self._member(conn, review["case_id"], user_id)
            return self._review_dict(conn, review_id)

    def list_release_reviews(self, user_id, case_id):
        with self.connect() as conn:
            self._member(conn, case_id, user_id)
            rows = conn.execute("SELECT id FROM release_reviews WHERE case_id=? ORDER BY id DESC", (case_id,)).fetchall()
            return [self._review_dict(conn, r["id"]) for r in rows]

    def _check_snapshot(self, conn, item, ev):
        """复核后证据若被改动或保留状态变更，返回作废原因列表。"""
        mismatches = []
        if ev["sha256"] != item["snap_sha256"]:
            mismatches.append("证据内容哈希被改动")
        if ev["legal_hold"] != item["snap_legal_hold"]:
            mismatches.append("保留状态变更：法律保留状态已改变")
        if ev["in_transit"] != item["snap_in_transit"]:
            mismatches.append("保管状态变更：证据正在移交（在途）")
        if ev["current_custodian"] != item["snap_current_custodian"]:
            mismatches.append("保管人已变更")
        if ev["retention_until"] != item["snap_retention_until"]:
            mismatches.append("保留期限被改动")
        event_count = conn.execute("SELECT COUNT(*) AS c FROM custody_events WHERE evidence_id=?", (ev["id"],)).fetchone()["c"]
        if event_count != item["snap_event_count"]:
            mismatches.append("保管记录新增变动")
        else:
            last = conn.execute(
                "SELECT event_hash FROM custody_events WHERE evidence_id=? ORDER BY sequence DESC LIMIT 1", (ev["id"],)
            ).fetchone()
            last_hash = last["event_hash"] if last else "GENESIS"
            if last_hash != item["snap_last_event_hash"]:
                mismatches.append("保管链被改动")
        deriv_count = conn.execute("SELECT COUNT(*) AS c FROM derivatives WHERE parent_evidence_id=?", (ev["id"],)).fetchone()["c"]
        if deriv_count != item["snap_derivative_count"]:
            mismatches.append("派生关系变更")
        return mismatches

    def _mark_released(self, conn, item, recipient, user_id, note, result_reason):
        conn.execute(
            """UPDATE release_review_items SET state='released', recipient=?, actor_id=?, release_note=?,
               result_reason=?, processed_at=?, updated_at=? WHERE id=?""",
            (recipient, user_id, note, result_reason, now(), now(), item["id"]),
        )

    def _void_item(self, conn, item, reason):
        conn.execute(
            "UPDATE release_review_items SET state='void', result_reason=?, processed_at=?, updated_at=? WHERE id=?",
            (reason, now(), now(), item["id"]),
        )

    def _skip_item(self, conn, item, reason):
        conn.execute(
            "UPDATE release_review_items SET state='skipped', result_reason=?, processed_at=?, updated_at=? WHERE id=?",
            (reason, now(), now(), item["id"]),
        )

    def submit_releases(self, user_id, review_id, recipient, note="", evidence_ids=None):
        """按复核单提交释放。幂等：已释放/已作废/已跳过的条目不再处理；崩溃后重启可再次调用继续。"""
        recipient = recipient.strip()
        if not recipient:
            raise BusinessError("接收方不能为空", 422, "recipient_required")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                review = conn.execute("SELECT * FROM release_reviews WHERE id=?", (review_id,)).fetchone()
                if not review:
                    raise BusinessError("释放复核单不存在", 404, "not_found")
                self._member(conn, review["case_id"], user_id, {"custodian"})
                if review["status"] == "cancelled":
                    raise BusinessError("复核单已作废，不能提交释放", 409, "review_cancelled")
                sql = "SELECT * FROM release_review_items WHERE review_id=?"
                params = [review_id]
                if evidence_ids:
                    sql += " AND evidence_id IN (%s)" % ",".join("?" * len(evidence_ids))
                    params += list(evidence_ids)
                sql += " ORDER BY id"
                items = conn.execute(sql, params).fetchall()
                summary = {"review_id": review_id, "released": [], "voided": [], "skipped": [], "reconciled": [], "unchanged": []}
                for item in items:
                    if item["state"] in ("released", "void", "skipped"):
                        summary["unchanged"].append({"evidence_id": item["evidence_id"], "state": item["state"]})
                        continue
                    ev = conn.execute("SELECT * FROM evidence WHERE id=?", (item["evidence_id"],)).fetchone()
                    if not ev:
                        self._void_item(conn, item, "证据不存在")
                        summary["voided"].append({"evidence_id": item["evidence_id"], "reason": "证据不存在"})
                        continue
                    if not item["eligible"]:
                        self._skip_item(conn, item, item["reason"])
                        summary["skipped"].append({"evidence_id": item["evidence_id"], "reason": item["reason"]})
                        continue
                    if ev["status"] == "released":
                        # 崩溃恢复或经其他途径已释放：对账补齐，不重复写入
                        self._mark_released(conn, item, recipient, user_id, note, "证据已释放（系统内已有释放记录）")
                        summary["reconciled"].append({"evidence_id": item["evidence_id"]})
                        continue
                    mismatches = self._check_snapshot(conn, item, ev)
                    if mismatches:
                        reason = "；".join(mismatches)
                        self._void_item(conn, item, reason)
                        summary["voided"].append({"evidence_id": item["evidence_id"], "reason": reason})
                        continue
                    if ev["legal_hold"]:
                        self._void_item(conn, item, "保留状态变更：证据现处于法律保留中")
                        summary["voided"].append({"evidence_id": item["evidence_id"], "reason": "法律保留中"})
                        continue
                    if ev["in_transit"]:
                        self._void_item(conn, item, "保管状态变更：证据正在移交（在途）")
                        summary["voided"].append({"evidence_id": item["evidence_id"], "reason": "正在移交（在途）"})
                        continue
                    self._release_in_tx(conn, user_id, ev["id"], recipient, note)
                    self._mark_released(conn, item, recipient, user_id, note, "已释放")
                    summary["released"].append({"evidence_id": ev["id"], "recipient": recipient})
                pending = conn.execute(
                    "SELECT COUNT(*) AS c FROM release_review_items WHERE review_id=? AND state='pending'", (review_id,)
                ).fetchone()["c"]
                new_status = "completed" if pending == 0 else "executing"
                conn.execute("UPDATE release_reviews SET status=?, updated_at=? WHERE id=?", (new_status, now(), review_id))
                self._audit(conn, review["case_id"], user_id, "release_review.submit",
                            {"review_id": review_id, "released": len(summary["released"]),
                             "voided": len(summary["voided"]), "skipped": len(summary["skipped"]),
                             "reconciled": len(summary["reconciled"])})
                summary["status"] = new_status
                return summary
            except Exception:
                conn.rollback()
                raise

    def report(self, user_id, case_id):
        with self.connect() as conn:
            self._member(conn, case_id, user_id)
            case = self._case(conn, case_id)
            items, all_valid = [], True
            for row in conn.execute("SELECT * FROM evidence WHERE case_id=? ORDER BY id", (case_id,)).fetchall():
                hash_valid = hashlib.sha256(row["content"]).hexdigest() == row["sha256"]
                events = conn.execute("SELECT * FROM custody_events WHERE evidence_id=? ORDER BY sequence", (row["id"],)).fetchall()
                expected_prev, chain_valid = "GENESIS", True
                for e in events:
                    payload = {
                        "evidence_id": e["evidence_id"], "sequence": e["sequence"], "event_type": e["event_type"],
                        "actor_id": e["actor_id"], "from_person": e["from_person"], "to_person": e["to_person"],
                        "location": e["location"], "note": e["note"], "previous_hash": e["previous_hash"], "created_at": e["created_at"],
                    }
                    if e["previous_hash"] != expected_prev or self._event_hash(payload) != e["event_hash"]:
                        chain_valid = False
                    expected_prev = e["event_hash"]
                all_valid = all_valid and hash_valid and chain_valid
                items.append({
                    "id": row["id"], "label": row["label"], "filename": row["filename"], "sha256": row["sha256"],
                    "size": row["size"], "status": row["status"], "current_custodian": row["current_custodian"],
                    "legal_hold": bool(row["legal_hold"]), "retention_until": row["retention_until"],
                    "hash_valid": hash_valid, "chain_valid": chain_valid,
                    "events": [dict(e) for e in events],
                    "derivatives": [dict(x) for x in conn.execute("SELECT * FROM derivatives WHERE parent_evidence_id=? ORDER BY id", (row["id"],)).fetchall()],
                })
            audit = conn.execute("SELECT * FROM audit_log WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
            release_reviews = []
            release_review_consistent = True
            for r in conn.execute("SELECT * FROM release_reviews WHERE case_id=? ORDER BY id", (case_id,)).fetchall():
                rd = self._review_dict(conn, r["id"])
                release_reviews.append(rd)
                if rd["review"]["status"] == "completed":
                    for it in rd["items"]:
                        if it["state"] == "pending":
                            release_review_consistent = False
                        ev = it["evidence"]
                        if ev and (it["state"] == "released") != (ev["status"] == "released"):
                            release_review_consistent = False
            return {
                "case": dict(case), "generated_at": now(), "overall_integrity_valid": all_valid,
                "evidence_count": len(items), "evidence": items,
                "release_review_count": len(release_reviews),
                "release_review_consistent": release_review_consistent,
                "release_reviews": release_reviews,
                "audit": [dict(a) | {"detail": json.loads(a["detail"])} for a in audit],
            }


class Handler(BaseHTTPRequestHandler):
    server_version = "EvidenceCustody/1.0"
    def _store(self): return self.server.store  # type: ignore[attr-defined]
    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try: data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError): raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict): raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data
    def _send(self, status, payload):
        body=json.dumps(payload,ensure_ascii=False).encode(); self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def _dispatch(self, method):
        path=urlparse(self.path).path.rstrip("/") or "/"; parts=[p for p in path.split("/") if p]
        user=self.headers.get("X-User-Id",""); store=self._store()
        if method=="GET" and path=="/":
            body=(BASE_DIR/"web"/"index.html").read_bytes(); self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if method=="GET" and path=="/health": return self._send(200,{"ok":True})
        if parts==["api","cases"] and method=="POST":
            d=self._body(); return self._send(201,store.create_case(user,d.get("case_number",""),d.get("title","")))
        if len(parts)==4 and parts[:2]==["api","cases"] and parts[3]=="members" and method=="POST":
            d=self._body(); return self._send(201,store.add_member(user,int(parts[2]),d.get("user_id",""),d.get("role","")))
        if len(parts)==4 and parts[:2]==["api","cases"] and parts[3]=="evidence" and method=="POST":
            d=self._body(); return self._send(201,store.ingest_evidence(user,int(parts[2]),d.get("label",""),d.get("filename",""),d.get("content_b64",""),d.get("retention_until",""),d.get("custodian")))
        if len(parts)==4 and parts[:2]==["api","cases"] and parts[3]=="report" and method=="GET":
            return self._send(200,store.report(user,int(parts[2])))
        if len(parts)==4 and parts[:2]==["api","cases"] and parts[3]=="release-reviews":
            if method=="POST":
                d=self._body(); return self._send(201,store.create_release_review(user,int(parts[2]),d.get("note","")))
            if method=="GET": return self._send(200,store.list_release_reviews(user,int(parts[2])))
        if len(parts)==3 and parts[:2]==["api","release-reviews"] and method=="GET":
            return self._send(200,store.get_release_review(user,int(parts[2])))
        if len(parts)==4 and parts[:2]==["api","release-reviews"] and parts[3]=="submit" and method=="POST":
            d=self._body(); return self._send(200,store.submit_releases(user,int(parts[2]),d.get("recipient",""),d.get("note",""),d.get("evidence_ids")))
        if len(parts)>=3 and parts[:2]==["api","evidence"]:
            evidence_id=int(parts[2])
            if len(parts)==3 and method=="GET": return self._send(200,store.get_evidence(user,evidence_id,bool(urlparse(self.path).query)))
            if len(parts)==4 and method=="POST":
                d=self._body()
                if parts[3]=="transfer": return self._send(200,store.transfer(user,evidence_id,d.get("to_person",""),d.get("location",""),d.get("note","")))
                if parts[3]=="open": return self._send(200,store.open_evidence(user,evidence_id,d.get("location",""),d.get("note","")))
                if parts[3]=="derive": return self._send(201,store.derive(user,evidence_id,d.get("method",""),d.get("label",""),d.get("filename",""),d.get("content_b64","")))
                if parts[3]=="release": return self._send(200,store.release(user,evidence_id,d.get("recipient",""),d.get("note","")))
                if parts[3]=="hold": return self._send(200,store.set_hold(user,evidence_id,bool(d.get("hold")),d.get("reason","")))
                if parts[3]=="dispatch": return self._send(200,store.dispatch(user,evidence_id,d.get("to_person",""),d.get("location",""),d.get("note","")))
                if parts[3]=="accept": return self._send(200,store.accept(user,evidence_id,d.get("note","")))
        raise BusinessError("接口不存在",404,"not_found")
    def _handle(self, method):
        try: self._dispatch(method)
        except BusinessError as exc: self._send(exc.status,{"error":{"code":exc.code,"message":exc.message}})
        except (ValueError,TypeError): self._send(400,{"error":{"code":"invalid_path","message":"路径参数格式错误"}})
        except Exception as exc: self._send(500,{"error":{"code":"internal_error","message":str(exc)}})
    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_DELETE(self): self._send(405,{"error":{"code":"immutable_audit","message":"证据和保管记录不提供删除接口"}})
    def log_message(self, fmt, *args): print(f"{self.address_string()} - {fmt % args}")


class CustodyServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,store): self.store=store; super().__init__(address,Handler)


def main():
    parser=argparse.ArgumentParser(description="法律证据保管与流转后台")
    parser.add_argument("--db",default=str(DEFAULT_DB)); parser.add_argument("--port",type=int,default=8105)
    parser.add_argument("--init",action="store_true"); parser.add_argument("--seed",action="store_true")
    args=parser.parse_args(); store=CustodyStore(args.db); store.init_schema()
    if args.seed: store.seed()
    if args.init or args.seed: print(f"数据库已初始化: {args.db}"); return
    server=CustodyServer(("127.0.0.1",args.port),store); print(f"证据保管系统运行于 http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__=="__main__": main()
