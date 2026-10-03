# ruff: noqa: E501  (URL literals)
import asyncio
import re
from pathlib import Path

import pytest

import linkedin_bot.adapters.linkedin as package
from linkedin_bot.adapters.linkedin.connector import LinkedInConnector, validate_target
from linkedin_bot.adapters.linkedin.limits import Limiter
from linkedin_bot.adapters.linkedin.models import LinkedInError
from linkedin_bot.adapters.linkedin.session import profile_dir
from tests.test_linkedin_pages import COMPANIES, MESSAGES, PEOPLE, PROFILE, SIGNED_IN

LOGIN = "<form>sign in</form>"


class FakePage:
    def __init__(self, pages):
        self.pages = pages  # url prefix -> (final_url, html)
        self.url = ""
        self.visited = []

    async def goto(self, url):
        self.visited.append(url)
        for prefix, (final, html) in self.pages.items():
            if url.startswith(prefix):
                self.url, self._html = final, html
                return
        self.url, self._html = url, SIGNED_IN

    async def content(self):
        return self._html


async def no_sleep(_):
    return None


def connector(pages):
    page = FakePage(pages)
    limiter = Limiter(sleep=no_sleep, delay=lambda: 0.0)
    return LinkedInConnector(page, limiter), page, limiter


def run(coro):
    return asyncio.run(coro)


def with_nav(html):
    return SIGNED_IN + html


def test_status_and_reads_on_a_ready_session():
    conn, page, _ = connector(
        {
            "https://www.linkedin.com/messaging": (
                "https://www.linkedin.com/messaging/",
                with_nav(MESSAGES),
            ),
            "https://www.linkedin.com/search/results/people": (
                "https://www.linkedin.com/search/results/people/",
                with_nav(PEOPLE),
            ),
            "https://www.linkedin.com/search/results/companies": (
                "https://www.linkedin.com/search/results/companies/",
                with_nav(COMPANIES),
            ),
            "https://www.linkedin.com/in/jane-doe": (
                "https://www.linkedin.com/in/jane-doe",
                with_nav(PROFILE),
            ),
        }
    )
    assert run(conn.status()) == "ready"
    assert [c.id for c in run(conn.messages(1)).items] == ["2-abc"]
    assert len(run(conn.search_people("ml engineer berlin")).items) == 2
    assert (
        page.visited[-1]
        == "https://www.linkedin.com/search/results/people/?keywords=ml%20engineer%20berlin"
    )
    assert run(conn.search_companies("acme")).items[0].name == "Acme"
    profile = run(conn.profile("https://www.linkedin.com/in/jane-doe/?trk=x"))
    assert profile.name == "Jane Doe" and page.visited[-1] == "https://www.linkedin.com/in/jane-doe"


def test_challenge_stops_everything_and_is_never_retried():
    conn, page, limiter = connector(
        {
            "https://www.linkedin.com/messaging": (
                "https://www.linkedin.com/checkpoint/challenge/x",
                "",
            )
        }
    )
    with pytest.raises(LinkedInError) as first:
        run(conn.messages(5))
    assert first.value.code == "challenge_required"
    loads = len(page.visited)
    for call in (conn.status(), conn.search_people("anyone")):
        with pytest.raises(LinkedInError) as again:
            run(call)
        assert again.value.code == "cooldown" and again.value.retry_after > 21_000
    assert len(page.visited) == loads  # no navigation while cooling down


def test_login_page_reports_auth_required_without_cooldown():
    conn, _, _ = connector(
        {"https://www.linkedin.com/messaging": ("https://www.linkedin.com/login", LOGIN)}
    )
    with pytest.raises(LinkedInError) as caught:
        run(conn.messages(5))
    assert caught.value.code == "auth_required"
    assert run(conn.status()) == "ready"  # the next probe goes to /feed/ and is allowed


def test_status_reports_state_instead_of_raising():
    conn, _, _ = connector(
        {"https://www.linkedin.com/feed": ("https://www.linkedin.com/login", LOGIN)}
    )
    assert run(conn.status()) == "auth_required"


def test_too_many_requests_page_trips_a_one_hour_cooldown():
    conn, _, _ = connector(
        {
            "https://www.linkedin.com/messaging": (
                "https://www.linkedin.com/messaging/",
                "<h1>Too many requests</h1>",
            )
        }
    )
    with pytest.raises(LinkedInError) as caught:
        run(conn.messages(5))
    assert caught.value.code == "rate_limited" and caught.value.retry_after == 3600


@pytest.mark.parametrize(
    "target",
    [
        "https://www.linkedin.com.evil.example/in/jane",
        "https://evil.example/www.linkedin.com/in/jane",
        "javascript:alert(1)",
        "http://www.linkedin.com/in/jane",
        "https://www.linkedin.com/jobs/view/123",
        "https://www.linkedin.com/in/",
        "https://www.linkedin.com/in/jane/edit",
        "https://user@www.linkedin.com/in/jane",
        "not a url",
    ],
)
def test_targets_are_validated_before_any_navigation(target):
    conn, page, _ = connector({})
    with pytest.raises(LinkedInError) as caught:
        run(conn.profile(target))
    assert caught.value.code == "invalid_target" and page.visited == []
    with pytest.raises(LinkedInError):
        validate_target(target)


def test_company_urls_validate_but_profiles_only_open_people_pages():
    assert validate_target("https://www.linkedin.com/company/acme/") == (
        "https://www.linkedin.com/company/acme"
    )
    conn, page, _ = connector({})
    with pytest.raises(LinkedInError) as caught:
        run(conn.profile("https://www.linkedin.com/company/acme"))
    assert caught.value.code == "invalid_target" and page.visited == []


@pytest.mark.parametrize("query", ["", "   ", "x" * 101])
def test_search_queries_are_bounded(query):
    conn, page, _ = connector({})
    with pytest.raises(LinkedInError) as caught:
        run(conn.search_people(query))
    assert caught.value.code == "invalid_query" and page.visited == []


def test_connector_package_has_no_way_to_act_on_pages():
    forbidden = re.compile(
        r"\.(click|dblclick|fill|type|press|check|select_option|set_input_files|"
        r"dispatch_event|evaluate|evaluate_handle|keyboard|mouse|route)\b|\bsend_keys\b"
    )
    for path in Path(package.__file__).parent.glob("*.py"):
        assert not forbidden.search(path.read_text()), path.name


def test_profile_directory_is_private_and_under_data_dir(tmp_path):
    directory = profile_dir(tmp_path)
    assert directory == tmp_path / "browser" and directory.is_dir()
    assert (directory.stat().st_mode & 0o777) == 0o700


def test_manual_login_command_is_a_plain_browser_on_the_app_profile(tmp_path):
    from linkedin_bot.adapters.linkedin.session import manual_login_command

    command = manual_login_command("/usr/bin/chromium", tmp_path)
    assert command[0] == "/usr/bin/chromium"
    assert f"--user-data-dir={tmp_path / 'browser'}" in command
    # Same cookie store as the automated reader, or the saved session would be unreadable.
    assert "--password-store=basic" in command
    assert command[-1] == "https://www.linkedin.com/login"
    joined = " ".join(command)
    assert "remote-debugging" not in joined and "enable-automation" not in joined
