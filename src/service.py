"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, choice, optional_text, text
from .repository import Repository
from .rules import PLAN_CHANGE_STATUSES, DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        plan = self.rules.current_plan(record["payload"])
        if plan is not None:
            record["current_plan"] = plan
        return record

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def request_plan_change(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_request_plan_change(actor.role):
            raise PermissionDenied("角色无权登记方案变更")
        record = self.repository.get(record_id)
        prepared = self.rules.prepare_plan_change(record, data or {})
        details = {
            "summary": "方案变更申请登记",
            "requested": {
                "payment": prepared["new_payment"],
                "remaining_months": prepared["new_remaining_months"],
                "reason": prepared["reason"],
                "effective_date": prepared["effective_date"],
            },
            "current_plan": {"payment": prepared["before_payment"], "remaining_months": prepared["before_remaining_months"]},
        }
        return self.repository.create_plan_change(record_id, prepared, actor.user_id, details)

    def list_plan_changes(self, actor: Actor, record_id: Optional[int] = None, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if status is not None:
            status = choice({"status": status}, "status", list(PLAN_CHANGE_STATUSES))
        if record_id is not None:
            self.repository.get(record_id)
        return self.repository.list_plan_changes(record_id=record_id, status=status, limit=limit)

    def get_plan_change(self, actor: Actor, record_id: int, change_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get(record_id)
        return self.repository.get_plan_change(record_id, change_id)

    def review_plan_change(self, actor: Actor, record_id: int, change_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_review_plan_change(actor.role):
            raise PermissionDenied("角色无权审批方案变更")
        data = data or {}
        decision = choice(data, "decision", ["approve", "reject"])
        note = optional_text(data, "decision_note")
        record = self.repository.get(record_id)
        change = self.repository.get_plan_change(record_id, change_id)
        status, note, new_payload, details = self.rules.decide_plan_change(record, change, decision, note)
        return self.repository.decide_plan_change(record_id, change_id, int(expected_version), status, note, new_payload, actor.user_id, details)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
