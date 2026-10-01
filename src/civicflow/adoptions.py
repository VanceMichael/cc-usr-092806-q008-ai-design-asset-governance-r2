"""采用记录：送展与产品采用的封存、审批、发布和后续处置。

每条采用记录把作品、素材、竞赛阶段、发布渠道、适用产品和地域关联起来。
批准时封存素材当时的可见版本作为送审依据；授权撤回或品牌限制更新只阻止
尚未发布的组合，已经进入展览或产品的使用通过替换记录和补充声明说明后续处置。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .assets import AssetService, covers
from .errors import ConflictError, NotFoundError, ValidationError
from .jobs import JobQueue
from .outbox import Outbox
from .repository import EntityRepository
from .security import AccessContext, assert_distinct, redact_record


ENTITY_TYPE = "adoptions"
STATES = ("pending", "approved", "published", "blocked", "replaced", "declared")
FIELDS = ("work_id", "asset_id", "product", "territory", "stage", "channel")
RESTRICTED_FIELDS = ("sealed_license", "approved_by")
TRANSITIONS = {
    "pending": {"approved", "blocked"},
    "approved": {"published", "blocked"},
    "published": {"replaced", "declared"},
    "blocked": {"blocked"},
    "replaced": {"replaced"},
    "declared": {"declared"},
}
SEAL_FIELDS = (
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


@dataclass(frozen=True)
class AdoptionService:
    """封装采用记录的提交、批准、发布、替换、声明和反查。"""

    _repository: EntityRepository
    _assets: AssetService
    _jobs: JobQueue
    _outbox: Outbox

    @property
    def entity_type(self) -> str:
        return ENTITY_TYPE

    def submit(self, context: AccessContext, values: Mapping[str, object], *, request_key: str) -> dict:
        """提交采用组合，进入待审。"""
        context.require("write:adoptions")
        payload = self._validate(values, partial=False)
        self._assets.raw(str(payload["asset_id"]))
        payload["state"] = STATES[0]
        created = self._repository.create(self.entity_type, payload, actor=context.actor_id, request_key=request_key)
        self._jobs.schedule(job_type="adoption-pending", subject_id=created["entity_id"], run_at=self._repository.clock.now(), payload={"adoption_id": created["entity_id"], "work_id": created["work_id"]})
        return created

    def approve(self, context: AccessContext, entity_id: str, *, expected_version: int, request_key: str) -> dict:
        """批准采用并封存素材当时的可见版本；提交人不能放行自己的素材。"""
        context.require("approve:adoptions")
        adoption = self._repository.get(self.entity_type, entity_id)
        if "approved" not in TRANSITIONS.get(str(adoption["state"]), set()):
            raise ConflictError(f"不允许从 {adoption['state']} 批准")
        asset = self._assets.raw(str(adoption["asset_id"]))
        assert_distinct(str(asset["submitter"]), context.actor_id)
        if asset["state"] != "active":
            raise ConflictError(f"素材状态为 {asset['state']}，不能批准采用")
        if self._repository.clock.is_due(str(asset["license_valid_to"])):
            raise ConflictError("素材授权已到期，不能批准采用")
        if asset["license_reuse"] == "forbidden":
            raise ConflictError("素材许可不允许再利用")
        if not covers(asset, product=str(adoption["product"]), territory=str(adoption["territory"]), channel=str(adoption["channel"])):
            raise ConflictError("素材许可范围不覆盖该产品、地域或渠道")
        sealed = {field: asset.get(field) for field in SEAL_FIELDS}
        changes = {
            "state": "approved",
            "sealed_license": sealed,
            "sealed_asset_version": asset["version"],
            "sealed_at": self._repository.clock.now(),
            "approved_by": context.actor_id,
        }
        return self._repository.update(self.entity_type, entity_id, changes, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def publish(self, context: AccessContext, entity_id: str, *, expected_version: int, reason: str, request_key: str) -> dict:
        """送展或产品采用正式发布。"""
        context.require("publish:adoptions")
        adoption = self._repository.get(self.entity_type, entity_id)
        if "published" not in TRANSITIONS.get(str(adoption["state"]), set()):
            raise ConflictError(f"不允许从 {adoption['state']} 发布")
        if not reason.strip():
            raise ValidationError("发布必须说明原因")
        updated = self._repository.update(self.entity_type, entity_id, {"state": "published", "published_at": self._repository.clock.now(), "transition_reason": reason.strip()}, actor=context.actor_id, expected_version=expected_version, request_key=request_key)
        self._outbox.enqueue(topic="adoption.published", aggregate_id=entity_id, payload={"adoption_id": entity_id, "work_id": adoption["work_id"], "channel": adoption["channel"], "stage": adoption["stage"]})
        return updated

    def replace(self, context: AccessContext, entity_id: str, *, new_asset_id: str, reason: str, expected_version: int, request_key: str) -> dict:
        """对已发布组合登记替换记录，并生成待审的替换组合。"""
        context.require("write:adoptions")
        adoption = self._repository.get(self.entity_type, entity_id)
        if "replaced" not in TRANSITIONS.get(str(adoption["state"]), set()):
            raise ConflictError(f"不允许从 {adoption['state']} 替换")
        if not reason.strip():
            raise ValidationError("替换必须说明原因")
        self._assets.raw(new_asset_id)
        replacement = {
            "new_asset_id": new_asset_id,
            "reason": reason.strip(),
            "replaced_at": self._repository.clock.now(),
            "replaced_by": context.actor_id,
        }
        follow_up = self.submit(context, {
            "work_id": adoption["work_id"],
            "asset_id": new_asset_id,
            "product": adoption["product"],
            "territory": adoption["territory"],
            "stage": adoption["stage"],
            "channel": adoption["channel"],
        }, request_key=f"{request_key}:follow-up")
        replacement["adoption_id"] = follow_up["entity_id"]
        return self._repository.update(self.entity_type, entity_id, {"state": "replaced", "replacement": replacement}, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def declare(self, context: AccessContext, entity_id: str, *, statement: str, expected_version: int, request_key: str) -> dict:
        """对已发布组合登记补充声明。"""
        context.require("write:adoptions")
        adoption = self._repository.get(self.entity_type, entity_id)
        if "declared" not in TRANSITIONS.get(str(adoption["state"]), set()):
            raise ConflictError(f"不允许从 {adoption['state']} 补充声明")
        if not statement.strip():
            raise ValidationError("补充声明不能为空")
        declaration = {
            "statement": statement.strip(),
            "declared_at": self._repository.clock.now(),
            "declared_by": context.actor_id,
        }
        return self._repository.update(self.entity_type, entity_id, {"state": "declared", "declaration": declaration}, actor=context.actor_id, expected_version=expected_version, request_key=request_key)

    def provenance(self, context: AccessContext, work_id: str) -> dict:
        """从作品反查每份素材的来历、采用时有效的许可、批准人和受影响的后续发布。"""
        context.require("read:adoptions")
        items = []
        for adoption in self._repository.search(self.entity_type, "work_id", work_id, limit=500):
            asset = self._assets.get(context, str(adoption["asset_id"]))
            raw_asset = self._assets.raw(str(adoption["asset_id"]))
            released = adoption["state"] in ("published", "replaced", "declared")
            scope_lost = not covers(raw_asset, product=str(adoption["product"]), territory=str(adoption["territory"]), channel=str(adoption["channel"]))
            affected = released and (raw_asset["state"] in ("withdrawn", "expired", "review") or scope_lost)
            follow_up = adoption.get("replacement") or adoption.get("declaration")
            items.append({
                "adoption_id": adoption["entity_id"],
                "state": adoption["state"],
                "stage": adoption["stage"],
                "channel": adoption["channel"],
                "product": adoption["product"],
                "territory": adoption["territory"],
                "asset_id": adoption["asset_id"],
                "digest": asset.get("digest"),
                "source": asset.get("source"),
                "attribution": asset.get("attribution"),
                "tool_version": asset.get("tool_version"),
                "sealed_license": adoption.get("sealed_license") if context.reveal_sensitive else ("***" if "sealed_license" in adoption else None),
                "sealed_at": adoption.get("sealed_at"),
                "approved_by": adoption.get("approved_by") if context.reveal_sensitive else ("***" if "approved_by" in adoption else None),
                "affected": affected,
                "follow_up": follow_up,
            })
        return {"work_id": work_id, "items": items}

    def get(self, context: AccessContext, entity_id: str) -> dict:
        context.require("read:adoptions")
        record = self._repository.get(self.entity_type, entity_id)
        return redact_record(record, RESTRICTED_FIELDS, context)

    def list_current(self, context: AccessContext, *, state: str | None = None, limit: int = 100) -> list[dict]:
        context.require("read:adoptions")
        if state is not None and state not in STATES:
            raise ValidationError("未知状态")
        rows = self._repository.list(self.entity_type, state=state, limit=limit)
        return [redact_record(row, RESTRICTED_FIELDS, context) for row in rows]

    def history(self, context: AccessContext, entity_id: str) -> list[dict]:
        context.require("history:adoptions")
        rows = self._repository.history(self.entity_type, entity_id)
        if not rows:
            raise NotFoundError(f"{self.entity_type}/{entity_id} 不存在")
        return [redact_record(row, RESTRICTED_FIELDS, context) for row in rows]

    def snapshot(self, context: AccessContext, entity_id: str, *, as_of: str) -> dict:
        context.require("history:adoptions")
        record = self._repository.snapshot(self.entity_type, entity_id, as_of=as_of)
        return redact_record(record, RESTRICTED_FIELDS, context)

    def bulk_get(self, context: AccessContext, entity_ids: Iterable[str]) -> list[dict]:
        context.require("read:adoptions")
        return [self.get(context, entity_id) for entity_id in dict.fromkeys(entity_ids)]

    def find_by_asset_id(self, asset_id: str, *, limit: int = 500) -> list[dict]:
        """内部按素材查询采用组合，不做字段裁剪。"""
        return self._repository.search(self.entity_type, "asset_id", asset_id, limit=limit)

    def block(self, entity_id: str, *, reason: str, actor: str) -> dict:
        """内部阻止尚未发布的组合，供撤回与限制传播调用。"""
        adoption = self._repository.get(self.entity_type, entity_id)
        if "blocked" not in TRANSITIONS.get(str(adoption["state"]), set()):
            raise ConflictError(f"不允许从 {adoption['state']} 阻止")
        return self._repository.update(self.entity_type, entity_id, {"state": "blocked", "blocked_reason": reason}, actor=actor, expected_version=adoption["version"], request_key=f"block:{entity_id}:{adoption['version']}")

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
            if isinstance(value, str):
                payload[key] = value.strip()
                if not payload[key]:
                    raise ValidationError(f"{key} 不能为空字符串")
        return payload
