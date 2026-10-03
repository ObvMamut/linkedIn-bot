# Architecture decision records

_Design baseline · 2026-09-30 · Requirements R01–R14 · ADR-01–ADR-04 · Decisions adopted for future implementation_

---

## 📋 Decision status and evidence

These records capture the agreed target and baseline engineering choices. "Adopted" means the implementation should follow the decision; it does not mean software or integration feasibility has been demonstrated. Model choice, dependency versions, and supported browser layouts are outputs of explicit experiments and release gates, not unspecified implementation preferences.

External evidence is maintained in [research and feasibility](research-and-feasibility.md). Numerical defaults and runtime behavior are owned by their subsystem documents. New evidence that changes a boundary or guarantee should supersede the affected record explicitly.

## 🔧 ADR-01: Modular Python application with local persistence

**Status:** adopted design. **Requirements:** R01, R06–R08, R11–R14.

**Context:** one user, one laptop, no existing implementation/stack, periodic I/O workflows, browser integration, local inference, and Telegram. Persistent recovery and inspectable behavior matter more than distributed scaling.

**Decision:** use one asynchronous Python application, SQLite through a dedicated storage worker, local immutable artifacts, and explicit adapters. Integrate Telegram lifecycle and a durable scheduler in the application. Use direct Python libraries rather than a general agent framework. Serialize LinkedIn browser operations and local inference; bound independent public HTTP collection.

**Alternatives:** a TypeScript implementation is viable but offers no compatibility benefit in this empty workspace; a standalone LinkedIn HTTP service introduces a process/auth/API boundary the user did not choose; microservices plus a broker/database server add deployment and recovery costs for one laptop.

**Consequences:** simple setup and coherent transactions; adapters can fail independently. Laptop sleep limits uptime, SQLite remains a single-node store, and work budgets must fit one host. Storage operations must not block the async loop. An LLM chat history is not durable workflow state.

**Revisit when:** multiple users, multiple worker hosts, sustained contention, or remote connector ownership becomes a requirement. Keep domain contracts stable while replacing infrastructure. See [system architecture](system-architecture.md) and [operations](operations.md).

## 🔗 ADR-02: Project-owned unofficial LinkedIn browser connector

**Status:** adopted design, capability release conditional on feasibility. **Requirements:** R02–R05, R09–R10.

**Context:** the user requires logged-in LinkedIn automation and selected an owned browser connector. Research does not identify open official API access for the required general workflows; LinkedIn's automation policy restricts unauthorized access. No live account capability has been verified.

**Decision:** use Playwright with a dedicated profile and user-completed login/challenges. Define capability-specific retrieval, preparation, execution, and reconciliation contracts. Start with UI flows, without relying on undocumented internal endpoints or a third-party unofficial wrapper. Keep account effects behind the shared policy/executor.

**Alternatives:** supported APIs plus user-provided exports would reduce required website automation but do not satisfy the user's chosen target; a wrapper-first approach has unverified coverage and maintenance risk; a separate unofficial API service adds complexity without improving source access.

**Consequences:** full control over tested behaviors and maintenance, but layouts, authentication, coverage, and account restrictions remain external risks. Human login recovery may be required. Account access cannot be guaranteed, and operational quotas do not eliminate restrictions. Broken capabilities must report gaps instead of using speculative clicks.

**Revisit when:** authorized official access covers a capability, tested private-request optimization materially improves reliability, or browser feasibility fails. Preserve the product requirement while documenting blocked capabilities. See [LinkedIn integration](linkedin-integration.md).

## 📚 ADR-03: Local-first task inference with configurable DeepSeek fallback

**Status:** adopted design, concrete local model selected by evaluation. **Requirements:** R07–R08, R10, R13–R14.

**Context:** the user prefers a local LLM, questions its performance, and explicitly wants the option to connect inexpensive DeepSeek models. The environment's RAM was observed, but GPU and model performance were not verified.

**Decision:** implement a provider-neutral typed task boundary, local Ollama by default, and optional DeepSeek fallback scoped by task/data permissions and spending cap. Validate both providers' outputs locally and evaluate factual/semantic behavior separately from JSON syntax. Use bounded context/repair, measured model selection, and explicit usage accounting.

**Alternatives:** local-only removes fallback but can leave tasks unavailable; cloud-first increases data disclosure and creates a paid runtime dependency; unrestricted automatic provider routing conceals spending and disclosure decisions. A large context or model is not assumed to be necessary or feasible.

**Consequences:** baseline tasks can operate without cloud credentials if a local model passes. Cloud fallback can improve availability within explicit limits, but cannot bypass factual or execution constraints. Different provider capabilities require separate adapters. Model/prompt changes need targeted evaluation.

**Revisit when:** another local runtime/model measurably improves quality/resources, a provider changes required capabilities, or a different cloud provider is explicitly requested. See [LLM and evaluation](llm-and-evaluation.md) and [privacy](security-and-privacy.md).

## 📍 ADR-04: Rules-based execution with exact approvals and reconciliation

**Status:** adopted design. **Requirements:** R04, R09–R12, R14.

**Context:** the user chose automatic action within configured rules, with Telegram approval for exceptions. Applications/messages are externally visible effects whose outcome may be uncertain after a timeout or crash.

**Decision:** separate research/preparation from a deterministic executor. Begin in draft-only mode, deliver reviewed execution, and enable rules-based autonomy after capability gates and configured limits. Bind approval to exact action revision/content/artifacts. Persist intent and reserve limits before dispatch. Classify uncertain effects as unknown, then reconcile before any retry.

**Alternatives:** approve every action forever would not provide the selected autonomy; unrestricted model-directed execution would not enforce the selected rules; retrying every error risks duplicate effects; claiming exactly-once delivery is unsupported by website interactions and local storage alone.

**Consequences:** consistent policy across all outbound kinds, inspectable authorization, and conservative recovery. Some actions wait for facts, login, or reconciliation. Approval cannot override hard missing facts, unsupported flows, pause, or exhausted quotas. A single pending unknown effect may constrain capacity until resolved.

**Revisit when:** a channel offers proven idempotent submission/receipt semantics, or the user explicitly changes autonomy preferences. Any relaxation must retain truthful facts, visible outcomes, and target-specific duplicate control. See [workflows and autonomy](workflows-and-autonomy.md) and [validation](validation.md).

## ADR-05: Measured M1 runtime and bounded recovery refinements

**Status:** adopted implementation refinement, 2026-10-01. **Requirements:** R06, R11, R14.

**Evidence:** [M0/M1 measurements](../feasibility/M0-M1-2026-10-01.md), locked toolchain,
public-source probes, and persistence/recovery checks.

**Decision:** support Python 3.14 on the initial Linux deployment, validated with 3.14.7,
using the selected uv/pytest/Ruff/mypy toolchain and direct Pydantic/HTTPX/Playwright adapters.
Use metadata-only Greenhouse listing reads and separately bounded detail/question reads;
board-wide descriptions exceeded the retained 2 MB response bound on the sample board.
Keep incomplete listing coverage explicit. Treat installed-system Chromium smoke evidence
as a prerequisite check, with the production Playwright/browser/session baseline still gated.

Restore into a fresh private data directory rather than overwriting an existing installation.
Verify the snapshot before publication; use an incomplete-restore marker and retain original
state. Preserve paused draft-only startup and the required external-history audit. A one-shot
collection creates completed work and must not replace or silently install a recurring schedule.

**Consequences:** no new services, credentials, or provider costs. Descriptions and complete
source monitoring require later detail/pagination work. Restore requires selecting a fresh
data path; old state remains available for inspection. Python minor-version expansion must
be validated and relocked. ADR-01–ADR-04 and all external-effect guarantees remain intact.
