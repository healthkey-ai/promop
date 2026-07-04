# Contributing to PRomop

Thank you for your interest in contributing. PRomop is an open-source project and we welcome
bug reports, feature requests, documentation improvements, and code contributions.

For questions or support, email **support@healthkey.ai**.

---

## Reporting bugs

Open a [GitHub issue](https://github.com/healthkey-ai/promop/issues) and include:

- A short description of the problem
- Steps to reproduce it
- What you expected to happen vs. what actually happened
- Your OS, Python version, and whether you are using Docker or a local install

For security vulnerabilities, please email **support@healthkey.ai** rather than opening a
public issue.

---

## Requesting features

Open a GitHub issue with the label `enhancement`. Describe the use case — what you are trying
to do and why the current software does not support it — rather than jumping straight to a
proposed implementation. This helps us assess fit with the project's direction before anyone
invests time in code.

---

## Contributing code

### 1. Fork and branch

```bash
git clone https://github.com/healthkey-ai/promop.git
cd promop
git checkout -b your-feature-branch
```

### 2. Set up your local environment

Follow the [local setup instructions](README.md#local-setup) in the README, or use
[Docker](BUILDING_WITH_DOCKER.md) if you prefer.

### 3. Make your changes

A few conventions to follow:

- **Adding a new patient attribute** — touch all required layers (model, migration,
  serializer, FHIR loader, TypeScript type, React UI, FHIR generator). The checklist in
  [CLAUDE.md](CLAUDE.md) walks through each layer in order.
- **New features must have tests.** Write them before considering the work done. See
  [Running tests](#running-tests) below.
- **No comments that describe *what* the code does** — well-named identifiers do that.
  Comments are for non-obvious *why*: a hidden constraint, a workaround, a subtle invariant.
- **Migrations** — always generate with `makemigrations`, never write by hand. Apply to
  your local `promop_dev` DB before committing.

### 4. Run the test suite

```bash
# Backend
DATABASE_URL="postgresql://postgres@localhost:5432/promop_test" \
  .venv/bin/python manage.py test omop_core patient_portal --verbosity=2 --noinput

# Frontend
cd frontend && npm test -- --run
```

Both suites must pass before you open a pull request.

### 5. Open a pull request

Target the `dev` branch (not `main`). In the PR description, explain:

- What the change does and why
- How you tested it
- Any migrations included and whether they are safe to apply to existing data

A maintainer will review and merge to `dev`; `dev` is periodically merged to `main` for
production deployment.

---

## Running tests

```bash
# One-liner: backend + frontend
DATABASE_URL="postgresql://postgres@localhost:5432/promop_test" \
  .venv/bin/python manage.py test omop_core patient_portal --verbosity=2 --noinput \
  && (cd frontend && npm test -- --run)
```

Local PostgreSQL setup (one-time, if you haven't done it):

```bash
brew services start postgresql@14
PATH="/opt/homebrew/opt/postgresql@14/bin:$PATH" psql -U $(whoami) -d postgres \
  -c "CREATE ROLE postgres WITH SUPERUSER CREATEDB CREATEROLE LOGIN;" 2>/dev/null || true
PATH="/opt/homebrew/opt/postgresql@14/bin:$PATH" psql -U postgres -d postgres \
  -c "CREATE DATABASE promop_test OWNER postgres;" 2>/dev/null || true
DATABASE_URL="postgresql://postgres@localhost:5432/promop_test" \
  .venv/bin/python manage.py migrate --noinput
```

Linux users: see [docs/linux-setup.md](docs/linux-setup.md).

---

## Code of conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).
By participating you agree to abide by its terms. Report violations to **support@healthkey.ai**.
