# Security Policy

Scope: this repo's REST API + MCP server (`src/idc_api/`).

## Threat model, in one paragraph

This service serves the public NCI Imaging Data Commons index — an open, de-identified dataset, not
secret data. The DuckDB backend is opened read-only, so no request can modify or delete it.
The realistic risk is therefore **abuse of the server** (cost, availability, resource
exhaustion) rather than data disclosure. Full rationale and the guarded-SQL threat model live in
[dev/api_v3_plan.md](dev/api_v3_plan.md) → "Safety for the guarded SQL tool".

## What's already in place

- **Read-only, hardened DuckDB connection** — `enable_external_access=false`,
  `autoload/autoinstall_known_extensions=false`, `lock_configuration=true` (frozen at connect
  time). See `DuckDBBackend._hardening_config` in
  [duckdb_backend.py](src/idc_api/core/backend/duckdb_backend.py). Regression-tested in
  [tests/test_backend_guards.py](tests/test_backend_guards.py): non-SELECT statements,
  multi-statement SQL, local/remote file access, and extension-loading/export statements
  (`INSTALL`, `LOAD`, `COPY ... TO`, `SET`) are all rejected.
- **Row/response caps** — `run_sql` and manifest endpoints clamp `max_rows` to a hard server
  ceiling (`IDC_API_SQL_MAX_ROWS_CAP`, `IDC_API_MANIFEST_HARD_CAP`); a caller cannot request an
  unbounded dump.
- **Parameterized SQL everywhere we author it** — values are always bound (`?` placeholders);
  identifiers that can't be bound (table/column names) are validated against allow-lists before
  being interpolated. This is an architecture invariant — see [CLAUDE.md](CLAUDE.md) — enforced
  in code review and spot-checked by `bandit` in CI (SQL-construction findings are individually
  annotated with why the identifier is trusted, not blanket-suppressed).
- **Structured audit logging** — every REST request and MCP tool call emits one JSON log line
  (path/tool, status/outcome, duration, row count where applicable) to stdout, which Cloud Run
  ships to Cloud Logging automatically. For the guarded SQL endpoint/tool, a rendering of the
  query is included — by default the first 200 chars (`IDC_API_SQL_LOG_MODE=snippet`), or a
  short digest instead (`IDC_API_SQL_LOG_MODE=hash`) if you'd rather correlate repeated queries
  without putting query text in logs. Either way this is public-schema SQL the caller wrote
  themselves, not sensitive data — the cap is about log-line hygiene (one pathological query
  can't inflate a line), not confidentiality. Client IPs are not logged at the application level
  (Cloud Run's own request log already has caller IP, correlatable by timestamp).
- **CI checks.** [gitleaks](.github/workflows/gitleaks.yml) (committed credentials) runs on **every**
  PR — deliberately not path-filtered, since a credential can be committed in any file.
  [ci.yml](.github/workflows/ci.yml) runs `ruff` (lint + format), `bandit` (static security lint),
  `pip-audit` (dependency CVEs), and the `tests` suite on Python 3.11/3.12 — but **only** for PRs
  touching `src/idc_api/**`, `tests/**`, `pyproject.toml`, `uv.lock`, or `ci.yml` itself.
  `actionlint` likewise runs only when a workflow changes. A docs-only PR therefore runs `gitleaks`
  and nothing else.
- **Dependency vulnerabilities, caught twice.** `pip-audit` fails CI on a PR whose dependencies
  carry a known CVE, and **Dependabot alerts + automated security updates** are enabled on the
  repository, so a CVE disclosed *after* a PR merges still opens a fix PR against `main` rather
  than waiting for someone to notice. [dependabot.yml](.github/dependabot.yml) separately schedules
  weekly grouped *version* updates for the `uv` and `github-actions` ecosystems; security updates
  are the repo setting, not that file, and are ungrouped so a fix ships on its own.
- **Credential hygiene, in three layers.** The service itself holds no secrets, but the *deploy*
  path does: each tier's deployer service-account JSON key. Those live in GitHub **Environment
  secrets**, never in the repo — and three independent guards keep them out of it:
  1. **Push protection** (GitHub secret scanning) rejects a push containing a recognized
     credential *before* it reaches GitHub. This is the only guard that prevents rather than
     detects. It does not cover pushes to forks.
  2. **[gitleaks](.github/workflows/gitleaks.yml)** scans the full history on every PR, including
     from forks, and flags generic private-key blocks and service-account JSON that provider
     pattern-matching misses. Findings are redacted in the log (this repo's Actions logs are
     public).
  3. **`.gitignore`** covers the filenames deployer keys actually land under. It is the weakest
     layer — a filename list is never exhaustive — and exists to catch the common `git add -A`.

  If a credential is ever committed: **rotate it first.** Deleting the commit does not un-leak it;
  the blob stays reachable and this repo is public. Purge from history afterwards, not instead.

> **These are repository settings, not files.** Secret scanning, push protection, Dependabot alerts,
> and Dependabot security updates are all enabled under *Settings → Code security*. They are not
> visible in any diff, so they can be switched off without a code review — this section is the only
> record that they are meant to be on. Verify with:
>
> ```bash
> gh api repos/ImagingDataCommons/IDC-REST-MCP \
>   --jq '.security_and_analysis | {secret_scanning, secret_scanning_push_protection, dependabot_security_updates}'
> ```
- **Non-root container** — `Dockerfile` drops to an unprivileged user before serving.

## Known residual risks (public deployment)

| Risk | Status |
|---|---|
| No per-IP rate limiting in front of the service | Mitigated by Cloud Run `--max-instances`/`--concurrency` caps (see [dev/deployment.md](dev/deployment.md)); a dedicated edge rate limit (Cloud Armor / API Gateway) is an infra decision outside this repo. |
| CORS allows all origins (`*`) | Intentional — the API serves only public, read-only data. Revisit if private data or auth is ever added. |
| MCP DNS-rebinding protection defaults off | Documented trade-off for the unauthenticated hosted transport; operators who want it set `IDC_API_MCP_DNS_REBINDING_PROTECTION=true` plus an allowed-hosts list. |
| DuckDB sandbox escape (0-day in DuckDB itself) | No code-level mitigation beyond the hardening config above and re-running the guard tests on every DuckDB upgrade; the container itself holds no secrets and reaches no internal network. |

If you run your own fork or self-hosted deployment, you own its network exposure, auth (if you
add non-public data), and update cadence — this document describes the project's own hosted
service.

## Reporting a vulnerability

Please use GitHub's private
["Report a vulnerability"](https://github.com/ImagingDataCommons/IDC-REST-MCP/security/advisories/new)
flow so it isn't publicly disclosed before a fix ships. For non-sensitive hardening suggestions
(e.g. a missing test case), a regular GitHub issue is fine.
