# Monzo Balance Bot 🐕

Monzo Balance Bot is a serverless Azure Function that listens to Monzo transaction webhooks and posts balance warnings back to your Monzo feed when your spendable balance drops below configurable thresholds.

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

## Architecture

- **Runtime:** Azure Functions (Python programming model v2)
- **State & token store:** Azure Table Storage (`monzotokens` table)
- **External API:** Monzo API (`/oauth2/token`, `/balance`, `/feed`, `/transactions/{id}`)
- **Auth model:** Monzo OAuth2 refresh token + managed identity or storage connection string

## Prerequisites

- Python 3.10+
- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- Azure subscription + Function App
- Azure Storage account (Table service)
- Monzo Developer app credentials (Client ID / Client Secret)

## Configuration

Set these as Function App settings (or in `local.settings.json` when running locally):

| Variable | Required | Description | Example |
|---|---|---|---|
| `MONZOCLIENTID` | Yes | Monzo OAuth2 client ID | `oauth2client_000...` |
| `MONZOCLIENTSECRET` | Yes | Monzo OAuth2 client secret | `mnzpub...` |
| `MONZOACCOUNTID` | Yes | The Monzo account ID to monitor | `acc_000...` |
| `MONZOREFRESHTOKEN` | Yes* | Initial fallback refresh token used when storage is empty | `eyJ...` |
| `WEBHOOKSECRET` | Yes | Shared secret used to verify incoming webhook calls | `a1b2c3...` |
| `AzureWebJobsStorage` | Yes** | Storage connection string (local/dev or classic config) | `DefaultEndpointsProtocol=...` |
| `AzureWebJobsStorage__tableServiceUri` | Optional** | Table endpoint for managed identity auth in Azure | `https://<acct>.table.core.windows.net` |
| `LIMIT_WARNING` | No | Warning threshold in pence | `25000` |
| `LIMIT_CRITICAL` | No | Critical threshold in pence | `10000` |
| `ALERT_FREQUENCY` | No | Send a repeat alert every N qualifying transactions | `10` |

\* Required initially. After first successful refresh+persist, storage becomes the source of truth.

\** You need either `AzureWebJobsStorage` _or_ `AzureWebJobsStorage__tableServiceUri` available in the environment where the app runs.

## Local development

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create `local.settings.json` with the settings listed above.
4. Start Functions runtime:

```bash
func start
```

5. Test webhook endpoint locally:

```bash
curl -X POST "http://localhost:7071/api/monzo_webhook?secret_key=TEST_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"type":"transaction.created","data":{"id":"tx_123","account_id":"acc_000"}}'
```

> Tip: once your Monzo webhook is configured to send `X-Webhook-Secret`, prefer header validation and stop relying on `secret_key` query parameter.

## Deployment

Publish to Azure Functions:

```bash
func azure functionapp publish <YOUR_APP_NAME>
```

After deployment:

1. Confirm app settings are present in Azure.
2. Register/update your Monzo webhook URL to point at:
   - `https://<YOUR_APP_NAME>.azurewebsites.net/api/monzo_webhook`
3. Ensure Monzo sends your shared secret (preferred via `X-Webhook-Secret`).

## Getting a refresh token (one-time helper)

Use `get_token.py` locally to complete OAuth and print a `MONZOREFRESHTOKEN` value:

```bash
MONZO_CLIENT_ID=... MONZO_CLIENT_SECRET=... python get_token.py
```

This opens a browser, receives the callback at `http://localhost:8080/callback`, and logs a token you can store in Key Vault / app settings.

## Operations notes

- **Token rotation:** the function refreshes access tokens automatically and persists them to Table Storage.
- **Concurrency safety:** ETag checks handle simultaneous refresh attempts.
- **Duplicate webhooks:** in-memory TTL dedupe prevents repeated processing in close succession.
- **Alert behavior:** alerts trigger on threshold escalation and then periodically while still below threshold.

## Security recommendations

- Store `MONZOCLIENTSECRET` and `MONZOREFRESHTOKEN` in Azure Key Vault (or equivalent secret store).
- Prefer managed identity with `AzureWebJobsStorage__tableServiceUri` in production.
- Use a long random `WEBHOOKSECRET` and rotate it periodically.
- Restrict Function App access and monitoring to trusted operators.

## Troubleshooting

- **401 Unauthorized from webhook:** verify `WEBHOOKSECRET` exactly matches what is sent.
- **No alerts:** check `MONZOACCOUNTID`, threshold settings, and function logs.
- **Refresh failures:** verify client ID/secret and seed refresh token are valid.
- **Storage errors:** confirm table endpoint or connection string configuration and permissions.

## License

Distributed under the MIT License.
