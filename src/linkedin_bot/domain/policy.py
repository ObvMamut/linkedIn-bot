from linkedin_bot.domain.models import ActionSnapshot, Policy


def category(action: ActionSnapshot) -> str:
    """`submit_application` uses the application cap; every other kind is communication."""
    return "application" if action.kind == "submit_application" else "communication"


def block_reasons(
    action: ActionSnapshot, policy: Policy, *, used_today: int = 0, has_adapter: bool = False
) -> tuple[str, ...]:
    """Hard constraints. An empty result means the action may wait for user approval.

    `used_today` counts dispatched or unresolved effects in this action's category.
    """
    reasons: list[str] = []
    if policy.mode == "draft_only":
        reasons.append("draft_only")
    cap = policy.application_cap if category(action) == "application" else policy.communication_cap
    if cap == 0:
        reasons.append("zero_outbound_cap")
    elif used_today >= cap:
        reasons.append("cap_exhausted")
    if not has_adapter:
        reasons.append("capability_not_released")
    if policy.paused:
        reasons.append("paused")
    if policy.restore_audit_required:
        reasons.append("restore_audit_required")
    if action.policy_revision != policy.revision:
        reasons.append("stale_policy")
    return tuple(reasons)
