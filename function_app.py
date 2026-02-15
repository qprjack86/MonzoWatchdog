import logging
import uuid

import azure.functions as func

from core.monzo_client import MonzoClient, build_session
from core.settings import load_settings
from core.webhook_service import WebhookService
from stores.factory import build_state_store


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

settings = load_settings()
store = build_state_store(settings)
monzo_client = MonzoClient(build_session(), settings.request_timeout)
service = WebhookService(settings, monzo_client, store)


@app.route(route="monzo_webhook", methods=["POST"])
def monzo_webhook(req: func.HttpRequest) -> func.HttpResponse:
    correlation_id = req.headers.get("X-Correlation-ID") or str(uuid.uuid4())
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON", status_code=400, headers={"X-Correlation-ID": correlation_id})

    result = service.handle_webhook(
        headers=dict(req.headers),
        query=dict(req.params),
        body=body,
        correlation_id=correlation_id,
    )
    return func.HttpResponse(result.body, status_code=result.status_code, headers={"X-Correlation-ID": correlation_id})


@app.route(route="health", methods=["GET"])
def health(_req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse('{"status":"ok"}', status_code=200, mimetype="application/json")
