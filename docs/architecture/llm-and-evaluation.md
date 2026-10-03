# Inference providers and model evaluation

_Design baseline · 2026-09-30 · Requirements R01, R07–R08, R10, R13–R14 · Decision ADR-03_

---

## 📋 Provider boundary

Use `InferenceProvider.generate(TaskRequest)` with a local Ollama adapter and an optional DeepSeek adapter. A request contains task kind, input references/content, output schema version, permitted data categories, token/context limits, deadline, and task ID. A response contains validated output, provider/model identifiers, prompt/template version, duration, usage, and validation outcome.

Task kinds are fact extraction, query generation, source normalization, relevance assessment, document tailoring, cover-letter drafting, message drafting, and user-command interpretation. Provider output cannot contain execution authority. Browser clicks, network destinations, recipients, factual answers, and permissions are checked by application code.

Ollama supports JSON-schema structured output[^1]. DeepSeek documents JSON output and token-usage fields[^2]; do not assume its JSON mode enforces the same schema as Ollama. Both adapters apply local schema and semantic validation, reject unknown fields, and treat truncated/empty output as failure.

## 🔧 Local model selection and resource policy

The inspected environment reported approximately 32 GB RAM and an installed Ollama executable. GPU availability and running-model inventory could not be verified; no local inference benchmark has run. This observation is not a guarantee that a model fits or performs well.

In M0 inventory the actual target machine, evaluate installed suitable models first, then assess one small quantized 3B–4B-class instruct model and one 7B–8B-class candidate if resources permit. Select the smallest candidate that passes the evaluation below; record exact model tag/digest, license, quantization, context setting, host RAM/VRAM, latency, and quality. Downloads belong to the explicit model setup step, not ordinary application startup.

Begin with one inference request at a time and a 4K-token working context, expanding to 8K only after resource measurement. Chunk long CVs, job descriptions, and conversations into bounded extraction tasks and assemble verified summaries; never silently truncate required answers or facts. Larger contexts consume additional memory[^3]. If no local candidate passes a task's gate, mark that task as unavailable locally rather than assume cloud access.

Warm-model target latency is 120 seconds per normal task, with a 300-second hard deadline. These are initial evaluation/operating thresholds, not measured performance. Excess memory use, timeout, or invalid output produces a visible task failure or permitted fallback; it does not change factual or action rules.

## 🔗 Cloud routing and budgets

DeepSeek remains disabled until the user supplies a key, chooses enabled task kinds and allowed data categories, and sets a positive monthly USD cap. API model names, capabilities, and pricing are version-sensitive; verify current official documentation during setup and record dated rates rather than embedding permanent price claims[^2].

Fallback may occur after a local task deadline or resource failure, or after the single bounded repair attempt fails schema/semantic validation. A route can also be configured for a task that failed the local evaluation gate. Do not escalate from an LLM's self-reported uncertainty alone.

Default cloud-eligible tasks, once enabled, use public job/company content and search preferences. Candidate facts or tailored-document text require a separate allowed-data setting. Raw inbox/thread bodies remain local unless the user explicitly allows that category. Credentials, cookies, storage state, tokens, and browser diagnostics are always excluded. Rules live in [privacy](security-and-privacy.md).

Before a request, reserve a conservative maximum cost using input size, output cap, model rates, and concurrent reservations. Reject the request if it would exceed the remaining monthly cap. Settle against returned usage; on a transport timeout keep the reservation as uncertain usage until reconciled or conservatively charged. Include retries in the budget. Without known rates, usage limits, or a permitted disclosure category, cloud routing is blocked.

## 📚 Validation, prompts, and bounded reasoning

Prompts identify external pages, posts, and messages as untrusted source content. Separate them from instructions; require source/fact IDs on assessments and generated claims. A returned command cannot expand tool access, rewrite policy, choose an unverified recipient, or modify confirmed candidate facts.

Validate output shape, enum values, lengths, source references, unsupported claims, and required facts. Allow at most one repair request for malformed output. A repair uses the same provider/disclosure/budget rules; repeated failure is reported. Structured syntax is not proof of truth.

Research loops have explicit query/page/time budgets in [discovery](discovery-and-matching.md). The orchestrator, not the model, tracks exhaustion. Cache successful public-content extraction by source content hash, task/schema version, model, and prompt version. Include candidate/preference revisions for personal tasks; invalidate caches when inputs change and apply the same deletion policy to cached personal content.

## 🎯 Evaluation and acceptance gates

Use a versioned initial evaluation set of 30 synthetic or redacted fixtures: 10 extraction, 10 matching/relevance, and 10 drafting. Include absent work authorization, conflicting dates, unsupported achievements, adversarial page instructions, and long inputs. Expected facts/eligibility labels are manually reviewed before evaluating providers.

| Gate | Initial threshold |
| --- | --- |
| Output structure | Every accepted output passes validation; at least 95% of requests succeed within one repair |
| Critical facts and eligibility | No invented critical fact, false eligibility approval, or hidden required unknown in the fixture set |
| Draft grounding | Every accepted factual claim references supporting confirmed facts; zero unsupported claims in accepted fixtures |
| Matching usefulness | At least 8 of 10 fixture judgments agree with reviewed labels; hard filters remain deterministic |
| Draft usefulness | At least 8 of 10 drafts judged usable without substantive factual/content correction |
| Resources | No out-of-memory event; measured latency and memory meet configured limits |

Fixture passes establish limited baseline evidence, not a universal guarantee. Repeat affected evaluations when a model, quantization, prompt, schema, or provider changes. Live feedback can expand the dataset; report disagreement rather than quietly weakening thresholds.

V08 covers extraction/ranking/drafting, V16 covers invalid responses/fallback, and V17 covers cloud disclosure/budgets. M4 requires an evaluated provider for each enabled task. All outputs still pass the application policy before any effect.

## 🔗 References

[^1]: Ollama documentation. "Structured outputs." Checked 2026-09-30. https://docs.ollama.com/capabilities/structured-outputs
[^2]: DeepSeek documentation. "JSON output," "Create chat completion," and "Your first API call." Checked 2026-09-30. https://api-docs.deepseek.com/guides/json_mode and https://api-docs.deepseek.com/api/create-chat-completion and https://api-docs.deepseek.com/
[^3]: Ollama documentation. "Context length." Checked 2026-09-30. https://docs.ollama.com/context-length
