import logging

import azure.functions as func

from core.monzo_client import MonzoClient, build_session
from core.settings import load_settings
from core.webhook_service import WebhookService
from stores.azure_table_store import AzureTableStore


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

settings = load_settings()
store = AzureTableStore(settings)
monzo_client = MonzoClient(build_session(), settings.request_timeout)
service = WebhookService(settings, monzo_client, store)


@app.route(route="monzo_webhook", methods=["POST"])
def monzo_webhook(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON", status_code=400)

    result = service.handle_webhook(
        headers=dict(req.headers),
        query=dict(req.params),
        body=body,
    )
    return func.HttpResponse(result.body, status_code=result.status_code)
