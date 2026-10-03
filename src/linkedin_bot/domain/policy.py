from linkedin_bot.domain.models import ActionSnapshot, Policy


def block_reasons(action: ActionSnapshot, policy: Policy) -> tuple[str, ...]:
    """M1 cannot release any employer-facing capability, even with approval."""
    reasons = ["draft_only", "zero_outbound_cap", "capability_not_released"]
    if policy.paused:
        reasons.append("paused")
    if policy.restore_audit_required:
        reasons.append("restore_audit_required")
    if action.policy_revision != policy.revision:
        reasons.append("stale_policy")
    return tuple(reasons)
