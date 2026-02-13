# Platform-Agnostic Refactor Plan

This document turns the previous recommendations into a concrete, low-risk implementation plan for this repository.

## Goals

- Keep current Azure Functions behavior working during migration.
- Isolate Monzo business logic from hosting/provider concerns.
- Add one additional runtime target (FastAPI) with shared core logic.
- Enable pluggable state backends beyond Azure Table Storage.

## Proposed target structure

```text
MonzoBalanceBot/
  function_app.py                 # Azure adapter only
  app_fastapi.py                  # Optional ASGI adapter (new)

  core/
    settings.py                   # Provider-neutral configuration + legacy aliases
    models.py                     # Typed payload/state models (dataclasses/TypedDict)
    webhook_service.py            # handle_webhook(headers, query, body) -> result
    monzo_client.py               # Monzo API wrapper (balance, tx verify, feed, notes)
    token_service.py              # refresh/access token orchestration
    alert_service.py              # threshold and state-machine behavior

  stores/
    interfaces.py                 # TokenStore, AlertStateStore, DedupeStore protocols
    azure_table_store.py          # Azure Table implementation (existing logic moved)
    memory_store.py               # Local dev/test backend
    redis_store.py                # Optional distributed dedupe/state backend (phase 3)

  tests/
    test_webhook_service.py       # transport-agnostic fixture tests
    test_alert_service.py         # state machine tests
    test_token_service.py         # token refresh + race behavior tests
    adapters/
      test_azure_adapter.py
      test_fastapi_adapter.py

  requirements-core.txt
  requirements-azure.txt
  requirements-fastapi.txt
  requirements-dev.txt
```

## Adapter boundary (important)

### Azure adapter (`function_app.py`)

Keep only:

1. Convert `func.HttpRequest` to plain dictionaries.
2. Call shared `core.webhook_service.handle_webhook(...)`.
3. Convert returned result to `func.HttpResponse`.

No Monzo API calls, token refresh logic, or persistence logic should remain in this file.

### FastAPI adapter (`app_fastapi.py`)

Implement the same translation boundary:

- `POST /monzo_webhook`
- Build plain `headers/query/body`
- Call `handle_webhook(...)`
- Return HTTP response from service result

## Interfaces to introduce

In `stores/interfaces.py`:

- `TokenStore`
  - `get_token_state() -> TokenState | None`
  - `save_token_state(state, etag=None) -> SaveResult`
- `AlertStateStore`
  - `get_alert_state() -> AlertState`
  - `save_alert_state(state) -> None`
- `DedupeStore`
  - `seen(tx_id: str, ttl_seconds: int) -> bool`

This keeps storage semantics generic while preserving optimistic concurrency behavior.

## Configuration normalization

`core/settings.py` should expose neutral names with legacy aliases:

- `MONZO_CLIENT_ID` (alias: `MONZOCLIENTID`)
- `MONZO_CLIENT_SECRET` (alias: `MONZOCLIENTSECRET`)
- `MONZO_ACCOUNT_ID` (alias: `MONZOACCOUNTID`)
- `MONZO_REFRESH_TOKEN` (alias: `MONZOREFRESHTOKEN`)
- `WEBHOOK_SECRET` (alias: `WEBHOOKSECRET`)
- `BALANCE_LIMIT_WARNING` (alias: `LIMIT_WARNING`)
- `BALANCE_LIMIT_CRITICAL` (alias: `LIMIT_CRITICAL`)
- `ALERT_FREQUENCY` (alias: `ALERT_FREQUENCY`)
- `STATE_BACKEND` (`azure_table`, `memory`, `redis`, ...)

Azure-specific values (`AzureWebJobsStorage*`) should be consumed only by `stores/azure_table_store.py`.

## Four PR rollout (recommended)

### PR 1 — Safe extraction (no behavior changes)

- Create `core/` and `stores/interfaces.py`.
- Move logic from `function_app.py` into:
  - `core/webhook_service.py`
  - `core/alert_service.py`
  - `core/token_service.py`
  - `core/monzo_client.py`
- Keep Azure Table-backed implementation only for now.
- Leave Azure function route unchanged externally.

Exit criteria:

- Existing webhook behavior unchanged.
- Local smoke test via `func start` still works.

### PR 2 — Add second runtime

- Add `app_fastapi.py` adapter.
- Add container entrypoint (`Dockerfile`) and run instructions.
- Confirm same webhook fixtures pass against FastAPI adapter.

Exit criteria:

- Same test fixtures pass on Azure adapter + FastAPI adapter.

### PR 3 — Add pluggable state backends

- Add `memory_store.py` and optionally `redis_store.py`.
- Add backend selection via `STATE_BACKEND`.
- Move process-local dedupe into `DedupeStore` abstraction.

Exit criteria:

- Dedupe works consistently in selected backend.
- Azure remains default backend for production.

### PR 4 — Dependency and CI matrix cleanup

- Split dependency files by target.
- Add CI matrix:
  - core unit tests
  - Azure adapter tests
  - FastAPI adapter tests
- Add lint/type checks for stable refactors.

Exit criteria:

- Green matrix for all targets.

## Risks and mitigations

- **Risk:** Token refresh race regressions.
  - **Mitigation:** Preserve ETag/conditional update semantics in Azure store implementation and add race tests.

- **Risk:** Behavior drift during extraction.
  - **Mitigation:** Create fixture tests from current webhook payloads before moving code.

- **Risk:** Secret/config breakage.
  - **Mitigation:** Keep legacy env var aliases until migration is complete.

## Immediate next step for this repo

Start with **PR 1** and make it explicitly “internal refactor only.”

A practical first commit in PR 1:

1. Add `core/settings.py` + alias mapping.
2. Add `stores/interfaces.py`.
3. Extract `handle_webhook(...)` into `core/webhook_service.py`.
4. Convert `function_app.py` into a thin adapter calling that function.

This gives maximum future flexibility with minimal runtime risk.
