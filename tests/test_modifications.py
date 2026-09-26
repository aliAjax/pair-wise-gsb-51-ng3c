import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9}
# reduction方案：proposed_payment = 7000 - (18000-9000)*0.4 = 3400，approved_months = 9
MOD_DATA = {'new_payment': 3000.0, 'remaining_months': 6, 'reason': '收入下降', 'effective_date': '2026-10-01'}


def make_active(service, reference="MORT-27001"):
    record = service.create(Actor("creator", "intake_officer"), reference, CREATE_DATA)
    record = service.act(Actor("op", "intake_officer"), record["id"], record["version"], "assess", {"assessment_note": "收入波动"})
    record = service.act(Actor("op", "underwriter"), record["id"], record["version"], "approve", {"exception_approved": False})
    record = service.act(Actor("op", "servicer"), record["id"], record["version"], "activate", {"borrower_ack": True})
    return record


class ModificationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_submit_and_approve_updates_current_plan(self):
        record = make_active(self.service)
        self.assertEqual(record["payload"]["current_payment"], 3400.0)
        self.assertEqual(record["payload"]["current_remaining_months"], 9)
        mod = self.service.submit_modification(Actor("svc", "servicer"), record["id"], MOD_DATA)
        self.assertEqual(mod["status"], "pending")
        self.assertEqual(mod["reason"], "收入下降")
        self.assertEqual(mod["effective_date"], "2026-10-01")
        reviewed = self.service.review_modification(Actor("uw", "underwriter"), record["id"], mod["id"], {"approve": True, "review_note": "同意"})
        self.assertEqual(reviewed["status"], "approved")
        self.assertEqual(reviewed["reviewed_by"], "uw")
        detail = self.service.get_record(Actor("svc", "servicer"), record["id"])
        self.assertEqual(detail["payload"]["current_payment"], 3000.0)
        self.assertEqual(detail["payload"]["current_remaining_months"], 6)
        self.assertEqual(detail["payload"]["current_effective_date"], "2026-10-01")
        timeline = self.service.timeline(Actor("svc", "servicer"), record["id"])
        actions = [event["action"] for event in timeline]
        self.assertIn("modification_submitted", actions)
        self.assertIn("modification_approved", actions)
        approved_event = next(event for event in timeline if event["action"] == "modification_approved")
        self.assertEqual(approved_event["details"]["before"], {"payment": 3400.0, "remaining_months": 9})
        self.assertEqual(approved_event["details"]["after"], {"payment": 3000.0, "remaining_months": 6})

    def test_only_one_pending_per_record(self):
        record = make_active(self.service)
        self.service.submit_modification(Actor("svc", "servicer"), record["id"], MOD_DATA)
        with self.assertRaises(Conflict):
            self.service.submit_modification(Actor("svc", "servicer"), record["id"], MOD_DATA)

    def test_approve_higher_payment_is_rejected_and_plan_kept(self):
        record = make_active(self.service)
        data = dict(MOD_DATA, new_payment=5000.0)
        mod = self.service.submit_modification(Actor("svc", "servicer"), record["id"], data)
        reviewed = self.service.review_modification(Actor("uw", "underwriter"), record["id"], mod["id"], {"approve": True})
        self.assertEqual(reviewed["status"], "rejected")
        self.assertIn("新月供高于现方案", reviewed["review_note"])
        detail = self.service.get_record(Actor("svc", "servicer"), record["id"])
        self.assertEqual(detail["payload"]["current_payment"], 3400.0)
        self.assertEqual(detail["payload"]["current_remaining_months"], 9)
        timeline = self.service.timeline(Actor("svc", "servicer"), record["id"])
        self.assertIn("modification_rejected", [event["action"] for event in timeline])

    def test_approve_longer_term_is_rejected(self):
        record = make_active(self.service)
        data = dict(MOD_DATA, remaining_months=24)
        mod = self.service.submit_modification(Actor("svc", "servicer"), record["id"], data)
        reviewed = self.service.review_modification(Actor("uw", "underwriter"), record["id"], mod["id"], {"approve": True})
        self.assertEqual(reviewed["status"], "rejected")
        self.assertIn("剩余期数超过原期限", reviewed["review_note"])

    def test_explicit_rejection_and_query_by_status(self):
        record = make_active(self.service)
        first = self.service.submit_modification(Actor("svc", "servicer"), record["id"], MOD_DATA)
        rejected = self.service.review_modification(Actor("uw", "underwriter"), record["id"], first["id"], {"approve": False, "review_note": "材料不全"})
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["review_note"], "材料不全")
        # 驳回后可再次登记
        second = self.service.submit_modification(Actor("svc", "servicer"), record["id"], MOD_DATA)
        self.service.review_modification(Actor("uw", "underwriter"), record["id"], second["id"], {"approve": True})
        all_items = self.service.list_modifications(Actor("svc", "servicer"), record["id"])
        self.assertEqual(len(all_items), 2)
        pending = self.service.list_modifications(Actor("svc", "servicer"), record["id"], status="pending")
        approved = self.service.list_modifications(Actor("svc", "servicer"), record["id"], status="approved")
        rejected_items = self.service.list_modifications(Actor("svc", "servicer"), record["id"], status="rejected")
        self.assertEqual(len(pending), 0)
        self.assertEqual([item["id"] for item in approved], [second["id"]])
        self.assertEqual([item["id"] for item in rejected_items], [first["id"]])

    def test_submit_requires_active_state(self):
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-27002", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.submit_modification(Actor("svc", "servicer"), record["id"], MOD_DATA)

    def test_permissions(self):
        record = make_active(self.service)
        with self.assertRaises(PermissionDenied):
            self.service.submit_modification(Actor("clerk", "intake_officer"), record["id"], MOD_DATA)
        mod = self.service.submit_modification(Actor("svc", "servicer"), record["id"], MOD_DATA)
        with self.assertRaises(PermissionDenied):
            self.service.review_modification(Actor("svc", "servicer"), record["id"], mod["id"], {"approve": True})

    def test_invalid_modification_input(self):
        record = make_active(self.service)
        with self.assertRaises(ValidationError):
            self.service.submit_modification(Actor("svc", "servicer"), record["id"], dict(MOD_DATA, reason=""))
        with self.assertRaises(ValidationError):
            self.service.submit_modification(Actor("svc", "servicer"), record["id"], dict(MOD_DATA, effective_date="2026/10/01"))
        with self.assertRaises(ValidationError):
            self.service.submit_modification(Actor("svc", "servicer"), record["id"], dict(MOD_DATA, remaining_months=0))

    def test_double_review_is_rejected(self):
        record = make_active(self.service)
        mod = self.service.submit_modification(Actor("svc", "servicer"), record["id"], MOD_DATA)
        self.service.review_modification(Actor("uw", "underwriter"), record["id"], mod["id"], {"approve": True})
        with self.assertRaises(Conflict):
            self.service.review_modification(Actor("uw", "underwriter"), record["id"], mod["id"], {"approve": True})

    def test_second_modification_uses_updated_plan_as_baseline(self):
        record = make_active(self.service)
        first = self.service.submit_modification(Actor("svc", "servicer"), record["id"], MOD_DATA)
        self.service.review_modification(Actor("uw", "underwriter"), record["id"], first["id"], {"approve": True})
        # 现方案月供已降至3000，再次申请3200应被驳回
        second = self.service.submit_modification(Actor("svc", "servicer"), record["id"], dict(MOD_DATA, new_payment=3200.0))
        reviewed = self.service.review_modification(Actor("uw", "underwriter"), record["id"], second["id"], {"approve": True})
        self.assertEqual(reviewed["status"], "rejected")
        detail = self.service.get_record(Actor("svc", "servicer"), record["id"])
        self.assertEqual(detail["payload"]["current_payment"], 3000.0)
