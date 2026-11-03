#!/usr/bin/env bash
# Deploy #hero to Cloud Run with a GCS bucket for the files and Cloud SQL (Postgres) for the database.
#   PROJECT=my-gcp-project REGION=asia-south1 BUCKET=my-hero-data SQL_INSTANCE=my-project:asia-south1:hero ./deploy/cloudrun.sh
# Secrets (create once in Secret Manager): hero-anthropic-key, hero-google-maps-key, hero-admin-password, hero-database-url
#   gcloud secrets create hero-anthropic-key --data-file=- <<< "$KEY"      (etc.)
# The database URL secret looks like  postgresql+psycopg://hero:PASSWORD@/hero?host=/cloudsql/PROJECT:REGION:INSTANCE
set -euo pipefail
: "${PROJECT:?set PROJECT}" "${REGION:=asia-south1}" "${BUCKET:?set BUCKET (GCS bucket for /data)}" "${SQL_INSTANCE:?set SQL_INSTANCE (project:region:instance)}"
SERVICE="${SERVICE:-hero}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/hero/hero:$(git rev-parse --short HEAD)"

gcloud --project "$PROJECT" artifacts repositories describe hero --location "$REGION" >/dev/null 2>&1 \
  || gcloud --project "$PROJECT" artifacts repositories create hero --repository-format docker --location "$REGION"
gcloud --project "$PROJECT" builds submit --tag "$IMAGE" .

gcloud --project "$PROJECT" run deploy "$SERVICE" --region "$REGION" --image "$IMAGE" \
  --allow-unauthenticated --execution-environment gen2 --cpu 2 --memory 4Gi --timeout 3600 \
  --min-instances 1 --max-instances 1 --no-cpu-throttling --concurrency 40 \
  --add-cloudsql-instances "$SQL_INSTANCE" \
  --add-volume "name=data,type=cloud-storage,bucket=${BUCKET}" --add-volume-mount "volume=data,mount-path=/data" \
  --set-env-vars "HERO_DATA_DIR=/data,HERO_CLAUDE_BUDGET_USD=${HERO_CLAUDE_BUDGET_USD:-50},HERO_LOG_LEVEL=INFO" \
  --set-secrets "HERO_ANTHROPIC_API_KEY=hero-anthropic-key:latest,HERO_GOOGLE_MAPS_API_KEY=hero-google-maps-key:latest,HERO_ADMIN_PASSWORD=hero-admin-password:latest,HERO_DATABASE_URL=hero-database-url:latest"

gcloud --project "$PROJECT" run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)'
