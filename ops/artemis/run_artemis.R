#!/usr/bin/env Rscript
# ARTEMIS execution entrypoint.  It defaults to validation-only dry run so a
# container invocation cannot create cohort tables without an explicit gate.
required <- c(
  "ARTEMIS_DBMS", "ARTEMIS_DB_SERVER", "ARTEMIS_DB_USER", "ARTEMIS_DB_PASSWORD",
  "ARTEMIS_CDM_SCHEMA", "ARTEMIS_WRITE_SCHEMA", "ARTEMIS_COHORT_JSON"
)
missing <- required[!nzchar(Sys.getenv(required, unset = ""))]
if (length(missing)) stop("Missing required environment variables: ", paste(missing, collapse = ", "))

mode <- Sys.getenv("ARTEMIS_MODE", unset = "dry-run")
if (!mode %in% c("dry-run", "execute")) stop("ARTEMIS_MODE must be dry-run or execute")
cdm_schema <- Sys.getenv("ARTEMIS_CDM_SCHEMA")
write_schema <- Sys.getenv("ARTEMIS_WRITE_SCHEMA")
if (cdm_schema == write_schema) stop("CDM and write schemas must be distinct")
cohort_path <- Sys.getenv("ARTEMIS_COHORT_JSON")
if (!file.exists(cohort_path)) stop("Cohort JSON is not readable: ", cohort_path)

cohort_md5 <- unname(tools::md5sum(cohort_path))
manifest <- list(
  run_at_utc = format(Sys.time(), tz = "UTC", usetz = TRUE),
  mode = mode,
  artemis_version = as.character(utils::packageVersion("ARTEMIS")),
  artemis_ref = "242b5a24864b85a44c62d95a98cbaa2d16c55539",
  cohort_file = basename(cohort_path),
  cohort_md5 = cohort_md5,
  cdm_schema = cdm_schema,
  write_schema = write_schema
)
dir.create("/work/output", recursive = TRUE, showWarnings = FALSE)
manifest_path <- file.path("/work/output", "artemis-run-manifest.json")

write_manifest <- function(result) {
  manifest$result <- result
  json <- paste(capture.output(str(manifest, give.attr = FALSE)), collapse = "\n")
  # JSON is intentionally assembled by jsonlite only when ARTEMIS's dependency
  # graph has installed it; fail closed if it has not.
  if (!requireNamespace("jsonlite", quietly = TRUE)) stop("jsonlite is required for audit manifest output")
  writeLines(jsonlite::toJSON(manifest, auto_unbox = TRUE, pretty = TRUE), manifest_path)
}

if (mode == "dry-run") {
  message("Dry run: validated configuration and wrote manifest. ARTEMIS and the database were not invoked.")
  write_manifest("dry-run-validated-no-database-connection")
  quit(status = 0)
}

if (Sys.getenv("ARTEMIS_NONPROD_APPROVED", unset = "") != "yes") {
  stop("Execution is blocked. Set ARTEMIS_NONPROD_APPROVED=yes only after non-production approval.")
}
if (Sys.getenv("ARTEMIS_ALLOW_WRITE", unset = "") != "yes") {
  stop("Execution is blocked. Set ARTEMIS_ALLOW_WRITE=yes for the dedicated disposable write schema.")
}

library(ARTEMIS)
connection_details <- DatabaseConnector::createConnectionDetails(
  dbms = Sys.getenv("ARTEMIS_DBMS"),
  server = Sys.getenv("ARTEMIS_DB_SERVER"),
  user = Sys.getenv("ARTEMIS_DB_USER"),
  password = Sys.getenv("ARTEMIS_DB_PASSWORD"),
  port = Sys.getenv("ARTEMIS_DB_PORT", unset = "5432"),
  pathToDriver = Sys.getenv("DATABASECONNECTOR_JAR_FOLDER")
)
cohort_json <- paste(readLines(cohort_path, warn = FALSE), collapse = "\n")
cohort_name <- Sys.getenv("ARTEMIS_COHORT_NAME", unset = "promop_scoped_cohort")

# getConDF creates cohort artifacts in write_schema.  It must never target the
# CDM schema; the two explicit environment gates above protect that invariant.
con_df <- getConDF(connectionDetails = connection_details, json = cohort_json,
                   name = cohort_name, cdmSchema = cdm_schema, writeSchema = write_schema)
valid_drugs <- loadDrugs()
regimens <- loadRegimens(condition = Sys.getenv("ARTEMIS_CONDITION", unset = "all"))
strings <- stringDF_from_cdm(con_df = con_df, validDrugs = valid_drugs)
raw <- generateRawAlignments(strings, regimens = regimens)
processed <- processAlignments(raw, regimenCombine = 28)
utils::write.csv(processed, "/work/output/artemis-alignments.csv", row.names = FALSE)
write_manifest("executed-nonproduction")
