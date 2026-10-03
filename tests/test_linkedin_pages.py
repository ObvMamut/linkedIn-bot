import pytest

from linkedin_bot.adapters.linkedin.models import LinkedInError
from linkedin_bot.adapters.linkedin.pages import (
    detect_state,
    parse_companies,
    parse_messages,
    parse_people,
    parse_profile,
)

# UNVERIFIED layout: structured by accessibility labels and data attributes, to be reconciled
# against real redacted pages in the supervised live step.
MESSAGES = """
<main aria-label="Messaging"><ul aria-label="Conversations">
 <li data-conversation-id="2-abc" data-unread="true">
  <h3>Jane Doe</h3><p>Are you free Tuesday?</p></li>
 <li data-conversation-id="2-def"><h3>Acme Recruiting</h3><p>Thanks for applying</p></li>
 <li><h3>Broken row without id</h3></li>
</ul></main>"""
PEOPLE = """
<div aria-label="Search results"><ul>
 <li><a href="https://www.linkedin.com/in/jane-doe/"><span class="name">Jane Doe</span></a>
  <div class="headline">ML Engineer at Acme</div><div class="location">Berlin</div></li>
 <li><a href="https://www.linkedin.com/in/john-roe"><span class="name">John Roe</span></a>
  <div class="headline">Recruiter</div></li>
 <li><span class="name">No link</span></li>
</ul></div>"""
COMPANIES = """
<div aria-label="Search results"><ul>
 <li><a href="https://www.linkedin.com/company/acme/"><span class="name">Acme</span></a>
  <div class="headline">Rockets for everyone</div></li>
</ul></div>"""
PROFILE = """
<main aria-label="Profile"><h1>Jane Doe</h1>
 <div data-field="headline">ML Engineer</div><div data-field="location">Berlin</div>
 <section aria-label="About"><p>I build models.</p></section>
 <section aria-label="Experience"><ul>
  <li>Acme, 2022-2024</li><li>Beta, 2020-2022</li></ul></section>
</main>"""
SIGNED_IN = '<header></header><main id="workspace"><p>feed</p></main>'


def test_messages_parse_rows_and_count_skipped():
    parsed = parse_messages(MESSAGES)
    assert [(c.id, c.title, c.unread) for c in parsed.items] == [
        ("2-abc", "Jane Doe", True),
        ("2-def", "Acme Recruiting", False),
    ]
    assert parsed.items[0].snippet == "Are you free Tuesday?" and parsed.skipped == 1


def test_people_and_companies_require_a_profile_link():
    people = parse_people(PEOPLE)
    assert [(p.name, p.url, p.location) for p in people.items] == [
        ("Jane Doe", "https://www.linkedin.com/in/jane-doe", "Berlin"),
        ("John Roe", "https://www.linkedin.com/in/john-roe", None),
    ]
    assert people.skipped == 1
    [firm] = parse_companies(COMPANIES).items
    assert (firm.name, firm.url) == ("Acme", "https://www.linkedin.com/company/acme")


def test_profile_fields():
    profile = parse_profile(PROFILE, "https://www.linkedin.com/in/jane-doe")
    assert profile.name == "Jane Doe" and profile.headline == "ML Engineer"
    assert profile.about == "I build models."
    assert profile.experience == ("Acme, 2022-2024", "Beta, 2020-2022")


def test_present_but_empty_containers_mean_none_not_layout_changed():
    assert parse_messages('<ul aria-label="Conversations"></ul>').items == ()
    assert parse_people('<div aria-label="Search results"><ul></ul></div>').items == ()


@pytest.mark.parametrize(
    "parse", [parse_messages, parse_people, parse_companies, lambda h: parse_profile(h, "u")]
)
def test_missing_anchor_is_layout_changed(parse):
    with pytest.raises(LinkedInError) as caught:
        parse("<html><body><div>Something new</div></body></html>")
    assert caught.value.code == "layout_changed"


@pytest.mark.parametrize(
    ("url", "html", "expected"),
    [
        ("https://www.linkedin.com/feed/", SIGNED_IN, "ready"),
        ("https://www.linkedin.com/login", "<form></form>", "auth_required"),
        ("https://www.linkedin.com/uas/login?x=1", "", "auth_required"),
        ("https://www.linkedin.com/authwall?trk=x", "", "auth_required"),
        ("https://www.linkedin.com/checkpoint/challenge/abc", "", "challenge_required"),
        (
            "https://www.linkedin.com/feed/",
            "<p>Unusual activity detected</p>",
            "challenge_required",
        ),
        ("https://www.linkedin.com/feed/", "<p>nothing</p>", "unavailable"),
        (
            "https://www.linkedin.com/feed/",
            '<main><form><input type="password"></form></main>',
            "auth_required",
        ),
    ],
)
def test_detect_state(url, html, expected):
    assert detect_state(url, html) == expected
