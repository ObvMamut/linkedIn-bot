import asyncio

import pytest

from linkedin_bot.adapters.linkedin.limits import Limiter
from linkedin_bot.adapters.linkedin.models import Conversation, LinkedInError, Person


class Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.slept = []

    def now(self):
        return self.t

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def limiter(clock, delay=lambda: 5.0):
    return Limiter(clock=clock.now, sleep=clock.sleep, delay=delay)


def acquire_many(lim, count):
    async def go():
        for _ in range(count):
            await lim.acquire()

    asyncio.run(go())


def test_hourly_budget_then_rate_limited_with_retry_after():
    clock = Clock()
    lim = limiter(clock, delay=lambda: 0.0)
    acquire_many(lim, 30)
    with pytest.raises(LinkedInError) as caught:
        acquire_many(lim, 1)
    assert caught.value.code == "rate_limited" and caught.value.retry_after > 0
    clock.t += 3601
    acquire_many(lim, 1)  # the window moved on


def test_daily_budget_survives_hourly_resets():
    clock = Clock()
    lim = limiter(clock, delay=lambda: 0.0)
    for _ in range(5):
        acquire_many(lim, 30)
        clock.t += 3601
    with pytest.raises(LinkedInError) as caught:
        acquire_many(lim, 1)
    assert caught.value.code == "rate_limited"


def test_back_to_back_loads_wait_a_jittered_delay_but_the_first_and_idle_ones_do_not():
    clock = Clock()
    values = iter([4.0, 10.0, 7.0])
    lim = limiter(clock, delay=lambda: next(values))
    acquire_many(lim, 3)
    assert clock.slept == [4.0, 10.0]  # the first load of a burst is immediate
    clock.t += 25  # idle for longer than the quiet gap
    acquire_many(lim, 1)
    assert clock.slept == [4.0, 10.0]


def test_default_delay_is_within_bounds():
    from linkedin_bot.adapters.linkedin.limits import default_delay

    assert all(4.0 <= default_delay() <= 10.0 for _ in range(200))


def test_trip_blocks_everything_until_cooldown_ends():
    clock = Clock()
    lim = limiter(clock, delay=lambda: 0.0)
    lim.trip(hours=6)
    with pytest.raises(LinkedInError) as caught:
        acquire_many(lim, 1)
    assert caught.value.code == "cooldown" and 21_500 < caught.value.retry_after <= 21_600
    clock.t += 6 * 3600 + 1
    acquire_many(lim, 1)


def test_models_reject_unknown_fields_and_error_carries_code():
    with pytest.raises(ValueError):
        Person.model_validate_json('{"url":"u","name":"n","headline":"h","location":null,"x":1}')
    assert Conversation(id="1", title="t", snippet="s", unread=False).unread is False
    assert str(LinkedInError("layout_changed")) == "layout_changed"


def test_budget_and_cooldown_survive_a_restart_and_are_shared(tmp_path):
    from linkedin_bot.adapters.linkedin.limits import BudgetFile

    clock = Clock()
    first = Limiter(
        clock=clock.now, sleep=clock.sleep, delay=lambda: 0.0, budget=BudgetFile(tmp_path)
    )
    acquire_many(first, 30)
    restarted = Limiter(
        clock=clock.now, sleep=clock.sleep, delay=lambda: 0.0, budget=BudgetFile(tmp_path)
    )
    with pytest.raises(LinkedInError) as caught:
        acquire_many(restarted, 1)
    assert caught.value.code == "rate_limited"
    assert restarted.usage()["hour"] == 30
    clock.t += 3601
    first.trip(hours=6)
    other = Limiter(
        clock=clock.now, sleep=clock.sleep, delay=lambda: 0.0, budget=BudgetFile(tmp_path)
    )
    with pytest.raises(LinkedInError) as caught:
        acquire_many(other, 1)
    assert caught.value.code == "cooldown"
    assert other.usage()["cooldown_seconds"] > 21_000
    assert (tmp_path / "linkedin-budget.json").stat().st_mode & 0o777 == 0o600


def test_an_unreadable_budget_file_starts_empty(tmp_path):
    from linkedin_bot.adapters.linkedin.limits import BudgetFile

    (tmp_path / "linkedin-budget.json").write_text("{not json")
    clock = Clock()
    lim = Limiter(
        clock=clock.now, sleep=clock.sleep, delay=lambda: 0.0, budget=BudgetFile(tmp_path)
    )
    acquire_many(lim, 1)
    assert lim.usage()["hour"] == 1
