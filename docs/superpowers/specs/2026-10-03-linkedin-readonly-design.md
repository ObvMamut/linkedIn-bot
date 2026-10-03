# LinkedIn read-only connector — design

_2026-10-03 · approved in chat (approach A) · extends [linkedin-integration.md](../../architecture/linkedin-integration.md) and ADR-02_

## Purpose

From the paired Telegram chat, read the user's own LinkedIn: connection status, messages,
people and company search, and profiles (own or by URL). Nothing is sent, accepted, posted or
applied. The user authenticates once; afterwards the bot works unattended until LinkedIn
expires the session or demands a challenge.

## Constraints

- LinkedIn's terms prohibit automated access. The user accepted this risk knowingly; the design
  keeps volume low and stops at the first challenge. It cannot remove the risk.
- Playwright with a persistent Chromium profile under `data_dir/browser`, owned by this app,
  never the user's everyday profile. UI flows only; no undocumented internal API.
- No CAPTCHA solving, stealth plugins, proxies or challenge bypass.
- Cookies, storage state, passwords and the browser profile never reach Telegram, a model, logs
  or backups. Backups already exclude the profile directory.
- Read-only: no code path in this connector clicks send, accept, connect, follow or apply.

## Components

| Unit | Responsibility | Depends on |
| --- | --- | --- |
| `adapters/linkedin/models.py` | Typed records: `Conversation`, `Person`, `Company`, `Profile`, `SessionStatus` | pydantic |
| `adapters/linkedin/pages.py` | Pure parsers from page HTML to records; return `layout_changed` when required anchors are absent | models |
| `adapters/linkedin/limits.py` | Page-load budget per hour, jittered delays, cooldown after challenge or 429 | clock |
| `adapters/linkedin/session.py` | Persistent context lifecycle, connector lock, status detection from URL and page anchors | Playwright |
| `adapters/linkedin/connector.py` | `status`, `messages`, `search_people`, `search_companies`, `profile`; composes limits, session and pages | the three above |
| `workflows/linkedin_commands.py` | Telegram command handlers, formatting, bounded output | connector |
| CLI `linkedin login` / `linkedin status` | Visible one-time login; headless status check | session |

Parsers take HTML strings, so every layout assumption is testable offline against saved,
redacted fixtures under `tests/fixtures/linkedin/`. The connector takes a `Page`-like
protocol so tests drive it with a fake page.

## Session states and recovery

`ready`, `auth_required`, `challenge_required`, `unavailable`. Detection: a URL on
`/login`, `/checkpoint` or `/authwall`, or a missing signed-in anchor. Any state other than
`ready` stops the operation, records a cooldown, and sends one Telegram notice (deduplicated
until the status changes) telling the user to run `linkedin-bot linkedin login`. The connector
never retries authentication.

## Rate limits

Defaults, configurable only downward: at most 30 page loads per rolling hour and 150 per day;
random 4–10 s delay between loads; 6 h cooldown after a challenge, 1 h after a 429 or
"unusual activity" page. A request that would exceed the budget fails with `rate_limited` and
the time it may be retried; it is never queued silently.

## Telegram commands

`/li_status`, `/li_messages [n]` (latest conversations, default 5, max 10), `/li_search people|firms <query>`
(max 10 results), `/li_profile [linkedin.com/in/... URL]` (own profile when omitted).
URLs are accepted only for `https://www.linkedin.com/in/<slug>` and `/company/<slug>`; anything
else is refused before any navigation. Output is truncated to Telegram limits.

## Data and privacy

Results are returned to the paired chat and not persisted. Message text is never logged and
never offered to an LLM. The only stored state is the session status and last success time in a
small table (migration 003); the cooldown lives in the running process. Restore clears it.

## Error handling

`layout_changed`, `rate_limited`, `auth_required`, `challenge_required`, `unavailable`,
`invalid_target`. Each maps to a short Telegram message with no page content. A partial parse
(some rows unrecognised) returns the recognised rows and reports how many were skipped.

## Layout learning

Parsers are written against pages observed once in the app's own logged-in browser window,
saved as redacted fixtures (names, headlines, message text replaced by placeholders). Until
that supervised inspection happens, parsers target documented accessibility roles and labels
and are marked unverified; `/li_status` reports "layout unverified" so the user knows.

## Testing

Unit: parsers on fixtures; limits with a fake clock; URL validation; session-state detection;
command formatting; the connector against a fake page, including challenge and rate-limit
paths; a guard test that fails if the connector module references click targets for send,
accept, connect, follow or apply. Live verification is a separate manual step after login.

## Out of scope

Sending or replying, invitations, applications, scheduled inbox monitoring, feed reading,
message storage, any LLM use of LinkedIn content.

## Deviations recorded during implementation

- `/li_profile` opens people pages only; company URLs validate but are refused until a company-page parser exists.
- No `notified_state` column: replies state the problem each time. Proactive deduplicated notices belong to scheduled monitoring, which is out of scope.
- The browser opens per command and closes afterwards, so none stays resident between requests.
