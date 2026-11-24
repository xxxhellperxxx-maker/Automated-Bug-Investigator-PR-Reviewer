"""Необязательный приём GitHub-вебхуков (когда появится публичный адрес). Без него работает опрос issues."""

import hashlib
import hmac
import json

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.watchers.issues import incident_from_issue

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict[str, str]:
    c = request.app.state.container
    secret = c.settings.github_webhook_secret
    if not secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Webhook disabled: GITHUB_WEBHOOK_SECRET is not set"
        )
    body = await request.body()
    if not verify_signature(secret, body, x_hub_signature_256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid signature")
    if x_github_event != "issues":
        return {"status": "ignored", "reason": "event"}

    payload = json.loads(body)
    issue, repo = payload.get("issue") or {}, (payload.get("repository") or {}).get("full_name", "")
    labels = {label.get("name") for label in issue.get("labels", [])}
    if payload.get("action") not in ("opened", "labeled") or c.settings.issue_label not in labels:
        return {"status": "ignored", "reason": "action or label"}
    watched = {r.lower() for r in c.settings.watched_repo_list}
    if watched and repo.lower() not in watched:
        return {"status": "ignored", "reason": "repository is not watched"}
    if not await c.store.mark_issue_seen(repo, issue["number"]):
        return {"status": "ignored", "reason": "already processed"}
    run_id = await c.runner.start(incident_from_issue(repo, issue, await c.runner.default_chat_id()))
    return {"status": "started", "id": run_id}
