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

Create a bot with BotFather and export `TELEGRAM_BOT_TOKEN` in your shell (never in a file
or chat). Then:

```sh
uv run linkedin-bot pair        # prints a one-use code, valid ten minutes
# send "/start <code>" to the bot from a private chat
uv run linkedin-bot telegram    # foreground long polling; --once handles one batch
```

Commands: `/opportunities` (ranked matches with reasons), `/prepare <id>` (cover-letter draft for
review; needs `inference.model` and a running local Ollama), `/pending`, `/approve <id>`,
`/reject <id>`, `/policy` (mode and daily caps, changes need `confirm`, hard ceilings 10
applications and 5 messages per day), `/resolve <action> success|none <note>`, `/pause`, `/resume`.

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
`TELEGRAM_BOT_TOKEN` from BotFather when that milestone is implemented; do not send tokens
in chat. LinkedIn needs user-completed login in a dedicated profile and an expected account
identity before read gates. Supply job goals, a baseline CV, and confirm extracted facts
through onboarding. Ollama must be explicitly started by the user before model inventory
and evaluation can proceed. DeepSeek remains optional and disabled.
