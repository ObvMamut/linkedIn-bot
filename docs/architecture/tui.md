# Terminal cockpit

## Process model

`linkedin-bot tui` is a long running front end that replaces `serve`. It takes the runtime
lock, opens the database, builds one `runtime.services.Services` object and runs the same
background work `serve` runs (board collection, scheduler for digest and alerts, daily upkeep,
optional Telegram polling) inside Textual's asyncio loop. One process therefore owns the
SQLite connection, the browser profile and the LinkedIn page budget, and every guarantee of
the single process design still holds.

```
CLI (runtime/cli.py)
 └─ RuntimeLock + Database
     └─ Services (runtime/services.py)
         ├─ Operations (workflows/operations.py)   user actions shared by Telegram and the TUI
         ├─ Onboarding, facts, approvals            shared workflows
         ├─ LinkedInCommands + WarmSession + Limiter(BudgetFile)
         ├─ Scheduler (digest, alerts) → Notifiers: Telegram, TUI
         ├─ Maintenance (snapshot, rotate, prune, vacuum)
         └─ Bot (Telegram, optional)
     └─ CockpitApp (tui/app.py) with one View per tab (tui/views/*.py)
```

## Rules the cockpit keeps

* Views only render; reads and changes go through `Operations`, the stores and the shared
  workflows, so Telegram and the cockpit behave the same way.
* Network, browser and model calls run in Textual workers so the interface never blocks;
  `CockpitApp.busy()` marks them so quitting asks first.
* Text from LinkedIn, job boards or the user is shown with markup disabled.
* Logging is redirected while the cockpit runs: the same redacting formatter feeds the Logs
  view and a private rotating file; stderr is restored on exit.
* Views refresh from storage when opened and every few seconds (unless they hold unsaved
  input or a dialog is open), so changes made from Telegram show up.

## Tests

`tests/test_tui.py` drives the app with Textual's pilot against a temporary database and the
fake LinkedIn connector: tab switching, the tracker, search with pin and save, settings, policy
confirmation, pause, help, logs, quitting and the onboarding form.
