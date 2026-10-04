# Firebase and GCP cloud pilot

**Portal:** https://inv-studio-740495548022.web.app

**Release boundary:** this URL is the previously verified hosted baseline. The current source adds manual draft export, large-source lookup, visible extraction stages, the readable-PDF fast path, Claude setup-token support and ChatGPT credential transfer. Those additions must not be described as deployed until the final release suite, image build, deployment and authenticated smoke checks pass.

The hosted pilot uses a separate Firebase Hosting site for static frontend files and same-origin API rewrites to Cloud Run. Cloud Run verifies a Firebase ID token, revocation and an explicit verified-email allowlist before every protected API request. The public configuration endpoint reveals only the Firebase web configuration needed to sign in.

Cloud SQL PostgreSQL stores jobs, references, edits, encrypted provider connections, allocations and exported workbooks. Private Cloud Storage holds uploaded evidence and supplier templates. Secret Manager supplies the database connection and encryption key. The runtime service account uses platform credentials; the deployment service-account JSON is never baked into the image or served to a browser.

Manual `DRAFT_UNVALIDATED` downloads are generated from the authenticated request body and recorded as an audit event. They do not update the job, approve a reference, write an approved export row or reserve receipt quantity. The reference lookup tables are a separate read-only evidence index: the current source profile contains 114,940 item rows and 297,199 PO rows. Lookup confirmation does not promote a row into canonical matching data.

The Claude subscription path stores an owner-provided setup token in the encrypted credential store and invokes the bundled CLI with tools disabled and a restricted environment. The ChatGPT path starts the application's own OAuth registration locally, exports one size-checked credential bundle, then deletes the local encrypted token and clears the local active account. The authenticated hosted owner imports and verifies that bundle. This is a move, not a copy, so protect and delete the transfer file. Neither path has completed live subscription inference acceptance in this release record.

## Deployment controls

- Use a dedicated application service account with Cloud SQL Client and Firebase Authentication Viewer roles; grant storage access only to the application's private bucket and secret access only to its runtime secrets.
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

Region must be selected before real business-data upload under the owner's residency requirements. The hosted verification used synthetic documents in us-central1. Real source extracts remain private local evidence until their mapping and business meanings are approved.

## Pending release validation

Current local gate: **101 backend tests pass, including the real local PostgreSQL contract; Ruff is clean; all three container reader warm checks recovered both synthetic lines.** `PENDING RELEASE VALIDATION — published image, browser, private catalog import, authenticated hosted-extension smoke, deployed revision and date.` Record those results in `docs/validation.md`, then update this section to name the deployed revision. Until then, the existing hosted baseline and the current source candidate must remain distinct in stakeholder communication.

## Primary documentation

- [Firebase Hosting with Cloud Run](https://firebase.google.com/docs/hosting/cloud-run): Hosting requests have a 60-second limit, which is why extraction is a queued job followed by polling.
- [Firebase token verification](https://firebase.google.com/docs/auth/admin/verify-id-tokens) and [session revocation](https://firebase.google.com/docs/auth/admin/manage-sessions).
- [Cloud Run to Cloud SQL](https://docs.cloud.google.com/sql/docs/postgres/connect-run).
- [Cloud SQL Auth Proxy](https://docs.cloud.google.com/sql/docs/postgres/sql-proxy).
