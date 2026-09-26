import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9, 'borrower_id': 'B-1'}
PRE_FLOW = [('assess', 'intake_officer', {'assessment_note': '收入波动'}), ('approve', 'underwriter', {'exception_approved': False}), ('activate', 'servicer', {'borrower_ack': True})]
# 现行方案：月供3400.0，剩余期数9，原期限9
CHANGE_DATA = {'new_payment': 3000.0, 'new_remaining_months': 9, 'reason': '收入下降', 'effective_date': '2026-10-01'}


class PlanChangeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-27001", CREATE_DATA)
        for action, role, data in PRE_FLOW:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
        self.record = record

    def tearDown(self):
        self.temp.cleanup()

    def _request(self, **overrides):
        data = dict(CHANGE_DATA)
        data.update(overrides)
        return self.service.request_plan_change(Actor("serv", "servicer"), self.record["id"], data)

    def _review(self, change, decision, note="", version=None):
        record = self.service.get_record(Actor("serv", "servicer"), self.record["id"])
        data = {"decision": decision}
        if note:
            data["decision_note"] = note
        return self.service.review_plan_change(Actor("uw", "underwriter"), self.record["id"], change["id"], version if version is not None else record["version"], data)

    def test_approve_applies_new_plan_and_audit(self):
        change = self._request()
        self.assertEqual(change["status"], "pending")
        self.assertEqual(change["before_payment"], 3400.0)
        change = self._review(change, "approve")
        self.assertEqual(change["status"], "approved")
        self.assertEqual(change["decided_by"], "uw")
        record = self.service.get_record(Actor("serv", "servicer"), self.record["id"])
        self.assertEqual(record["state"], "active")
        self.assertEqual(record["payload"]["current_payment"], 3000.0)
        self.assertEqual(record["payload"]["current_remaining_months"], 9)
        self.assertEqual(record["payload"]["plan_revision"], 1)
        self.assertEqual(record["current_plan"]["payment"], 3000.0)
        self.assertEqual(record["current_plan"]["original_months"], 9)
        timeline = self.service.timeline(Actor("serv", "servicer"), self.record["id"])
        requested = [e for e in timeline if e["action"] == "plan_change_requested"]
        approved = [e for e in timeline if e["action"] == "plan_change_approved"]
        self.assertEqual(len(requested), 1)
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["details"]["before"], {"payment": 3400.0, "remaining_months": 9})
        self.assertEqual(approved[0]["details"]["after"], {"payment": 3000.0, "remaining_months": 9})
        self.assertEqual(len(self.service.list_plan_changes(Actor("serv", "servicer"), status="approved")), 1)
        self.assertEqual(self.service.list_plan_changes(Actor("serv", "servicer"), status="pending"), [])

    def test_single_pending_limit(self):
        self._request()
        with self.assertRaises(Conflict):
            self._request()

    def test_non_compliant_approve_is_rejected(self):
        change = self._request(new_payment=5000.0)
        change = self._review(change, "approve")
        self.assertEqual(change["status"], "rejected")
        self.assertIn("新月供高于现方案", change["decision_note"])
        record = self.service.get_record(Actor("serv", "servicer"), self.record["id"])
        self.assertEqual(record["payload"]["current_payment"], 3400.0)
        self.assertEqual(record["payload"]["plan_revision"], 0)
        change = self._request(new_remaining_months=12)
        change = self._review(change, "approve")
        self.assertEqual(change["status"], "rejected")
        self.assertIn("剩余期数超过原期限", change["decision_note"])
        rejected = self.service.list_plan_changes(Actor("uw", "underwriter"), record_id=self.record["id"], status="rejected")
        self.assertEqual(len(rejected), 2)

    def test_explicit_reject_keeps_plan(self):
        change = self._request()
        change = self._review(change, "reject", "材料不足")
        self.assertEqual(change["status"], "rejected")
        self.assertEqual(change["decision_note"], "材料不足")
        record = self.service.get_record(Actor("serv", "servicer"), self.record["id"])
        self.assertEqual(record["payload"]["current_payment"], 3400.0)
        timeline = self.service.timeline(Actor("serv", "servicer"), self.record["id"])
        rejected = [e for e in timeline if e["action"] == "plan_change_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["details"]["before"], rejected[0]["details"]["after"])

    def test_sequential_change_uses_updated_current_plan(self):
        change = self._review(self._request(), "approve")
        self.assertEqual(change["status"], "approved")
        change = self._request(new_payment=3200.0)
        change = self._review(change, "approve")
        self.assertEqual(change["status"], "rejected")
        change = self._request(new_payment=2500.0, new_remaining_months=6)
        change = self._review(change, "approve")
        self.assertEqual(change["status"], "approved")
        record = self.service.get_record(Actor("serv", "servicer"), self.record["id"])
        self.assertEqual(record["current_plan"]["payment"], 2500.0)
        self.assertEqual(record["current_plan"]["remaining_months"], 6)
        self.assertEqual(record["current_plan"]["revision"], 2)

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.request_plan_change(Actor("uw", "underwriter"), self.record["id"], dict(CHANGE_DATA))
        change = self._request()
        with self.assertRaises(PermissionDenied):
            self.service.review_plan_change(Actor("serv", "servicer"), self.record["id"], change["id"], self.record["version"], {"decision": "approve"})
        with self.assertRaises(PermissionDenied):
            self.service.list_plan_changes(Actor("x", "outsider"))

    def test_request_requires_active_state(self):
        data = dict(CREATE_DATA)
        data["borrower_id"] = "B-2"
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-27002", data)
        with self.assertRaises(Conflict):
            self.service.request_plan_change(Actor("serv", "servicer"), record["id"], dict(CHANGE_DATA))

    def test_stale_version_and_double_review(self):
        change = self._request()
        with self.assertRaises(Conflict):
            self._review(change, "approve", version=self.record["version"] + 5)
        change = self._review(change, "approve")
        with self.assertRaises(Conflict):
            self._review(change, "reject")

    def test_validation_errors(self):
        with self.assertRaises(ValidationError):
            self._request(reason="")
        with self.assertRaises(ValidationError):
            self._request(effective_date="2026/10/01")
        with self.assertRaises(ValidationError):
            self._request(new_remaining_months=0)
        with self.assertRaises(ValidationError):
            self._request(new_payment=-1)
        change = self._request()
        with self.assertRaises(ValidationError):
            self._review(change, "maybe")
        with self.assertRaises(ValidationError):
            self.service.list_plan_changes(Actor("serv", "servicer"), status="unknown")
