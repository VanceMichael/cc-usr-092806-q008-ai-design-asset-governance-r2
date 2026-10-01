"""素材登记：指纹去重、许可陈述、撤回与品牌限制。

素材记录把文件指纹、来源、训练与再利用许可、署名条件、模型或工具版本、
适用产品、地域、发布渠道和授权到期时间关联起来。相同指纹再次上传返回原素材；
指纹一致但许可陈述冲突时转入复核。授权撤回和品牌限制更新通过定时任务传播，
只阻止尚未发布的采用组合。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .errors import ConflictError, NotFoundError, ValidationError
from .jobs import JobQueue
from .outbox import Outbox
from .repository import EntityRepository
from .security import AccessContext, redact_record
from .timeutil import canonical_instant


ENTITY_TYPE = "assets"
STATES = ("registered", "review", "active", "withdrawn", "expired")
FIELDS = (
    "digest",
    "source",
    "license_training",
    "license_reuse",
    "attribution",
    "tool_version",
    "allowed_products",
    "allowed_territories",
    "allowed_channels",
    "license_valid_to",
)
RESTRICTED_FIELDS = ("source", "submitter")
TRANSITIONS = {
    "registered": {"review", "active", "withdrawn"},
    "review": {"active", "withdrawn"},
    "active": {"review", "withdrawn", "expired"},
    "withdrawn": {"withdrawn"},
    "expired": {"expired"},
}
LICENSE_TRAINING = ("allowed", "internal", "forbidden")
LICENSE_REUSE = ("allowed", "restricted", "forbidden")
SCOPE_FIELDS = ("allowed_products", "allowed_territories", "allowed_channels")
LICENSE_STATEMENT_FIELDS = (
    "license_training",
    "license_reuse",
    "attribution",
    "tool_version",
    "allowed_products",
    "allowed_territories",
    "allowed_channels",
    "license_valid_to",
)


def covers(asset: Mapping[str, object], *, product: str, territory: str, channel: str) -> bool:
    """判断素材当前许可范围是否覆盖指定的产品、地域和渠道。"""
    def allowed(field: str, value: str) -> bool:
        values = asset.get(field) or []
        return "*" in values or value in values
    return (
        allowed("allowed_products", product)
        and allowed("allowed_territories", territory)
        and allowed("allowed_channels", channel)
    )


def license_statement(asset: Mapping[str, object]) -> dict:
    """提取用于冲突比对的许可陈述。"""
    return {field: asset.get(field) for field in LICENSE_STATEMENT_FIELDS}


@dataclass(frozen=True)
class AssetService:
    """封装素材的登记、复核、撤回、限制和查询。"""

    _repository: EntityRepository
    _jobs: JobQueue
    _outbox: Outbox

    @property
    def entity_type(self) -> str:
        return ENTITY_TYPE

    def register(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        """登记素材；相同指纹返回原素材，许可陈述冲突转入复核。"""
        context.require("write:assets")
        payload = self._validate(values, partial=False)
        payload["submitter"] = context.actor_id
        payload["state"] = STATES[0]
        existing = self._find_by_digest(payload["digest"])
        if existing is not None:
            return self._deduplicate(context, existing, payload)
        created = self._repository.create(self.entity_type, payload, actor=context.actor_id, request_key=request_key)
        self._jobs.schedule(job_type="asset-expiry", subject_id=created["entity_id"], run_at=created["license_valid_to"], payload={"asset_id": created["entity_id"]})
        created["deduplicated"] = False
        created["conflict"] = False
        return created

    def activate(self, context: AccessContext, entity_id: str, *, expected_version: int, reason: str, request_key: str) -> dict:
        """复核通过或直接启用素材。"""
        context.require("transition:assets")
        current = self._repository.get(self.entity_type, entity_id)
        if "active" not in TRANSITIONS.get(str(current["state"]), set()):
            raise ConflictError(f"不允许从 {current['state']} 启用")
        if not reason.strip():
            raise ValidationError("状态变更必须说明原因")
        return self._repository.update(self.entity_type, entity_id, {"state": "active", "transition_reason": reason.strip()}, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def withdraw(self, context: AccessContext, entity_id: str, *, expected_version: int, reason: str, request_key: str) -> dict:
        """撤回授权；对采用组合的传播由 asset-withdrawal 任务完成。"""
        context.require("transition:assets")
        current = self._repository.get(self.entity_type, entity_id)
        if "withdrawn" not in TRANSITIONS.get(str(current["state"]), set()):
            raise ConflictError(f"不允许从 {current['state']} 撤回")
        if not reason.strip():
            raise ValidationError("撤回必须说明原因")
        updated = self._repository.update(self.entity_type, entity_id, {"state": "withdrawn", "transition_reason": reason.strip()}, actor=context.actor_id, expected_version=expected_version, request_key=request_key)
        self._jobs.schedule(job_type="asset-withdrawal", subject_id=entity_id, run_at=self._repository.clock.now(), payload={"asset_id": entity_id, "reason": reason.strip()})
        return updated

    def update_scope(self, context: AccessContext, entity_id: str, scope: Mapping[str, object], *, expected_version: int, reason: str, request_key: str) -> dict:
        """更新品牌、地域或渠道限制；传播由 asset-restriction 任务完成。"""
        context.require("transition:assets")
        unknown = set(scope) - set(SCOPE_FIELDS)
        if unknown:
            raise ValidationError("未知字段: " + ", ".join(sorted(unknown)))
        if not scope:
            raise ValidationError("限制更新不能为空")
        if not reason.strip():
            raise ValidationError("限制更新必须说明原因")
        changes = {"transition_reason": reason.strip()}
        for field, value in scope.items():
            changes[field] = self._validate_string_list(field, value)
        updated = self._repository.update(self.entity_type, entity_id, changes, actor=context.actor_id, expected_version=expected_version, request_key=request_key)
        self._jobs.schedule(job_type="asset-restriction", subject_id=entity_id, run_at=self._repository.clock.now(), payload={"asset_id": entity_id, "reason": reason.strip()})
        return updated

    def get(self, context: AccessContext, entity_id: str) -> dict:
        context.require("read:assets")
        record = self._repository.get(self.entity_type, entity_id)
        return redact_record(record, RESTRICTED_FIELDS, context)

    def list_current(self, context: AccessContext, *, state: str | None = None, limit: int = 100) -> list[dict]:
        context.require("read:assets")
        if state is not None and state not in STATES:
            raise ValidationError("未知状态")
        rows = self._repository.list(self.entity_type, state=state, limit=limit)
        return [redact_record(row, RESTRICTED_FIELDS, context) for row in rows]

    def history(self, context: AccessContext, entity_id: str) -> list[dict]:
        context.require("history:assets")
        rows = self._repository.history(self.entity_type, entity_id)
        if not rows:
            raise NotFoundError(f"{self.entity_type}/{entity_id} 不存在")
        return [redact_record(row, RESTRICTED_FIELDS, context) for row in rows]

    def snapshot(self, context: AccessContext, entity_id: str, *, as_of: str) -> dict:
        context.require("history:assets")
        record = self._repository.snapshot(self.entity_type, entity_id, as_of=as_of)
        return redact_record(record, RESTRICTED_FIELDS, context)

    def bulk_get(self, context: AccessContext, entity_ids: Iterable[str]) -> list[dict]:
        context.require("read:assets")
        return [self.get(context, entity_id) for entity_id in dict.fromkeys(entity_ids)]

    def find_by_digest(self, context: AccessContext, digest: str, *, limit: int = 100) -> list[dict]:
        """按文件指纹查询并保持稳定顺序。"""
        context.require("read:assets")
        rows = self._repository.search(self.entity_type, "digest", digest, limit=limit)
        return [redact_record(row, RESTRICTED_FIELDS, context) for row in rows]

    def raw(self, entity_id: str) -> dict:
        """内部读取，不做字段裁剪。"""
        return self._repository.get(self.entity_type, entity_id)

    def _find_by_digest(self, digest: str) -> dict | None:
        rows = self._repository.search(self.entity_type, "digest", digest, limit=1)
        return rows[0] if rows else None

    def _deduplicate(self, context: AccessContext, existing: dict, incoming: dict) -> dict:
        if license_statement(existing) == license_statement(incoming):
            result = dict(existing)
            result["deduplicated"] = True
            result["conflict"] = False
            return result
        note = {
            "at": self._repository.clock.now(),
            "by": context.actor_id,
            "incoming": license_statement(incoming),
            "existing": license_statement(existing),
        }
        notes = list(existing.get("conflict_notes") or [])
        notes.append(note)
        changes = {"conflict_notes": notes}
        if "review" in TRANSITIONS.get(str(existing["state"]), set()):
            changes["state"] = "review"
        updated = self._repository.update(self.entity_type, existing["entity_id"], changes, actor=context.actor_id, expected_version=existing["version"], request_key=f"conflict:{existing['entity_id']}:{len(notes)}")
        self._outbox.enqueue(topic="asset.license-conflict", aggregate_id=existing["entity_id"], payload={"asset_id": existing["entity_id"], "digest": existing["digest"], "reported_by": context.actor_id})
        result = dict(updated)
        result["deduplicated"] = True
        result["conflict"] = True
        return result

    def _validate(self, values: Mapping[str, object], *, partial: bool) -> dict:
        unknown = set(values) - set(FIELDS) - {"state", "transition_reason"}
        if unknown:
            raise ValidationError("未知字段: " + ", ".join(sorted(unknown)))
        if not partial:
            missing = [field for field in FIELDS if field not in values]
            if missing:
                raise ValidationError("缺少字段: " + ", ".join(missing))
        payload = dict(values)
        for key, value in payload.items():
            if value is None:
                raise ValidationError(f"{key} 不能为空")
            if key in SCOPE_FIELDS:
                payload[key] = self._validate_string_list(key, value)
                continue
            if isinstance(value, str):
                payload[key] = value.strip()
                if not payload[key]:
                    raise ValidationError(f"{key} 不能为空字符串")
        if "license_training" in payload and payload["license_training"] not in LICENSE_TRAINING:
            raise ValidationError("license_training 必须是 " + "/".join(LICENSE_TRAINING))
        if "license_reuse" in payload and payload["license_reuse"] not in LICENSE_REUSE:
            raise ValidationError("license_reuse 必须是 " + "/".join(LICENSE_REUSE))
        if "license_valid_to" in payload:
            payload["license_valid_to"] = canonical_instant(str(payload["license_valid_to"]))
        return payload

    @staticmethod
    def _validate_string_list(field: str, value: object) -> list[str]:
        if not isinstance(value, (list, tuple)) or not value:
            raise ValidationError(f"{field} 必须是非空列表")
        result = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValidationError(f"{field} 含有空项")
            result.append(item.strip())
        return result
