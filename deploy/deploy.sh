#!/usr/bin/env bash
# Deploy a tested image to a separately provisioned Invoice Studio environment.
# Never put a service-account JSON or provider key in this repository.
set -euo pipefail
: "${GCP_PROJECT:?Set the target project}"
: "${GCP_REGION:=us-central1}"
: "${INV_STUDIO_IMAGE:?Set a tested Artifact Registry image digest or immutable tag}"
: "${INV_STUDIO_RUNTIME_SA:?Set the dedicated runtime service account email}"
: "${INV_STUDIO_CLOUDSQL:?Set project:region:instance}"
: "${INV_STUDIO_ENV_FILE:?Set the path to a private YAML environment file}"
: "${INV_STUDIO_SITE:?Set the separate Firebase Hosting site ID}"
: "${INV_STUDIO_DB_SECRET:=inv-studio-database-url}"
: "${INV_STUDIO_VAULT_SECRET:=inv-studio-vault-key}"
: "${INV_STUDIO_CPU:=2}"
cd "$(dirname "$0")/.."

gcloud run deploy inv-studio-api --project="$GCP_PROJECT" --region="$GCP_REGION" \
  --image="$INV_STUDIO_IMAGE" --service-account="$INV_STUDIO_RUNTIME_SA" \
  --set-cloudsql-instances="$INV_STUDIO_CLOUDSQL" \
  --set-secrets="INV_STUDIO_DATABASE_URL=$INV_STUDIO_DB_SECRET:latest,INV_STUDIO_VAULT_KEY=$INV_STUDIO_VAULT_SECRET:latest" \
  --env-vars-file="$INV_STUDIO_ENV_FILE" --port=8080 --cpu="$INV_STUDIO_CPU" --memory=8Gi \
  --max-instances=1 --min-instances=0 --concurrency=20 --timeout=300 \
  --no-cpu-throttling --execution-environment=gen2 --allow-unauthenticated --quiet

# Cloud Run is publicly invokable for the Hosting rewrite; the application verifies
# Firebase ID tokens and the explicit owner allowlist on every protected API route.
mkdir -p .data/hosting/static
cp app/static/index.html .data/hosting/index.html
cp app/static/app.js app/static/style.css app/static/firebase-auth.bundle.js .data/hosting/static/
mkdir -p .data/hosting/review
cp -R docs/review-artifact/. .data/hosting/review/
python3 - <<'PY'
import json,os,pathlib
root=pathlib.Path.cwd()
config={'hosting':{'site':os.environ['INV_STUDIO_SITE'],'public':str(root/'.data/hosting'),
 'ignore':['firebase.json','**/.*','**/node_modules/**'],
 'headers':[{'source':'**','headers':[{'key':'X-Content-Type-Options','value':'nosniff'},{'key':'Referrer-Policy','value':'no-referrer'},{'key':'Cache-Control','value':'no-store'},{'key':'Content-Security-Policy','value':"default-src 'self'; script-src 'self' https://apis.google.com; style-src 'self'; img-src 'self' data: blob:; frame-src 'self' blob: https://*.firebaseapp.com; connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com https://www.googleapis.com; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"}]}],
 'rewrites':[{'source':path,'run':{'serviceId':'inv-studio-api','region':os.getenv('GCP_REGION','us-central1')}} for path in ['/api/**','/health','/auth/**']]+[{'source':'**','destination':'/index.html'}]}}
(root/'.data/firebase.deploy.json').write_text(json.dumps(config,indent=2))
PY
firebase deploy --project="$GCP_PROJECT" --config=.data/firebase.deploy.json --only hosting --non-interactive
