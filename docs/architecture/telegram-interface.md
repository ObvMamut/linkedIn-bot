# Telegram user interface and authorization

_Design baseline · 2026-09-30 · Requirements R01, R08, R11–R14 · Decisions ADR-01 and ADR-04_

---

## 📋 Transport and account pairing

Use the Telegram Bot API through `python-telegram-bot` with long polling. Telegram supports long polling and webhooks; the two update-delivery mechanisms are mutually exclusive[^1]. Laptop deployment uses polling and does not require an inbound public endpoint. No webhook is configured by default.

The user creates a bot through Telegram's BotFather and stores its token locally[^2]. Pair one private chat and one Telegram user with the local installation. A local `pair` command generates a one-use random pairing code, valid for ten minutes, displayed only in the local console. `/start <code>` in a private chat binds the user/chat after a successful comparison. Disable pairing once bound; re-pairing requires a local command. Knowing the bot username does not confer control.

After pairing, authorize every message, document, and callback against both stored user ID and private chat ID. Ignore group/channel use and other senders. The bot must not reveal profile, account status, or documents to an unauthorized sender. Run only one poller for a token.

## 📍 Onboarding journey

1. Pair the private chat and show the current `draft_only` execution mode.
2. Ask for role/internship targets, locations and remote rules, dates, company preferences, exclusions, and languages. Do not infer job geography from the local timezone.
3. Accept a searchable CV PDF or pasted text; extract proposed facts locally and present confirmation batches. Ask for unresolved eligibility/contact/availability facts.
4. Provide local browser login instructions for LinkedIn. Telegram never asks for the LinkedIn password, OTP, session cookie, or CAPTCHA content.
5. Show model-evaluation status and explain optional cloud task/data permissions and caps.
6. Configure schedule and daily outbound limits. Introduce reviewed mode first for released flows; enable rules-based mode only when M6 gates and user settings are satisfied.

Onboarding is resumable. Missing optional preferences do not stop browsing; required unknown facts block only dependent documents/actions.

## 📚 Commands and conversational behavior

These commands specify the future interface; they are not currently implemented.

| Command | Behavior |
| --- | --- |
| `/start` | Pair with a supplied code or resume authorized onboarding |
| `/goals` | View/edit search preferences and confirm a new revision |
| `/profile` | Review confirmed facts, unresolved facts, and baseline CV |
| `/search` | Enqueue one bounded research run; show run ID |
| `/opportunities` | Show ranked opportunities with source links and reasons |
| `/applications` | Show prepared, submitted, and unknown applications |
| `/pending` | Show factual questions, approvals, blocks, and unresolved effects |
| `/status` | Show source coverage, capability health, schedule, model route, and remaining limits |
| `/pause` / `/resume` | Pause work or resume current configured mode; resume is not approval |
| `/policy` | Edit mode, action permissions, target rules, and numerical caps |
| `/model` | Show local evaluation and configure permitted DeepSeek fallback |
| `/cancel <action_id>` | Cancel undispatched action or request cancellation of in-flight work |
| `/retry <action_id>` | Re-propose a confirmed failed action; unknown outcomes cannot retry |
| `/resolve <action_id>` | Record a user-inspected outcome for an unknown effect, with an explicit confirmation and evidence note |
| `/help` | Explain available commands and recovery paths |

Natural-language requests can create typed command proposals, but application code validates them. Read-only queries run directly. Changes to facts, goals, execution rules, recipients, or disclosure settings require a displayed before/after confirmation. Persist the chosen revision; a conversational suggestion alone is not configuration.

An opportunity card contains role/company, source, eligibility, fit reasons, gaps, and buttons for details, prepare, exclude, or mark applied elsewhere. An action card identifies recipient/role, full draft or linked artifacts, screening answers, artifact hashes/revision, and approval expiry. Show reasons for any block rather than offering an approval that cannot execute.

## 🔗 Approval callbacks and document delivery

Use short opaque callback tokens mapped to a server-side record containing action ID, revision, digest, permitted operation, owner/chat, and expiry. Telegram callback data has a bounded size[^1], so do not embed entire documents or policies in it.

Process callbacks transactionally: authorize sender/chat, load token, compare action revision/state, validate expiry, and record decision once. A repeated callback answers with the current status. An edited/replaced action invalidates old tokens. Acknowledge the callback quickly, then enqueue long work; never block approval handling on model generation or browser navigation.

Approval is permission for a specific action subject to hard constraints, not evidence of delivery. Update cards to show prepared, blocked, executing, succeeded, failed, or unknown. Avoid promising cancellation after dispatch. Policy semantics and transitions are owned by [autonomy](workflows-and-autonomy.md).

An unknown-outcome card links to the exact job/conversation and offers inspected-success or confirmed-no-effect resolution. Require the authorized user to confirm what they observed and provide an evidence note; persist it as `user_attestation`, distinct from connector-observed evidence. Resolution updates outcome/quota accounting atomically. Inability to inspect leaves the action unknown; it is not a reason to mark it failed and retry.

Send final artifact previews as documents to the paired chat. Label them with role and revision, and keep the local manifest as the source of truth. Telegram's file size and message limits must be checked against current API documentation when implementing[^1]. If an artifact exceeds a limit, report that and provide local retrieval instructions; do not upload to another service automatically.

## 🎯 Digests, recovery, and acceptance

Default digest time is 18:00 in the configured timezone. Summarize new opportunities, relevant posts, new messages/requests, completed effects, pending questions, unknown outcomes, and coverage gaps. Urgent authentication/challenge and uncertain-effect notices are sent when discovered and deduplicated until status changes. A failed notification leaves approval-dependent work pending.

Persist inbound update IDs after their command/decision is durably recorded; replayed updates do not repeat commands. Persist notification IDs separately. Telegram delivery cannot be guaranteed exactly once after an ambiguous send timeout, but duplicate cards must never cause duplicate employer-facing effects.

V02 tests onboarding/revisions, V15 tests authorization and callback replay, V09 tests exact approval semantics, and V14 tests pause/recovery. See [operations](operations.md) for polling lifecycle and [privacy](security-and-privacy.md) for data disclosure.

## 🔗 References

[^1]: Telegram. "Bot API." Checked 2026-09-30. https://core.telegram.org/bots/api
[^2]: Telegram. "Bots FAQ." Checked 2026-09-30. https://core.telegram.org/bots/faq
