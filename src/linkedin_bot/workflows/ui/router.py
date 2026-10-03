"""Button presses and pending text input. Everything the user can tap goes through here."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from linkedin_bot.adapters.ats import Board
from linkedin_bot.adapters.linkedin.pages import Parsed
from linkedin_bot.adapters.telegram import TelegramAPI
from linkedin_bot.domain.matching import rank
from linkedin_bot.domain.models import (
    DEFAULT_PRIORITIES,
    SearchPreferences,
    SourceObservation,
    digest,
    now,
)
from linkedin_bot.domain.workspace import Pin
from linkedin_bot.runtime.privacy import BoundaryError
from linkedin_bot.storage.database import Database
from linkedin_bot.storage.linkedin import LinkedInStore
from linkedin_bot.storage.workspace import WorkspaceStore
from linkedin_bot.workflows.linkedin_commands import Connector, LinkedInCommands
from linkedin_bot.workflows.ui import screens
from linkedin_bot.workflows.ui.keyboard import Button, Screen, to_markup
from linkedin_bot.workflows.ui.screens import LIST_FIELDS, LOADING, pin_key
from linkedin_bot.workflows.ui.tokens import TokenMap

BoardFinder = Callable[[str], Awaitable[tuple[str, str] | None]]
SEARCH_KINDS = ("people", "firms")  # kinds whose results can be pinned
ALL_KINDS = ("people", "firms", "jobs")
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


@dataclass
class Ctx:
    chat_id: int
    message_id: int | None
    callback_id: str | None = None
    answered: bool = False


def split_job_query(text: str) -> tuple[str, str | None]:
    """'security intern in Berlin' -> ('security intern', 'Berlin')."""
    words, separator, place = text.rpartition(" in ")
    if separator and words.strip() and place.strip():
        return words.strip(), place.strip()
    return text.strip(), None


def split_items(text: str) -> list[str]:
    parts = [part.strip() for part in text.replace("\n", ",").split(",")]
    return [part for part in parts if 0 < len(part) <= 60][:20]


class UI:
    def __init__(
        self,
        database: Database,
        api: TelegramAPI,
        linkedin: LinkedInCommands | None,
        board_finder: BoardFinder | None = None,
        llm_enabled: bool = False,
    ) -> None:
        self.db, self.api, self.linkedin, self.finder = database, api, linkedin, board_finder
        self.llm_enabled = llm_enabled
        self.store = WorkspaceStore(database)
        self.linked = LinkedInStore(database)
        self.tokens = TokenMap()

    # ---- entry points -----------------------------------------------------------------
    async def open_menu(self, chat_id: int) -> None:
        await self.store.clear_awaiting()
        await self.show(Ctx(chat_id, None), screens.main_menu())

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
        if self.linkedin is None:
            return None, "LinkedIn is not enabled on this installation."
        return await self.linkedin.fetch(call)

    async def preferences(self) -> SearchPreferences:
        return await self.db.preferences()

    async def edit_prefs(self, change: Callable[[SearchPreferences], SearchPreferences]) -> None:
        await self.db.update_preferences(change)

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
        else:
            return "Unknown button."
        return None

    # ---- messages, status, jobs ---------------------------------------------------------
    async def messages(self, ctx: Ctx, *, refresh: bool) -> None:
        result, error = await self.fetch(ctx, lambda c: c.messages(10, refresh=refresh))
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
        recent = await self.db.opportunities(since=now() - timedelta(days=60))
        matches = rank(recent, await self.preferences(), await self.store.pinned_tokens())
        if not matches:
            await self.show(
                ctx,
                Screen(
                    "💼 No matches yet. Add roles in Settings and pin firms with a job board.",
                    [[Button("⚙️ Settings", "m:set"), screens.HOME]],
                ),
            )
            return
        items = [
            {
                "title": f"💼 {m.title}",
                "lines": [
                    f"{m.company or m.source} · {m.location or 'location unknown'}",
                    f"score {m.score} — {', '.join(m.reasons)}",
                    f"Gaps: {', '.join(m.gaps)}" if m.gaps else "",
                ],
                "url": m.url,
                "key": m.url,
            }
            for m in matches[:30]
        ]
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
            can_save=payload["kind"] in ALL_KINDS,
        )

    async def page(self, ctx: Ctx, rest: str) -> str | None:
        token, _, index = rest.partition(":")
        payload = self.tokens.get(token)
        if payload is None or not index.isdecimal() or int(index) >= len(payload["items"]):
            return EXPIRED
        await self.show(ctx, self.render_card(payload, token, int(index)))
        return None

    def cards(self, kind: str, parsed: Parsed[Any]) -> list[dict[str, Any]]:
        if kind == "people":
            return [
                {
                    "title": f"👤 {p.name}",
                    "lines": [p.headline, p.location or ""],
                    "url": p.url,
                    "key": p.url,
                    "name": p.name,
                }
                for p in parsed.items
            ]
        return [
            {
                "title": f"🏢 {c.name}",
                "lines": [c.tagline],
                "url": c.url,
                "key": c.url,
                "name": c.name,
            }
            for c in parsed.items
        ]

    async def search(
        self, ctx: Ctx, kind: str, query: str, *, refresh: bool = False
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        if kind == "jobs":
            words, place = split_job_query(query)
            result, error = await self.fetch(
                ctx, lambda c: c.search_jobs(words, place, refresh=refresh)
            )
            if error is not None:
                return None, error
            return await self.job_cards(result), None
        if kind == "people":
            result, error = await self.fetch(ctx, lambda c: c.search_people(query, refresh=refresh))
        else:
            result, error = await self.fetch(
                ctx, lambda c: c.search_companies(query, refresh=refresh)
            )
        if error is not None:
            return None, error
        return self.cards(kind, result), None

    async def job_cards(self, parsed: Parsed[Any]) -> list[dict[str, Any]]:
        """Store the public postings, then order them by your settings (best fit first)."""
        stamp = now()
        observations = [
            SourceObservation(
                source="linkedin",
                external_id=job.id,
                url=job.url,
                title=job.title,
                location=job.location,
                company=job.company,
                content_hash=digest(f"{job.id}|{job.title}|{job.company}|{job.location}".encode()),
                observed_at=stamp,
            )
            for job in parsed.items
        ]
        await self.db.save_observations(observations)
        matches = rank(observations, await self.preferences(), await self.store.pinned_tokens())
        matched = {m.external_id for m in matches}
        cards = [
            {
                "title": f"💼 {m.title}",
                "lines": [
                    f"{m.company} · {m.location or 'location unknown'}",
                    f"score {m.score} — {', '.join(m.reasons)}",
                    f"Gaps: {', '.join(m.gaps)}" if m.gaps else "",
                ],
                "url": m.url,
                "key": m.url,
            }
            for m in matches
        ]
        cards += [
            {
                "title": f"💼 {o.title}",
                "lines": [
                    f"{o.company} · {o.location or 'location unknown'}",
                    "outside your settings",
                ],
                "url": o.url,
                "key": o.url,
            }
            for o in observations
            if o.external_id not in matched
        ]
        return cards

    async def run_search(self, ctx: Ctx, kind: str, query: str) -> None:
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
        saved = next((s for s in await self.store.saved() if s.id == saved_id), None)
        if saved is None:
            await self.show(
                ctx, screens.error_screen("That saved search no longer exists.", "m:saved")
            )
            return
        items, error = await self.search(ctx, saved.kind, saved.query, refresh=True)
        if error is not None or items is None:
            await self.show(ctx, screens.error_screen(error or "Search failed.", "m:saved"))
            return
        fresh = set(await self.store.record_run(saved_id, [item["key"] for item in items]))
        new_items = [item for item in items if item["key"] in fresh]
        if not new_items:
            await self.show(ctx, screens.no_new_screen(saved.name, saved_id))
            return
        await self.show_results(
            ctx,
            saved.kind,
            saved.query,
            new_items,
            header=f"💾 {saved.name} — {len(new_items)} new",
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
        saved_id = await self.store.save_search(
            payload["kind"], payload["query"], f"{payload['kind']}: {payload['query']}"
        )
        await self.store.record_run(saved_id, [item["key"] for item in payload["items"]])
        return "Saved. Rerun it from 💾 Saved to see only new results."

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
        if self.finder is None:
            return "Job-board lookup is not configured."
        await self.loading(ctx, "🔎 Looking for a public job board…")
        found = await self.finder(pin.name)
        if found is None:
            await self.show(ctx, screens.pin_detail(pin))
            return "No public Greenhouse, Lever or Ashby board found."
        provider, slug = found
        await self.store.link_board(pin.url, provider, slug)
        board = Board.model_validate({"provider": provider, "slug": slug})
        await self.db.schedule(
            f"ats:{provider}:{slug}", board.model_dump_json(), now(), BOARD_INTERVAL_SECONDS
        )
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
            await self.edit_prefs(
                lambda p: p.model_copy(
                    update={"remote": {None: True, True: False, False: None}[p.remote]}
                )
            )
            await self.show(ctx, await self.settings_screen("loc"))
        elif area == "intern":
            await self.edit_prefs(
                lambda p: p.model_copy(update={"internship": None if p.internship else True})
            )
            await self.show(ctx, await self.settings_screen("loc"))
        elif area == "add" and len(parts) == 2 and parts[1] in LIST_FIELDS:
            await self.store.set_awaiting(f"add:{parts[1]}", {"msg": ctx.message_id})
            label = LIST_FIELDS[parts[1]]
            await self.show(
                ctx,
                screens.prompt(f"Send the {label}(s) to add, separated by commas or new lines."),
            )
        elif area == "del" and len(parts) == 3 and parts[1] in LIST_FIELDS and parts[2].isdecimal():
            await self.edit_list(parts[1], delete=int(parts[2]))
            await self.show(ctx, await self.settings_screen(FIELD_AREA[parts[1]]))
        elif area == "lang" and len(parts) == 2 and parts[1] in screens.LANGUAGES:
            await self.toggle_language(parts[1])
            await self.show(ctx, await self.settings_screen("lang"))
        elif area == "age" and len(parts) == 2 and parts[1] in screens.AGE_PRESETS:
            days = None if parts[1] == "any" else int(parts[1])
            await self.edit_prefs(lambda p: p.model_copy(update={"max_age_days": days}))
            await self.show(ctx, await self.settings_screen("lang"))
        elif area == "prio" and len(parts) == 3:
            await self.change_priority(parts[1], parts[2])
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

    async def edit_list(
        self, field: str, *, add: list[str] | None = None, delete: int | None = None
    ) -> None:
        def change(p: SearchPreferences) -> SearchPreferences:
            values = list(getattr(p, field))
            if delete is not None and 0 <= delete < len(values):
                values.pop(delete)
            lowered = {value.lower() for value in values}
            for item in add or []:
                if item.lower() not in lowered:
                    values.append(item)
                    lowered.add(item.lower())
            return p.model_copy(update={field: tuple(values)})

        await self.edit_prefs(change)

    async def toggle_language(self, code: str) -> None:
        def change(p: SearchPreferences) -> SearchPreferences:
            chosen = list(p.languages)
            chosen.remove(code) if code in chosen else chosen.append(code)
            return p.model_copy(update={"languages": tuple(chosen)})

        await self.edit_prefs(change)

    async def change_priority(self, operation: str, name: str) -> None:
        if name not in DEFAULT_PRIORITIES:
            raise ValueError("unknown_priority")

        def change(p: SearchPreferences) -> SearchPreferences:
            order = list(p.priorities)
            if operation == "add" and name not in order:
                order.append(name)
            elif operation == "rm" and name in order and len(order) > 1:
                order.remove(name)
            elif operation in ("up", "dn") and name in order:
                index = order.index(name)
                target = index - 1 if operation == "up" else index + 1
                if 0 <= target < len(order):
                    order[index], order[target] = order[target], order[index]
            return p.model_copy(update={"priorities": tuple(order)})

        await self.edit_prefs(change)

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
        else:
            raise ValueError("unknown_digest_action")
        await self.show(ctx, await self.settings_screen("digest"))

    # ---- pending text input -------------------------------------------------------------
    async def consume(self, ctx: Ctx, name: str, text: str) -> None:
        kind, _, arg = name.partition(":")
        if kind == "search" and arg in ALL_KINDS:
            if not 1 <= len(text) <= 100:
                await self.show(
                    ctx,
                    screens.error_screen("Give search words of 1 to 100 characters.", "m:search"),
                )
                return
            await self.run_search(ctx, arg, text)
        elif kind == "add" and arg in LIST_FIELDS:
            await self.edit_list(arg, add=split_items(text))
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
            await self.edit_prefs(lambda p: p.model_copy(update={"interests": text[:2000]}))
            await self.show(ctx, await self.settings_screen("interests"))
        else:
            await self.show(ctx, screens.main_menu())
