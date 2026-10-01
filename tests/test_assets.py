from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.errors import ConflictError, PermissionDenied
from civicflow.governance import Governance
from civicflow.security import AccessContext


NOW = "2026-10-01T12:00:00+08:00"


def asset_values(digest="sha256:aaa", **overrides):
    values = {
        "digest": digest,
        "source": "vendor:genlab",
        "license_training": "internal",
        "license_reuse": "restricted",
        "attribution": "署名：设计师A",
        "tool_version": "genlab-diffusion/2.1",
        "allowed_products": ["poster", "packaging"],
        "allowed_territories": ["CN"],
        "allowed_channels": ["exhibition"],
        "license_valid_to": "2027-01-01T00:00:00Z",
    }
    values.update(overrides)
    return values


def adoption_values(asset_id, work_id="work:tour-poster", **overrides):
    values = {
        "work_id": work_id,
        "asset_id": asset_id,
        "product": "poster",
        "territory": "CN",
        "stage": "finalist",
        "channel": "exhibition",
    }
    values.update(overrides)
    return values


class AssetGovernanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "test.sqlite3"
        self.app = CivicFlow.open(self.db_path, fixed_now=NOW)
        self.gov = Governance(self.app)
        self.system = AccessContext.system("tester")
        self.uploader = AccessContext(actor_id="uploader", permissions=frozenset({"write:assets", "read:assets"}))
        self.legal = AccessContext(actor_id="legal", permissions=frozenset({"write:adoptions", "approve:adoptions", "publish:adoptions", "read:adoptions", "read:assets", "history:adoptions", "history:assets"}))
        self.partner = AccessContext(actor_id="partner", permissions=frozenset({"read:assets", "read:adoptions"}))

    def tearDown(self):
        self.temp.cleanup()

    def reopen(self, fixed_now=NOW):
        self.app = CivicFlow.open(self.db_path, fixed_now=fixed_now)
        self.gov = Governance(self.app)

    def register_active(self, digest="sha256:aaa", key="asset-1", **overrides):
        asset = self.gov.assets.register(self.uploader, asset_values(digest, **overrides), request_key=key)
        return self.gov.assets.activate(self.system, asset["entity_id"], expected_version=asset["version"], reason="许可核验通过", request_key=key + ":activate")

    def topics(self):
        leased = self.app.outbox.lease(owner="worker", limit=100)
        return sorted(item["topic"] for item in leased)

    def test_same_digest_returns_original_asset(self):
        first = self.gov.assets.register(self.uploader, asset_values(), request_key="up-1")
        second = self.gov.assets.register(self.uploader, asset_values(), request_key="up-2")
        self.assertEqual(first["entity_id"], second["entity_id"])
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertFalse(second["conflict"])
        self.assertEqual(len(self.gov.assets.list_current(self.system)), 1)

    def test_conflicting_license_statement_goes_to_review(self):
        asset = self.register_active()
        conflicted = self.gov.assets.register(self.uploader, asset_values(license_reuse="allowed", attribution="署名：设计师B"), request_key="up-2")
        self.assertTrue(conflicted["conflict"])
        self.assertEqual(conflicted["state"], "review")
        self.assertEqual(len(conflicted["conflict_notes"]), 1)
        self.assertIn("asset.license-conflict", self.topics())

    def test_approve_seals_visible_version(self):
        asset = self.register_active()
        adoption = self.gov.adoptions.submit(self.legal, adoption_values(asset["entity_id"]), request_key="ad-1")
        approved = self.gov.adoptions.approve(self.legal, adoption["entity_id"], expected_version=1, request_key="ad-1:approve")
        self.assertEqual(approved["sealed_license"]["allowed_territories"], ["CN"])
        self.assertEqual(approved["sealed_asset_version"], asset["version"])
        self.assertEqual(approved["approved_by"], "legal")
        self.reopen("2026-10-01T12:05:00+08:00")
        self.gov.assets.update_scope(self.system, asset["entity_id"], {"allowed_territories": ["JP"]}, expected_version=asset["version"], reason="品牌限制更新", request_key="scope-1")
        sealed = self.gov.adoptions.get(self.system, adoption["entity_id"])
        self.assertEqual(sealed["sealed_license"]["allowed_territories"], ["CN"])
        snapshot = self.gov.assets.snapshot(self.system, asset["entity_id"], as_of=approved["sealed_at"])
        self.assertEqual(snapshot["allowed_territories"], ["CN"])

    def test_submitter_cannot_approve_own_asset(self):
        asset = self.register_active()
        adoption = self.gov.adoptions.submit(self.legal, adoption_values(asset["entity_id"]), request_key="ad-1")
        self_approver = AccessContext(actor_id="uploader", permissions=frozenset({"approve:adoptions"}))
        with self.assertRaises(PermissionDenied):
            self.gov.adoptions.approve(self_approver, adoption["entity_id"], expected_version=1, request_key="ad-1:self")

    def test_partner_reads_only_granted_fields(self):
        asset = self.register_active()
        adoption = self.gov.adoptions.submit(self.legal, adoption_values(asset["entity_id"]), request_key="ad-1")
        self.gov.adoptions.approve(self.legal, adoption["entity_id"], expected_version=1, request_key="ad-1:approve")
        visible_asset = self.gov.assets.get(self.partner, asset["entity_id"])
        self.assertEqual(visible_asset["source"], "***")
        self.assertEqual(visible_asset["submitter"], "***")
        self.assertEqual(visible_asset["license_reuse"], "restricted")
        visible_adoption = self.gov.adoptions.get(self.partner, adoption["entity_id"])
        self.assertEqual(visible_adoption["sealed_license"], "***")
        self.assertEqual(visible_adoption["approved_by"], "***")
        self.assertEqual(visible_adoption["channel"], "exhibition")

    def test_approve_rejects_out_of_scope_and_expired(self):
        asset = self.register_active()
        adoption = self.gov.adoptions.submit(self.legal, adoption_values(asset["entity_id"], territory="JP"), request_key="ad-jp")
        with self.assertRaises(ConflictError):
            self.gov.adoptions.approve(self.legal, adoption["entity_id"], expected_version=1, request_key="ad-jp:approve")
        expired = self.register_active(digest="sha256:old", key="asset-old", license_valid_to="2026-09-01T00:00:00Z")
        adoption2 = self.gov.adoptions.submit(self.legal, adoption_values(expired["entity_id"]), request_key="ad-old")
        with self.assertRaises(ConflictError):
            self.gov.adoptions.approve(self.legal, adoption2["entity_id"], expected_version=1, request_key="ad-old:approve")
        forbidden = self.register_active(digest="sha256:no", key="asset-no", license_reuse="forbidden")
        adoption3 = self.gov.adoptions.submit(self.legal, adoption_values(forbidden["entity_id"]), request_key="ad-no")
        with self.assertRaises(ConflictError):
            self.gov.adoptions.approve(self.legal, adoption3["entity_id"], expected_version=1, request_key="ad-no:approve")

    def test_withdrawal_blocks_unpublished_and_flags_published(self):
        asset = self.register_active()
        pending = self.gov.adoptions.submit(self.legal, adoption_values(asset["entity_id"], work_id="work:a"), request_key="ad-pending")
        approved = self.gov.adoptions.submit(self.legal, adoption_values(asset["entity_id"], work_id="work:b"), request_key="ad-approved")
        self.gov.adoptions.approve(self.legal, approved["entity_id"], expected_version=1, request_key="ad-approved:ok")
        published = self.gov.adoptions.submit(self.legal, adoption_values(asset["entity_id"], work_id="work:c"), request_key="ad-published")
        self.gov.adoptions.approve(self.legal, published["entity_id"], expected_version=1, request_key="ad-published:ok")
        self.gov.adoptions.publish(self.legal, published["entity_id"], expected_version=2, reason="巡展首站", request_key="ad-published:go")
        self.gov.assets.withdraw(self.system, asset["entity_id"], expected_version=asset["version"], reason="授权方撤回", request_key="wd-1")
        handled = self.gov.run_due_jobs(self.system)
        self.assertEqual(handled.get("asset-withdrawal"), 1)
        self.assertEqual(self.gov.adoptions.get(self.system, pending["entity_id"])["state"], "blocked")
        self.assertEqual(self.gov.adoptions.get(self.system, approved["entity_id"])["state"], "blocked")
        self.assertEqual(self.gov.adoptions.get(self.system, published["entity_id"])["state"], "published")
        topics = self.topics()
        self.assertEqual(topics.count("adoption.blocked"), 2)
        self.assertEqual(topics.count("asset.follow-up-required"), 1)

    def test_published_follow_up_replace_and_declare(self):
        asset = self.register_active()
        new_asset = self.register_active(digest="sha256:bbb", key="asset-2")
        adoption = self.gov.adoptions.submit(self.legal, adoption_values(asset["entity_id"]), request_key="ad-1")
        self.gov.adoptions.approve(self.legal, adoption["entity_id"], expected_version=1, request_key="ad-1:ok")
        self.gov.adoptions.publish(self.legal, adoption["entity_id"], expected_version=2, reason="量产包装", request_key="ad-1:go")
        replaced = self.gov.adoptions.replace(self.legal, adoption["entity_id"], new_asset_id=new_asset["entity_id"], reason="撤回后替换素材", expected_version=3, request_key="ad-1:replace")
        self.assertEqual(replaced["state"], "replaced")
        self.assertEqual(replaced["replacement"]["new_asset_id"], new_asset["entity_id"])
        follow_up = self.gov.adoptions.get(self.system, replaced["replacement"]["adoption_id"])
        self.assertEqual(follow_up["state"], "pending")
        self.assertEqual(follow_up["asset_id"], new_asset["entity_id"])
        other = self.gov.adoptions.submit(self.legal, adoption_values(asset["entity_id"], work_id="work:decl"), request_key="ad-2")
        self.gov.adoptions.approve(self.legal, other["entity_id"], expected_version=1, request_key="ad-2:ok")
        self.gov.adoptions.publish(self.legal, other["entity_id"], expected_version=2, reason="巡展", request_key="ad-2:go")
        declared = self.gov.adoptions.declare(self.legal, other["entity_id"], statement="授权撤回后补充署名与下架时间表", expected_version=3, request_key="ad-2:declare")
        self.assertEqual(declared["state"], "declared")
        self.assertEqual(declared["declaration"]["declared_by"], "legal")

    def test_expiry_job_expires_asset_and_blocks_pending(self):
        asset = self.register_active(license_valid_to="2026-10-01T05:00:00Z")
        adoption = self.gov.adoptions.submit(self.legal, adoption_values(asset["entity_id"]), request_key="ad-1")
        self.reopen("2026-10-02T00:00:00Z")
        handled = self.gov.run_due_jobs(self.system)
        self.assertEqual(handled.get("asset-expiry"), 1)
        self.assertEqual(self.gov.assets.get(self.system, asset["entity_id"])["state"], "expired")
        self.assertEqual(self.gov.adoptions.get(self.system, adoption["entity_id"])["state"], "blocked")

    def test_restriction_update_blocks_only_out_of_scope(self):
        asset = self.register_active(allowed_territories=["CN", "JP"])
        kept = self.gov.adoptions.submit(self.legal, adoption_values(asset["entity_id"], work_id="work:keep"), request_key="ad-keep")
        self.gov.adoptions.approve(self.legal, kept["entity_id"], expected_version=1, request_key="ad-keep:ok")
        dropped = self.gov.adoptions.submit(self.legal, adoption_values(asset["entity_id"], work_id="work:drop", territory="JP"), request_key="ad-drop")
        self.gov.adoptions.approve(self.legal, dropped["entity_id"], expected_version=1, request_key="ad-drop:ok")
        published = self.gov.adoptions.submit(self.legal, adoption_values(asset["entity_id"], work_id="work:pub", territory="JP"), request_key="ad-pub")
        self.gov.adoptions.approve(self.legal, published["entity_id"], expected_version=1, request_key="ad-pub:ok")
        self.gov.adoptions.publish(self.legal, published["entity_id"], expected_version=2, reason="东京巡展", request_key="ad-pub:go")
        self.gov.assets.update_scope(self.system, asset["entity_id"], {"allowed_territories": ["CN"]}, expected_version=asset["version"], reason="品牌限制更新", request_key="scope-1")
        handled = self.gov.run_due_jobs(self.system)
        self.assertEqual(handled.get("asset-restriction"), 1)
        self.assertEqual(self.gov.adoptions.get(self.system, kept["entity_id"])["state"], "approved")
        self.assertEqual(self.gov.adoptions.get(self.system, dropped["entity_id"])["state"], "blocked")
        self.assertEqual(self.gov.adoptions.get(self.system, published["entity_id"])["state"], "published")
        self.assertIn("asset.follow-up-required", self.topics())

    def test_recovery_continues_pending_and_propagation(self):
        asset = self.register_active()
        adoption = self.gov.adoptions.submit(self.legal, adoption_values(asset["entity_id"]), request_key="ad-1")
        handled = self.gov.run_due_jobs(self.system)
        self.assertEqual(handled.get("adoption-pending"), 1)
        self.assertIn("adoption.pending-review", self.topics())
        self.gov.assets.withdraw(self.system, asset["entity_id"], expected_version=asset["version"], reason="授权方撤回", request_key="wd-1")
        self.reopen("2026-10-01T13:00:00+08:00")
        handled = self.gov.run_due_jobs(self.system)
        self.assertEqual(handled.get("asset-withdrawal"), 1)
        self.assertEqual(handled.get("asset-expiry"), None)
        self.assertIn("adoption.blocked", self.topics())
        self.assertEqual(self.gov.adoptions.get(self.system, adoption["entity_id"])["state"], "blocked")
        self.assertEqual(self.gov.run_due_jobs(self.system), {})

    def test_provenance_reverse_lookup(self):
        asset = self.register_active()
        other = self.register_active(digest="sha256:bbb", key="asset-2")
        first = self.gov.adoptions.submit(self.legal, adoption_values(asset["entity_id"]), request_key="ad-1")
        self.gov.adoptions.approve(self.legal, first["entity_id"], expected_version=1, request_key="ad-1:ok")
        self.gov.adoptions.publish(self.legal, first["entity_id"], expected_version=2, reason="巡展", request_key="ad-1:go")
        second = self.gov.adoptions.submit(self.legal, adoption_values(other["entity_id"]), request_key="ad-2")
        self.gov.adoptions.approve(self.legal, second["entity_id"], expected_version=1, request_key="ad-2:ok")
        self.gov.assets.withdraw(self.system, asset["entity_id"], expected_version=asset["version"], reason="授权方撤回", request_key="wd-1")
        self.gov.run_due_jobs(self.system)
        self.gov.adoptions.replace(self.legal, first["entity_id"], new_asset_id=other["entity_id"], reason="撤回后替换", expected_version=3, request_key="ad-1:replace")
        result = self.gov.adoptions.provenance(self.system, "work:tour-poster")
        self.assertEqual(len(result["items"]), 3)
        by_id = {item["adoption_id"]: item for item in result["items"]}
        released = by_id[first["entity_id"]]
        self.assertEqual(released["source"], "vendor:genlab")
        self.assertEqual(released["sealed_license"]["license_reuse"], "restricted")
        self.assertEqual(released["approved_by"], "legal")
        self.assertTrue(released["affected"])
        self.assertEqual(released["follow_up"]["new_asset_id"], other["entity_id"])
        unaffected = by_id[second["entity_id"]]
        self.assertFalse(unaffected["affected"])
        partner_view = self.gov.adoptions.provenance(self.partner, "work:tour-poster")
        partner_first = {item["adoption_id"]: item for item in partner_view["items"]}[first["entity_id"]]
        self.assertEqual(partner_first["source"], "***")
        self.assertEqual(partner_first["approved_by"], "***")
        self.assertEqual(partner_first["sealed_license"], "***")
        self.assertGreater(self.app.verify()["audit_entries"], 0)


if __name__ == "__main__":
    unittest.main()
