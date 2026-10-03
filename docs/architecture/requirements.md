# Requirements and goal traceability

_Design baseline · 2026-09-30 · Requirements R01–R14 · Decisions ADR-01–ADR-04_

---

## 🎯 Intended outcome

The agent periodically looks for jobs and internships, monitors the user's LinkedIn jobs, conversations, requests, and relevant posts, and researches companies independently of LinkedIn recommendations. It prepares truthful tailored CVs and cover letters, completes supported applications, and handles LinkedIn communication within user-configured rules. Telegram is the user interface.

The source of product intent is [goals.md](../../goals.md) and the user's subsequent choices: personal laptop deployment, local LLM preference, optional DeepSeek fallback, mandatory unofficial LinkedIn automation through a project-owned browser connector, rules-based autonomy, and LinkedIn-first employer communication.

## 📋 Functional requirements

| ID | Required behavior | Acceptance criterion |
| --- | --- | --- |
| R01 | Capture candidate facts and user search goals | Store confirmed facts separately from inferred facts; persist role, location, internship, availability, and exclusion preferences; changes affect future ranking and action evaluation. |
| R02 | Monitor LinkedIn opportunities | Retrieve and normalize jobs from configured searches; identify new and changed records; show observed coverage and source links. |
| R03 | Monitor LinkedIn messages | Incrementally collect accessible conversations; identify new messages and questions without duplicating notifications. |
| R04 | Monitor LinkedIn requests | Record incoming connection requests; propose or execute configured accept/decline decisions through the shared action policy. |
| R05 | Identify interesting posts | Summarize newly observed relevant feed posts with author/source and reasons for relevance; disclose bounded feed coverage. |
| R06 | Discover employers independently | Generate searches from user goals, find startups and smaller companies, verify their career pages, and monitor available public job boards. |
| R07 | Match opportunities to the user | Apply hard eligibility filters before explained fit ranking; distinguish unknown eligibility from a confirmed match. |
| R08 | Prepare tailored materials | Produce versioned CV PDFs and cover letters grounded in confirmed facts, with an inspectable change summary and artifact hashes. |
| R09 | Apply to jobs and internships | Complete supported LinkedIn Easy Apply and employer-hosted forms under policy; retain confirmation or classify the outcome as unknown. |
| R10 | Communicate on LinkedIn | Draft and send supported replies, initial outreach, follow-ups, and connection invitations under recipient and frequency rules. |
| R11 | Operate periodically and autonomously | Run configured workflows while the laptop is awake; execute allowed actions, request approval for exceptions, and recover without blindly replaying effects. |
| R12 | Communicate through Telegram | Support onboarding, preference changes, digests, artifact previews, approvals, pause/resume, and recovery notices in one authorized private chat. |

## 🔧 Nonfunctional requirements

| ID | Required behavior | Acceptance criterion |
| --- | --- | --- |
| R13 | Local inference with optional cloud fallback | Run core inference without a paid provider; select models by measured quality/resources; allow DeepSeek only for permitted tasks/data within a configured cap. |
| R14 | Reliable and private local operation | Preserve durable state across restart; protect sessions and secrets; redact logs; provide backup, deletion, and visible degraded status. |

No specific laptop GPU, latency, or model quality is assumed. External source coverage is bounded by access and observed pages. The product must report those limits rather than claim exhaustive monitoring.

## 📍 Scope and user journeys

**Onboarding:** the user connects Telegram, supplies a baseline CV and preferences, confirms extracted career facts, logs into LinkedIn in a dedicated local browser, evaluates local inference, and configures execution permissions. The application starts in `draft_only` mode; enabling rules-based execution is a separate persistent setting.

**Periodic research:** retrieve configured sources, record coverage, deduplicate, filter, rank, and present new opportunities. Company research can continue when LinkedIn is unavailable. A discovered company without an open role can become an outreach candidate, clearly distinguished from a vacancy.

**Application:** prepare artifacts and screening answers; pause for unknown facts or unsupported forms; evaluate the exact proposed submission; execute if authorized; record confirmation or uncertainty. A tailored CV never modifies the baseline facts or implies qualifications the user has not confirmed.

**Communication:** summarize incoming messages and requests; prepare a context-aware response or request decision; send or accept only through the action policy. Feed posts are read and summarized. Automatic likes, comments, public posting, and resharing are outside this baseline.

Email integration, a web dashboard, multiple users, paid search providers, cloud hosting, automated challenge solving, and arbitrary employer portals are later extensions. These exclusions do not remove the target of automated applications and communication on the supported initial channels.

## 🔗 Traceability matrix

| Requirement | Primary specification | Milestone | Validation |
| --- | --- | --- | --- |
| R01 | [Domain and data](domain-and-data.md), [Telegram](telegram-interface.md) | M1–M2 | V01, V02 |
| R02 | [LinkedIn](linkedin-integration.md) | M0, M3 | V03, V04 |
| R03 | [LinkedIn](linkedin-integration.md), [Telegram](telegram-interface.md) | M3 | V03, V05 |
| R04 | [LinkedIn](linkedin-integration.md), [Autonomy](workflows-and-autonomy.md) | M3, M5–M6 | V05, V09 |
| R05 | [LinkedIn](linkedin-integration.md), [Matching](discovery-and-matching.md) | M3–M4 | V04, V06 |
| R06 | [Discovery](discovery-and-matching.md) | M0, M3 | V04, V07 |
| R07 | [Matching](discovery-and-matching.md) | M4 | V06, V08 |
| R08 | [Applications](applications-and-outreach.md), [LLM](llm-and-evaluation.md) | M4 | V08, V10 |
| R09 | [Applications](applications-and-outreach.md), [Autonomy](workflows-and-autonomy.md) | M0, M5–M6 | V09, V11, V12 |
| R10 | [Outreach](applications-and-outreach.md), [Autonomy](workflows-and-autonomy.md) | M5–M6 | V09, V12, V13 |
| R11 | [Autonomy](workflows-and-autonomy.md), [Operations](operations.md) | M1, M5–M6 | V09, V11, V14 |
| R12 | [Telegram](telegram-interface.md) | M2 | V02, V15 |
| R13 | [LLM](llm-and-evaluation.md) | M0, M4 | V08, V16, V17 |
| R14 | [Privacy](security-and-privacy.md), [Operations](operations.md) | M1–M6 | V11, V14, V18, V19, V20 |

The final target requires all rows. Earlier milestones intentionally expose narrower capabilities; a source integration or application flow cannot be declared supported until its gate in [validation](validation.md) passes.
