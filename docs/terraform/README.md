# Using Terraform to Set Up PRomop

[`promop.tf`](promop.tf) creates a complete PRomop environment in a Google Cloud
project: Cloud Run (web, release job, Celery worker), Cloud SQL Postgres, Redis,
generated secrets, and an Artifact Registry repository for your images.

You need: a GCP project with billing, `terraform`, `gcloud`, `docker`, `git-lfs`,
a clone of PRomop, and a Google Drive folder with the Athena vocabularies shared
as *anyone with the link* (see [vocabularies.md](../vocabularies.md)).

## 1. Configure

Copy `promop.tf` into an empty directory, e.g. `~/promop-infra`, and create
`terraform.tfvars` next to it:

```hcl
project_id                   = "your-gcp-project"
admin_email                  = "you@example.org"
athena_vocabulary_gdrive_url = "https://drive.google.com/drive/folders/<folder-id>"
```

## 2. Apply

The services start from an image, so push one before the full apply:

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project your-gcp-project

cd ~/promop-infra
terraform init
terraform apply -target=google_project_service.apis -target=google_artifact_registry_repository.promop

cd ~/promop   # your PRomop clone
git lfs pull
REPO=us-central1-docker.pkg.dev/your-gcp-project/promop
gcloud auth configure-docker us-central1-docker.pkg.dev
docker build --platform linux/amd64 -t $REPO/promop:latest .
docker push $REPO/promop:latest

cd ~/promop-infra
terraform apply
```

## 3. Initialize the database

```bash
gcloud run jobs execute promop-dev-migrate --region us-central1 --wait
```

From `~/promop-infra`, open the `service_url` output and sign in as `admin_email`
with the password from `$(terraform output -raw admin_password_command)`. The
full vocabulary load continues in the background on the worker.

## 4. Deploy a new version

Run this from your own CI, or by hand, whenever you pull a new PRomop version:

```bash
TAG=$(git rev-parse --short HEAD)
docker build --platform linux/amd64 -t $REPO/promop:$TAG .
docker push $REPO/promop:$TAG

gcloud run services update promop-dev --image $REPO/promop:$TAG --region us-central1 --no-traffic
gcloud run jobs update promop-dev-migrate --image $REPO/promop:$TAG --region us-central1
gcloud run jobs execute promop-dev-migrate --region us-central1 --wait
gcloud run worker-pools update promop-dev-worker --image $REPO/promop:$TAG --region us-central1
gcloud run services update-traffic promop-dev --to-latest --region us-central1
```

The new revision takes traffic only after the release job (migrations) succeeds.
Terraform ignores image changes, so a later `terraform apply` will not roll back
a deploy.

Optional settings (custom domain, API keys such as `ANTHROPIC_API_KEY`) are the
variables at the top of `promop.tf`.

Uploaded patient documents are stored on the container's local disk, which
Cloud Run does not persist: they are lost when a revision is replaced.
