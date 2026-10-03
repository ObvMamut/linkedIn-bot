# LinkedIn browser integration

_Design baseline · 2026-09-30 · Requirements R02–R05, R09–R11 · Decision ADR-02_

---

## 📋 Integration boundary and feasibility

Implement a project-owned `LinkedInConnector` using Playwright with a dedicated local browser profile. This is an unofficial website integration. LinkedIn prohibits unauthorized automated access/activity[^1], and its documented open API permissions do not cover the general inbox, feed, and candidate-application access required here[^2]. The user's requested automation remains a target, with feasibility gates rather than an assertion of reliable or permitted access.

No live login, inbox retrieval, feed retrieval, messaging, or application capability has been verified. Each capability is independently released only after the evidence required in [research and feasibility](research-and-feasibility.md) and [validation](validation.md) is obtained. Browser technology feasibility does not establish LinkedIn capability feasibility.

## 🔧 Session ownership

Launch a persistent Chromium context in a profile directory owned solely by this application. Playwright supports persistent contexts and reusable authentication state[^3]. Do not attach to the user's everyday browser profile or store the LinkedIn password in configuration.

Initial setup opens a visible browser. The user logs in and completes any authentication challenge directly; the adapter verifies the expected account before storing a session-ready status. Session status is `ready`, `auth_required`, `challenge_required`, or `unavailable`. Credentials, cookies, CSRF values, browser profiles, and storage state are never sent to an LLM or Telegram.

Use one connector lock for session changes and LinkedIn interactions. Reads and writes share that lock, preventing a scheduled navigation from disturbing a form or message being prepared. External employer form adapters use separate unauthenticated contexts unless a supported portal explicitly requires user login.

Session expiry/challenge detection stops the affected operations and sends a single recovery notice. The user opens a local browser recovery command; after verified login, the connector resets its health and queued actions are re-prepared. No automated CAPTCHA solving, stealth plugins, proxy rotation, or challenge bypass is part of this design.

## 📚 Capability contracts

| Capability | Retrieval or effect | Checkpoint / confirmation |
| --- | --- | --- |
| Jobs | Configured search/result pages and job details | Stable job ID/URL; query watermark; coverage per run |
| Messages | Accessible conversation list and new message pages | Conversation/message IDs; latest persisted observation |
| Incoming requests | Pending requests with sender/context | Stable request/member ID; accept/decline requires observed updated state |
| Feed | Bounded newly observed posts matched to user interests | Post ID/URL and relevance; coverage is normally partial |
| Easy Apply | Inspect steps, populate supported fields, attach exact artifacts, submit via executor | Post-submit confirmation or visible applied state for that job |
| Replies/outreach | Open the exact conversation/member target, prepare text, send via executor | Matching outbound message in the intended conversation |
| Invitations | Prepare recipient and optional note; send via executor | Pending/sent invitation state for that recipient |

Read methods return `CollectionPage` with coverage. `prepare()` methods return an exact `PreparedAction` including form questions, typed answers, file hashes, recipient/job identifiers, and observation digest. `execute()` and `reconcile()` return the shared `ActionResult`. Effects and state transitions follow [autonomy](workflows-and-autonomy.md).

LinkedIn roles with external application URLs are handed to the appropriate employer-form adapter and keep the same canonical opportunity/application identity. Do not automatically open an external link carrying privileged browser state.

## 🔗 Retrieval and browser interaction

Use visible labels, accessible roles, and stable source identifiers for locators. Encapsulate page-specific locators in adapter modules, with saved redacted fixtures for recognized layouts. An unexpected page or missing required locator produces `layout_changed` or `unsupported`, not a speculative click.

Bound every collection run by pages/items and wall time; persist a cursor only after the batch is committed. Use overlap on subsequent collections to catch reordered items. Treat unread status as a display hint, not the sole message identity. Opening content may affect LinkedIn's read indicators; this behavior must be observed and disclosed during M0.

Maintain configured searches independently of the recommendation feed. Feed relevance is a separate workflow; employer discovery also uses [external sources](discovery-and-matching.md). A partial feed/search run cannot imply there are no relevant posts or jobs elsewhere.

Direct authenticated request optimization is deferred. The initial connector uses browser UI flows; introducing undocumented internal endpoints requires separate capability tests and a decision update. Do not rely on an unofficial wrapper merely because its API resembles the needed operations.

## 📍 Failure handling and maintenance

Handle rate limits with bounded read backoff and a capability cooldown; do not keep retrying authentication or challenges. Unexpected forms stop before final submission. After the final control, a timeout, ambiguous UI, or lost session becomes `unknown` unless channel-specific evidence proves otherwise.

Recovery uses read-only evidence: application status/confirmation, outbound conversation messages, or invitation/request state. Same text alone is insufficient to identify a sent message when multiple historical messages match. If confirmation cannot be attributed to this target/attempt, preserve uncertainty and ask the user to inspect it.

Capability status includes last successful run, last tested adapter version, supported account language/layout, coverage, cooldown, and failure reason. A supported-layout claim is limited to tested fixtures and live verification; other locales/layouts remain unsupported until checked. Store diagnostic traces only in the private location described in [privacy](security-and-privacy.md).

Acceptance: V03–V05 cover session and collection behavior; V09, V11–V13 cover effects. Changes to locators require fixture regression checks before re-enabling the affected capability.

## 🔗 References

[^1]: LinkedIn Help. "Prohibited software and extensions." Checked 2026-09-30. https://www.linkedin.com/help/linkedin/answer/a1341387/prohibited-software-and-extensions
[^2]: Microsoft Learn / LinkedIn. "Getting access to LinkedIn APIs." Checked 2026-09-30. https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access
[^3]: Playwright Python documentation. "Authentication" and "BrowserType." Checked 2026-09-30. https://playwright.dev/python/docs/auth and https://playwright.dev/python/docs/api/class-browsertype
