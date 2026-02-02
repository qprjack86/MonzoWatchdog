# Monzo Watchdog 🐕

A serverless Azure Function that monitors your Monzo account in real-time. It tracks your spending habits and sends "Sticky Notification" alerts to your feed when your balance drops below critical thresholds.

![Build Status](https://img.shields.io/badge/status-stable-brightgreen) ![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![Azure](https://img.shields.io/badge/cloud-azure-0078D4)

## Key Features

* **Real-time Webhooks:** Reacts instantly to every transaction.
* **Smart Alerts:**
    * **Amber Alert:** Balance < £250 (Configurable).
    * **Red Alert:** Balance < £100 (Configurable).
* **Sticky Reminders:** Updates the actual transaction notes in your app so you see the warning when checking what you bought.
* **Auto-Healing Auth:** Automatically rotates Monzo OAuth2 tokens and handles race conditions using Azure Table Storage ETags.
* **Idempotency:** Prevents duplicate alerts if Monzo sends the same webhook twice.

## Architecture

* **Runtime:** Azure Functions (Python v2 model).
* **State Store:** Azure Table Storage (stores tokens & last known balance state).
* **Security:** HMAC signature verification (via Header or Query String).

## Setup & Deployment

### 1. Prerequisites
* An Azure Subscription.
* A Monzo Developer Account (to get Client ID/Secret).

### 2. Environment Variables
Configure these in your Azure Function App Settings:

| Variable | Description | Example |
| :--- | :--- | :--- |
| `MONZOCLIENTID` | OAuth2 Client ID from Monzo | `oauth2client_000...` |
| `MONZOCLIENTSECRET` | OAuth2 Client Secret | `mnzpub...` |
| `MONZOACCOUNTID` | The specific Account ID to watch | `acc_000...` |
| `MONZOREFRESHTOKEN` | **Initial** Refresh Token (Backup) | `eyJ...` |
| `WEBHOOKSECRET` | A random strong password for security | `a1b2c3...` |
| `AzureWebJobsStorage` | Connection string for state DB | `DefaultEndpointsProtocol=...` |

**Optional Config:**
* `LIMIT_WARNING`: Warning threshold in pence (Default: 25000 = £250).
* `LIMIT_CRITICAL`: Critical threshold in pence (Default: 10000 = £100).

### 3. Installation

1.  **Deploy to Azure:**
    ```bash
    func azure functionapp publish <YOUR_APP_NAME>
    ```

2.  **Register the Webhook:**
    Edit the `register_webhook.py` script with your credentials and run it locally once to connect Monzo to your new Azure Function.
    ```bash
    python register_webhook.py
    ```

## Local Development

1.  Create a `local.settings.json` file with the environment variables above.
2.  Start the local runtime:
    ```bash
    func start
    ```
3.  Simulate a webhook:
    ```bash
    curl -X POST http://localhost:7071/api/monzo_webhook?secret_key=TEST \
         -H "Content-Type: application/json" \
         -d '{"type": "transaction.created", "data": {"id": "tx_123", "account_id": "acc_000"}}'
    ```

## Maintenance

* **