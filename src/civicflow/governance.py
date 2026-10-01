"""素材治理门面：组合素材与采用服务，并处理可恢复的定时任务。

到期授权、待审组合提醒、授权撤回传播和品牌限制传播都通过定时任务执行；
进程恢复后再次调用 run_due_jobs 即可继续处理。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from .adoptions import AdoptionService
from .application import CivicFlow
from .assets import AssetService, covers
from .errors import CivicFlowError, NotFoundError
from .security import AccessContext
from .timeutil import parse_instant


@dataclass(frozen=True)
class Governance:
    """素材采用记录的组装入口。"""

    _app: CivicFlow

    @property
    def assets(self) -> AssetService:
        return AssetService(self._app.repository, self._app.jobs, self._app.outbox)

    @property
    def adoptions(self) -> AdoptionService:
        return AdoptionService(self._app.repository, self.assets, self._app.jobs, self._app.outbox)

    def run_due_jobs(self, context: AccessContext, *, limit: int = 20) -> dict:
        """处理到期的授权、撤回与限制传播和待审组合提醒；可重复调用。"""
        context.require("transition:assets")
        handled: dict[str, int] = {}
        for job in self._app.jobs.claim_due(limit=limit):
            try:
                self._handle(job)
            except CivicFlowError as exc:
                retry_at = (parse_instant(self._app.clock.now()) + timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
                self._app.jobs.retry(job["job_id"], error=str(exc), retry_at=retry_at)
                handled["failed"] = handled.get("failed", 0) + 1
            else:
                self._app.jobs.finish(job["job_id"])
                handled[job["job_type"]] = handled.get(job["job_type"], 0) + 1
        return handled

    def _handle(self, job: dict) -> None:
        payload = json.loads(job["payload_json"])
        job_type = job["job_type"]
        if job_type == "asset-expiry":
            self._expire_asset(str(payload["asset_id"]))
        elif job_type == "asset-withdrawal":
            self._propagate(str(payload["asset_id"]), reason=str(payload.get("reason") or "授权撤回"), only_out_of_scope=False)
        elif job_type == "asset-restriction":
            self._propagate(str(payload["asset_id"]), reason=str(payload.get("reason") or "品牌限制更新"), only_out_of_scope=True)
        elif job_type == "adoption-pending":
            self._remind_pending(str(payload["adoption_id"]), str(payload["work_id"]))
        else:
            raise NotFoundError(f"未知任务类型: {job_type}")

    def _expire_asset(self, asset_id: str) -> None:
        asset = self.assets.raw(asset_id)
        if asset["state"] != "active":
            return
        if not self._app.clock.is_due(str(asset["license_valid_to"])):
            return
        self._app.repository.update("assets", asset_id, {"state": "expired", "transition_reason": "授权到期"}, actor="asset-expiry", expected_version=asset["version"], request_key=f"expire:{asset_id}:{asset['version']}")
        self._propagate(asset_id, reason="授权到期", only_out_of_scope=False)

    def _propagate(self, asset_id: str, *, reason: str, only_out_of_scope: bool) -> None:
        asset = self.assets.raw(asset_id)
        adoptions = self.adoptions
        for adoption in adoptions.find_by_asset_id(asset_id):
            if only_out_of_scope and covers(asset, product=str(adoption["product"]), territory=str(adoption["territory"]), channel=str(adoption["channel"])):
                continue
            state = adoption["state"]
            if state in ("pending", "approved"):
                adoptions.block(adoption["entity_id"], reason=reason, actor="asset-governance")
                self._app.outbox.enqueue(topic="adoption.blocked", aggregate_id=adoption["entity_id"], payload={"adoption_id": adoption["entity_id"], "work_id": adoption["work_id"], "asset_id": asset_id, "reason": reason})
            elif state == "published":
                self._app.outbox.enqueue(topic="asset.follow-up-required", aggregate_id=adoption["entity_id"], payload={"adoption_id": adoption["entity_id"], "work_id": adoption["work_id"], "asset_id": asset_id, "reason": reason})

    def _remind_pending(self, adoption_id: str, work_id: str) -> None:
        try:
            adoption = self._app.repository.get("adoptions", adoption_id)
        except NotFoundError:
            return
        if adoption["state"] != "pending":
            return
        self._app.outbox.enqueue(topic="adoption.pending-review", aggregate_id=adoption_id, payload={"adoption_id": adoption_id, "work_id": work_id})
