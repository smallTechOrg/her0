# Running #hero on Cloud Run (files on GCS, database on Cloud SQL)

Everything the pipeline produces - Street View frames, satellite crops, stitched panoramas, the frozen ground and
wall photos, the exported street models - is written under `HERO_DATA_DIR`. On Cloud Run that directory is a
**GCS bucket mounted as a volume** (`/data`), so images are fetched once and kept; the admin, the runs (fetch /
fusion / regenerate) and the game all run in the cloud, nothing depends on your laptop. The SQLite file cannot live
on a bucket (no file locks), so the database is **Cloud SQL for PostgreSQL**; the code is database-agnostic
(SQLAlchemy + Alembic) and the container applies migrations at start.

## One-time setup

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com
gcloud storage buckets create gs://MY-HERO-DATA --location=asia-south1
gcloud sql instances create hero --database-version=POSTGRES_16 --tier=db-g1-small --region=asia-south1
gcloud sql databases create hero --instance=hero
gcloud sql users create hero --instance=hero --password='CHOOSE-A-PASSWORD'
```

Secrets (the app reads them as env vars; Cloud Run injects them from Secret Manager):

```bash
printf %s "$ANTHROPIC_KEY"  | gcloud secrets create hero-anthropic-key --data-file=-
printf %s "$MAPS_KEY"       | gcloud secrets create hero-google-maps-key --data-file=-
printf %s "$ADMIN_PASSWORD" | gcloud secrets create hero-admin-password --data-file=-
printf %s 'postgresql+psycopg://hero:CHOOSE-A-PASSWORD@/hero?host=/cloudsql/PROJECT:asia-south1:hero' | gcloud secrets create hero-database-url --data-file=-
```

Grant the Cloud Run service account `roles/secretmanager.secretAccessor`, `roles/cloudsql.client` and
`roles/storage.objectAdmin` on the bucket.

## Deploy

```bash
PROJECT=my-project REGION=asia-south1 BUCKET=MY-HERO-DATA SQL_INSTANCE=my-project:asia-south1:hero ./deploy/cloudrun.sh
```

or push to the deploy branch with the GitHub workflow (`.github/workflows/deploy-cloudrun.yml`; repository secrets
`GCP_PROJECT`, `GCP_REGION`, `GCP_BUCKET`, `GCP_SQL_INSTANCE` and the auth secrets it names).

The service runs one always-on instance (`--min-instances 1 --max-instances 1 --no-cpu-throttling`): the fusion
runs are in-process background tasks and must not be interrupted; the request timeout is one hour for the long
admin calls. The game itself is static files plus small JSON, served by the same container.

## Moving your local data up

```bash
gcloud storage rsync -r data gs://MY-HERO-DATA --exclude 'hero.*\.db.*'
```

then re-run `Regenerate` for each street once (the exports and panoramas are content-addressed, so already-baked
files are reused). The SQLite rows are not copied: run the legacy import or re-fetch on the cloud (the Google
imagery cache in the bucket makes re-fetching free).
