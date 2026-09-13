# Environment conventions

- Staging always means Render: https://promop-staging.onrender.com.
- The staging web service is `promop-staging`; its worker is `promop-staging-worker`.
- Do not use the old Google Cloud / Cloud Run staging deployment to investigate or verify staging unless the user explicitly asks for it.
- See `docs/render-staging-celery.md` for Render staging configuration and verification.

# Pull request reviews

- There is no team review process. Do not request team reviewers (including `healthkey`) or configure required team-review rules.
- Request individual reviewers as directed by the user. Lars Burgess is `@larsburgess` on GitHub.
