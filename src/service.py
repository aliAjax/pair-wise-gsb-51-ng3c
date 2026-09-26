"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, choice, text
from .repository import Repository
from .rules import MODIFICATION_STATUSES, DomainRules


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
        return self.repository.get(record_id)

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

    def submit_modification(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_submit_modification(actor.role):
            raise PermissionDenied("角色无权登记方案变更")
        record = self.repository.get(record_id)
        self.rules.check_modification_allowed(record)
        prepared = self.rules.validate_modification(data or {})
        if self.repository.list_modifications(record_id, status="pending"):
            raise Conflict("该贷款已有待审的方案变更申请")
        return self.repository.create_modification(record_id, prepared, actor.user_id)

    def list_modifications(self, actor: Actor, record_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if status is not None:
            status = choice({"status": status}, "status", list(MODIFICATION_STATUSES))
        return self.repository.list_modifications(record_id, status=status)

    def review_modification(self, actor: Actor, record_id: int, modification_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_review_modification(actor.role):
            raise PermissionDenied("角色无权审批方案变更")
        record = self.repository.get(record_id)
        modification = self.repository.get_modification(record_id, modification_id)
        if modification["status"] != "pending":
            raise Conflict("该申请已审批")
        decision = self.rules.validate_review_decision(data or {})
        note = decision["review_note"]
        if decision["approve"]:
            problems = self.rules.evaluate_modification(record, modification)
            if not problems:
                new_payload, details = self.rules.apply_modification(record, modification)
                details["summary"] = "方案变更通过"
                details["review_note"] = note
                return self.repository.review_modification(record_id, modification_id, "approved", actor.user_id, note, new_payload, "modification_approved", details)
            note = (note + "；" if note else "") + "；".join(problems)
            details = self.rules.modification_rejection_details(record, modification, problems)
        else:
            details = self.rules.modification_rejection_details(record, modification)
        details["summary"] = "方案变更驳回"
        details["review_note"] = note
        return self.repository.review_modification(record_id, modification_id, "rejected", actor.user_id, note, None, "modification_rejected", details)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
