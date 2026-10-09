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

as_omop_date <- function(value) {
  if (inherits(value, "Date")) return(value)
  if (is.numeric(value)) return(as.Date(as.POSIXct(value, origin = "1970-01-01", tz = "UTC")))
  as.Date(value)
}

fetch_exposure_lineage <- function(connection_details, cdm_schema, write_schema, cohort_name, valid_drugs) {
  # ARTEMIS's getConDF intentionally returns a compact alignment input and
  # omits drug_exposure_id. Re-read the same cohort with local event IDs so the
  # PRomop materializer can validate every EpisodeEvent reference.
  connection <- DatabaseConnector::connect(connectionDetails = connection_details)
  on.exit(DatabaseConnector::disconnect(connection), add = TRUE)
  sql_template <- "
    SELECT DISTINCT de.drug_exposure_id,
           CAST(de.person_id AS VARCHAR) AS person_id,
           de.drug_exposure_start_date,
           de.drug_exposure_end_date,
           ca.ancestor_concept_id,
           c.concept_name
      FROM @cdmSchema.drug_exposure de
      JOIN @cohortDatabaseSchema.@cohortName ch ON ch.subject_id = de.person_id
      JOIN @cdmSchema.concept_ancestor ca
        ON de.drug_concept_id = ca.descendant_concept_id
      JOIN @cdmSchema.concept c ON ca.ancestor_concept_id = c.concept_id
     WHERE LOWER(c.concept_class_id) = 'ingredient'
  "
  rendered <- SqlRender::render(sql_template, cdmSchema = cdm_schema,
                                cohortDatabaseSchema = write_schema, cohortName = cohort_name)
  translated <- SqlRender::translate(rendered, targetDialect = connection@dbms)
  lineage <- as.data.frame(DatabaseConnector::dbGetQuery(connection, translated))
  lineage[lineage$ancestor_concept_id %in% valid_drugs$valid_concept_id, , drop = FALSE]
}

normalise_artemis_component <- function(value) {
  tolower(gsub(" ", "", gsub(",", "_", as.character(value), fixed = TRUE), fixed = TRUE))
}

record_components <- function(record) {
  tokens <- strsplit(as.character(record), ";", fixed = TRUE)[[1]]
  tokens <- tokens[nzchar(tokens)]
  if (!length(tokens)) stop("ARTEMIS processed row has no CompleteDrugRecord")
  pieces <- regexec("^([0-9]+(?:\\.[0-9]+)?)\\.(.+)$", tokens)
  matches <- regmatches(tokens, pieces)
  if (any(lengths(matches) != 3L)) stop("Cannot parse ARTEMIS CompleteDrugRecord")
  gaps <- as.numeric(vapply(matches, `[[`, character(1), 2L))
  components <- vapply(matches, `[[`, character(1), 3L)
  data.frame(offset = cumsum(gaps), component = components, stringsAsFactors = FALSE)
}

write_adapter_output <- function(processed, con_df, lineage) {
  if (!nrow(processed)) {
    writeLines(jsonlite::toJSON(list(schema_version = "1", episodes = list()), auto_unbox = TRUE, pretty = TRUE),
               "/work/output/artemis-episodes.json")
    return(0L)
  }
  con_df$person_id <- as.character(con_df$person_id)
  con_df$drug_exposure_start_date <- as_omop_date(con_df$drug_exposure_start_date)
  lineage$person_id <- as.character(lineage$person_id)
  lineage$drug_exposure_start_date <- as_omop_date(lineage$drug_exposure_start_date)
  lineage$drug_exposure_end_date <- as_omop_date(lineage$drug_exposure_end_date)
  lineage$normalised_component <- normalise_artemis_component(lineage$concept_name)
  processed$personID <- as.character(processed$personID)
  episodes <- list()

  for (person_id in unique(processed$personID)) {
    rows <- processed[processed$personID == person_id, , drop = FALSE]
    rows <- rows[order(rows$t_start, rows$t_end, rows$component), , drop = FALSE]
    baseline <- min(con_df$drug_exposure_start_date[con_df$person_id == person_id])
    if (is.na(baseline)) stop("ARTEMIS alignment refers to a person absent from its cohort input: ", person_id)
    person_lineage <- lineage[lineage$person_id == person_id, , drop = FALSE]
    for (index in seq_len(nrow(rows))) {
      selected_components <- record_components(rows$CompleteDrugRecord[index])
      selected_components <- selected_components[
        selected_components$offset >= rows$t_start[index] & selected_components$offset <= rows$t_end[index],
        , drop = FALSE
      ]
      if (!nrow(selected_components)) {
        stop("Cannot identify selected ARTEMIS components for person ", person_id, " line ", index)
      }
      resolved_exposures <- lapply(seq_len(nrow(selected_components)), function(component_index) {
        component_date <- baseline + as.integer(round(selected_components$offset[component_index]))
        component_name <- normalise_artemis_component(selected_components$component[component_index])
        candidate_rows <- person_lineage[
          person_lineage$drug_exposure_start_date == component_date &
            person_lineage$normalised_component == component_name
          , drop = FALSE]
        candidates <- unique(as.integer(candidate_rows$drug_exposure_id))
        candidates <- candidates[!is.na(candidates)]
        if (length(candidates) != 1L) {
          stop("Cannot prove a unique local drug_exposure_id for ARTEMIS component ",
               selected_components$component[component_index], " on ", component_date,
               " for person ", person_id, "; refusing to emit materializer JSON")
        }
        candidate_rows[candidate_rows$drug_exposure_id == candidates[[1]], , drop = FALSE][1, , drop = FALSE]
      })
      resolved_exposures <- do.call(rbind, resolved_exposures)
      exposure_ids <- unique(as.integer(resolved_exposures$drug_exposure_id))
      start_date <- min(resolved_exposures$drug_exposure_start_date)
      exposure_end_dates <- resolved_exposures$drug_exposure_end_date
      exposure_end_dates[is.na(exposure_end_dates)] <- resolved_exposures$drug_exposure_start_date[is.na(exposure_end_dates)]
      end_date <- max(exposure_end_dates)
      episodes[[length(episodes) + 1L]] <- list(
        person_id = as.integer(person_id),
        line_number = index,
        start_date = format(start_date, "%Y-%m-%d"),
        end_date = format(end_date, "%Y-%m-%d"),
        drug_exposure_ids = as.list(exposure_ids)
      )
    }
  }
  payload <- list(schema_version = "1", episodes = episodes)
  writeLines(jsonlite::toJSON(payload, auto_unbox = TRUE, pretty = TRUE),
             "/work/output/artemis-episodes.json")
  length(episodes)
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
if (file.exists("/work/output/artemis-episodes.json")) {
  stop("Refusing to overwrite artemis-episodes.json; use a fresh run output directory to avoid stale materialization input")
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
lineage <- fetch_exposure_lineage(connection_details, cdm_schema, write_schema, cohort_name, valid_drugs)
episode_count <- write_adapter_output(processed, con_df, lineage)
manifest$adapter_schema_version <- "1"
manifest$adapter_episode_count <- episode_count
write_manifest("executed-nonproduction")
