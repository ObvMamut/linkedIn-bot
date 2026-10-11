"""Approvals and policy: decide proposed actions, record outcomes, change caps.

Nothing here can send anything by itself: no effect adapter exists, so an approved action is
blocked with `capability_not_released`. The same rules as Telegram's /policy apply.
"""

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Label, Select, Static

from linkedin_bot.domain.models import MAX_DAILY_APPLICATIONS, MAX_DAILY_COMMUNICATIONS
from linkedin_bot.tui.views.base import View
from linkedin_bot.tui.widgets import ChoiceModal, ConfirmModal, clip, fill_table, selected_key
from linkedin_bot.workflows import approvals


class Approvals(View):
    AUTO_REFRESH = True

    def compose(self) -> ComposeResult:
        yield Label("Policy", classes="section")
        yield Static("", id="policy-now", markup=False)
        with Horizontal(classes="field"):
            yield Select(
                [
                    ("draft_only (nothing sent)", "draft_only"),
                    ("reviewed (needs approval)", "reviewed"),
                ],
                allow_blank=False,
                id="mode",
            )
            yield Select(
                [(f"{n} applications/day", n) for n in range(MAX_DAILY_APPLICATIONS + 1)],
                allow_blank=False,
                id="application-cap",
            )
            yield Select(
                [(f"{n} messages/day", n) for n in range(MAX_DAILY_COMMUNICATIONS + 1)],
                allow_blank=False,
                id="communication-cap",
            )
            yield Button("Apply policy", variant="warning", id="apply-policy")
        with Vertical(classes="panel"):
            yield Label("Waiting for your approval", classes="section")
            pending: DataTable[Text] = DataTable(id="pending", cursor_type="row")
            pending.add_columns("Kind", "Target", "Content")
            yield pending
            with Horizontal(classes="toolbar"):
                yield Button("Approve", variant="success", id="approve")
                yield Button("Reject", variant="error", id="reject")
        with Vertical(classes="panel"):
            yield Label(
                "Unknown outcomes (inspect yourself, then record what you saw)", classes="section"
            )
            unknown: DataTable[Text] = DataTable(id="unknown", cursor_type="row")
            unknown.add_columns("Kind", "Target", "Action id")
            yield unknown
            yield Button("Record outcome", id="resolve")

    async def reload(self) -> None:
        policy = await self.services.db.policy()
        self.query_one("#policy-now", Static).update(
            f"Revision {policy.revision}: {approvals.describe(policy)}"
            + (" · paused" if policy.paused else "")
            + (" · restore audit required" if policy.restore_audit_required else "")
            + "\nNo effect adapter is installed, so even approved actions are never sent."
        )
        with self.prevent(Select.Changed):
            self.query_one("#mode", Select).value = policy.mode
            self.query_one("#application-cap", Select).value = policy.application_cap
            self.query_one("#communication-cap", Select).value = policy.communication_cap
        waiting = await approvals.pending(self.services.db)
        fill_table(
            self.query_one("#pending", DataTable),
            (
                (
                    item.approval_id or item.action.id,
                    (item.action.kind, clip(item.action.target, 30), clip(item.action.content, 60)),
                )
                for item in waiting
            ),
        )
        fill_table(
            self.query_one("#unknown", DataTable),
            (
                (action.id, (action.kind, clip(action.target, 30), action.id))
                for action in await approvals.unknown(self.services.db)
            ),
        )

    @on(Button.Pressed, "#apply-policy")
    def apply_pressed(self) -> None:
        self.apply_policy()

    @work(group="policy")
    async def apply_policy(self) -> None:
        current = await self.services.db.policy()
        try:
            proposed = approvals.propose(
                current,
                mode=str(self.query_one("#mode", Select).value),
                application_cap=int(str(self.query_one("#application-cap", Select).value)),
                communication_cap=int(str(self.query_one("#communication-cap", Select).value)),
            )
        except ValueError as exc:
            self.report(str(exc), error=True)
            return
        if (proposed.mode, proposed.application_cap, proposed.communication_cap) == (
            current.mode,
            current.application_cap,
            current.communication_cap,
        ):
            self.report("Nothing changed.")
            return
        question = (
            f"Before: {approvals.describe(current)}\nAfter:  {approvals.describe(proposed)}\n\n"
            "Pending approvals will be invalidated and need a fresh decision."
        )
        if await self.app.push_screen_wait(ConfirmModal(question, confirm_label="Change policy")):
            await approvals.apply(self.services.db, proposed, current)
            self.report(f"Policy updated to revision {proposed.revision}.")
        await self.reload()

    @on(Button.Pressed, "#approve")
    def approve_pressed(self) -> None:
        self.decide(True)

    @on(Button.Pressed, "#reject")
    def reject_pressed(self) -> None:
        self.decide(False)

    @work(group="decide")
    async def decide(self, approve: bool) -> None:
        approval = selected_key(self.query_one("#pending", DataTable))
        if approval is None:
            self.report("Nothing is waiting.", error=True)
            return
        verb = "Approve" if approve else "Reject"
        if await self.app.push_screen_wait(
            ConfirmModal(f"{verb} this action?", confirm_label=verb)
        ):
            message = await approvals.decide(
                self.services.db, self.services.executor, approval, approve
            )
            self.report(message)
        await self.reload()

    @on(Button.Pressed, "#resolve")
    def resolve_pressed(self) -> None:
        self.resolve()

    @work(group="resolve")
    async def resolve(self) -> None:
        action_id = selected_key(self.query_one("#unknown", DataTable))
        if action_id is None:
            self.report("No unknown outcomes.", error=True)
            return
        answer = await self.app.push_screen_wait(
            ChoiceModal(
                "What did you see when you checked?",
                [("It happened (success)", "success"), ("It did not happen", "none")],
                note="Evidence note: what you inspected (required)",
            )
        )
        if answer is None:
            return
        outcome, _, note = answer.partition("|")
        self.report(
            await approvals.resolve(self.services.db, action_id, outcome == "success", note)
        )
        await self.reload()
