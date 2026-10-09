# Render production configuration

Django combines `ALLOWED_HOSTS` (a comma-separated list of custom domains) with
Render's automatically supplied `RENDER_EXTERNAL_HOSTNAME`. A service using its
Render hostname can boot without a dashboard `ALLOWED_HOSTS` value. Custom
domains still need an explicit entry. This does not enable wildcard hosts.

For an **existing** Blueprint service, `sync: false` does not add a value or
prompt again during updates. Set missing values on the service's Environment
page. In particular, `CORS_ALLOWED_ORIGINS` must contain the frontend origins;
Render's backend hostname does not determine which external frontends to trust.

`scripts/prepare-deployment.sh`, which `start.sh` invokes, runs `check --deploy --fail-level ERROR`
before migrations. This check
now applies the same production settings guard as the web process and the
Athena loader, so missing runtime configuration stops deployment before database
work. Build-time `collectstatic`, ordinary `check`, and standalone `migrate`
retain their existing import exemptions.

An Athena startup traceback beginning with `KeyError:
'load_athena_vocabularies'` can be command discovery failing to import settings.
Read the final exception: `Missing required production settings` means the
configuration must be fixed, not that the loader command needs reinstalling.
OpenAPI schema warnings earlier in the log do not cause a deployment using
`--fail-level ERROR` to fail.

The Athena loader downloads only the selected ZIP from a Drive folder and reads
each CSV directly from the compressed archive. It does not expand the entire
release onto the web instance's temporary filesystem. Downloaded archives and
partial downloads are removed when the command exits, including on errors.
Local `--archive` inputs remain intact. Python's `TMPDIR` can point scratch
downloads at a separately mounted directory when needed.

Web startup runs `prepare_production_database`. If migration 0201 is pending, it
first checks whether all LOINC concepts that migration resolves already exist.
Only when some are absent does it download Athena and scan `CONCEPT.csv`; this
bounded mode inserts exactly the required concepts and verifies the complete set
before applying 0201. It skips the relationship tables, release publication,
code-mapping artifact, and optional UMLS cache. After 0201 is recorded, later
deploys skip Athena entirely and apply ordinary migrations before Gunicorn.

The complete Athena vocabulary is loaded as a separate maintenance operation.
That bulk import is too large for Render's 15-minute web start command and may
also exceed its 30-minute pre-deploy command. The migration bootstrap is not a
substitute for that maintenance load; it exists only to satisfy 0201 safely and
let the web process bind within Render's deadline.

Render references: [Blueprint environment variables](https://render.com/docs/blueprint-spec#setting-environment-variables)
and [default environment variables](https://render.com/docs/environment-variables).
