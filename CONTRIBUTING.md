# Contributing

Thanks for helping improve the IDC REST API + MCP server. This document covers how we branch,
commit, changelog, version, and release. For *how the code is laid out* and *how to add a
capability*, see [dev/developer_guide.md](dev/developer_guide.md); for *why*, see
[dev/architecture.md](dev/architecture.md).

## Getting set up

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run --directory . pytest tests -q
```

The first test run downloads the specialized `idc-index` Parquet indices (~40 MB). No GCP
account, network credentials, or authentication are needed — everything queries local Parquet.

## Before you open a pull request

Run what CI runs:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run bandit -q -r src/idc_api
uv run pytest tests -q
```

CI also runs `pip-audit` for dependency CVEs.

## Architecture invariants

The design depends on a short list of rules — `core/` never imports an adapter, adapters stay
thin, services return Pydantic models, SQL we author is parameterized, untrusted SQL only ever
goes through `backend.run_user_sql`, and errors are raised as `IDCAPIError` subclasses. They are
enumerated in [CLAUDE.md](CLAUDE.md#architecture-invariants-do-not-break) and are what reviewers
check first. Adding a capability touches five places: model → service → REST route → MCP tool →
parity test.

Documentation is split by audience (user guide vs. the agent-facing `idc://guide` resource vs.
the always-on MCP `INSTRUCTIONS` vs. `dev/`). Keep each in its lane — the conventions, and which
file to touch when, are in [CLAUDE.md](CLAUDE.md#documentation-conventions).

## Branches

Branch off `main`, named `<type>/<short-slug>` using the same type vocabulary as commits:

```
feat/cohort-size-estimate     fix/mcp-trailing-slash
docs/api-endpoint-examples    ci/multi-tier-deploy
```

Pull requests are merged with a merge commit, so the individual commits on your branch land in
`main`'s history. Make them ones you'd want to read later.

## Commits

We follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```
<type>(<optional scope>): <imperative, lowercase summary>
```

**Types:** `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `style`, `build`, `ci`, `chore`.
**Scopes** in use: `rest`, `mcp`, `api`, `deploy`, `deps`.

```
feat(rest): redirect the bare domain to the interactive docs
fix(mcp): serve /mcp and /mcp/ directly instead of redirecting
docs(api): add OpenAPI summaries/descriptions to every REST route
```

This is a convention, not a CI gate — nothing will fail your build if you deviate. It exists so
history stays skimmable and so the changelog is easy to assemble at release time. Dependabot's
commits don't always conform; that's fine.

Mark a breaking change to the REST or MCP contract with a `!` (`feat(rest)!: …`) and a
`BREAKING CHANGE:` footer. See [Versioning](#versioning) — such a change needs a new URL prefix,
so it is a much bigger conversation than a commit message.

## Changelog

[CHANGELOG.md](CHANGELOG.md) is **hand-curated**, in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
format. It is not generated from commits: it describes what changed for *callers of the API*,
which is a different thing from what changed in the tree.

**If your PR changes user-visible behavior, add an entry to `## [Unreleased]` in the same PR.**
User-visible means an endpoint, an MCP tool or its description, a response shape, a configuration
variable, or a default. Use the standard groupings — `Added`, `Changed`, `Deprecated`, `Removed`,
`Fixed`, `Security` — and write for someone consuming the API, not someone reading the diff:

```markdown
### Fixed
- MCP: `/mcp` and `/mcp/` are both served directly; neither redirects.
```

Refactors, test changes, CI, formatting, and dependency bumps do **not** get an entry. The git
history already records them.

> While `3.0.0b1` is still unreleased, fixes to code that has never shipped are folded into that
> release's `Added` section rather than listed under `Fixed` — there is no released behavior to
> have fixed. Once the beta ships, use the groupings normally.

## Versioning

[Semantic Versioning](https://semver.org/spec/v2.0.0.html), with one house rule:

**MAJOR is pinned to the served URL prefix.** `/v3` ↔ `3.y.z`, always.

| Change | Version | URL |
|---|---|---|
| Add an endpoint, MCP tool, or optional field | MINOR — `3.1.0` | `/v3` |
| Fix a bug without changing the contract | PATCH — `3.0.1` | `/v3` |
| Break the REST or MCP contract | MAJOR — `4.0.0` | new prefix `/v4` |

So a breaking change is never a silent break under `/v3`: it is a new prefix served alongside the
old one. This keeps `api_version` predictive of the URL, and matches the clean break v3 already
made from v1/v2.

Pre-releases use [PEP 440](https://peps.python.org/pep-0440/) spelling so the Python package
version and the git tag agree: `3.0.0b1` → tag `v3.0.0b1`; `3.0.0rc1` → tag `v3.0.0rc1`.

**The version lives in exactly one place: `version` in [pyproject.toml](pyproject.toml).**
Everything else derives from it — `idc_api.__version__` and `core/version.py:package_version()`
both read the installed distribution metadata. Never hardcode it a second time.

## Releasing

> [!IMPORTANT]
> **Pushing a `v*` tag deploys to production.** [promote.yml](.github/workflows/promote.yml)
> triggers on `push: tags: ["v*"]`, and that glob matches pre-release tags too — `v3.0.0b1` goes
> to prod exactly like `v3.0.0`. Never create a `v*` tag for bookkeeping, and be careful with
> `git push --tags`, which can fire a deploy from a stale local tag.

Two constraints follow from how the pipeline is built (see [dev/deployment.md](dev/deployment.md)):

1. **Prod deploys test's image, by digest, without rebuilding.** A `v*` tag must therefore point
   at a commit that was already promoted to `test` via the `promote.yml` manual dispatch — tag a
   commit that never went through test and the deploy fails fast at the digest-resolve step.
2. **The version bump must be its own commit, and it must go through test.** The version is baked
   into the image at build time (it comes from the installed package metadata), while
   `IDC_API_BUILD` only stamps the git SHA. Tagging `v3.0.0` on the same commit that shipped as
   `3.0.0b1` would redeploy an image that still reports `3.0.0b1` at `/v3/version`.

### Steps

1. **Bump and curate.** In one PR: set `version` in `pyproject.toml`, and in `CHANGELOG.md`
   rename `## [Unreleased]` to `## [X.Y.Z] — YYYY-MM-DD`, open a fresh empty `[Unreleased]`, and
   update the link definitions at the foot of the file. Merge it.
2. **Promote to test.** Run `promote.yml` via workflow dispatch against that merge commit. It
   builds the canonical image into test's registry and deploys `testing-api.canceridc.dev`.
3. **Verify** against test — `/v3/health`, `/v3/version` (confirm it reports the version you just
   set), and the MCP handshake at `/mcp`.
4. **Tag.** `git tag -a v3.0.0 -m "v3.0.0" <that commit> && git push origin v3.0.0`. This starts
   the prod deploy, which waits on the `prod` Environment's required-reviewer gate.
5. **Approve** the deployment, then confirm `api.imaging.datacommons.cancer.gov/v3/version`.
6. **Publish a GitHub Release** on the tag, with the changelog section as its body. Tick
   **"Set as a pre-release"** for `bN` / `rcN` tags.

### The v3 beta

v3 ships to production as `3.0.0b1` before `3.0.0`.

The beta is **not** a traffic-safety measure — it can't be one. Prod's load balancer routes only
`/v3/*` to the `idc-api-v3` service; every other path falls through to the legacy backend. No
existing caller reaches v3, so shipping it cannot break them. What the beta buys is the freedom
to **change the `/v3` contract in response to real usage** without spending a major version, and
an honest signal to early adopters that it may move. Exit the beta by tagging `v3.0.0` once the
contract has held under real use and the docs match it.

Deliberately **not** doing a Cloud Run traffic split (`--tag beta --no-traffic` + a percentage
rollout) for this release. There is no incumbent v3 revision to canary against, and percentage
splits are applied **per request**, not per session — an MCP streamable-http session could have
its requests land on different revisions mid-conversation unless `--session-affinity` is enabled.
If a canary becomes worthwhile for a later release (`3.1.0` onward, once v3 has consumers), use a
**tagged revision** at zero traffic, which gets its own `beta---idc-api-v3-*.run.app` URL that
testers opt into explicitly, rather than a percentage split of the live domain.

## Reporting security issues

Please don't open a public issue for a vulnerability — use GitHub's private reporting flow, as
described in [SECURITY.md](SECURITY.md).
