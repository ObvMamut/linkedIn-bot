# Faster LinkedIn reads, settings, pins, jobs, digest and a button UI for the Telegram bot

## Context
The read-only LinkedIn connector works, but each `/li_*` command takes ~30 s. You also want a
real interface (buttons, not typed commands), saved settings that rank results without an
LLM, pinned firms and people, saved searches, LinkedIn jobs search, and a daily digest.
Decisions you made: warm browser plus smarter pacing; saved searches, pins, jobs search,
digest and alerts; hard settings for locations/remote, role keywords, firm rules, languages
and recency; inline button menus. The LLM part ("analyse my profile and flag internships at
pinned firms in cybersecurity, LLMs, data analysis, finance, defense") is built as a stored
free-text preference plus an interface, and stays switched off until you choose and approve an
Ollama model. Nothing here downloads or starts a model.

## Where the ~30 s goes (to be measured first, not assumed)
Estimated from the code: cold Chromium launch per command (`open_connector`, ~2-4 s);
`Limiter.acquire` sleeps 4-10 s before every load including the first
(`adapters/linkedin/limits.py`); `ReadOnlyPage.goto` waits for `main`, then the expected
element, then a content-stabilising loop of at least 3 s (`adapters/linkedin/session.py`);
images, fonts and ads all load. Telegram polling itself is not the delay (long-poll returns
on arrival).

## Phase 0 - measure
Add per-phase timings (launch, pacing sleep, goto, wait, settle, parse) as `duration_ms` log
events (already whitelisted in `runtime/privacy.py`) and a small timing script. Record a
baseline for `/li_status`, messages, people search, profile. Re-run after Phase 1.

## Phase 1 - speed (target: warm uncached read <= 8 s, cached < 1 s)
1. **Warm session**: `WarmSession` in `adapters/linkedin/session.py` opens the persistent
   context lazily, keeps it open, closes after 10 idle minutes, reopens on demand, one lock.
   `LinkedInCommands` takes it instead of `open_connector`. `linkedin login` needs the profile:
   add `/li_release` (and an automatic release before login via the profile `SingletonLock`
   check) so the plain login window never fights the warm browser.
2. **Pacing**: `Limiter` skips the jitter when the last load was > 20 s ago; keeps the 4-10 s
   gap between back-to-back loads. Hourly (30), daily (150) and cooldown rules unchanged.
3. **Waiting**: drop the stabilise loop when `wait_for` is given; keep a ~0.3 s settle.
4. **Lighter pages**: block images, media, fonts and known ad/analytics hosts via a
   `context.route` handler that only ever calls `abort` or `continue_`. Allowed in `session.py`
   only; the guard test stays strict for `connector.py` and `pages.py`, and a new test pins the
   handler to those two calls. Verify parsers still pass live.
5. **Cache**: in-memory TTL cache keyed by URL (messages 60 s, searches 5 min, profiles 10
   min); a Refresh button bypasses it but still goes through the limiter.
6. **Perceived speed**: send a typing action and a "Loading..." message at once, then edit it
   with the result.

## Phase 2 - settings and ranking (no LLM)
Extend `SearchPreferences` (`domain/models.py`) with defaults so old rows still load:
`must_keywords`, `nice_keywords`, `exclude_keywords`, `seniority`, `job_types`,
`languages`, `max_age_days`, `blocked_firms`, `preferred_industries`, `priorities`
(ordered: role, location, firm, recency, language), `interests` (free text for the later LLM).
Saved through the existing `Database.save_preferences` (revisioned, invalidates stale actions).
Extend `domain/matching.score_posting`: weights derived from the priority order, "pinned firm"
boost, blocked firms and excluded keywords as hard filters, recency from `published_at` with
`first_seen` as fallback, a small stopword-based language detector (`domain/language.py`,
en/fr/de/es/it/pl; unknown passes). `SourceObservation` gains an optional `company` set by
`adapters/ats.py` from the board slug so pinned firms can match.

## Phase 3 - pins, saved searches, digest storage
Migration `004_workspace.sql`: `pins` (kind firm|person, canonical URL, name, note, tags,
optional board provider+slug), `saved_searches` (name, kind people|firms|jobs, query,
last_run, last_seen keys), `digest_settings` (enabled, local time, alerts on/off, quiet hours,
last_sent), `ui_state` (pending text input). Stores in `storage/workspace.py`, same style as
`storage/telegram.py`. Restore keeps these but clears LinkedIn session state as today.
Pinning a firm offers "find job board": probe Greenhouse/Lever/Ashby slug candidates with the
existing `ATSSource` (public, no LinkedIn), verified with `adapters/company.py`.

## Phase 4 - Telegram button UI
- `adapters/telegram.py`: parse `callback_query`; `allowed_updates` includes it; add
  `send_message(..., markup)` returning the message id, `edit_message`, `answer_callback`,
  `send_chat_action`. Extend the `TelegramAPI` protocol and the test fake.
- Callback data is a short opaque token (<= 64 bytes) mapped server-side to the action;
  expired tokens answer "that button expired". Same user and private-chat authorisation as
  messages.
- New `workflows/ui/` package: `screens.py` (pure: state -> text + keyboard), `router.py`
  (callback dispatch), `inputs.py` (text-input flows such as "send me the cities"). Existing
  typed commands stay and open the same screens; `workflows/telegram_bot.py` shrinks to
  authorisation, update bookkeeping and dispatch.
- Screens: main `/menu` (Messages, Search, Pinned, Jobs, Saved, Settings, Status); result
  cards with Prev/Next, Open, Pin, Save search, Refresh; Settings with toggles and steppers,
  priority up/down, add/remove for locations, keywords and blocked firms, digest on/off/time.

## Phase 5 - LinkedIn jobs search
Supervised live inspection first (redacted structure only, as before), then `parse_jobs`, a
`Job` model, `search_jobs(query, location)` on the connector with its own `wait_for`, `/jobs/view/<id>`
URL validation, and a `linkedin` source in `SourceObservation` so jobs rank together with the
public boards. Public job metadata only is stored; messages are still never stored.

## Phase 6 - one foreground `serve` process (off until you switch features on)
`linkedin-bot serve` runs the Telegram poller, due board collection (`workflows/monitor.run_one`),
the daily digest (default 18:00 in the configured timezone) and optional alerts in one
foreground process. Alerts: new matching openings at pinned firms (public boards, no LinkedIn),
and new unread LinkedIn messages (separate toggle, >= 30 min interval, quiet hours). The digest
uses at most 8 LinkedIn page loads inside the existing hourly/daily budget. Nothing recurring
starts unless you enable it in Settings.

## Phase 7 - LLM-assisted preferences (stub only)
Define `assess(posting, profile, interests) -> Assessment` behind the existing `Generator`
protocol (`workflows/materials.py`), used only for internships at pinned firms. Until an
evaluated Ollama model is configured, the UI shows "LLM check not enabled" and ranking uses
the hard settings only.

## Critical files
Modify: `adapters/linkedin/{session,limits,connector,pages}.py`, `adapters/telegram.py`,
`adapters/ats.py`, `domain/{models,matching}.py`, `workflows/{telegram_bot,linkedin_commands}.py`,
`runtime/{cli,config}.py`, `storage/{database,restore}.py`.
Create: `adapters/linkedin/cache.py`, `domain/language.py`, `storage/workspace.py`,
`storage/migrations/004_workspace.sql`, `workflows/ui/{screens,router,inputs}.py`,
`workflows/{digest,serve}.py`.
Reuse: `Database.save_preferences`, `Limiter`, `TelegramStore`, `Executor` untouched,
`ATSSource`/`schedule-board`, `RedactedFormatter` events, existing test fakes in
`tests/test_telegram.py`.

## Verification
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q`,
  `uv build --offline`; every phase test-first as in the previous milestones.
- Timing: re-run the Phase 0 script live; accept only if warm uncached reads are <= 8 s and
  cached reads < 1 s, and the live parsers (inbox, people, firms, profile, jobs) still return
  full results with images blocked.
- Telegram walkthrough on the real bot: /menu, page through results, pin a firm and person,
  link a job board, save and rerun a search, change each setting and see the ranking change,
  toggle the digest and trigger one on demand, `/li_release` then `linkedin login`.
- Safety checks: a stranger's button press is ignored; an expired button is refused; the
  read-only guard test and the "route handler only aborts or continues" test pass; no
  employer-facing effect path exists.

## Risks to keep visible
- More automated LinkedIn visits (digest, alerts, jobs) raise the chance of a restriction on
  your account; limits stay at 30/hour and 150/day and alerts are opt-in.
- Blocking images and ads changes how the browser looks to LinkedIn; confirm live and be ready
  to turn it off with a setting.
- Jobs and some selectors are unverified until the live inspection step; markup can change.
- A resident warm browser holds your signed-in session in memory; it closes after 10 idle minutes.
