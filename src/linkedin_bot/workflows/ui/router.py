"""Button presses and pending text input. Everything the user can tap goes through here.

This module renders and routes; reading and changing state lives in `workflows.operations`.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from linkedin_bot.adapters.linkedin.pages import Parsed
from linkedin_bot.adapters.public_http import AdapterError
from linkedin_bot.adapters.telegram import TelegramAPI
from linkedin_bot.domain.models import SearchPreferences
from linkedin_bot.domain.workspace import Pin
from linkedin_bot.runtime.privacy import BoundaryError
from linkedin_bot.storage.database import Database, TrackerStatus
from linkedin_bot.storage.linkedin import LinkedInStore
from linkedin_bot.storage.workspace import WorkspaceStore
from linkedin_bot.workflows import facts
from linkedin_bot.workflows.digest import DigestService
from linkedin_bot.workflows.facts import add_typed_fact
from linkedin_bot.workflows.linkedin_commands import Connector, LinkedInCommands
from linkedin_bot.workflows.materials import Generator, draft_cover_letter
from linkedin_bot.workflows.onboarding import baseline_cv
from linkedin_bot.workflows.operations import (
    BoardFinder,
    Operations,
    SearchKind,
    search_kind,
    split_items,
)
from linkedin_bot.workflows.ui import screens
from linkedin_bot.workflows.ui.keyboard import Button, Screen, to_markup
from linkedin_bot.workflows.ui.screens import LIST_FIELDS, LOADING, pin_key
from linkedin_bot.workflows.ui.tokens import TokenMap

__all__ = ["BOARD_INTERVAL_SECONDS", "UI", "BoardFinder", "Ctx", "split_items"]

SEARCH_KINDS = ("people", "firms")  # kinds whose results can be pinned
ALL_KINDS: tuple[SearchKind, ...] = ("people", "firms", "jobs")
EXPIRED = "This button expired. Open it again from the menu."
FIELD_AREA = {
    "locations": "loc",
    "roles": "roles",
    "must_keywords": "roles",
    "nice_keywords": "roles",
    "exclusions": "roles",
    "blocked_firms": "firm",
    "preferred_industries": "firm",
}
BOARD_INTERVAL_SECONDS = 6 * 3600
SCREEN_LIMIT = 3900  # an edited screen is one message; longer text is sent instead


@dataclass
class Ctx:
    chat_id: int
    message_id: int | None
    callback_id: str | None = None
    answered: bool = False


class UI:
    def __init__(
        self,
        database: Database,
        api: TelegramAPI,
        linkedin: LinkedInCommands | None,
        board_finder: BoardFinder | None = None,
        llm_enabled: bool = False,
        digest_service: DigestService | None = None,
        board_interval_seconds: int = BOARD_INTERVAL_SECONDS,
        operations: Operations | None = None,
        generator: Generator | None = None,
    ) -> None:
        self.db, self.api, self.linkedin, self.finder = database, api, linkedin, board_finder
        self.generator = generator
        self.llm_enabled = llm_enabled or generator is not None
        self.digest_service = digest_service
        self.ops = operations or Operations(
            database, linkedin, board_finder, board_interval_seconds
        )
        self.store = WorkspaceStore(database)
        self.linked = LinkedInStore(database)
        self.tokens = TokenMap()

    # ---- entry points -----------------------------------------------------------------
    async def open_menu(self, chat_id: int) -> None:
        await self.store.clear_awaiting()
        await self.show(Ctx(chat_id, None), screens.main_menu())

    async def open(self, chat_id: int, data: str) -> None:
        """A typed shortcut (/jobs, /pins, ...) opens the same screen a button would."""
        await self.store.clear_awaiting()
        ctx = Ctx(chat_id, None)
        try:
            toast = await self.dispatch(ctx, data)
        except BoundaryError, ValueError, LookupError:
            logging.warning("", extra={"event": "ui_refused"})
            toast = "That did not work. Try again from the menu."
        if toast and ctx.message_id is None:
            await self.api.send_message(chat_id, toast)

    async def on_callback(
        self, chat_id: int, message_id: int | None, callback_id: str, data: str
    ) -> None:
        ctx = Ctx(chat_id, message_id, callback_id)
        try:
            toast = await self.dispatch(ctx, data)
        except BoundaryError, ValueError, LookupError:
            logging.warning("", extra={"event": "ui_refused"})
            toast = "That did not work. Try again from the menu."
        except Exception:
            logging.warning("", extra={"event": "ui_failed"})
            toast = "Something went wrong."
        await self.ack(ctx, toast or "")

    async def handle_text(self, chat_id: int, text: str) -> bool:
        awaiting = await self.store.awaiting()
        if awaiting is None:
            return False
        name, context = awaiting
        raw_message = context.get("msg")
        ctx = Ctx(chat_id, raw_message if isinstance(raw_message, int) else None)
        await self.store.clear_awaiting()
        try:
            await self.consume(ctx, name, text.strip())
        except Exception:
            logging.warning("", extra={"event": "ui_failed"})
            await self.show(ctx, screens.error_screen("Something went wrong. Try again."))
        return True

    # ---- plumbing ---------------------------------------------------------------------
    async def ack(self, ctx: Ctx, text: str = "") -> None:
        if ctx.callback_id and not ctx.answered:
            ctx.answered = True
            await self.api.answer_callback(ctx.callback_id, text)

    async def show(self, ctx: Ctx, screen: Screen) -> None:
        markup = to_markup(screen.keyboard)
        if ctx.message_id is not None:
            try:
                await self.api.edit_message(ctx.chat_id, ctx.message_id, screen.text, markup)
                return
            except BoundaryError:
                pass  # the old message is gone or too old to edit: send a fresh one
        ctx.message_id = await self.api.send_message(ctx.chat_id, screen.text, markup)

    async def loading(self, ctx: Ctx, text: str = LOADING) -> None:
        await self.ack(ctx)
        await self.api.send_chat_action(ctx.chat_id, "typing")
        if ctx.message_id is not None:
            try:
                await self.api.edit_message(ctx.chat_id, ctx.message_id, text, None)
            except BoundaryError:
                ctx.message_id = None

    async def fetch(
        self, ctx: Ctx, call: Callable[[Connector], Awaitable[Any]]
    ) -> tuple[Any, str | None]:
        await self.loading(ctx)
        return await self.ops.fetch(call)

    async def preferences(self) -> SearchPreferences:
        return await self.ops.preferences()

    # ---- dispatch ---------------------------------------------------------------------
    async def dispatch(self, ctx: Ctx, data: str) -> str | None:  # noqa: C901
        head, _, rest = data.partition(":")
        if data == "m:main":
            await self.store.clear_awaiting()
            await self.show(ctx, screens.main_menu())
        elif data in ("m:msgs", "m:msgs:r"):
            await self.messages(ctx, refresh=data.endswith(":r"))
        elif data == "m:search":
            await self.show(ctx, screens.search_menu())
        elif data in ("s:people", "s:firms", "s:jobs"):
            await self.store.set_awaiting(f"search:{rest}", {"msg": ctx.message_id})
            await self.show(ctx, screens.search_prompt(rest))
        elif data == "m:pins":
            await self.show(ctx, screens.pins_screen(await self.store.pins()))
        elif data == "m:jobs":
            await self.jobs(ctx)
        elif data == "m:saved":
            await self.show(ctx, screens.saved_screen(await self.store.saved()))
        elif data == "m:set":
            await self.show(ctx, screens.settings_main(await self.preferences()))
        elif data == "m:status":
            await self.status(ctx)
        elif data == "x:cancel":
            await self.store.clear_awaiting()
            await self.show(ctx, screens.main_menu())
        elif data == "x:noop":
            return None
        elif head == "set":
            return await self.settings(ctx, rest)
        elif head == "dg":
            await self.digest(ctx, rest)
        elif head == "r":
            return await self.page(ctx, rest)
        elif head == "pin":
            return await self.pin_result(rest)
        elif head == "sv":
            return await self.save_search(rest)
        elif head == "sr":
            await self.run_saved(ctx, rest)
        elif head == "sd":
            await self.store.delete_saved(rest)
            await self.show(ctx, screens.saved_screen(await self.store.saved()))
            return "Deleted"
        elif head in ("pd", "up", "nt", "tg", "lb"):
            return await self.pin_action(ctx, head, rest)
        elif data == "m:facts":
            await self.facts(ctx)
        elif head == "fc":
            return await self.fact_decision(ctx, rest)
        elif data == "fa":
            await self.store.set_awaiting("fact", {"msg": ctx.message_id})
            await self.show(
                ctx,
                screens.prompt(
                    "Send the fact as kind: text, e.g. skill: Python, 3 years. Kinds: "
                    + ", ".join(facts.FACT_KINDS)
                    + ". It is saved as confirmed."
                ),
            )
        elif data == "fx":
            return await self.extract_facts(ctx)
        elif head == "tk":
            return await self.track(ctx, rest)
        else:
            return "Unknown button."
        return None

    # ---- messages, status, jobs ---------------------------------------------------------
    async def messages(self, ctx: Ctx, *, refresh: bool) -> None:
        await self.loading(ctx)
        result, error = await self.ops.messages(refresh=refresh)
        if error is not None:
            await self.show(ctx, screens.error_screen(error))
            return
        parsed: Parsed[Any] = result
        await self.show(ctx, screens.messages_screen(parsed.items, parsed.skipped))

    async def status(self, ctx: Ctx) -> None:
        policy = await self.db.policy()
        state, ok = await self.linked.state()
        preferences = await self.preferences()
        digest = await self.store.digest()
        await self.show(
            ctx,
            screens.status_screen(
                mode=policy.mode,
                paused=policy.paused,
                linkedin_state=state,
                linkedin_ok=ok,
                pinned=len(await self.store.pins()),
                saved=len(await self.store.saved()),
                preferences_revision=preferences.revision,
                digest_on=digest.enabled,
                llm_enabled=self.llm_enabled,
            ),
        )

    async def jobs(self, ctx: Ctx) -> None:
        matches = await self.ops.ranked_jobs()
        if not matches:
            await self.show(
                ctx,
                Screen(
                    "💼 No matches yet. Add roles in Settings and pin firms with a job board.",
                    [[Button("⚙️ Settings", "m:set"), screens.HOME]],
                ),
            )
            return
        items = [item.card() for item in matches[:30]]
        await self.show_results(ctx, "jobs", "", items, header="💼 Best matches for your settings")

    # ---- search and results -------------------------------------------------------------
    async def show_results(
        self,
        ctx: Ctx,
        kind: str,
        query: str,
        items: list[dict[str, Any]],
        *,
        header: str,
        saved_id: str | None = None,
    ) -> None:
        token = self.tokens.issue(
            {"kind": kind, "query": query, "items": items, "header": header, "saved_id": saved_id}
        )
        await self.show(ctx, self.render_card(self.tokens.get(token) or {}, token, 0))

    def render_card(self, payload: dict[str, Any], token: str, index: int) -> Screen:
        return screens.card(
            payload["header"],
            payload["items"],
            index,
            token,
            pinnable=payload["kind"] in SEARCH_KINDS,
            saved_id=payload["saved_id"],
            # "Best matches" has no query to rerun, so it cannot become a saved search.
            can_save=payload["kind"] in ALL_KINDS and bool(payload["query"]),
            draftable=self.generator is not None,
        )

    async def page(self, ctx: Ctx, rest: str) -> str | None:
        token, _, index = rest.partition(":")
        payload = self.tokens.get(token)
        if payload is None or not index.isdecimal() or int(index) >= len(payload["items"]):
            return EXPIRED
        await self.show(ctx, self.render_card(payload, token, int(index)))
        return None

    async def search(
        self, ctx: Ctx, kind: SearchKind, query: str, *, refresh: bool = False
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        await self.loading(ctx)
        items, error = await self.ops.search(kind, query, refresh=refresh)
        if error is not None or items is None:
            return None, error
        return [item.card() for item in items], None

    async def run_search(self, ctx: Ctx, kind: SearchKind, query: str) -> None:
        items, error = await self.search(ctx, kind, query)
        if error is not None or items is None:
            await self.show(ctx, screens.error_screen(error or "Search failed.", "m:search"))
        elif not items:
            await self.show(
                ctx, screens.error_screen(f"No {kind} found for “{query}”.", "m:search")
            )
        else:
            await self.show_results(ctx, kind, query, items, header=f"🔎 {kind}: {query}")

    async def run_saved(self, ctx: Ctx, saved_id: str) -> None:
        if await self.ops.saved(saved_id) is not None:
            await self.loading(ctx)
        run = await self.ops.run_saved(saved_id)
        if run.saved is None:
            await self.show(ctx, screens.error_screen(run.error or "", "m:saved"))
            return
        if run.error is not None:
            await self.show(ctx, screens.error_screen(run.error, "m:saved"))
            return
        if not run.new_items:
            await self.show(ctx, screens.no_new_screen(run.saved.name, saved_id))
            return
        await self.show_results(
            ctx,
            run.saved.kind,
            run.saved.query,
            [item.card() for item in run.new_items],
            header=f"💾 {run.saved.name} — {len(run.new_items)} new",
            saved_id=saved_id,
        )

    async def pin_result(self, rest: str) -> str:
        token, _, index = rest.partition(":")
        payload = self.tokens.get(token)
        if payload is None or not index.isdecimal() or int(index) >= len(payload["items"]):
            return EXPIRED
        item = payload["items"][int(index)]
        await self.store.pin(
            "person" if payload["kind"] == "people" else "firm", item["url"], item["name"]
        )
        return f"Pinned {item['name']}"

    async def save_search(self, token: str) -> str:
        payload = self.tokens.get(token)
        if payload is None:
            return EXPIRED
        if not payload["query"]:
            return "These results have no search words to save."
        await self.ops.save_search(
            payload["kind"], payload["query"], [item["key"] for item in payload["items"]]
        )
        return "Saved. Rerun it from 💾 Saved to see only new results."

    # ---- tracker and drafts -------------------------------------------------------------
    async def track(self, ctx: Ctx, rest: str) -> str | None:
        token, _, tail = rest.partition(":")
        index, _, action = tail.partition(":")
        payload = self.tokens.get(token)
        if payload is None or not index.isdecimal() or int(index) >= len(payload["items"]):
            return EXPIRED
        item = payload["items"][int(index)]
        if not item.get("posting"):
            return "Only postings can be tracked."
        source, _, external_id = str(item["posting"]).partition(":")
        if action == "w":
            return await self.draft(ctx, source, external_id)
        statuses: dict[str, TrackerStatus] = {"i": "interested", "a": "applied", "x": "dismissed"}
        status = statuses.get(action)
        if status is None:
            return "Unknown button."
        if item.get("status") == status:
            status = "new"  # pressing the same status again clears it
        await self.ops.set_status(source, external_id, status)
        item["status"] = status
        await self.show(ctx, self.render_card(payload, token, int(index)))
        return "Cleared." if status == "new" else f"Marked {status}."

    async def draft(self, ctx: Ctx, source: str, external_id: str) -> str | None:
        if self.generator is None:
            return "No local model is configured (inference.model)."
        record = await self.db.posting(source, external_id)
        if record is None:
            return "That posting is gone."
        await self.ack(ctx, "Drafting with your local model…")
        await self.api.send_chat_action(ctx.chat_id, "typing")
        try:
            artifact = await draft_cover_letter(self.db, self.generator, record.observation)
        except AdapterError as exc:
            await self.api.send_message(
                ctx.chat_id, f"Draft not produced: {exc.code}. Nothing was saved."
            )
            return None
        text = await self.db.artifact_text(artifact)
        await self.api.send_message(ctx.chat_id, f"Draft for review (not sent anywhere):\n\n{text}")
        return None

    # ---- facts ----------------------------------------------------------------------------
    async def facts(self, ctx: Ctx) -> None:
        await self.show(ctx, screens.facts_screen(await facts.facts(self.db), self.llm_enabled))

    async def fact_decision(self, ctx: Ctx, rest: str) -> str:
        fact_id, _, decision = rest.rpartition(":")
        if decision == "c":
            await facts.confirm(self.db, fact_id)
        elif decision == "r":
            await facts.reject(self.db, fact_id)
        else:
            return "Unknown button."
        await self.facts(ctx)
        return "Confirmed." if decision == "c" else "Rejected."

    async def extract_facts(self, ctx: Ctx) -> str | None:
        if self.generator is None:
            return "No local model is configured (inference.model)."
        profile = await self.db.profile()
        cv = await baseline_cv(self.db)
        if cv is None or profile.baseline_artifact_id is None:
            return "Store your CV first (finish onboarding)."
        await self.loading(ctx, "🤖 Reading your CV with the local model…")
        try:
            added = await facts.extract(self.db, self.generator, cv, profile.baseline_artifact_id)
        except AdapterError as exc:
            await self.facts(ctx)
            return f"No facts proposed: {exc.code}."
        await self.facts(ctx)
        return f"{len(added)} fact(s) proposed. Confirm the true ones."

    # ---- pins ---------------------------------------------------------------------------
    async def pin_by_key(self, key: str) -> Pin | None:
        return next((p for p in await self.store.pins() if pin_key(p.url) == key), None)

    async def pin_action(self, ctx: Ctx, action: str, key: str) -> str | None:
        pin = await self.pin_by_key(key)
        if pin is None:
            await self.show(ctx, screens.pins_screen(await self.store.pins()))
            return "That pin no longer exists."
        if action == "pd":
            await self.show(ctx, screens.pin_detail(pin))
        elif action == "up":
            await self.store.unpin(pin.kind, pin.url)
            await self.show(ctx, screens.pins_screen(await self.store.pins()))
            return f"Unpinned {pin.name}"
        elif action == "nt":
            await self.store.set_awaiting(f"note:{key}", {"msg": ctx.message_id})
            await self.show(
                ctx, screens.prompt(f"Send a note for {pin.name} (max 500 characters).")
            )
        elif action == "tg":
            await self.store.set_awaiting(f"tags:{key}", {"msg": ctx.message_id})
            await self.show(ctx, screens.prompt(f"Send tags for {pin.name}, separated by commas."))
        else:
            return await self.link_board(ctx, pin)
        return None

    async def link_board(self, ctx: Ctx, pin: Pin) -> str | None:
        if self.ops.finder is None:
            return "Job-board lookup is not configured."
        await self.loading(ctx, "🔎 Looking for a public job board…")
        found = await self.ops.link_board(pin)
        if found is None:
            await self.show(ctx, screens.pin_detail(pin))
            return "No public Greenhouse, Lever or Ashby board found."
        provider, slug = found
        updated = await self.pin_by_key(pin_key(pin.url))
        await self.show(ctx, screens.pin_detail(updated or pin))
        return f"Linked {provider}/{slug}. Openings are collected when the bot runs (serve)."

    # ---- settings -----------------------------------------------------------------------
    async def settings_screen(self, area: str) -> Screen:
        preferences = await self.preferences()
        if area == "loc":
            return screens.locations_screen(preferences)
        if area == "roles":
            return screens.roles_screen(preferences)
        if area == "firm":
            return screens.firms_screen(preferences, len(await self.store.pins("firm")))
        if area == "lang":
            return screens.language_screen(preferences)
        if area == "prio":
            return screens.priorities_screen(preferences)
        if area == "digest":
            return screens.digest_screen(await self.store.digest())
        if area == "interests":
            return screens.interests_screen(preferences, self.llm_enabled)
        return screens.settings_main(preferences)

    async def settings(self, ctx: Ctx, rest: str) -> str | None:  # noqa: C901
        parts = rest.split(":")
        area = parts[0]
        if len(parts) == 1 and area in (
            "loc",
            "roles",
            "firm",
            "lang",
            "prio",
            "digest",
            "interests",
        ):
            await self.show(ctx, await self.settings_screen(area))
        elif area == "remote":
            await self.ops.cycle_remote()
            await self.show(ctx, await self.settings_screen("loc"))
        elif area == "intern":
            await self.ops.toggle_internship()
            await self.show(ctx, await self.settings_screen("loc"))
        elif area == "add" and len(parts) == 2 and parts[1] in LIST_FIELDS:
            await self.store.set_awaiting(f"add:{parts[1]}", {"msg": ctx.message_id})
            label = LIST_FIELDS[parts[1]]
            await self.show(
                ctx,
                screens.prompt(f"Send the {label}(s) to add, separated by commas or new lines."),
            )
        elif area == "del" and len(parts) == 3 and parts[1] in LIST_FIELDS and parts[2].isdecimal():
            await self.ops.edit_list(parts[1], delete=int(parts[2]))
            await self.show(ctx, await self.settings_screen(FIELD_AREA[parts[1]]))
        elif area == "lang" and len(parts) == 2 and parts[1] in screens.LANGUAGES:
            await self.ops.toggle_language(parts[1])
            await self.show(ctx, await self.settings_screen("lang"))
        elif area == "age" and len(parts) == 2 and parts[1] in screens.AGE_PRESETS:
            await self.ops.set_max_age(None if parts[1] == "any" else int(parts[1]))
            await self.show(ctx, await self.settings_screen("lang"))
        elif area == "prio" and len(parts) == 3:
            await self.ops.change_priority(parts[1], parts[2])
            await self.show(ctx, await self.settings_screen("prio"))
        elif area == "interests" and parts[1:] == ["edit"]:
            await self.store.set_awaiting("interests", {"msg": ctx.message_id})
            await self.show(
                ctx,
                screens.prompt("Send what you are interested in (free text, max 2000 characters)."),
            )
        else:
            return "Unknown button."
        return None

    async def digest(self, ctx: Ctx, rest: str) -> None:
        current = await self.store.digest()
        if rest == "toggle":
            await self.store.update_digest(enabled=not current.enabled)
        elif rest in ("alerts_boards", "alerts_messages"):
            await self.store.update_digest(**{rest: not getattr(current, rest)})
        elif rest in ("time:+", "time:-"):
            hours, minutes = (int(part) for part in current.local_time.split(":"))
            total = (hours * 60 + minutes + (30 if rest.endswith("+") else -30)) % (24 * 60)
            await self.store.update_digest(local_time=f"{total // 60:02d}:{total % 60:02d}")
        elif rest == "now":
            await self.digest_now(ctx)
            return
        else:
            raise ValueError("unknown_digest_action")
        await self.show(ctx, await self.settings_screen("digest"))

    async def digest_now(self, ctx: Ctx) -> None:
        if self.digest_service is None:
            await self.show(ctx, screens.error_screen("The digest is not available.", "set:digest"))
            return
        await self.loading(ctx, "⏳ Building your digest…")
        text = await self.digest_service.build()
        back = [[Button("⬅ Digest settings", "set:digest"), screens.HOME]]
        if len(text) <= SCREEN_LIMIT:
            await self.show(ctx, Screen(text, back))
            return
        # Too long for one screen: send it whole (split by the adapter), then the buttons.
        await self.api.send_message(ctx.chat_id, text)
        ctx.message_id = None
        await self.show(ctx, Screen("📰 Digest sent above.", back))

    # ---- pending text input -------------------------------------------------------------
    async def consume(self, ctx: Ctx, name: str, text: str) -> None:
        kind, _, arg = name.partition(":")
        chosen = search_kind(arg)
        if kind == "search" and chosen is not None:
            if not 1 <= len(text) <= 100:
                await self.show(
                    ctx,
                    screens.error_screen("Give search words of 1 to 100 characters.", "m:search"),
                )
                return
            await self.run_search(ctx, chosen, text)
        elif kind == "add" and arg in LIST_FIELDS:
            await self.ops.edit_list(arg, add=split_items(text))
            await self.show(ctx, await self.settings_screen(FIELD_AREA[arg]))
        elif kind in ("note", "tags"):
            pin = await self.pin_by_key(arg)
            if pin is not None:
                if kind == "note":
                    await self.store.set_note(pin.kind, pin.url, text[:500])
                else:
                    await self.store.set_tags(pin.kind, pin.url, tuple(split_items(text)))
            updated = await self.pin_by_key(arg)
            await self.show(
                ctx,
                screens.pin_detail(updated)
                if updated
                else screens.pins_screen(await self.store.pins()),
            )
        elif kind == "interests":
            await self.ops.set_interests(text)
            await self.show(ctx, await self.settings_screen("interests"))
        elif kind == "fact":
            try:
                await add_typed_fact(self.db, text)
            except ValueError:
                await self.show(
                    ctx, screens.error_screen("A fact is 1 to 500 characters.", "m:facts")
                )
                return
            await self.facts(ctx)
        else:
            await self.show(ctx, screens.main_menu())
