"""住房贷款纾困申请与履约跟踪领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, date_text, integer, number, text, text_list


INITIAL_STATE = "submitted"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'assess': {'intake_officer'}, 'approve': {'underwriter'}, 'activate': {'servicer'}, 'cure': {'servicer'}, 'default': {'servicer'}}
TRANSITIONS = {'assess': {'submitted': 'assessed'}, 'approve': {'assessed': 'approved'}, 'activate': {'approved': 'active'}, 'cure': {'active': 'cured'}, 'default': {'active': 'defaulted'}}
PLAN_CHANGE_REQUEST_ROLES = {'servicer'}
PLAN_CHANGE_REVIEW_ROLES = {'underwriter'}
PLAN_CHANGE_STATUSES = ('pending', 'approved', 'rejected')


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_request_plan_change(self, role: str) -> bool:
        return role == "admin" or role in PLAN_CHANGE_REQUEST_ROLES

    def role_can_review_plan_change(self, role: str) -> bool:
        return role == "admin" or role in PLAN_CHANGE_REVIEW_ROLES

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        income = number(p, "monthly_income", 1)
        number(p, "monthly_expenses", 0)
        payment = number(p, "monthly_payment", 0)
        number(p, "arrears", 0)
        number(p, "hardship_factor", 0, 1)
        choice(p, "program_type", ["deferral", "reduction", "restructure"])
        integer(p, "requested_months", 1, 24)
        if p["monthly_expenses"] >= income:
            raise ValidationError("支出不能达到或超过收入")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        income = float(p["monthly_income"])
        disposable = income - float(p["monthly_expenses"])
        ratio = float(p["monthly_payment"]) / income
        months = min(int(p["requested_months"]), 12)
        if p["program_type"] == "deferral":
            proposed = 0.0
        elif p["program_type"] == "reduction":
            proposed = max(0.0, float(p["monthly_payment"]) - disposable * 0.4)
        else:
            proposed = max(float(p["monthly_payment"]) * 0.7, disposable * 0.25)
        p["disposable_income"] = round(disposable, 2)
        p["housing_ratio"] = round(ratio, 3)
        p["eligible_months"] = months
        p["proposed_payment"] = round(proposed, 2)
        p["risk_score"] = round(min(100.0, ratio * 60 + float(p["hardship_factor"]) * 40), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "approved", "assessed"} and item["payload"].get("borrower_id") == payload.get("borrower_id"):
                raise Conflict("该借款人已有处理中纾困申请")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "assess":
            changes["assessment_note"] = text(data, "assessment_note")
            changes["eligibility"] = bool(float(p["housing_ratio"]) <= 0.8 and float(p["arrears"]) <= float(p["monthly_payment"]) * 6)
            summary = "偿付能力评估完成"
        elif action == "approve":
            exception = boolean(data, "exception_approved")
            if not p.get("eligibility") and not exception:
                raise ValidationError("不符合纾困资格且无例外批准")
            changes["approved_program"] = p["program_type"]
            changes["approved_months"] = int(p["eligible_months"])
            changes["approved_payment"] = float(p["proposed_payment"])
            changes["current_payment"] = float(p["proposed_payment"])
            changes["current_remaining_months"] = int(p["eligible_months"])
            changes["plan_revision"] = 0
            changes["exception_approved"] = exception
            summary = "纾困方案批准"
        elif action == "activate":
            if not boolean(data, "borrower_ack"):
                raise ValidationError("借款人尚未确认方案")
            changes["borrower_ack"] = True
            summary = "纾困方案生效"
        elif action == "cure":
            if not boolean(data, "arrears_cleared"):
                raise ValidationError("欠款尚未清偿")
            changes["arrears_cleared"] = True
            summary = "贷款恢复正常"
        elif action == "default":
            changes["default_reason"] = text(data, "default_reason")
            summary = "纾困方案违约"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    def current_plan(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if "approved_payment" not in payload:
            return None
        return {
            "payment": float(payload.get("current_payment", payload["approved_payment"])),
            "remaining_months": int(payload.get("current_remaining_months", payload["approved_months"])),
            "original_months": int(payload["approved_months"]),
            "revision": int(payload.get("plan_revision", 0)),
        }

    def prepare_plan_change(self, record: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        if record["state"] != "active":
            raise Conflict("仅履约中的纾困方案可申请变更")
        plan = self.current_plan(record["payload"])
        if plan is None:
            raise Conflict("当前记录尚无生效方案")
        return {
            "new_payment": number(data, "new_payment", 0),
            "new_remaining_months": integer(data, "new_remaining_months", 1),
            "reason": text(data, "reason"),
            "effective_date": date_text(data, "effective_date"),
            "before_payment": plan["payment"],
            "before_remaining_months": plan["remaining_months"],
        }

    def decide_plan_change(self, record: Dict[str, Any], change: Dict[str, Any], decision: str, note: str) -> Tuple[str, str, Dict[str, Any], Dict[str, Any]]:
        plan = self.current_plan(record["payload"])
        if plan is None:
            raise Conflict("当前记录尚无生效方案")
        before = {"payment": plan["payment"], "remaining_months": plan["remaining_months"]}
        requested = {"payment": float(change["new_payment"]), "remaining_months": int(change["new_remaining_months"])}
        note = note.strip()
        if decision == "approve":
            violations = []
            if requested["payment"] > before["payment"]:
                violations.append("新月供高于现方案")
            if requested["remaining_months"] > plan["original_months"]:
                violations.append("剩余期数超过原期限")
            if violations:
                status = "rejected"
                note = "；".join(violations) + ("；" + note if note else "")
            else:
                status = "approved"
        else:
            status = "rejected"
            note = note or "审批人驳回"
        new_payload = dict(record["payload"])
        if status == "approved":
            new_payload["current_payment"] = requested["payment"]
            new_payload["current_remaining_months"] = requested["remaining_months"]
            new_payload["current_plan_effective_date"] = change["effective_date"]
            new_payload["plan_revision"] = plan["revision"] + 1
            after = dict(requested)
        else:
            after = dict(before)
        details = {
            "summary": "方案变更通过" if status == "approved" else "方案变更驳回",
            "change_id": int(change["id"]),
            "before": before,
            "after": after,
            "requested": requested,
            "reason": change["reason"],
            "effective_date": change["effective_date"],
            "decision_note": note,
        }
        return status, note, new_payload, details
