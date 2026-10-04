# Firebase and GCP cloud pilot

The hosted pilot uses a separate Firebase Hosting site for static frontend files and same-origin API rewrites to Cloud Run. Cloud Run verifies a Firebase ID token, revocation and an explicit verified-email allowlist before every protected API request. The public configuration endpoint reveals only the Firebase web configuration needed to sign in.

Cloud SQL PostgreSQL stores jobs, references, edits, encrypted provider connections, allocations and exported workbooks. Private Cloud Storage holds uploaded evidence and supplier templates. Secret Manager supplies the database connection and encryption key. The runtime service account uses platform credentials; the deployment service-account JSON is never baked into the image or served to a browser.

## Deployment controls

- Use a dedicated application service account with Cloud SQL Client and Firebase Authentication Viewer roles; grant storage access only to the application's private bucket and secret access only to its runtime secrets.
- Enable uniform bucket access and public-access prevention. Do not publish real reference extracts or invoice files as Hosting assets.
- Provision a PostgreSQL database/user and the two Secret Manager values before deployment. Use Cloud SQL's managed connector/socket; do not authorize the public internet as a database network.
- Build with `docker build -t IMAGE .`. The image installs all three local readers and warms their public model files using synthetic examples. It runs as an unprivileged user. Build failure stops deployment.
- Set the environment values in `deploy/cloud.env.example.yaml` in a private file. Empty allowed-email configuration denies all workspace access. The current project uses email/password sign-in; the UI shows Google sign-in only when that provider is explicitly configured.
- Push the tested image to Artifact Registry, set the variables documented in `deploy/deploy.sh`, and run that script with an authenticated deployment identity. It targets only the specified Hosting site and named Cloud Run service.
- Run access-denial, sign-in, source-upload, extraction, review/export and instance-replacement persistence checks before handoff. Delete temporary test identities when done.

## Pilot capacity and recovery

This release deliberately uses one Cloud Run instance, two reader workers and a bounded in-process queue. CPU remains allocated while the instance runs so asynchronous readers can finish; minimum instances is zero. The database and documents are durable, but the queue and processing-plan tokens are not. A terminated job is marked interrupted on startup and requires a new confirmed retry. Do not describe this as a distributed durable queue.

Drain queued/processing work before a deployment or rollback. Cloud Run revisions can overlap during rollout; the current single-instance application does not offer cross-revision job leasing. Database advisory transaction locking protects export allocation, but it is not a distributed job scheduler. Add a durable queue, leases, idempotent worker completion and schema migrations before horizontal scaling.

Cloud SQL uses automated backups. Test restore into a separate database and recover the corresponding encryption-key version before production approval. Keep source evidence and export allocation history together. Rollback means selecting the previous tested image/revision; it does not mean deleting the database or bucket.

## Costs and region

The pilot's infrastructure includes an always-provisioned small Cloud SQL database plus usage-based Cloud Run, storage and network traffic. This is not a zero-cost deployment. Reader latency, memory usage, cold starts and AI fallback share determine the practical cost. Minimum instances is zero and maximum instances is one; set project billing alerts and review the measured workload before increasing either.

Region must be selected before real business-data upload under the owner's residency requirements. The initial hosted verification uses synthetic documents. Real source extracts remain private local evidence until their mapping and business meanings are approved.

## Primary documentation

- [Firebase Hosting with Cloud Run](https://firebase.google.com/docs/hosting/cloud-run): Hosting requests have a 60-second limit, which is why extraction is a queued job followed by polling.
- [Firebase token verification](https://firebase.google.com/docs/auth/admin/verify-id-tokens) and [session revocation](https://firebase.google.com/docs/auth/admin/manage-sessions).
- [Cloud Run to Cloud SQL](https://docs.cloud.google.com/sql/docs/postgres/connect-run).
- [Cloud SQL Auth Proxy](https://docs.cloud.google.com/sql/docs/postgres/sql-proxy).
