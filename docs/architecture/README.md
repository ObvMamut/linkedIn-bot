# Autonomous job-search agent architecture

_Design baseline · 2026-09-30 · Single-user, laptop deployment · M0 evidence and M1 foundations implemented_

---

## 📋 Purpose and current state

This architecture translates the original [project goals](../../goals.md) into specifications for a personal job-search agent. It covers LinkedIn monitoring and automation, independent company research, tailored applications, LinkedIn communication, and Telegram control. The original goals remain unchanged.

The workspace was inspected before writing these documents: it contained `goals.md`, no application code or manifest, and no Git metadata. This is a target architecture, not a description of an existing system. No accounts have been accessed, messages sent, applications submitted, model downloads performed, or persistent services started as part of this documentation work.

The agreed direction is local Python, SQLite, a project-owned Playwright LinkedIn connector, Telegram, local inference, optional DeepSeek fallback, and rules-based autonomy. Functional claims about external systems are recorded in the [research record](research-and-feasibility.md); live evidence is now recorded in [M0/M1 measurements](../feasibility/M0-M1-2026-10-01.md); LinkedIn, Telegram, and local inference remain gated.

## 📚 Reading order and document ownership

| Order | Document | Owns |
| --- | --- | --- |
| 1 | [Requirements](requirements.md) | Goals, acceptance criteria, scope, traceability |
| 2 | [Research and feasibility](research-and-feasibility.md) | External evidence, limitations, experiments |
| 3 | [System architecture](system-architecture.md) | Components, dependency rules, runtime, interfaces |
| 4 | [Domain and data](domain-and-data.md) | Entities, identifiers, persistence, migrations |
| 5 | [Workflows and autonomy](workflows-and-autonomy.md) | Action states, permission checks, recovery |
| 6 | [LinkedIn integration](linkedin-integration.md) | Browser session and LinkedIn capabilities |
| 7 | [Discovery and matching](discovery-and-matching.md) | External research and opportunity ranking |
| 8 | [Applications and outreach](applications-and-outreach.md) | Application materials, forms, employer communication |
| 9 | [LLM and evaluation](llm-and-evaluation.md) | Inference routing, validation, model selection |
| 10 | [Telegram interface](telegram-interface.md) | User journeys, commands, approval interactions |
| 10b | [Terminal cockpit](tui.md) | The `tui` process model, views and their rules |
| 11 | [Security and privacy](security-and-privacy.md) | Secrets, personal data, external-content boundaries |
| 12 | [Operations](operations.md) | Configuration, scheduling, startup, maintenance |
| 13 | [Validation](validation.md) | Tests and release gates |
| 14 | [Decisions](decisions.md) | ADR-01 through ADR-04 and revisit conditions |
| 15 | [Implementation roadmap](implementation-roadmap.md) | Dependency-ordered milestones M0 through M6 |

## 🎯 How to use the specifications

Start a coding plan from one milestone in the roadmap. Read its owning documents and the acceptance scenarios before proposing files, dependencies, or tests. Carry the requirement IDs `R01`–`R14`, validation IDs `V01`–`V20`, and milestone IDs into implementation plans so coverage can be checked.

The documents distinguish three kinds of statements:

- **Baseline decision:** the behavior the implementation should follow unless this architecture is revised.
- **Research finding:** a documented property of an external system, with a source and check date.
- **Feasibility gate:** an experiment needed before a capability can be marked working or enabled autonomously.

Configuration examples are specifications for future implementation. They are not executable files, installation instructions for an existing package, or actual user preferences. Personal job targets, employment eligibility, numerical action limits, and cloud disclosure permissions must be supplied during onboarding.

## 🔧 Maintenance and acceptance

Change the owning document first when behavior changes, then update related diagrams, contracts, requirements, validation scenarios, and roadmap gates in the same change. Use a new decision record when changing a component boundary, provider policy, or execution guarantee. Do not silently repurpose an existing ID.

Keep external sources dated. Recheck version-sensitive documentation when selecting dependency versions; lock compatible versions in M1. An unavailable source or failed feasibility experiment must be recorded with its effect on the roadmap.

This documentation set is accepted when every requirement has a specification and validation scenario, relative links resolve, diagrams parse, shared contracts agree, and research does not imply that untested capabilities already work. Application-level tests are defined here for later work; they have not run against an application that does not yet exist.

## Implementation status · 2026-10-01

The [application README](../../README.md) documents the implemented M1 CLI and checks.
M0 public-source/browser measurements and blocked account/model checks are recorded in
[the evidence report](../feasibility/M0-M1-2026-10-01.md). This does not release M2–M6 features.
