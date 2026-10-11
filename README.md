# Local job-search agent

A single user, local job search assistant for Linux. It collects postings from public job
boards (Greenhouse, Lever, Ashby) and from read only LinkedIn searches, ranks them against your
own rules, keeps a tracker of what you applied to, drafts grounded cover letters with a local
model, and talks to you through a terminal cockpit, Telegram, or both.

It never sends anything to an employer. Every installation starts in **draft only mode with
application and message caps of zero**, no effect adapter exists, and LinkedIn is only ever
read. The architecture and its requirements are in [docs/architecture](docs/architecture/README.md).

## Install

Python 3.14 and [uv](https://docs.astral.sh/uv/):

```sh
uv sync --locked
uv run linkedin-bot --config config.example.toml init
uv run linkedin-bot --config config.example.toml doctor
```

`init` creates the private data directory and SQLite database. `doctor` reports schema,
postings, work queue health, the stored LinkedIn session state and page budget, whether
Telegram is paired, the configured model and the digest switches. Secrets never go in TOML.
Unknown keys, remote Ollama endpoints, invalid timezones and shared or symlinked storage paths
are rejected. The examples below omit `--config`; pass it (or `--data-dir`) as you need.

## The cockpit (terminal UI)

```sh
uv run linkedin-bot tui                 # everything serve does, plus every view and setting
uv run linkedin-bot tui --no-telegram   # do not poll Telegram even if a token is set
uv run linkedin-bot tui --no-background # browse and edit only: no collection, digest or alerts
```

The cockpit replaces `serve`: one process owns the database, the browser profile and the
LinkedIn budget, and runs board collection, the digest, alerts, daily upkeep and (when
`LINKEDIN_AUTOMATION_BOT_TOKEN` is set) Telegram polling. Alerts show up as notifications in the
cockpit and in Telegram. Run it in tmux if you want it to keep working after you close the
terminal.

| Key | View | What it does |
| --- | --- | --- |
| 1 | Dashboard | Counts, session and budget, pause/resume, digest preview, alert feed |
| 2 | Jobs | Ranked postings with reasons and gaps; filter, sort, tracker, pin firm, draft letter |
| 3 | Search | LinkedIn people, firms and jobs (read only); pin, save search |
| 4 | Messages | LinkedIn inbox, read only, never stored |
| 5 | Pinned | Firms and people with notes and tags; find and collect job boards |
| 6 | Saved | Saved searches; rerun to see only new results |
| 7 | Boards | Public board queue: add, run now, interval, delete |
| 8 | Settings | Every ranking rule, priority order, interests, digest and alerts (live match count) |
| 9 | Approvals | Policy with confirmation, approve or reject, record unknown outcomes |
| 0 | Profile | Onboarding, facts, CV, Telegram pairing |
| F2 / F3 / F4 | Drafts / Logs / System | Letters and CVs; live event names; health, backups, LinkedIn sign in |

`?` shows every key, Ctrl+P opens the command palette, `P` pauses or resumes all background
work, Escape leaves a text field, `q` quits. In Jobs: `/` filter, `o` open, `c` copy URL,
`i` interested, `a` applied, `v` interviewing, `x` dismiss, `u` unmark, `n` note, `p` pin the
firm, `w` draft a cover letter. Text from LinkedIn and job boards is never interpreted as
markup, and while the cockpit runs, logs (event names only) go to the Logs view and a private
rotating `data_dir/logs/events.log`.

## Telegram

Create a bot with BotFather and keep its token in `LINKEDIN_AUTOMATION_BOT_TOKEN` (an
environment variable, or a private file your shell sources; never in the repo). Then:

```sh
uv run linkedin-bot pair        # prints a one-use code, valid ten minutes
# send "/start <code>" to the bot from a private chat
uv run linkedin-bot serve       # Telegram plus board collection, digest, alerts, daily upkeep
uv run linkedin-bot telegram    # Telegram only; --once handles one batch
uv run linkedin-bot unpair      # forget the paired chat
```

The cockpit's Profile view can also create a pairing code. Only the paired user in that
private chat is answered; everyone else is silently ignored. One failing request never stops
the bot: you get a short apology and it carries on.

`/menu` opens the button interface: Messages, Search, Pinned, Jobs, Saved, Settings, Facts and
Status. Results are cards with Prev/Next, Pin, Open and Save search; job cards also have
Interested, Applied, Dismiss and (with a local model) Draft. `/jobs`, `/pins`, `/saved`,
`/settings`, `/digest` and `/facts` open a screen directly, and the bot registers its command
list so Telegram shows it in the menu button.

Other commands: `/opportunities`, `/prepare <source>:<id>`, `/fact kind: text`, `/pending`,
`/approve <id>`, `/reject <id>`, `/policy` (changes need `confirm`; hard ceilings 10
applications and 5 messages per day), `/resolve <action> success|none <note>`, `/pause`,
`/resume`, `/help`. Long replies are split into several messages rather than cut.

## Ranking, tracker, pins, saved searches

All ranking is plain rules, no AI, edited from Settings in either interface:

* Locations and remote (`on-site only` marks remote postings as a gap), internship
  preference, role keywords (must have and excluded words are hard filters, nice to have only
  ranks), blocked firms, preferred industries, languages and maximum posting age, and the
  **priority order** of the criteria (role, location, firm, freshness, language).
* Keywords match whole words with plural endings: "Internal Audit" is not an internship and
  excluding "java" does not hide "javascript".
* Freshness uses the board's publish date where it gives one, otherwise the first time the
  posting was seen. An edited posting is never announced as new.
* The **tracker** records your own status per posting (interested, applied, interviewing,
  offer, rejected, dismissed) with a note. Dismissed postings disappear from rankings, the
  digest and alerts.
* **Pinned** firms get the biggest ranking boost. "Find job board" probes Greenhouse, Lever
  and Ashby (public requests only) and schedules collection; pinning a firm from a board
  posting links its board directly.
* **Saved searches** rerun with one tap or key and show only what is new.
* **Daily digest and alerts** are off until you switch them on: digest at a chosen time,
  alerts for new openings at pinned firms, optional alerts for unread LinkedIn messages (never
  quoting message text), with quiet hours. While paused, no alerts are sent and LinkedIn is not
  visited; the digest is built from local data only.

## Facts and drafts

Drafts may only cite **confirmed facts** about you. Add them yourself (`/fact skill: Python,
3 years`, or the Profile view), or, with a local model configured, let it propose facts from
your CV; proposals stay unconfirmed until you confirm each one. A cover letter draft is
checked so every claim cites confirmed facts and invents no numbers; it is stored as a checked
artifact for your review (Drafts view, or the Telegram reply) and never sent anywhere.

Set `inference.model` to an Ollama model you installed and started yourself. Nothing starts
Ollama or downloads a model for you.

## Public job boards

```sh
uv run linkedin-bot collect greenhouse astranis --limit 20     # one explicit read
uv run linkedin-bot schedule-board ashby Linear                # recurring, every jobs_interval
uv run linkedin-bot run                                        # foreground collection only
```

Scheduled reads keep whole boards: Greenhouse and Ashby return everything in one response
(up to 500 postings), Lever is walked page by page (up to 500 per read). Greenhouse descriptions
are fetched as plain text when the board fits the byte budget. A board token is a research lead
until linked from the firm's own careers page; collection does not claim employer verification.
Boards are never read more often than every 30 minutes (`linkedin.jobs_interval_minutes`,
default 6 hours).

## LinkedIn (read only)

```sh
uv run linkedin-bot linkedin login    # visible window on the app's own profile; sign in once
uv run linkedin-bot linkedin status   # headless session check
uv run linkedin-bot linkedin timing   # per-step timings, never content
```

The cockpit's System view does the same sign in (it releases the warm browser first). In
Telegram: `/li_status`, `/li_messages [1-10]`, `/li_search people|firms <query>`,
`/li_profile [https://www.linkedin.com/in/<name>]`, `/li_release` (free the profile before a
CLI login).

The connector can only navigate and read; a test fails if the package gains any click, type
or press call. It stops at the first login or verification page and pauses for 6 hours, never
solving challenges. Limits: 30 page loads per hour, 150 per day, 4 to 10 s between back to back
loads. The budget and any cooldown are stored in `data_dir/linkedin-budget.json`, so they
survive restarts and are shared by the CLI and the long running process. Page parsers were
verified against live pages on 2026-10-03; LinkedIn changes its markup often, so a
`layout_changed` reply means they need updating. LinkedIn's terms prohibit automated access,
so your account could be restricted; the limits reduce but do not remove that risk. Message
text is never stored or logged.

A warm headless browser stays open between reads and closes after 10 idle minutes
(`linkedin.idle_close_seconds`). Images, fonts and ads are not loaded
(`linkedin.block_resources`), and repeat reads are cached in memory (messages 60 s, searches
5 min, profiles 10 min; Refresh bypasses the cache).

## Backups and upkeep

```sh
uv run linkedin-bot backup
uv run linkedin-bot prune             # snapshot, rotate, prune and compact now
uv run linkedin-bot --data-dir /path/to/fresh/private-directory restore /path/to/snapshot
```

While `serve` or the cockpit runs, daily upkeep takes an automatic snapshot (`backups/auto-*`,
the newest 7 are kept; manual snapshots are never removed), drops superseded posting versions
older than 180 days and dismissed or untracked postings gone for a long time, and compacts the
database monthly. Restore requires a fresh target. Checksums, schema compatibility, database
integrity and artifact manifests are checked before publication. Restored data starts paused
and draft only with an audit flag, and must be paired with Telegram again. Backups exclude
credentials, browser profiles and diagnostics. Files are private by permissions, not encrypted
by this application, so keep your disk protection on.

## Configuration

See [config.example.toml](config.example.toml): timezone, data directory, board and inbox
intervals, browser options and the local model. `requests_interval_minutes` and
`posts_interval_minutes` from older files are still accepted and ignored.

## Checks

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build --out-dir /tmp/linkedin-bot-dist
```

The tests use synthetic facts, real temporary SQLite databases, HTTP doubles, fake LinkedIn
connectors and Textual's headless pilot for the cockpit. Only the separately recorded probes
establish live source access; see the [measured evidence](docs/feasibility/M0-M1-2026-10-01.md).
Explicit feasibility probes stay separate from the default tests:

```sh
uv run linkedin-bot probe ollama --model INSTALLED_MODEL_TAG
uv run linkedin-bot probe browser --browser-executable /usr/bin/chromium
uv run python scripts/feasibility.py
```
