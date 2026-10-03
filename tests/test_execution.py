import asyncio
from datetime import timedelta

import pytest

from linkedin_bot.domain.models import ActionResult, ActionSnapshot, Policy, now
from linkedin_bot.domain.policy import block_reasons
from linkedin_bot.runtime.privacy import BoundaryError
from linkedin_bot.storage.database import Database, RevisionConflict
from linkedin_bot.workflows.executor import DraftExecutor, Executor

REVIEWED = {"mode": "reviewed", "application_cap": 1, "communication_cap": 1}


def snap(policy_revision=1, **changes):
    return ActionSnapshot(
        **{
            "kind": "send_reply",
            "target": "recipient",
            "logical_key": "reply:t1",
            "content": "Hello",
            "profile_revision": 1,
            "preferences_revision": 1,
            "policy_revision": policy_revision,
            **changes,
        }
    )


class Adapter:
    def __init__(self, result=None, error=None):
        self.result = result or ActionResult(outcome="succeeded", reason_code="sent")
        self.error, self.calls = error, []

    async def perform(self, action):
        self.calls.append(action.id)
        if self.error:
            raise self.error
        return self.result


def run(tmp_path, scenario):
    async def main():
        db = Database(tmp_path)
        try:
            await db.open()
            return await scenario(db)
        finally:
            await db.close()

    return asyncio.run(main())


async def reviewed(db, **overrides):
    policy = Policy.model_validate({**REVIEWED, **overrides, "revision": 2})
    await db.save_policy(policy, expected_revision=1)
    return policy


def test_default_policy_still_blocks_everything_and_caps_have_hard_ceilings():
    reasons = block_reasons(snap(), Policy())
    assert {"draft_only", "zero_outbound_cap", "capability_not_released"} <= set(reasons)
    with pytest.raises(ValueError):
        Policy.model_validate({**REVIEWED, "application_cap": 11})


def test_reviewed_policy_needs_adapter_cap_and_clean_state():
    policy = Policy.model_validate({**REVIEWED, "revision": 1})
    assert block_reasons(snap(), policy, used_today=0, has_adapter=True) == ()
    assert "cap_exhausted" in block_reasons(snap(), policy, used_today=1, has_adapter=True)
    assert "capability_not_released" in block_reasons(snap(), policy, used_today=0)
    paused = policy.model_copy(update={"paused": True, "restore_audit_required": True})
    assert {"paused", "restore_audit_required"} <= set(
        block_reasons(snap(), paused, used_today=0, has_adapter=True)
    )
    assert "stale_policy" in block_reasons(snap(2), policy, used_today=0, has_adapter=True)


def test_without_adapter_an_approved_action_is_blocked_at_dispatch(tmp_path):
    async def scenario(db):
        await reviewed(db)
        executor = Executor(db, {})
        assert "capability_not_released" in await executor.propose(snap(policy_revision=2))
        assert await executor.state("reply:t1") == "blocked"

    run(tmp_path, scenario)


def test_approve_then_execute_once_counts_against_daily_cap(tmp_path):
    async def scenario(db):
        await reviewed(db)
        adapter = Adapter()
        executor = Executor(db, {"send_reply": adapter})
        first = snap(policy_revision=2)
        assert await executor.propose(first) == ()
        assert await executor.state("reply:t1") == "awaiting_approval"
        approval = await executor.approval_id(first.id)
        assert await executor.decide(approval, approve=True) == "approved"
        assert await executor.decide(approval, approve=True) == "stale"  # replay is inert
        assert await executor.run(first.id) == ()
        assert adapter.calls == [first.id] and await executor.state("reply:t1") == "succeeded"
        second = snap(policy_revision=2, logical_key="reply:t2")
        assert "cap_exhausted" in await executor.propose(second)

    run(tmp_path, scenario)


def test_edit_expiry_and_rejection_invalidate_approval(tmp_path):
    async def scenario(db):
        await reviewed(db)
        adapter = Adapter()
        executor = Executor(db, {"send_reply": adapter})
        original = snap(policy_revision=2)
        await executor.propose(original)
        approval = await executor.approval_id(original.id)
        edited = original.model_copy(update={"revision": 2, "content": "Changed"})
        await db.save_action(edited, expected_revision=1)
        assert await executor.decide(approval, approve=True) == "stale"
        with pytest.raises(RevisionConflict):
            await executor.run(original.id)
        assert adapter.calls == []
        late = snap(policy_revision=2, logical_key="reply:t3")
        await executor.propose(late)
        expired = await executor.approval_id(late.id)
        assert await executor.decide(expired, approve=True, at=now() + timedelta(days=2)) == "stale"
        third = snap(policy_revision=2, logical_key="reply:t4")
        await executor.propose(third)
        assert (
            await executor.decide(await executor.approval_id(third.id), approve=False) == "rejected"
        )
        assert await executor.state("reply:t4") == "cancelled"

    run(tmp_path, scenario)


def test_failed_releases_slot_but_unknown_keeps_it_and_blocks_retry(tmp_path):
    async def scenario(db):
        await reviewed(db)
        failing = Adapter(ActionResult(outcome="failed", reason_code="rejected"))
        executor = Executor(db, {"send_reply": failing})
        a = snap(policy_revision=2)
        await executor.propose(a)
        await executor.decide(await executor.approval_id(a.id), approve=True)
        await executor.run(a.id)
        assert await executor.state("reply:t1") == "failed"
        crashing = Adapter(error=RuntimeError("boom"))
        executor = Executor(db, {"send_reply": crashing})
        b = snap(policy_revision=2, logical_key="reply:t2")
        assert await executor.propose(b) == ()  # the failed attempt released its slot
        await executor.decide(await executor.approval_id(b.id), approve=True)
        await executor.run(b.id)
        assert await executor.state("reply:t2") == "unknown"
        c = snap(policy_revision=2, logical_key="reply:t3")
        assert "cap_exhausted" in await executor.propose(c)  # unknown still holds its slot

    run(tmp_path, scenario)


def test_policy_change_before_dispatch_blocks_and_never_calls_adapter(tmp_path):
    async def scenario(db):
        await reviewed(db)
        adapter = Adapter()
        executor = Executor(db, {"send_reply": adapter})
        a = snap(policy_revision=2)
        await executor.propose(a)
        approval = await executor.approval_id(a.id)
        await executor.decide(approval, approve=True)
        await db.save_policy(Policy(revision=3), expected_revision=2)  # back to draft-only
        with pytest.raises(RevisionConflict):
            await executor.run(a.id)
        assert adapter.calls == []

    run(tmp_path, scenario)


def test_paused_runtime_blocks_dispatch_of_approved_action(tmp_path):
    async def scenario(db):
        await reviewed(db)
        adapter = Adapter()
        executor = Executor(db, {"send_reply": adapter})
        a = snap(policy_revision=2)
        await executor.propose(a)
        await executor.decide(await executor.approval_id(a.id), approve=True)
        await db.pause(True)
        assert "paused" in await executor.run(a.id)
        assert adapter.calls == []

    run(tmp_path, scenario)


def test_save_policy_checks_revision(tmp_path):
    async def scenario(db):
        with pytest.raises(RevisionConflict):
            await db.save_policy(Policy(revision=3), expected_revision=1)
        with pytest.raises(BoundaryError):
            await db.save_policy(Policy(revision=2, restore_audit_required=True), 1)

    run(tmp_path, scenario)


def test_draft_executor_alias_keeps_m1_behavior(tmp_path):
    async def scenario(db):
        assert "draft_only" in await DraftExecutor(db).propose(snap())

    run(tmp_path, scenario)
