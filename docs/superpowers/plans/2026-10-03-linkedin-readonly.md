# LinkedIn read-only connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline, chosen by the user's "go").

**Goal:** Read-only LinkedIn access (status, messages, people/firm search, profiles) from the paired Telegram chat.

**Architecture:** Pure HTML parsers + a rate-limited connector over a `Page` protocol; Playwright persistent profile for the session; Telegram handlers call the connector. Strict TDD, fixtures for parsers.

**Tech Stack:** Python 3.14, Playwright (already a dependency), pydantic strict models, pytest, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-03-linkedin-readonly-design.md`

## Global Constraints

- Read-only: no code path may click send, accept, connect, follow or apply.
- Profile directory is `data_dir/browser`; never the user's everyday profile; excluded from backups.
- Limits: 30 page loads/hour, 150/day, 4–10 s jittered delay, 6 h cooldown after challenge, 1 h after 429.
- Allowed URL targets only `https://www.linkedin.com/in/<slug>` and `/company/<slug>`.
- Message text is never logged, stored, or sent to a model. Results go only to the paired chat.
- Checks: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q`.

## Review Focus

- A challenge or login page mid-operation must stop everything and never retry (Task 3).
- URL validation must reject look-alike hosts, `javascript:` and non-profile paths (Task 3).
- Empty inbox / zero search results must read as "none", not `layout_changed` (Task 2).
- Rate budget survives across commands within the process and reports when to retry (Task 1).
- A partial parse returns recognised rows and a skipped count (Task 2).

---

### Task 1: Models and limits
**Files:** Create `src/linkedin_bot/adapters/linkedin/{__init__,models,limits}.py`; Test `tests/test_linkedin_limits.py`
**Interfaces:** Produces `Conversation(id,title,snippet,unread)`, `Person(url,name,headline,location)`, `Company(url,name,tagline)`, `Profile(url,name,headline,location,about,experience:tuple[str,...])`, `SessionState = Literal["ready","auth_required","challenge_required","unavailable"]`, `LinkedInError(code:str, retry_after:int|None)`, `Limiter(clock, rng_delay)` with `async acquire()` raising `LinkedInError("rate_limited", retry_after=s)` / `("cooldown", ...)`, and `trip(hours:int)`.
- [ ] Failing tests: hourly budget of 30 then `rate_limited` with `retry_after>0`; daily 150; cooldown after `trip(6)`; delay drawn from 4–10 s via injected function; models reject unknown fields.
- [ ] Implement; run tests; commit.

### Task 2: Parsers and fixtures
**Files:** Create `adapters/linkedin/pages.py`, `tests/fixtures/linkedin/*.html`; Test `tests/test_linkedin_pages.py`
**Interfaces:** Consumes Task 1 models. Produces `parse_messages(html)->ParsedList[Conversation]`, `parse_people(html)`, `parse_companies(html)`, `parse_profile(html)->Profile`, `ParsedList(items, skipped:int)`; all raise `LinkedInError("layout_changed")` when the list container/anchor is absent but return empty lists for a present-but-empty container; `detect_state(url, html)->SessionState`.
- [ ] Failing tests per parser (fixtures built from accessibility roles/labels, marked unverified), empty-container → empty list, missing anchor → `layout_changed`, one malformed row → `skipped==1`, `detect_state` for `/login`, `/checkpoint`, `/authwall`, signed-in anchor.
- [ ] Implement with `html.parser` (stdlib; no new dependency); run; commit.

### Task 3: Session and connector
**Files:** Create `adapters/linkedin/{session,connector}.py`; Test `tests/test_linkedin_connector.py`
**Interfaces:** Consumes Tasks 1–2. Produces `class Page(Protocol): async goto(url)->None; async content()->str; url:str`, `LinkedInConnector(page_factory, limiter)` with `status()`, `messages(n)`, `search_people(q)`, `search_companies(q)`, `profile(target|None)`, `validate_target(url)->str` (raises `LinkedInError("invalid_target")`), and `PlaywrightSession` opening a persistent context at `data_dir/browser`.
- [ ] Failing tests with a fake Page: ready path; challenge page → `challenge_required` + limiter tripped 6 h + no second navigation; look-alike host / `javascript:` / `/jobs/` rejected before any `goto`; connector module source contains none of `send|accept|connect|follow|apply` as click targets (guard test); query is URL-encoded.
- [ ] Implement; run; commit.

### Task 4: Storage, Telegram commands, CLI
**Files:** Create `storage/migrations/003_linkedin.sql`, `workflows/linkedin_commands.py`; Modify `workflows/telegram_bot.py`, `runtime/cli.py`, `storage/restore.py`; Test `tests/test_linkedin_commands.py`
**Interfaces:** Consumes Task 3 connector. Produces migration table `linkedin_state(slot, state, last_success, notified_state)`; Bot gains optional `linkedin: LinkedInConnector|None`; commands `/li_status /li_messages /li_search /li_profile`; CLI `linkedin login` (visible window) and `linkedin status`.
- [ ] Failing tests: commands refuse strangers (existing auth path); not-configured reply; formatted output truncated; one deduplicated notice per state change; restore clears `linkedin_state`; message text absent from logs.
- [ ] Implement; run full checks; update README; commit.

### Task 5: Supervised live verification (manual)
- [ ] User runs `uv run linkedin-bot linkedin login` and signs in. With the user's go-ahead, open inbox/search/profile once, record real layout deltas, replace "unverified" fixtures with redacted real ones, fix parsers, rerun tests.
