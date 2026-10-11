# ruff: noqa: E501  (fixture text)
import asyncio
from datetime import timedelta

import pytest

from linkedin_bot.adapters.telegram import Update, parse_updates
from linkedin_bot.domain.models import now
from linkedin_bot.runtime.privacy import BoundaryError
from linkedin_bot.storage.database import Database
from linkedin_bot.storage.telegram import TelegramStore
from linkedin_bot.workflows.telegram_bot import Bot, poll_once

USER, CHAT = 111, 222


class FakeAPI:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.markups: list[dict | None] = []
        self.edits: list[tuple[int, int, str, dict | None]] = []
        self.answers: list[tuple[str, str]] = []
        self.actions: list[tuple[int, str]] = []
        self._next_id = 100

    async def send_message(self, chat_id: int, text: str, markup: dict | None = None) -> int:
        self.sent.append((chat_id, text))
        self.markups.append(markup)
        self._next_id += 1
        return self._next_id

    async def edit_message(
        self, chat_id: int, message_id: int, text: str, markup: dict | None = None
    ) -> None:
        self.edits.append((chat_id, message_id, text, markup))

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answers.append((callback_id, text))

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.actions.append((chat_id, action))


def callback(
    update_id, data, user=USER, chat=CHAT, message_id=500, chat_type="private", callback_id="cb"
):
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"{callback_id}{update_id}",
                "from": {"id": user},
                "data": data,
                "message": {"message_id": message_id, "chat": {"id": chat, "type": chat_type}},
            },
        }
    )


def update(update_id, text, user=USER, chat=CHAT, chat_type="private"):
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "from": {"id": user},
                "chat": {"id": chat, "type": chat_type},
                "text": text,
            },
        }
    )


def run(tmp_path, scenario):
    async def main():
        db = Database(tmp_path)
        try:
            await db.open()
            api = FakeAPI()
            return await scenario(db, api, Bot(db, api), TelegramStore(db))
        finally:
            await db.close()

    return asyncio.run(main())


def test_pairing_binds_one_private_chat_with_one_use_code(tmp_path):
    async def scenario(db, api, bot, store):
        code = await store.begin_pairing(now())
        assert await store.binding() is None
        await bot.handle(update(1, "/start wrong-code"))
        assert await store.binding() is None and api.sent == []
        await bot.handle(update(2, f"/start {code}", chat_type="group"))
        assert await store.binding() is None
        await bot.handle(update(3, f"/start {code}"))
        assert await store.binding() == (USER, CHAT)
        assert any("draft_only" in text for _, text in api.sent)
        with pytest.raises(BoundaryError):
            await store.begin_pairing(now())

    run(tmp_path, scenario)


def test_expired_and_exhausted_codes_do_not_pair(tmp_path):
    async def scenario(db, api, bot, store):
        code = await store.begin_pairing(now() - timedelta(minutes=11))
        await bot.handle(update(1, f"/start {code}"))
        assert await store.binding() is None
        code = await store.begin_pairing(now())
        for number in range(2, 9):
            await bot.handle(update(number, "/start nope"))
        await bot.handle(update(20, f"/start {code}"))
        assert await store.binding() is None

    run(tmp_path, scenario)


def test_strangers_get_no_reply_and_no_state_change(tmp_path):
    async def scenario(db, api, bot, store):
        code = await store.begin_pairing(now())
        await bot.handle(update(1, f"/start {code}"))
        sent = len(api.sent)
        await bot.handle(update(2, "/status", user=999))
        await bot.handle(update(3, "/pause", chat=333))
        await bot.handle(update(4, "/status", chat_type="group"))
        assert len(api.sent) == sent
        assert (await db.status())["paused"] is False

    run(tmp_path, scenario)


def test_replayed_update_ids_are_ignored(tmp_path):
    async def scenario(db, api, bot, store):
        code = await store.begin_pairing(now())
        await bot.handle(update(5, f"/start {code}"))
        sent = len(api.sent)
        await bot.handle(update(5, "/status"))
        await bot.handle(update(4, "/status"))
        assert len(api.sent) == sent

    run(tmp_path, scenario)


def test_onboarding_is_resumable_and_persists_preferences_facts_and_cv(tmp_path):
    async def scenario(db, api, bot, store):
        code = await store.begin_pairing(now())
        await bot.handle(update(1, f"/start {code}"))
        await bot.handle(update(2, "ML engineer internship, data scientist"))
        await bot.handle(update(3, "Warsaw, Berlin; remote ok"))
        # A fresh Bot object proves state lives in storage, not memory.
        bot = Bot(db, api)
        await bot.handle(update(4, "crypto, gambling"))
        await bot.handle(update(5, "EU citizen, no visa needed"))
        await bot.handle(update(6, "Jane Doe\nPython, SQL, 2 years at Acme"))
        assert (await store.onboarding())[0] == "done"
        preferences = await db.call(
            lambda c: c.execute("SELECT MAX(revision) FROM preferences").fetchone()[0]
        )
        assert preferences == 2
        facts = await db.facts(confirmed_only=True)
        assert [f.kind for f in facts] == ["work_eligibility"]
        assert facts[0].value == "EU citizen, no visa needed"
        assert (await db.profile()).baseline_artifact_id is not None
        await bot.handle(update(7, "/start"))
        assert "onboarding is complete" in api.sent[-1][1].lower()

    run(tmp_path, scenario)


def test_pause_and_resume_commands_do_not_release_effects(tmp_path):
    async def scenario(db, api, bot, store):
        code = await store.begin_pairing(now())
        await bot.handle(update(1, f"/start {code}"))
        await bot.handle(update(2, "/pause"))
        assert (await db.status())["paused"] is True
        await bot.handle(update(3, "/resume"))
        assert (await db.status())["paused"] is False
        assert "draft_only" in api.sent[-1][1]

    run(tmp_path, scenario)


def test_parse_updates_rejects_malformed_payloads_and_keeps_text_only():
    parsed = parse_updates(
        {
            "ok": True,
            "result": [
                {
                    "update_id": 1,
                    "message": {
                        "from": {"id": 1},
                        "chat": {"id": 2, "type": "private"},
                        "text": "hi",
                    },
                },
                {"update_id": 2, "edited_message": {}},
                {"update_id": 3, "message": {"chat": {"id": 2, "type": "private"}}},
            ],
        }
    )
    assert [u.update_id for u in parsed] == [1, 2, 3]
    assert parsed[0].message is not None and parsed[0].message.text == "hi"
    assert parsed[1].message is None and parsed[2].message is None
    with pytest.raises(BoundaryError):
        parse_updates({"ok": False})


def test_poll_once_advances_offset_and_handles_batch(tmp_path):
    class Poller(FakeAPI):
        def __init__(self, batches):
            super().__init__()
            self.batches, self.offsets = batches, []

        async def get_updates(self, offset, poll_seconds=30):
            self.offsets.append(offset)
            return self.batches.pop(0)

    async def scenario(db, api, bot, store):
        code = await store.begin_pairing(now())
        poller = Poller([[update(7, f"/start {code}")], [update(8, "/status")], []])
        bot = Bot(db, poller)
        assert await poll_once(bot, poller) == 1
        assert await poll_once(bot, poller) == 1
        assert await poll_once(bot, poller) == 0
        assert poller.offsets == [1, 8, 9]
        assert await store.binding() == (USER, CHAT)

    run(tmp_path, scenario)


def test_http_telegram_requires_token_from_environment(monkeypatch):
    from linkedin_bot.adapters.telegram import HttpTelegram

    monkeypatch.delenv("LINKEDIN_AUTOMATION_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:generic-name-must-be-ignored")
    with pytest.raises(BoundaryError, match="telegram_token_missing"):
        HttpTelegram()
    monkeypatch.setenv("LINKEDIN_AUTOMATION_BOT_TOKEN", "123:specific")
    assert HttpTelegram()._base.endswith("bot123:specific")


def test_opportunities_command_lists_ranked_matches_with_reasons(tmp_path):
    from linkedin_bot.domain.models import (
        CollectionPage,
        SearchPreferences,
        SourceObservation,
        digest,
    )

    async def scenario(db, api, bot, store):
        code = await store.begin_pairing(now())
        await bot.handle(update(1, f"/start {code}"))
        await bot.handle(update(2, "/opportunities"))
        assert "No matches yet" in api.sent[-1][1]
        await db.save_preferences(SearchPreferences(revision=2, roles=("data scientist",)), 1)
        await db.schedule("w", "{}", now(), 3600)
        work = await db.claim(now(), "w")
        item = SourceObservation(
            source="lever",
            external_id="x1",
            url="https://jobs.lever.co/acme/x1",
            title="Data Scientist",
            location="Berlin",
            content_hash=digest(b"x1"),
        )
        await db.persist_page(work, CollectionPage(items=(item,), coverage="complete"), now())
        await bot.handle(update(3, "/opportunities"))
        reply = api.sent[-1][1]
        assert "Data Scientist" in reply and "https://jobs.lever.co/acme/x1" in reply
        assert "role matches" in reply

    run(tmp_path, scenario)


def test_prepare_command_drafts_for_review_and_reports_missing_model(tmp_path):
    import json

    from linkedin_bot.domain.models import CandidateFact, CollectionPage, SourceObservation, digest

    class Gen:
        async def generate(self, prompt, schema):
            text = "I used Python and SQL at Acme."
            return json.dumps({"paragraphs": [{"text": text, "fact_ids": [fact_id]}]})

    fact_id = "f1"

    async def scenario(db, api, bot, store):
        code = await store.begin_pairing(now())
        await bot.handle(update(1, f"/start {code}"))
        await bot.handle(update(2, "/prepare lever:x1"))
        assert "No local model" in api.sent[-1][1]
        bot = Bot(db, api, Gen())
        await db.save_fact(
            CandidateFact(
                id=fact_id,
                kind="skill",
                value="Python and SQL at Acme",
                source_reference="t",
                status="confirmed",
                confirmed_at=now(),
            )
        )
        await db.schedule("w", "{}", now(), 3600)
        work = await db.claim(now(), "w")
        item = SourceObservation(
            source="lever",
            external_id="x1",
            url="https://jobs.lever.co/acme/x1",
            title="Data Scientist",
            content_hash=digest(b"x1"),
        )
        await db.persist_page(work, CollectionPage(items=(item,), coverage="complete"), now())
        await bot.handle(update(3, "/prepare lever:missing"))
        assert "Unknown opportunity" in api.sent[-1][1]
        await bot.handle(update(4, "/prepare lever:x1"))
        assert "not sent anywhere" in api.sent[-1][1] and "Python and SQL" in api.sent[-1][1]
        assert (await db.status())["artifacts"] == 1

    run(tmp_path, scenario)


def effect_setup(adapter_result="succeeded"):
    from linkedin_bot.domain.models import ActionResult, ActionSnapshot
    from linkedin_bot.workflows.executor import Executor

    class Adapter:
        calls = 0

        async def perform(self, action):
            Adapter.calls += 1
            return ActionResult(outcome=adapter_result, reason_code="done")

    def action(key="reply:1", policy_revision=3):
        return ActionSnapshot(
            kind="send_reply",
            target="someone",
            logical_key=key,
            content="Hi there",
            profile_revision=1,
            preferences_revision=1,
            policy_revision=policy_revision,
        )

    return Adapter, action, Executor


def test_policy_changes_need_confirmation_and_respect_hard_ceilings(tmp_path):
    async def scenario(db, api, bot, store):
        code = await store.begin_pairing(now())
        await bot.handle(update(1, f"/start {code}"))
        await bot.handle(update(2, "/policy cap communication 2"))
        assert "confirm" in api.sent[-1][1] and (await db.policy()).communication_cap == 0
        await bot.handle(update(3, "/policy cap communication 99 confirm"))
        assert "ceiling" in api.sent[-1][1] and (await db.policy()).communication_cap == 0
        await bot.handle(update(4, "/policy cap communication 2 confirm"))
        await bot.handle(update(5, "/policy mode reviewed confirm"))
        policy = await db.policy()
        assert (policy.communication_cap, policy.mode, policy.revision) == (2, "reviewed", 3)

    run(tmp_path, scenario)


def test_pending_approve_and_reject_flow_dispatches_only_after_approval(tmp_path):
    Adapter, action, Executor = effect_setup()

    async def scenario(db, api, bot, store):
        code = await store.begin_pairing(now())
        await bot.handle(update(1, f"/start {code}"))
        await bot.handle(update(2, "/policy cap communication 2 confirm"))
        await bot.handle(update(3, "/policy mode reviewed confirm"))
        executor = Executor(db, {"send_reply": Adapter()})
        bot = Bot(db, api, executor=executor)
        first, second = action(), action("reply:2")
        assert await executor.propose(first) == () and await executor.propose(second) == ()
        await bot.handle(update(4, "/pending"))
        listing = api.sent[-1][1]
        approve_id = await executor.approval_id(first.id)
        reject_id = await executor.approval_id(second.id)
        assert approve_id in listing and "Hi there" in listing and Adapter.calls == 0
        await bot.handle(update(5, f"/reject {reject_id}"))
        assert "Rejected" in api.sent[-1][1] and Adapter.calls == 0
        await bot.handle(update(6, f"/approve {approve_id}"))
        assert Adapter.calls == 1 and "succeeded" in api.sent[-1][1].lower()
        await bot.handle(update(7, f"/approve {approve_id}"))
        assert Adapter.calls == 1 and "no longer valid" in api.sent[-1][1].lower()

    run(tmp_path, scenario)


def test_default_install_cannot_dispatch_even_if_everything_is_approved(tmp_path):
    Adapter, action, Executor = effect_setup()

    async def scenario(db, api, bot, store):
        code = await store.begin_pairing(now())
        await bot.handle(update(1, f"/start {code}"))
        executor = Executor(db, {"send_reply": Adapter()})
        assert "zero_outbound_cap" in await executor.propose(action(policy_revision=1))
        await bot.handle(update(2, "/pending"))
        assert "Nothing waiting" in api.sent[-1][1] and Adapter.calls == 0

    run(tmp_path, scenario)


def test_resolve_records_user_attestation_for_unknown_outcomes(tmp_path):
    Adapter, action, Executor = effect_setup("unknown")

    async def scenario(db, api, bot, store):
        code = await store.begin_pairing(now())
        await bot.handle(update(1, f"/start {code}"))
        await bot.handle(update(2, "/policy cap communication 1 confirm"))
        await bot.handle(update(3, "/policy mode reviewed confirm"))
        executor = Executor(db, {"send_reply": Adapter()})
        bot = Bot(db, api, executor=executor)
        item = action()
        await executor.propose(item)
        await bot.handle(update(4, f"/approve {await executor.approval_id(item.id)}"))
        assert await executor.state("reply:1") == "unknown"
        await bot.handle(update(5, "/pending"))
        assert item.id in api.sent[-1][1]
        await bot.handle(update(6, f"/resolve {item.id} none"))
        assert "evidence note" in api.sent[-1][1]
        await bot.handle(update(7, f"/resolve {item.id} none sent folder shows nothing"))
        assert await executor.state("reply:1") == "failed"
        used = await db.used_today(executor.local_date(now()))
        assert used["communication"] == 0  # a confirmed no-effect frees the slot

    run(tmp_path, scenario)


def test_polling_is_allowed_while_a_pairing_code_is_pending(tmp_path):
    async def scenario(db, api, bot, store):
        assert await store.polling_allowed(now()) is False  # nothing to listen for
        code = await store.begin_pairing(now())
        assert await store.polling_allowed(now()) is True
        assert await store.polling_allowed(now() + timedelta(minutes=11)) is False
        await bot.handle(update(1, f"/start {code}"))
        assert await store.polling_allowed(now() + timedelta(days=1)) is True  # paired

    run(tmp_path, scenario)


def test_parse_updates_keeps_callback_queries_and_drops_malformed_ones():
    parsed = parse_updates(
        {
            "ok": True,
            "result": [
                {
                    "update_id": 5,
                    "callback_query": {
                        "id": "abc",
                        "from": {"id": 1},
                        "data": "m:main",
                        "message": {"message_id": 9, "chat": {"id": 2, "type": "private"}},
                    },
                },
                {"update_id": 6, "callback_query": {"id": "x"}},
            ],
        }
    )
    assert (
        parsed[0].callback_query.data == "m:main"
        and parsed[0].callback_query.message.message_id == 9
    )
    assert parsed[1].callback_query is None  # malformed: no sender, but the offset still advances


def test_http_telegram_sends_edits_answers_and_tolerates_unchanged_edits():
    import json

    import httpx

    from linkedin_bot.adapters.telegram import HttpTelegram

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = request.url.path.rsplit("/", 1)[1]
        calls.append((method, body))
        if method == "editMessageText" and body.get("text") == "same":
            return httpx.Response(
                400, json={"ok": False, "description": "Bad Request: message is not modified"}
            )
        if method == "editMessageText" and body.get("text") == "gone":
            return httpx.Response(
                400, json={"ok": False, "description": "message to edit not found"}
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    async def go():
        api = HttpTelegram("123:token", transport=httpx.MockTransport(handler))
        markup = {"inline_keyboard": [[{"text": "Go", "callback_data": "m:main"}]]}
        assert await api.send_message(5, "hi", markup) == 77
        await api.edit_message(5, 77, "new", markup)
        await api.edit_message(5, 77, "same", markup)  # Telegram refuses identical text; fine
        with pytest.raises(BoundaryError, match="telegram_edit_failed"):
            await api.edit_message(5, 77, "gone", markup)
        await api.answer_callback("cb1", "Pinned")
        await api.send_chat_action(5, "typing")
        await api.close()

    asyncio.run(go())
    methods = [m for m, _ in calls]
    assert methods == [
        "sendMessage",
        "editMessageText",
        "editMessageText",
        "editMessageText",
        "answerCallbackQuery",
        "sendChatAction",
    ]
    assert calls[0][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "m:main"
    assert calls[4][1] == {"callback_query_id": "cb1", "text": "Pinned"}


def test_get_updates_asks_for_messages_and_callbacks():
    import json

    import httpx

    from linkedin_bot.adapters.telegram import HttpTelegram

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": []})

    async def go():
        api = HttpTelegram("123:token", transport=httpx.MockTransport(handler))
        await api.get_updates(1, 0)
        await api.close()

    asyncio.run(go())
    assert seen[0]["allowed_updates"] == ["message", "callback_query"]


def test_poll_loop_survives_network_errors_and_stops_on_request(tmp_path):
    import httpx

    from linkedin_bot.workflows.telegram_bot import poll_forever

    class Flaky(FakeAPI):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def get_updates(self, offset, poll_seconds=30):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ReadTimeout("slow network")
            if self.calls == 2:
                raise BoundaryError("telegram_http_status")
            await asyncio.sleep(0.01)
            return []

    async def scenario(db, api, bot, store):
        flaky = Flaky()
        bot = Bot(db, flaky)
        stop = asyncio.Event()
        task = asyncio.create_task(poll_forever(bot, flaky, stop, backoff=0.01))
        await asyncio.sleep(0.2)
        assert flaky.calls >= 4  # kept polling after both failures
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    run(tmp_path, scenario)


def test_a_failing_handler_answers_once_and_the_bot_keeps_going(tmp_path):
    async def scenario(db, api, bot, store):
        code = await store.begin_pairing(now())
        await bot.handle(update(1, f"/start {code}"))
        original = db.status

        async def broken():
            raise RuntimeError("boom")

        db.status = broken
        await bot.handle(update(2, "/status"))  # must not raise
        assert "something went wrong" in api.sent[-1][1].lower()
        assert await store.last_update_id() == 2
        db.status = original
        await bot.handle(update(3, "/status"))
        assert "Paused" in api.sent[-1][1]
        sent = len(api.sent)
        db.status = broken
        await bot.handle(update(4, "/status", user=999))  # strangers still get nothing
        assert len(api.sent) == sent

    run(tmp_path, scenario)


def test_poll_forever_survives_a_failing_update(tmp_path):
    class Poller(FakeAPI):
        def __init__(self):
            super().__init__()
            self.batches = [[update(1, "/start nope")], [update(2, "/start nope")]]

        async def get_updates(self, offset, poll_seconds=30):
            if not self.batches:
                stop.set()
                return []
            return self.batches.pop(0)

    from linkedin_bot.workflows.telegram_bot import poll_forever

    stop = asyncio.Event()

    async def scenario(db, api, bot, store):
        poller = Poller()
        bot = Bot(db, poller)

        async def broken(*_args):
            raise RuntimeError("boom")

        bot.store.binding = broken
        await asyncio.wait_for(poll_forever(bot, poller, stop), timeout=5)
        assert await store.last_update_id() == 2

    run(tmp_path, scenario)


def test_a_typed_command_abandons_a_pending_button_prompt(tmp_path):
    async def scenario(db, api, bot, store):
        code = await store.begin_pairing(now())
        await bot.handle(update(1, f"/start {code}"))
        await bot.ui.store.set_awaiting("add:locations", {"msg": None})
        await bot.handle(update(2, "/status"))
        assert await bot.ui.store.awaiting() is None

    run(tmp_path, scenario)


def test_onboarding_keeps_menu_settings_and_drops_the_cv_copy(tmp_path):
    async def scenario(db, api, bot, store):
        code = await store.begin_pairing(now())
        await bot.handle(update(1, f"/start {code}"))
        await db.update_preferences(
            lambda p: p.model_copy(update={"must_keywords": ("python",), "roles": ("analyst",)})
        )
        for number, text in enumerate(
            ("data scientist", "Berlin", "none", "EU citizen", "Jane Doe CV text"), start=2
        ):
            await bot.handle(update(number, text))
        preferences = await db.preferences()
        assert preferences.must_keywords == ("python",)
        assert preferences.roles == ("analyst", "data scientist")
        assert preferences.locations == ("Berlin",)
        step, answers = await store.onboarding()
        assert step == "done" and "cv" not in answers and answers["roles"] == "data scientist"

    run(tmp_path, scenario)


def test_long_replies_are_split_on_line_boundaries_with_buttons_on_the_last_part():
    import json

    import httpx

    from linkedin_bot.adapters.telegram import HttpTelegram, chunks

    text = "\n".join(f"line {index} " + "x" * 90 for index in range(120))
    parts = chunks(text)
    assert len(parts) > 1 and all(len(part) <= 4000 for part in parts)
    assert "\n".join(parts) == text
    assert chunks("y" * 9000) == ["y" * 4000, "y" * 4000, "y" * 1000]

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(calls)}})

    async def go():
        api = HttpTelegram("123:token", transport=httpx.MockTransport(handler))
        markup = {"inline_keyboard": [[{"text": "Go", "callback_data": "m:main"}]]}
        message_id = await api.send_message(5, text, markup)
        await api.close()
        return message_id

    assert asyncio.run(go()) == len(parts)
    assert [("reply_markup" in call) for call in calls] == [False] * (len(parts) - 1) + [True]
