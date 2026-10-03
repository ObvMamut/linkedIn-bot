# Local job-search agent

M0 feasibility work, M1 foundations, M2 Telegram onboarding, M3 matching, M4 grounded
drafting and the M5 approval/quota machinery are implemented. No effect adapter and no LinkedIn
connector exist, so nothing can be sent to an employer. This is a Linux, single-user,
foreground Python application. LinkedIn monitoring, evaluated
inference, materials generation, and employer-facing execution belong to later milestones.
The architecture and its requirements remain in [docs/architecture](docs/architecture/README.md).

Every installation starts in **draft-only mode with application and communication caps
of zero**. M1 cannot enable effects, cloud inference, or private-account access. Nothing
starts Ollama, downloads models, installs a service, or schedules recurring research during
installation or ordinary initialization.

Use Python 3.14 (validated with 3.14.7) and uv (validated with 0.10.2):

```sh
uv sync --locked
uv run linkedin-bot --config config.example.toml init
uv run linkedin-bot --config config.example.toml doctor
```

`init` creates the configured private data directory and SQLite database. `doctor` reports
schema, profile revisions, work coverage/errors, unknown actions, artifact integrity, and
unreleased capabilities. Secrets do not belong in TOML. Unknown keys, remote Ollama
endpoints, invalid timezones, and shared/symlinked storage paths are rejected.

One explicitly requested public read can be run without a model, LinkedIn, or Telegram:

```sh
uv run linkedin-bot collect greenhouse astranis --limit 20
uv run linkedin-bot collect lever verygoodsecurity --limit 20
uv run linkedin-bot collect ashby Linear --limit 20
```

These create completed one-shot durable work records and persist public observations.
They do not enable recurring work. A board token is a research lead until linked from a
verified official company page; the collection command does not claim employer verification.
Greenhouse/Ashby coverage is partial if the selected item bound truncates their list.
Greenhouse listing descriptions are absent until a separate detail read. Lever returns an
opaque adapter cursor when more results may exist. Complete monitoring/deduplication and
opportunity matching are M3/M4 work.

Recurring reads require an explicit scheduling command and an explicit foreground runner:

```sh
uv run linkedin-bot schedule-board greenhouse astranis --interval-seconds 21600
uv run linkedin-bot run
```

Use `run --once` to process at most one due item. Ctrl-C or SIGTERM stops claims, settles
the bounded current read, closes clients/storage, and releases the kernel-held lock.
## Telegram (M2)

Create a bot with BotFather and keep its token in `LINKEDIN_AUTOMATION_BOT_TOKEN` (an environment
variable, or a private file your shell sources; never in the repo). Then:

```sh
uv run linkedin-bot pair        # prints a one-use code, valid ten minutes
# send "/start <code>" to the bot from a private chat
uv run linkedin-bot telegram    # foreground long polling; --once handles one batch
uv run linkedin-bot serve       # the same, plus board collection and the opt-in digest and alerts
```

Send `/menu` for the button interface: Messages, Search (people, firms, jobs), Pinned, Jobs,
Saved, Settings and Status. Results are cards with Prev/Next, Pin, Open and Save search; one
message edits itself in place. Typed commands below still work.

Commands: `/opportunities` (ranked matches with reasons), `/prepare <id>` (cover-letter draft for
review; needs `inference.model` and a running local Ollama), `/pending`, `/approve <id>`,
`/reject <id>`, `/policy` (mode and daily caps, changes need `confirm`, hard ceilings 10
applications and 5 messages per day), `/resolve <action> success|none <note>`, `/pause`, `/resume`.

## Settings, pins, saved searches, digest

All of this is plain rules, no AI, and is edited from the buttons:

- **Settings**: locations and remote, internship preference, role keywords (must-have and
  excluded words are hard filters; nice-to-have only ranks), blocked firms and preferred
  industries, languages, maximum posting age, and the **priority order** of the ranking criteria
  (role, location, firm, freshness, language). Free-text **interests** are stored for the later
  AI check, which stays off until a local model is installed.
- **Pinned** firms and people with notes and tags. For a firm, "Find job board" probes Greenhouse,
  Lever and Ashby (public requests only) and schedules collection; pinned firms get the biggest
  ranking boost.
- **Saved searches** (people, firms, jobs): rerun with one tap to see only what is new.
- **Daily digest and alerts** (all off until you switch them on, and only while `serve` runs):
  digest at a chosen time, alerts for new openings at pinned firms, optional alerts for unread
  LinkedIn messages (never quoting message text), with quiet hours. A digest uses at most 5
  LinkedIn page loads inside the hourly and daily limits.

## Speed

A warm headless browser stays open between commands and closes after 10 idle minutes
(`linkedin.idle_close_seconds`). Burst pacing applies only to back-to-back
loads, images/fonts/ads are not loaded (`linkedin.block_resources`), each read waits for the one
element it needs, and repeat reads are cached in memory (messages 60 s, searches 5 min, profiles
10 min; Refresh bypasses the cache). `linkedin-bot linkedin timing` prints per-step times (never
content). Measured warm: status ~3 s, inbox ~4 s, searches and profile ~2 s, cached 0 s. Send
`/li_release` before `linkedin login` so the warm browser lets go of the profile.

## LinkedIn (read-only)

```sh
uv run linkedin-bot linkedin login    # visible window on the app's own profile; sign in once
uv run linkedin-bot linkedin status   # headless session check
```

Then, in Telegram: `/li_status`, `/li_messages [1-10]`, `/li_search people|firms <query>`,
`/li_profile [https://www.linkedin.com/in/<name>]`. The connector can only navigate and read;
a test fails if the package gains any click, type or press call. It stops at the first login or
verification page and pauses for 6 hours, never solving challenges. Limits: 30 page loads/hour,
150/day, 4-10 s between loads. Page parsers were verified against live LinkedIn pages on 2026-10-03; LinkedIn changes its
markup often, so a `layout_changed` reply means they need updating (`/li_status` shows the date). LinkedIn's terms prohibit automated
access, so your account could be restricted; the limits reduce but do not remove that risk.
Message text is never stored or logged.

Only the paired user and private chat are answered; everyone else is silently ignored.
Onboarding (roles, locations, exclusions, work eligibility, pasted CV) is resumable and
stored locally. Restoring a backup clears the pairing.

`pause` stops new claims; `resume` resumes reads and retains draft-only restrictions.
Overdue periodic work coalesces into one current read. The application does not run when
stopped or while the laptop sleeps. No recurring schedule was installed during development.

Create and restore private snapshots:

```sh
uv run linkedin-bot backup
uv run linkedin-bot --data-dir /path/to/fresh/private-directory restore /path/to/snapshot
```

Restore requires a fresh target so existing history is preserved. Checksums, schema/app
compatibility, database integrity, and artifact manifests are checked before publication.
Restored data starts paused and draft-only, with an external-history audit flag that M1
cannot clear. Ready/executing effects become unknown. Backups exclude credentials, browser
profiles, and diagnostics. Backups are manual in this foundation; automatic daily backup
and retention integration will be added with the long-running control lifecycle. Keep your
laptop's normal disk protection enabled; files are private by permissions, not encrypted by
this application.

Explicit feasibility probes are separate from default tests:

```sh
uv run linkedin-bot probe ollama
uv run linkedin-bot probe ollama --model INSTALLED_MODEL_TAG
uv run linkedin-bot probe browser --browser-executable /usr/bin/chromium
uv run python scripts/feasibility.py
```

The model probe uses synthetic facts, a 4K context, bounded output/deadline, and unloads
the model afterward. It never downloads a missing model. A single probe does not pass the
30-case M4 evaluation gate. The browser probe uses a local synthetic page with network
requests blocked. Installed Chromium passed; the Playwright bundled browser was not
provisioned, and a custom executable is not the reproducible production browser baseline.
The public probe makes bounded GETs only and reports failures instead of empty successes.

Project checks:

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build --offline --out-dir /tmp/linkedin-bot-dist
```

The tests use synthetic facts and real temporary SQLite databases, including rollback,
revision conflicts, dispatch recovery, quota-reservation retention, work claims/checkpoints,
pause/sleep coalescing, backup/restore integrity, runtime locking, and boundary failures.
HTTP doubles test adapter contracts; only the separately recorded probes establish live
source access. This execution environment required approved unsandboxed runs for thread
wakeups, browser sockets, and network checks; see the [measured evidence](docs/feasibility/M0-M1-2026-10-01.md).

Next is M2: Telegram private-chat pairing and resumable onboarding. Set a locally stored
`LINKEDIN_AUTOMATION_BOT_TOKEN` from BotFather when that milestone is implemented; do not send tokens
in chat. LinkedIn needs user-completed login in a dedicated profile and an expected account
identity before read gates. Supply job goals, a baseline CV, and confirm extracted facts
through onboarding. Ollama must be explicitly started by the user before model inventory
and evaluation can proceed. DeepSeek remains optional and disabled.
