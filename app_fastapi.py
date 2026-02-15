import uuid

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from core.monzo_client import MonzoClient, build_session
from core.settings import load_settings
from core.webhook_service import WebhookService
from stores.factory import build_state_store


app = FastAPI(title="Monzo Balance Bot")

settings = load_settings()
store = build_state_store(settings)
monzo_client = MonzoClient(build_session(), settings.request_timeout)
service = WebhookService(settings, monzo_client, store)


@app.post("/monzo_webhook")
async def monzo_webhook(request: Request) -> PlainTextResponse:
    correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("Invalid JSON", status_code=400, headers={"X-Correlation-ID": correlation_id})

    result = service.handle_webhook(
        headers=dict(request.headers),
        query=dict(request.query_params),
        body=body,
        correlation_id=correlation_id,
    )
    return PlainTextResponse(result.body, status_code=result.status_code, headers={"X-Correlation-ID": correlation_id})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
