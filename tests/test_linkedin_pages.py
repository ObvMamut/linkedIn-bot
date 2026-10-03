import pytest

from linkedin_bot.adapters.linkedin.models import LinkedInError
from linkedin_bot.adapters.linkedin.pages import (
    detect_state,
    parse_companies,
    parse_messages,
    parse_people,
    parse_profile,
)

# Structure mirrors pages observed live on 2026-10-03 (class names are obfuscated hashes and
# are deliberately absent; `data-view-name` markers appear on some loads only, so they are not
# used). All names and text here are synthetic.
MESSAGES = """
<main id="main"><div><ul aria-label="Conversation List">
 <li></li>
 <li id="ember47"><div id="conversation-card-ember48"><div>
   <span>Status is offline</span>
   <h3><div><span>Jane Doe</span></div></h3><time>Sep 30</time><span>Sep 30</span>
   <p>Are you free Tuesday?</p><span>. Active conversation</span>
   <span>Unread message</span>
 </div></div></li>
 <li id="ember55"><div id="conversation-card-ember56"><div>
   <h3><div><span>Acme Recruiting</span></div></h3><time>Sep 29</time>
   <p>Thanks for applying</p>
 </div></div></li>
 <li><span>Orphan row without a heading</span></li>
</ul></div></main>"""
PEOPLE = """
<main id="workspace"><div role="list">
 <div role="listitem"><div>
  <a href="https://www.linkedin.com/in/jane-doe"><div><div>
   <span>Jane Doe</span><span>Jane Doe</span><span>• 3rd+</span>
   <span>ML Engineer at Acme</span><span>Paris, France</span></div></div></a>
  <span>Connect</span><span>Past: Data Scientist at Beta</span></div><hr role="presentation"></div>
 <div role="listitem"><div>
  <a href="https://www.linkedin.com/in/john-roe/"><span>John Roe</span><span>John Roe</span>
   <span>Recruiter</span></a></div></div>
 <div role="listitem"><div><span>No link</span></div></div>
</div></main>"""
COMPANIES = """
<main id="workspace"><div role="list">
 <div role="listitem"><div>
  <a href="https://www.linkedin.com/company/acme/"><span>Acme</span></a>
  <span>Aerospace</span><span>Miami, Florida</span><span>Follow</span>
  <span>Rockets for everyone</span><span>733,216 followers</span></div></div>
</div></main>"""
PROFILE = """
<main id="workspace"><section aria-label="Primary content">
 <h2>Jane Doe</h2><span>She/Her</span><span>Verify in 2 minutes</span>
 <span>ML engineer building search</span><span>Acme</span><span>Paris, France</span>
 <span>·</span><a>Contact info</a>
 <section><h2>About</h2><div><span>I build models.</span></div></section>
 <section><h2>Experience</h2><ul><li>Acme, 2022-2024</li><li>Beta, 2020-2022</li></ul></section>
</section></main>"""
SIGNED_IN = '<header></header><main id="workspace"><p>feed</p></main>'


def test_messages_use_the_list_label_and_skip_only_unusable_rows():
    parsed = parse_messages(MESSAGES)
    assert [(c.title, c.snippet, c.when, c.unread) for c in parsed.items] == [
        ("Jane Doe", "Are you free Tuesday?", "Sep 30", True),
        ("Acme Recruiting", "Thanks for applying", "Sep 29", False),
    ]
    assert parsed.skipped == 1  # the empty <li> is padding, the headingless row is skipped


def test_people_parse_name_headline_location_and_dedupe_hidden_duplicates():
    people = parse_people(PEOPLE)
    assert [(p.name, p.url, p.headline, p.location) for p in people.items] == [
        (
            "Jane Doe",
            "https://www.linkedin.com/in/jane-doe",
            "ML Engineer at Acme",
            "Paris, France",
        ),
        ("John Roe", "https://www.linkedin.com/in/john-roe", "Recruiter", None),
    ]
    assert people.skipped == 1


def test_companies_parse_name_and_industry_location():
    [firm] = parse_companies(COMPANIES).items
    assert firm.name == "Acme" and firm.url == "https://www.linkedin.com/company/acme"
    assert firm.tagline == "Aerospace, Miami, Florida"


def test_profile_fields_from_headings_and_fragments():
    profile = parse_profile(PROFILE, "https://www.linkedin.com/in/jane-doe")
    assert profile.name == "Jane Doe"
    assert profile.headline == "ML engineer building search"  # pronouns and badges skipped
    assert profile.location == "Paris, France"  # the fragment right before the separator
    assert profile.about == "I build models."
    assert profile.experience == ("Acme, 2022-2024", "Beta, 2020-2022")


def test_profile_without_about_or_experience_is_still_valid():
    html = "<main><h2>Jane Doe</h2><span>Engineer</span><span>·</span></main>"
    profile = parse_profile(html, "https://www.linkedin.com/in/jane-doe")
    assert (profile.name, profile.headline, profile.about, profile.experience) == (
        "Jane Doe",
        "Engineer",
        "",
        (),
    )


def test_zero_results_means_none_but_an_unknown_page_is_layout_changed():
    empty = "<main><div><h2>No results found</h2></div></main>"
    assert parse_people(empty).items == () and parse_companies(empty).items == ()
    assert parse_messages('<main><ul aria-label="Conversation List"></ul></main>').items == ()
    with pytest.raises(LinkedInError) as caught:
        parse_people("<main><div>Something new</div></main>")
    assert caught.value.code == "layout_changed"


@pytest.mark.parametrize(
    "parse", [parse_messages, parse_people, parse_companies, lambda h: parse_profile(h, "u")]
)
def test_missing_main_region_is_layout_changed(parse):
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


def test_people_cards_pick_the_text_link_not_the_avatar_link():
    html = """<main><div role="list"><div role="listitem">
      <a href="https://www.linkedin.com/in/jane-doe"><img alt=""></a>
      <a href="https://www.linkedin.com/in/jane-doe"><span>Jane Doe</span><span>Jane Doe</span>
       <span>Engineer</span></a></div></div></main>"""
    [person] = parse_people(html).items
    assert (person.name, person.headline) == ("Jane Doe", "Engineer")


JOBS = """
<main><div><ul>
 <li><div><a href="/jobs/view/4012345678/?trk=flagship3_search_srp_jobs">
   <span>Security Intern (m/f/d)</span><span>Security Intern (m/f/d)</span></a>
   <span>Acme Cyber</span><span>Berlin, Germany (On-site)</span></div></li>
 <li><a href="https://www.linkedin.com/jobs/view/4099/?trk=x"><span>Data Intern</span></a>
   <span>Beta</span><span>Remote</span></li>
 <li><a href="/jobs/view/12"><span>Broken card</span></a></li>
 <li><span>Unrelated list item</span></li>
 <li><a href="/jobs/view/4012345678/?trk=again"><span>Duplicate</span></a>
   <span>Acme Cyber</span><span>Berlin</span></li>
</ul></main>"""


def test_jobs_parse_title_company_location_and_dedupe_by_id():
    from linkedin_bot.adapters.linkedin.pages import parse_jobs

    parsed = parse_jobs(JOBS)
    assert [(j.id, j.title, j.company, j.location) for j in parsed.items] == [
        ("4012345678", "Security Intern (m/f/d)", "Acme Cyber", "Berlin, Germany (On-site)"),
        ("4099", "Data Intern", "Beta", "Remote"),
    ]
    assert parsed.items[0].url == "https://www.linkedin.com/jobs/view/4012345678"
    assert parsed.skipped == 1  # the card with one text piece; the repeat id is just ignored


def test_jobs_zero_results_and_unknown_pages():
    from linkedin_bot.adapters.linkedin.pages import parse_jobs

    assert parse_jobs("<main><div>No matching jobs found.</div></main>").items == ()
    with pytest.raises(LinkedInError) as caught:
        parse_jobs("<main><div>Something new</div></main>")
    assert caught.value.code == "layout_changed"


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/jobs/view/123",
        "https://www.linkedin.com.evil.example/jobs/view/123",
        "javascript:alert(1)",
        "/jobs/collections/recommended",
        "/jobs/view/abc",
    ],
)
def test_job_links_must_be_real_job_urls(href):
    from linkedin_bot.adapters.linkedin.pages import canonical_job

    assert canonical_job(href) is None
