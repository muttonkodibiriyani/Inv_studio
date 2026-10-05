# Firebase and GCP cloud pilot

**Portal:** https://inv-studio-740495548022.web.app

**Release boundary:** access hotfix revision `00005-zdk` is deployed. A hosted browser check confirmed login, 31 retained jobs and sign-out after the only uncommitted reference import was cancelled. The current feature bundle is not yet claimed live, and the hosted lookup currently reports zero loaded rows.

The hosted pilot uses a separate Firebase Hosting site for static frontend files and same-origin API rewrites to Cloud Run. Cloud Run verifies a Firebase ID token, revocation and an explicit verified-email allowlist before every protected API request. The public configuration endpoint reveals only the Firebase web configuration needed to sign in.

Cloud SQL PostgreSQL stores jobs, references, edits, encrypted provider connections, allocations and exported workbooks. Private Cloud Storage holds uploaded evidence and supplier templates. Secret Manager supplies the database connection and encryption key. The runtime service account uses platform credentials; the deployment service-account JSON is never baked into the image or served to a browser.

Manual `DRAFT_UNVALIDATED` downloads and combined `EXTRACTION_REVIEW_ONLY` downloads are generated from explicit authenticated requests and recorded as audit events. They do not update jobs, approve references, write approved export rows or reserve receipt quantity. Approved batch export remains a separate atomic path. The private source profile contains 412,139 rows (114,940 item and 297,199 PO/GRN), but the hosted import is paused/cancelling during API recovery and the live lookup reports zero rows. Lookup availability, its recovery receipt and supplier/site/receipt/baseline meanings remain unapproved.

Managed Vertex AI uses the Cloud Run service identity through Application Default Credentials, `roles/aiplatform.user`, the configured owner project/region and Google's fixed Vertex endpoint. No user API key is stored for this path. Invoice bytes or extracted text are sent only after processing-plan confirmation; output must pass the strict invoice schema and ordinary validation/review gates. In a private 15-document, 570-line run from one layout family, all source-tested fields matched. That narrow result does not establish broad supplier accuracy. The 15 successful calls used 36,450 input tokens and 91,892 output tokens including reasoning. At Google's current global introductory price of USD 0.75/3.75 per million input/output tokens through 31 December 2026, the calculated model usage is USD 0.3719325, or about USD 24.7955 per 1,000 documents at the same token mix. This is a calculation, not a cloud bill, and excludes retries, hosting, storage and database cost.

The Claude subscription path stores an owner-provided setup token in the encrypted credential store and invokes the bundled CLI with tools disabled and a restricted environment. The ChatGPT path starts the application's own OAuth registration locally, exports one size-checked credential bundle, then deletes the local encrypted token and clears the local active account. The authenticated hosted owner imports and verifies that bundle. This is a move, not a copy, so protect and delete the transfer file. Neither path has completed live subscription inference acceptance in this release record.

## Deployment controls

- Use a dedicated application service account with Cloud SQL Client, Firebase Authentication Viewer and Vertex AI User roles; grant storage access only to the application's private bucket and secret access only to its runtime secrets.
- Enable uniform bucket access and public-access prevention. Do not publish real reference extracts or invoice files as Hosting assets.
- Provision a PostgreSQL database/user and the two Secret Manager values before deployment. Use Cloud SQL's managed connector/socket; do not authorize the public internet as a database network.
- Build with `docker build -t IMAGE .`. The image installs all three local readers and warms their public model files using synthetic examples. It runs as an unprivileged user. Build failure stops deployment.
- Set the environment values in `deploy/cloud.env.example.yaml` in a private file. Empty allowed-email configuration denies all workspace access. The current project uses email/password sign-in; the UI shows Google sign-in only when that provider is explicitly configured.
- Push the tested image to Artifact Registry, set the variables documented in `deploy/deploy.sh`, and run that script with an authenticated deployment identity. It targets only the specified Hosting site and named Cloud Run service.
- Confirm the image includes the supported Claude CLI before enabling setup-token connections. A saved token without the CLI must remain visibly unavailable.
- Run access-denial, sign-in, source-upload, readable-PDF/OCR stages, lookup conflicts and confirmation, manual draft immutability, approved review/export, subscription transfer boundaries and instance-replacement persistence checks before handoff. Delete temporary test identities and one-time transfer files when done.

## Pilot capacity and recovery

This release deliberately uses one Cloud Run instance, two reader workers and a bounded in-process queue. CPU remains allocated while the instance runs so asynchronous readers can finish; minimum instances is zero. The database and documents are durable, but the queue and processing-plan tokens are not. A terminated job is marked interrupted on startup and requires a new confirmed retry. Do not describe this as a distributed durable queue.

Drain queued/processing work before a deployment or rollback. Cloud Run revisions can overlap during rollout; the current single-instance application does not offer cross-revision job leasing. Database advisory transaction locking protects approved export allocation, but it is not a distributed job scheduler. Manual drafts deliberately stay outside that allocation transaction. Add a durable queue, leases, idempotent worker completion and schema migrations before horizontal scaling.

Cloud SQL uses automated backups. Test restore into a separate database and recover the corresponding encryption-key version before production approval. Keep source evidence and export allocation history together. Rollback means selecting the previous tested image/revision; it does not mean deleting the database or bucket.

## Costs and region

The pilot's infrastructure includes an always-provisioned small Cloud SQL database plus usage-based Cloud Run, storage and network traffic. This is not a zero-cost deployment. Reader latency, memory usage, cold starts and AI fallback share determine the practical cost. Minimum instances is zero and maximum instances is one; set project billing alerts and review the measured workload before increasing either.

Region must be selected before real business-data upload under the owner's residency requirements. The hosted verification used synthetic documents in us-central1. The private catalog import is currently paused/cancelling; availability, recovery, mappings and business meanings must be confirmed before any lookup or matching claim.

## Pending release validation

Current gate: **148 repository tests pass with PostgreSQL included and no skips; Ruff and the full Chromium workflow pass. All three Docker reader warmups passed in 63 seconds with two synthetic lines each. Access hotfix `00005-zdk` passed hosted login, retained-job and sign-out checks.** `PENDING RELEASE VALIDATION — deploy and verify the complete current feature bundle; restore and receipt the private lookup import; run the final hosted extraction, review-only batch, approved export and persistence smoke.`

## Primary documentation

- [Firebase Hosting with Cloud Run](https://firebase.google.com/docs/hosting/cloud-run): Hosting requests have a 60-second limit, which is why extraction is a queued job followed by polling.
- [Firebase token verification](https://firebase.google.com/docs/auth/admin/verify-id-tokens) and [session revocation](https://firebase.google.com/docs/auth/admin/manage-sessions).
- [Cloud Run to Cloud SQL](https://docs.cloud.google.com/sql/docs/postgres/connect-run).
- [Cloud SQL Auth Proxy](https://docs.cloud.google.com/sql/docs/postgres/sql-proxy).
- [Google Agent Platform generative AI pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing): source for the time-bounded Gemini 3.7 Flash token rates used in the calculation above.
