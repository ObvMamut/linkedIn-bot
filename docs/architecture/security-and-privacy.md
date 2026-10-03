# Security and personal-data boundaries

_Design baseline · 2026-09-30 · Requirements R01, R08–R14 · Decisions ADR-02–ADR-04_

---

## 📋 Data inventory and trust boundaries

The application processes candidate identity/contact details, career history, preferences, private LinkedIn conversations, browser sessions, generated CVs/letters, recipient information, and action history. Local disk is the default persistence boundary. Telegram receives selected user-interface content; DeepSeek receives only categories explicitly enabled by the user.

| Destination | Allowed baseline data | Excluded data |
| --- | --- | --- |
| Local SQLite/artifacts | Profile, observations, assessments, actions, generated materials | Plaintext LinkedIn password |
| Dedicated browser profile | LinkedIn session required for user-authorized workflows | Everyday browser history/profile reuse |
| Telegram owner chat | Digests, relevant excerpts, previews, decisions, recovery notices | Credentials, OTPs, cookies, raw diagnostic traces |
| Local Ollama | Task-specific content required for enabled local tasks | Credentials and execution authority |
| Optional DeepSeek | Configured task/data categories after routing checks | Secrets and browser diagnostics; unapproved personal/inbox content |
| Operational logs | IDs, timing, counts, states, reason codes | Raw CVs, message bodies, tokens, session state |

Remote Telegram delivery is a deliberate disclosure even when inference remains local. Do not describe the whole application as offline or all-local: discovery, LinkedIn, and Telegram require network access. Privacy controls do not establish legal compliance certification.

## 🔧 Secrets and local storage

Keep Telegram and optional DeepSeek keys in environment variables or a private credentials file outside source control. Use a private data/config directory with owner-only permissions on the target OS. Browser profiles and session state are secrets; Playwright notes that authentication state can enable account impersonation[^1]. Never attach them to bug reports, prompts, backups shared with others, or Telegram messages.

Bind local inference and any future diagnostic listener to loopback. The initial app needs no inbound server. Validate configured provider endpoints and keep local-provider access distinct from public URL retrieval. An endpoint change cannot silently route local candidate data to a remote host.

Use parameterized SQL, explicit transaction boundaries, file names based on generated IDs, and path containment checks. Uploaded document names are display metadata, not filesystem destinations. Accept only the supported file types and size bounds; parse as data. Reject arbitrary script execution, shell commands, or executable attachments proposed by retrieved content or model output.

The baseline uses filesystem permissions and recommends the user's existing full-disk protection; it does not add a custom database encryption scheme or promise protection against a compromised OS account. Define that limitation in installation guidance.

## 🔗 External content and cloud disclosure

Pages, job descriptions, messages, and attachments are untrusted. A instruction embedded in a vacancy cannot override prompts, policy, quotas, recipients, or confirmed facts. Maintain provenance and validate references before generated claims or actions are accepted. A natural-language Telegram command can propose a setting change; confirmation and authorization happen in application code.

Public-source retrieval allows only HTTP(S), applies time/size/redirect limits, rejects local/private/link-local destinations, and rechecks redirects/resolution to prevent external content from probing the local machine. The configured loopback Ollama endpoint is accessed through the provider adapter, not the public fetcher. Do not forward LinkedIn cookies or authorization headers to external application domains.

Generate CV PDFs from escaped content and controlled local templates, with external resource loading disabled. Browser form adapters navigate only verified career/application destinations for the action; unexpected redirects pause for review.

Cloud disclosure settings are separate for public source text, search preferences, candidate facts/documents, and inbox/thread content. Default cloud configuration is disabled, and raw inbox content is excluded until explicitly permitted. Redact unnecessary identity/contact information even for approved categories. Apply the same checks to fallback and repair requests; no adapter-level shortcut may skip them.

## 📍 Retention, diagnostics, and deletion

Default retention is an initial operating policy, editable by the user:

| Data class | Default retention |
| --- | --- |
| Confirmed profile and active preferences | Until corrected/deleted by user |
| Active application artifacts and audit history | Until user deletes the application/history |
| Raw public observations and unlinked message bodies | 90 days after last use |
| Expired/cancelled drafts and temporary files | 30 days; unfinished temporary files cleaned on recovery |
| Redacted operational logs | 14 days |
| Opt-in browser diagnostics | 7 days |
| Local backups | Last seven daily backups plus one pre-migration backup |

Retain necessary evidence linked to an unresolved effect or active application until resolved/deleted, even if it exceeds ordinary observation retention. Browser session/profile data remains until logout/disconnect. Retention runs locally and is paused during backup/migration.

Disable diagnostic screenshots/traces by default. When explicitly enabled for a failure, store them privately, redact where practical, apply the short retention, and exclude them from routine backups. Diagnostic artifacts can contain private account content even when filenames are sanitized.

Provide explicit local export, disconnect, and delete commands in a later implementation step. Deleting a record removes its linked artifacts, personal caches, and observations where no other retained record requires them. Explain that backups and previously sent Telegram/employer/cloud content have separate lifecycles; local deletion cannot retract those disclosures. Removing deduplication history can permit future duplicates, so pause execution during deletion and warn about that concrete consequence before confirmation.

## 🎯 Acceptance and validation

V15 covers unauthorized Telegram access; V17 covers cloud category checks and budget failures; V18 covers secrets, adversarial content, URL/path boundaries, and renderer isolation; V19 covers private backup/recovery; V20 covers retention and linked deletion. No production personal data belongs in fixtures or repository documentation.

## 🔗 References

[^1]: Playwright Python documentation. "Authentication." Checked 2026-09-30. https://playwright.dev/python/docs/auth
