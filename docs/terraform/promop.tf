# ==========================================================================
# PRomop on Google Cloud — example Terraform
#
# A self-contained, single-file example that stands up one PRomop environment
# in its own GCP project:
#
#   Cloud Run service  <name>            web (gunicorn, serves API + UI)
#   Cloud Run job      <name>-migrate    release step: scripts/prepare-deployment.sh
#   Cloud Run pool     <name>-worker     Celery worker (start-worker.sh)
#   Cloud SQL          <name>-db         PostgreSQL 18 (pg_trgm + pgvector)
#   Memorystore        <name>-redis      Celery broker + result backend
#   Secret Manager                       generated keys + DATABASE_URL
#   Artifact Registry  promop            Docker images
#
# where <name> is "promop-<environment>". Read README.md in this directory before
# applying: the first apply needs an image in the registry.
#
# This is an example to copy and adapt, not a module consumed in place.
# ==========================================================================

terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Keep state in a GCS bucket you create once by hand (backend blocks cannot
  # read variables). Without this block state is a local terraform.tfstate,
  # which holds the generated secrets — keep it out of Git either way.
  #
  # backend "gcs" {
  #   bucket = "<your-tf-state-bucket>"
  #   prefix = "promop/dev"
  # }
}

# ---------- Inputs ----------

variable "project_id" {
  type        = string
  description = "GCP project to deploy into"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "environment" {
  type        = string
  description = "Suffix for every resource name, e.g. dev, staging, prod"
  default     = "dev"
}

variable "athena_vocabulary_gdrive_url" {
  type        = string
  description = "Google Drive folder holding the Athena vocabulary ZIP, shared 'anyone with the link'. The release job refuses to run without it."
}

variable "admin_email" {
  type        = string
  description = "Login for the Django admin user that setup_admin creates on every deploy"
}

variable "custom_domains" {
  type        = list(string)
  description = "Hostnames already pointed at the service (domain mapping or load balancer, set up separately); only added to Django's host settings. The first one becomes APP_BASE_URL."
  default     = []
}

variable "extra_cors_origins" {
  type        = list(string)
  description = "Additional browser origins allowed to call the API (e.g. a federation host app)"
  default     = []
}

variable "extra_env" {
  type        = map(string)
  description = "Extra non-secret environment for service, job and worker, e.g. MAILGUN_SENDER_DOMAIN, SENTRY_ENVIRONMENT, PHR_AUDIENCE"
  default     = {}
}

variable "secret_env" {
  type        = map(string)
  description = <<-EOT
    Extra secret environment as ENV_NAME => Secret Manager secret id, for
    secrets you create yourself (ANTHROPIC_API_KEY, LOINC_USER, LOINC_PASSWORD,
    UMLS_API_KEY, MAILGUN_API_KEY, SENTRY_DSN, SERVICE_AUTH_TOKEN, ...). Each
    secret must exist and have a version before apply.
  EOT
  default     = {}
}

variable "allow_unauthenticated" {
  type        = bool
  description = "Grant allUsers run.invoker. PRomop does its own authentication; turn off only behind IAP or a load balancer."
  default     = true
}

variable "db_tier" {
  type    = string
  default = "db-custom-2-7680"
}

variable "db_disk_size_gb" {
  type        = number
  description = "Initial disk. A full Athena load plus concept embeddings needs tens of GB; autoresize grows it after."
  default     = 100
}

variable "db_deletion_protection" {
  type    = bool
  default = true
}

variable "max_instances" {
  type    = number
  default = 2
}

data "google_project" "this" {
  project_id = var.project_id
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  name = "promop-${var.environment}"

  # Cloud Run's deterministic URL, known before the service exists, so the
  # service can be told its own hostname without a dependency cycle.
  run_host     = "${local.name}-${data.google_project.this.number}.${var.region}.run.app"
  hosts        = concat([local.run_host], var.custom_domains)
  app_base_url = "https://${length(var.custom_domains) > 0 ? var.custom_domains[0] : local.run_host}"

  image = "${google_artifact_registry_repository.promop.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.promop.repository_id}/promop:latest"

  redis_url = "redis://${google_redis_instance.promop.host}:${google_redis_instance.promop.port}/0"

  # Service, release job and worker run one settings.py, so they read one map.
  # ENVIRONMENT marks every process as deployed (IS_DEPLOYED), so deploy-only
  # checks are errors; not var.environment, because "dev" counts as local.
  env = merge({
    DJANGO_SETTINGS_MODULE       = "promop.settings"
    ENVIRONMENT                  = "deployed"
    DEBUG                        = "False"
    PYTHONUNBUFFERED             = "1"
    ALLOWED_HOSTS                = join(",", local.hosts)
    CORS_ALLOWED_ORIGINS         = join(",", concat([for h in local.hosts : "https://${h}"], var.extra_cors_origins))
    APP_BASE_URL                 = local.app_base_url
    ADMIN_EMAIL                  = var.admin_email
    ATHENA_VOCABULARY_GDRIVE_URL = var.athena_vocabulary_gdrive_url
    CELERY_BROKER_URL            = local.redis_url
  }, var.extra_env)

  # SECRET_KEY, AUDIT_HMAC_KEY and EXPORT_SIGNING_KEY must be three distinct
  # values; `check --deploy` (patient_portal.E001-E003) refuses to deploy
  # otherwise.
  generated_secrets = toset(["SECRET_KEY", "AUDIT_HMAC_KEY", "EXPORT_SIGNING_KEY", "ADMIN_PASSWORD"])

  secret_env = merge(
    { for env in local.generated_secrets : env => google_secret_manager_secret.generated[env].secret_id },
    { DATABASE_URL = google_secret_manager_secret.database_url.secret_id },
    var.secret_env,
  )
}

# ---------- APIs ----------
resource "google_project_service" "apis" {
  for_each = toset([
    "artifactregistry.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "redis.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "sqladmin.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# ---------- Network ----------
# Only Memorystore needs it: Redis has no public address. Cloud SQL is reached
# over the /cloudsql socket, which needs no network.
resource "google_compute_network" "vpc" {
  name                    = local.name
  auto_create_subnetworks = false

  depends_on = [google_project_service.apis]
}

resource "google_compute_subnetwork" "vpc" {
  name                     = "${local.name}-${var.region}"
  network                  = google_compute_network.vpc.id
  region                   = var.region
  ip_cidr_range            = "10.8.0.0/24"
  private_ip_google_access = true
}

# ---------- Artifact Registry ----------
resource "google_artifact_registry_repository" "promop" {
  location      = var.region
  repository_id = "promop"
  format        = "DOCKER"

  depends_on = [google_project_service.apis]
}

# ---------- Service accounts ----------
resource "google_service_account" "run" {
  account_id   = "${local.name}-run"
  display_name = "PRomop runtime (${var.environment})"

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "run_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.run.email}"
}

# ---------- Cloud SQL ----------
resource "google_sql_database_instance" "promop" {
  name             = "${local.name}-db"
  database_version = "POSTGRES_18"
  region           = var.region

  settings {
    # Explicit: PostgreSQL 16+ otherwise defaults to Enterprise Plus, which
    # does not accept db-custom tiers.
    edition           = "ENTERPRISE"
    tier              = var.db_tier
    availability_type = "ZONAL"
    disk_size         = var.db_disk_size_gb

    # A public IP with no authorized networks: nothing can reach it directly,
    # and the Cloud SQL connector Cloud Run uses authorizes by IAM. If your
    # organization enforces constraints/sql.restrictPublicIp, switch to private
    # IP (see README.md in this directory).
    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }

    # Sized for the default tier's 7.5 GB; scale with db_tier. Concept search
    # is trigram- and index-heavy, so random_page_cost is set for SSD.
    database_flags {
      name  = "random_page_cost"
      value = "1.1"
    }
    database_flags {
      name  = "effective_cache_size"
      value = "655360" # 5 GB in 8 kB pages
    }
    database_flags {
      name  = "work_mem"
      value = "16384" # 16 MB in kB
    }
    database_flags {
      name  = "maintenance_work_mem"
      value = "524288" # 512 MB in kB
    }
    database_flags {
      name  = "effective_io_concurrency"
      value = "200"
    }
  }

  deletion_protection = var.db_deletion_protection

  lifecycle {
    # Autoresize grows the disk past disk_size, and a shrink forces replacement.
    ignore_changes = [settings[0].disk_size]
  }

  depends_on = [google_project_service.apis]
}

resource "google_sql_database" "promop" {
  name     = "promop"
  instance = google_sql_database_instance.promop.name
}

resource "random_password" "db" {
  length  = 32
  special = false # alphanumeric, so the URL needs no percent-encoding
}

resource "google_sql_user" "promop" {
  name     = "promop"
  instance = google_sql_database_instance.promop.name
  password = random_password.db.result
}

# ---------- Redis (Celery broker + result backend) ----------
# No AUTH: private IP only, reachable from the authorized network alone.
resource "google_redis_instance" "promop" {
  name           = "${local.name}-redis"
  region         = var.region
  tier           = "BASIC"
  memory_size_gb = 1
  redis_version  = "REDIS_7_0"

  # Without snapshots a maintenance restart loses every queued job and result,
  # and a polled task id Celery no longer knows reads PENDING for ever.
  persistence_config {
    persistence_mode    = "RDB"
    rdb_snapshot_period = "ONE_HOUR"
  }

  authorized_network = google_compute_network.vpc.id
  connect_mode       = "DIRECT_PEERING"

  depends_on = [google_project_service.apis]
}

# ---------- Secrets ----------
# Generated here, so a fresh environment needs no hand-made secrets. They are
# also in Terraform state, which is why state belongs in a locked-down bucket.
resource "random_password" "generated" {
  for_each = local.generated_secrets
  length   = 50
  special  = false
}

resource "google_secret_manager_secret" "generated" {
  for_each  = local.generated_secrets
  secret_id = "${local.name}-${lower(replace(each.key, "_", "-"))}"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "generated" {
  for_each    = local.generated_secrets
  secret      = google_secret_manager_secret.generated[each.key].id
  secret_data = random_password.generated[each.key].result
}

resource "google_secret_manager_secret" "database_url" {
  secret_id = "${local.name}-database-url"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

# Written in the same apply as the SQL user, so password and URL cannot drift.
resource "google_secret_manager_secret_version" "database_url" {
  secret      = google_secret_manager_secret.database_url.id
  secret_data = "postgres://promop:${random_password.db.result}@/promop?host=/cloudsql/${google_sql_database_instance.promop.connection_name}"
}

resource "google_secret_manager_secret_iam_member" "run_reads" {
  for_each  = local.secret_env
  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.run.email}"
}

# ---------- Cloud Run: web service ----------
resource "google_cloud_run_v2_service" "promop" {
  name                = local.name
  location            = var.region
  deletion_protection = false

  template {
    service_account = google_service_account.run.email

    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }

    # Only so requests can enqueue onto Redis; public egress is unchanged.
    vpc_access {
      network_interfaces {
        subnetwork = google_compute_subnetwork.vpc.id
      }
      egress = "PRIVATE_RANGES_ONLY"
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.promop.connection_name]
      }
    }

    containers {
      image = local.image

      # Explicit, so either Dockerfile works: the standalone image's own CMD
      # would migrate on every cold start, which the release job already did.
      command = ["gunicorn"]
      args = [
        "promop.wsgi:application",
        "--bind", "0.0.0.0:8080",
        "--workers", "2",
        "--threads", "2",
        "--timeout", "120",
        "--access-logfile", "-",
        "--error-logfile", "-",
      ]

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "4Gi"
        }
        startup_cpu_boost = true
      }

      dynamic "env" {
        for_each = local.env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
    }
  }

  lifecycle {
    # CI deploys immutable :<sha> tags; an apply must not drag the service
    # back to :latest and undo a rollback.
    ignore_changes = [template[0].containers[0].image]
  }

  depends_on = [
    google_secret_manager_secret_iam_member.run_reads,
    google_secret_manager_secret_version.generated,
    google_secret_manager_secret_version.database_url,
    google_project_iam_member.run_sql_client,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = var.allow_unauthenticated ? 1 : 0
  name     = google_cloud_run_v2_service.promop.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---------- Cloud Run: release job ----------
# Everything that must be true before a revision takes traffic: deploy checks,
# migrations (bootstrapping the LOINC concepts migration 0201 needs on an empty
# database), queuing the Athena vocabulary sync, and the admin user. CI runs
# it between deploying a no-traffic revision and shifting traffic.
resource "google_cloud_run_v2_job" "migrate" {
  name                = "${local.name}-migrate"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.run.email
      timeout         = "3600s"
      max_retries     = 0

      # It publishes the Athena sync onto Redis.
      vpc_access {
        network_interfaces {
          subnetwork = google_compute_subnetwork.vpc.id
        }
        egress = "PRIVATE_RANGES_ONLY"
      }

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [google_sql_database_instance.promop.connection_name]
        }
      }

      containers {
        image   = local.image
        command = ["bash"]
        args    = ["scripts/prepare-deployment.sh"]

        resources {
          limits = {
            cpu    = "2"
            memory = "4Gi"
          }
        }

        dynamic "env" {
          for_each = local.env
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = local.secret_env
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }

        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image]
  }

  depends_on = [
    google_secret_manager_secret_iam_member.run_reads,
    google_secret_manager_secret_version.generated,
    google_secret_manager_secret_version.database_url,
  ]
}

# ---------- Cloud Run: Celery worker ----------
# Consumes the default and `athena` queues: queued PatientRecord derivation,
# Code Mapping Suggest runs, the Athena vocabulary load and concept embeddings.
#
# A worker pool, not a service: a queue consumer opens no port, so a service
# revision would never go ready, and a pool keeps CPU allocated between polls.
resource "google_cloud_run_v2_worker_pool" "worker" {
  name                = "${local.name}-worker"
  location            = var.region
  deletion_protection = false

  template {
    service_account = google_service_account.run.email

    vpc_access {
      network_interfaces {
        subnetwork = google_compute_subnetwork.vpc.id
      }
      egress = "PRIVATE_RANGES_ONLY"
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.promop.connection_name]
      }
    }

    containers {
      image   = local.image
      command = ["bash"]
      args    = ["start-worker.sh"]

      # Each worker process holds a full OMOP row set, its concept cache and
      # the embedding model; too little memory is an OOM kill, after which
      # Redis redelivers the same job to the same instance.
      resources {
        limits = {
          cpu    = "2"
          memory = "4Gi"
        }
      }

      dynamic "env" {
        for_each = local.env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
    }
  }

  # A pool cannot see the queue, so it does not autoscale. Instances x
  # CELERY_WORKER_CONCURRENCY database connections count against Cloud SQL.
  scaling {
    manual_instance_count = 1
  }

  lifecycle {
    ignore_changes = [template[0].containers[0].image]
  }

  depends_on = [
    google_secret_manager_secret_iam_member.run_reads,
    google_secret_manager_secret_version.generated,
    google_secret_manager_secret_version.database_url,
  ]
}

# ---------- Outputs ----------
output "service_url" {
  value = local.app_base_url
}

output "image_repository" {
  description = "Push images here as promop:<tag>"
  value       = "${google_artifact_registry_repository.promop.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.promop.repository_id}"
}

output "service_name" {
  value = google_cloud_run_v2_service.promop.name
}

output "migrate_job_name" {
  value = google_cloud_run_v2_job.migrate.name
}

output "worker_pool_name" {
  value = google_cloud_run_v2_worker_pool.worker.name
}

output "admin_password_command" {
  value = "gcloud secrets versions access latest --secret=${google_secret_manager_secret.generated["ADMIN_PASSWORD"].secret_id} --project=${var.project_id}"
}
