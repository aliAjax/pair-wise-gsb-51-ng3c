"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS modifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    new_payment REAL NOT NULL,
                    remaining_months INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    effective_date TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    reviewed_by TEXT,
                    review_note TEXT,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_modifications_record ON modifications(record_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_modifications_pending ON modifications(record_id) WHERE status='pending';
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def create_modification(self, record_id: int, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
                if row is None:
                    connection.rollback()
                    raise NotFound("记录不存在")
                cursor = connection.execute(
                    "INSERT INTO modifications(record_id,status,new_payment,remaining_months,reason,effective_date,requested_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (record_id, "pending", float(data["new_payment"]), int(data["remaining_months"]), data["reason"], data["effective_date"], actor_id, now),
                )
                modification_id = int(cursor.lastrowid)
                details = {"summary": "方案变更申请登记", "modification_id": modification_id, "request": data}
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "modification_submitted", actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), now),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("该贷款已有待审的方案变更申请") from exc
        return self.get_modification(record_id, modification_id)

    def get_modification(self, record_id: int, modification_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM modifications WHERE id=? AND record_id=?", (modification_id, record_id)).fetchone()
        if row is None:
            raise NotFound("方案变更申请不存在")
        return dict(row)

    def list_modifications(self, record_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            if status:
                rows = connection.execute("SELECT * FROM modifications WHERE record_id=? AND status=? ORDER BY id DESC", (record_id, status)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM modifications WHERE record_id=? ORDER BY id DESC", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def review_modification(self, record_id: int, modification_id: int, status: str, reviewer_id: str, review_note: str, new_payload: Optional[Dict[str, Any]], audit_action: str, audit_details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record_row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if record_row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            mod_row = connection.execute("SELECT status FROM modifications WHERE id=? AND record_id=?", (modification_id, record_id)).fetchone()
            if mod_row is None:
                connection.rollback()
                raise NotFound("方案变更申请不存在")
            if mod_row["status"] != "pending":
                connection.rollback()
                raise Conflict("该申请已审批")
            connection.execute(
                "UPDATE modifications SET status=?,reviewed_by=?,review_note=?,reviewed_at=? WHERE id=?",
                (status, reviewer_id, review_note, now, modification_id),
            )
            version = int(record_row["version"])
            if new_payload is not None:
                version += 1
                connection.execute(
                    "UPDATE records SET version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                    (version, json.dumps(new_payload, ensure_ascii=False, sort_keys=True), reviewer_id, now, record_id),
                )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, audit_action, reviewer_id, version, json.dumps(audit_details, ensure_ascii=False, sort_keys=True), now),
            )
            connection.commit()
        return self.get_modification(record_id, modification_id)

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
