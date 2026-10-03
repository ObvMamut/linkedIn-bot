# Implementation roadmap

_Reconstructed 2026-10-03. The original file was overwritten by a copy of `README.md`
and no history existed to restore it. Milestones are rebuilt from the traceability table
in [requirements.md](requirements.md) and the evidence in
[M0/M1 measurements](../feasibility/M0-M1-2026-10-01.md)._

Every milestone keeps the global constraints: draft-only by default, application and
communication caps of 0, tokens only from the environment, no cloud inference unless
explicitly enabled, and no employer-facing effect without a recorded approval.

| Milestone | Scope | Requirements | Gate |
| --- | --- | --- | --- |
| M0 | Feasibility measurements (public ATS, browser, resources) | R02, R06, R09, R13 | Done; account and model gates still blocked |
| M1 | Foundations: SQLite, scheduler, policy stub, backup/restore, CLI | R01, R11, R14 | Done |
| M2 | Telegram pairing, allow-list, resumable onboarding (goals, CV, facts) | R01, R12 | V02, V15 |
| M3 | Discovery, pagination, opportunity identity and dedup, company verification, LinkedIn read-only connector, matching inputs | R02–R06 | V03–V07 |
| M4 | Matching and ranking, local inference, truthful application materials | R05, R07, R08, R13 | V06, V08, V10, V16, V17 |
| M5 | Autonomy policy, quotas, reservations, approval-gated execution | R04, R09–R11 | V09, V11, V12, V14 |
| M6 | Supervised pilot, outreach, monitoring of messages, requests and posts | R04, R09, R10, R14 | V09, V12, V13, V18–V20 |

## Dependencies

M2 precedes everything that talks to the user. M3 produces the opportunities that M4
ranks. M5 needs M4 drafts and M2 approvals. M6 needs M5 and a clean pilot review.

## Live inputs only the user can supply

Telegram bot token (`TELEGRAM_BOT_TOKEN`), job goals and baseline CV, authorization to
start Ollama and download a model, and a manual LinkedIn login in the application-owned
browser profile. Code for each milestone is verified against fakes and fixtures; the live
gates stay pending until these exist.
