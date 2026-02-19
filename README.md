# Monzo Balance Bot 🐕

Monzo Balance Bot listens to Monzo transaction webhooks and posts balance warnings back to your Monzo feed when your spendable balance drops below configurable thresholds. It now supports both Azure Functions and a platform-neutral FastAPI runtime.

![Status](https://img.shields.io/badge/status-active-brightgreen)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Azure Functions](https://img.shields.io/badge/azure-functions-0078D4)

## Why this exists

Monzo already shows your balance, but it is easy to miss when spending quickly. This bot adds proactive alerts directly into your Monzo activity feed and (optionally) transaction notes so warnings appear exactly where you are looking.
It is a feature missing from the Monzo App (and one of the most requested features - https://community.monzo.com/t/notification-on-reaching-a-set-balance/153931)

## Features

- **Real-time webhook processing** for `transaction.created` events.
- **Two alert levels** (both configurable):
  - **Warning (Amber):** balance below `LIMIT_WARNING` (default `25000` pence / £250).
  - **Critical (Red):** balance below `LIMIT_CRITICAL` (default `10000` pence / £100).
- **Repeat reminder cadence** while you remain below a threshold using `ALERT_FREQUENCY` (default every 10 transactions).
- **Idempotency protection** to avoid duplicate processing when webhook events are retried.
- **Token auto-refresh** with optimistic concurrency (ETag-safe updates in Azure Table Storage).
- **Webhook secret verification** via either:
  - Header: `X-Webhook-Secret` (recommended)
  - Query parameter: `secret_key` (legacy compatibility)
- **Health check endpoints** available at `/health` for both Azure Functions and FastAPI runtimes.
- **Correlation IDs** supported through `X-Correlation-ID` (request/response) for easier tracing.

## Architecture

- **Core logic:** Transport-agnostic Python service (`core/webhook_service.py`)
- **Runtime adapters:**
  - Azure Functions (`function_app.py`)
  - FastAPI (`app_fastapi.py`)
- **State & token store:** pluggable backend (`azure_table` default, `memory` local option)
- **External API:** Monzo API (`/oauth2/token`, `/balance`, `/feed`, `/transactions/{id}`)
- **Auth model:** Monzo OAuth2 refresh token + managed identity or storage connection string

## Prerequisites

- Python 3.10+
- Azure Table-capable storage configuration (`AzureWebJobsStorage` or `AzureWebJobsStorage__tableServiceUri`)
- Monzo Developer app credentials (Client ID / Client Secret)
- For Azure runtime: [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- For FastAPI runtime: `fastapi` + `uvicorn` (included in `requirements-fastapi.txt`)

## Configuration

Set these as Function App settings (or in `local.settings.json` when running locally):

| Variable | Required | Description | Example |
|---|---|---|---|
| `MONZOCLIENTID` | Yes | Monzo OAuth2 client ID | `oauth2client_000...` |
| `MONZOCLIENTSECRET` | Yes | Monzo OAuth2 client secret | `mnzpub...` |
| `MONZOACCOUNTID` | Yes | The Monzo account ID to monitor | `acc_000...` |
| `MONZOREFRESHTOKEN` | Yes* | Initial fallback refresh token used when storage is empty | `eyJ...` |
| `WEBHOOKSECRET` | Yes | Shared secret used to verify incoming webhook calls | `a1b2c3...` |
| `STATE_BACKEND` | No | State backend: `azure_table` (default) or `memory` | `memory` |
| `ALLOW_QUERY_SECRET` | No | Allow Monzo query-string secret auth (`true` default for Monzo compatibility) | `true` |
| `AzureWebJobsStorage` | Yes** | Storage connection string (local/dev or classic config) | `DefaultEndpointsProtocol=...` |
| `AzureWebJobsStorage__tableServiceUri` | Optional** | Table endpoint for managed identity auth in Azure | `https://<acct>.table.core.windows.net` |
| `LIMIT_WARNING` | No | Warning threshold in pence | `25000` |
| `LIMIT_CRITICAL` | No | Critical threshold in pence | `10000` |
| `ALERT_FREQUENCY` | No | Send a repeat warning alert every N qualifying transactions | `10` |
| `COMMITMENTS_POT_ID` | No | Pot ID used to reserve monthly commitments when critical | `pot_000...` |
| `COMMITMENTS_SWEEP_ENABLED` | No | Enable monthly commitment sweep when critical (`true` default) | `true` |

\* Required initially. After first successful refresh+persist, storage becomes the source of truth.

\** You need either `AzureWebJobsStorage` _or_ `AzureWebJobsStorage__tableServiceUri` available in the environment where the app runs.

## Local development

1. Create and activate a virtual environment.
2. Install dependencies for your chosen runtime:

```bash
# Core only
pip install -r requirements-core.txt

# Azure Functions runtime
pip install -r requirements-azure.txt

# FastAPI runtime
pip install -r requirements-fastapi.txt

# Everything for local development
pip install -r requirements-dev.txt
```

3. Configure environment variables listed above. For Azure local runtime, put them in `local.settings.json`. For local FastAPI development without Azure Storage, set `STATE_BACKEND=memory`.

### Run with Azure Functions

```bash
func start
```

Webhook URL: `http://localhost:7071/api/monzo_webhook`

### Run with FastAPI

```bash
uvicorn app_fastapi:app --reload --host 0.0.0.0 --port 8000
```

Webhook URL: `http://localhost:8000/monzo_webhook`

### Test webhook endpoint locally

```bash
curl -X POST "http://localhost:8000/monzo_webhook?secret_key=TEST_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"type":"transaction.created","data":{"id":"tx_123","account_id":"acc_000"}}'
```

> Tip: use `X-Webhook-Secret` header validation. Monzo typically authenticates via query-string secret. Keep `ALLOW_QUERY_SECRET=true` unless you have an upstream gateway that injects/validates headers.

## Deployment

### Azure Functions

```bash
func azure functionapp publish <YOUR_APP_NAME>
```

> Monzo webhook compatibility: ensure `ALLOW_QUERY_SECRET=true` unless you have an upstream gateway injecting/validating `X-Webhook-Secret`.

Webhook URL:
- `https://<YOUR_APP_NAME>.azurewebsites.net/api/monzo_webhook`

### Container / generic platforms

Build and run locally:

```bash
docker build -t monzo-balance-bot .
docker run --rm -p 8000:8000 --env-file .env monzo-balance-bot
```

Webhook URL:
- `https://<YOUR_HOST>/monzo_webhook`

This container target can be deployed to Cloud Run, ECS/Fargate, Azure Container Apps, Fly.io, or Kubernetes.


## CI

GitHub Actions runs a 3-job matrix:
- **core-tests** (core modules + webhook service unit tests)
- **azure-adapter-tests** (Azure adapter compile + import checks)
- **fastapi-adapter-tests** (FastAPI adapter compile + route checks)

Workflow file: `.github/workflows/ci.yml`.

Azure deploy workflow note: `.github/workflows/main_monzowatchdog-js.yml` now enforces that one of `AzureWebJobsStorage` or `AzureWebJobsStorage__accountName` is present before deploy. If the app setting is missing, provide repository secret `AZUREWEBJOBSSTORAGE` (connection string) or `AZUREWEBJOBSSTORAGE_ACCOUNTNAME` (RBAC account name).

## Getting a refresh token (one-time helper)

Use `get_token.py` locally to complete OAuth and print a `MONZOREFRESHTOKEN` value:

```bash
MONZO_CLIENT_ID=... MONZO_CLIENT_SECRET=... python get_token.py
```

This opens a browser, receives the callback at `http://localhost:8080/callback`, and logs a token you can store in Key Vault / app settings.

## Operations notes

- **Token rotation:** the function refreshes access tokens automatically and persists them to Table Storage.
- **Concurrency safety:** ETag checks handle simultaneous refresh attempts.
- **Duplicate webhooks:** dedupe is store-backed (`azure_table` or `memory`) to avoid repeated processing in close succession.
- **Alert behavior:** warning alerts trigger on threshold escalation and then periodically; critical alerts trigger on every qualifying transaction while balance remains critical.
- **Critical commitment sweep:** when critical, the bot calculates active monthly scheduled payments and moves that amount to `COMMITMENTS_POT_ID` once per calendar month.

## Security recommendations

- Store `MONZOCLIENTSECRET` and `MONZOREFRESHTOKEN` in Azure Key Vault (or equivalent secret store).
- Prefer managed identity with `AzureWebJobsStorage__tableServiceUri` in production.
- Use a long random `WEBHOOKSECRET` and rotate it periodically.
- If you can terminate/validate webhook auth at an upstream gateway, set `ALLOW_QUERY_SECRET=false` and use header-only auth in the app layer.
- Restrict Function App access and monitoring to trusted operators.

## Troubleshooting

- **401 Unauthorized from webhook:** verify `WEBHOOKSECRET` exactly matches what is sent.
- **No alerts:** check `MONZOACCOUNTID`, threshold settings, and function logs.
- **Refresh failures:** verify client ID/secret and seed refresh token are valid.
- **Storage errors:** confirm table endpoint or connection string configuration and permissions.

## License

Distributed under the MIT License.
