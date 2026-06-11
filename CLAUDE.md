# CLAUDE.md

Guidance for working in this repository.

## What this repo is

Two things live here:

- **IDC API v3** (`src/idc_api/`, tests in `tests_v3/`) — an LLM-first **REST API** + **MCP
  server** for the NCI Imaging Data Commons, backed by the `idc-index` Parquet index queried
  locally with DuckDB. One backend-agnostic **core** library, two thin adapters (`rest/`,
  `mcp/`). This is the active development focus.
- **Legacy v2 API** (`api/`, tests in `tests/`) — the older Django/BigQuery service. Don't
  touch it unless a task is explicitly about v2.

## Commands (v3)

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run --directory . pytest tests_v3 -q     # run the v3 test suite
uv run idc-api                              # REST API → http://127.0.0.1:8000 (/docs)
uv run idc-mcp                              # MCP server over stdio
```

## Architecture invariants (do not break)

These are the rules the v3 design depends on. Full detail and walkthroughs are in
[dev/developer_guide.md](dev/developer_guide.md); the design rationale is in
[dev/architecture.md](dev/architecture.md).

1. **`core/` never imports an adapter.** No `fastapi` / `mcp` imports under `core/`. Adapters
   import `core/`, never the reverse.
2. **Adapters are thin.** A REST route or MCP tool validates input and calls a service. No SQL
   or domain logic in `rest/` or `mcp/`.
3. **Services return Pydantic models** from `core/models.py` — never raw dicts or DataFrames.
   Both adapters serialize the *same* models; parity tests enforce REST output == MCP output.
4. **SQL we author is parameterized.** Use `backend.query(sql, params)` with `?` placeholders;
   never f-string user *values* into SQL. Identifiers (table/column names) that can't be bound
   must be validated against `schema` allow-lists and double-quoted (see `core/filters.py`).
5. **Raw caller/LLM SQL only via `backend.run_user_sql`.** Never route untrusted SQL through
   `backend.query`.
6. **MCP tool descriptions are prescriptive** about *when* to call the tool (e.g. "call this
   before filtering"), not just what it does — this measurably improves tool selection.
7. **Errors:** raise an `IDCAPIError` subclass from services. REST maps it to
   `{status, code, message}`; the MCP `guard` decorator converts it to a clean `ToolError`.
   Never leak tracebacks.

Adding a capability touches five places (model → service → REST route → MCP tool → parity
test). See the walkthrough in [dev/developer_guide.md](dev/developer_guide.md).

## Documentation conventions

v3 docs are split by audience — keep them in their lanes:

- **[docs/user-guide.md](docs/user-guide.md)** — the **human-facing** user guide: concepts, the
  query surfaces (Discovery → Cohort → Retrieval, with SQL as the escape hatch) and how they
  relate, the recommended workflow, worked REST/MCP examples, and the config reference. Usage
  documentation belongs here.
- **`idc://guide` MCP resource** (the `_GUIDE` string in `src/idc_api/mcp/server.py`) — the
  **agent-facing** guide. It mirrors the *same conceptual model* as the user guide (tool
  families, how they relate, the workflow). **Keep it in sync** when the conceptual model
  changes. Note: `tests_v3/test_mcp.py` asserts this resource contains "Data model".
- **[README_v3.md](README_v3.md)** — kept **lean**: intro, status, install, run one-liners,
  deploy, and pointers. **Do not** add endpoint/tool reference or usage detail here — that goes
  in the user guide.
- **`dev/`** — design & contributor docs: [architecture.md](dev/architecture.md),
  [api_v3_plan.md](dev/api_v3_plan.md) (design rationale + SQL threat model),
  [deployment.md](dev/deployment.md), [developer_guide.md](dev/developer_guide.md).

When you add or change a v3 capability: update `docs/user-guide.md`; mirror any conceptual
change into the `idc://guide` resource; keep `README_v3.md` a pointer.
