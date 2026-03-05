"""
AWS Lambda handler for the RCA automation webhook.

Uses a two-phase pattern to work within API Gateway's 30-second limit:
  1. API Gateway invokes this handler synchronously (phase = "gateway").
     It validates the payload, then re-invokes *itself* asynchronously
     (InvocationType=Event) and returns 200 immediately.
  2. The async invocation runs with phase = "process" and does the
     actual RCA generation (OpenAI + Notion), which can take 60-90s.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

import boto3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SECRET_NAME = os.environ.get("SECRET_NAME", "rca-automation/api-keys")
FUNCTION_NAME = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "rca-automation-webhook")

_cached_secrets: Optional[Dict[str, str]] = None


def _get_secrets() -> Dict[str, str]:
    """Fetch and cache secrets from AWS Secrets Manager."""
    global _cached_secrets
    if _cached_secrets is not None:
        return _cached_secrets

    client = boto3.client("secretsmanager")
    resp = client.get_secret_value(SecretId=SECRET_NAME)
    _cached_secrets = json.loads(resp["SecretString"])
    return _cached_secrets


def _extract_page_id(payload: Dict[str, Any]) -> Optional[str]:
    candidates = [
        ("page_id",),
        ("pageId",),
        ("data", "page_id"),
        ("data", "pageId"),
        ("data", "id"),
        ("page", "id"),
        ("data", "page", "id"),
    ]
    for path in candidates:
        cur: Any = payload
        ok = True
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                ok = False
                break
            cur = cur[key]
        if ok and isinstance(cur, str) and cur.strip():
            return cur.strip()
    return None


def _process_rca(page_id: str, force: bool) -> Dict[str, Any]:
    """Run the full RCA generation pipeline."""
    import rca_notion_ops as notion_ops
    from rca_generator import generate_customer_rca

    already_processed = notion_ops.has_existing_customer_rca(page_id)

    logger.info("Processing RCA page_id=%s already_processed=%s force=%s", page_id, already_processed, force)

    # Clean up old artifacts before reading content for generation
    if already_processed:
        archived_id = notion_ops.archive_old_customer_rca_child(page_id)
        logger.info("Archived old child page: %s", archived_id)
        deleted_count = notion_ops.remove_old_customer_rca_blocks(page_id)
        logger.info("Removed %d old customer-RCA blocks from parent", deleted_count)

    blocks = notion_ops.get_page_blocks(page_id)
    rca_text = notion_ops.blocks_to_text(blocks)
    title_hint = notion_ops.build_title_hint(page_id)

    rca_data = generate_customer_rca(rca_text=rca_text, title_hint=title_hint)
    _, child_url = notion_ops.create_customer_rca_child_page(page_id, rca_data)

    notion_ops.append_customer_rca_section_and_link(page_id, rca_data, child_url)
    notion_ops.set_page_url_property(page_id, notion_ops.CUSTOMER_RCA_DOC_PROP, child_url)
    return {"child_url": child_url, "regenerated": bool(already_processed or force)}


def _parse_body(event: Dict[str, Any]) -> Dict[str, Any]:
    body_str = event.get("body", "{}")
    if event.get("isBase64Encoded"):
        import base64
        body_str = base64.b64decode(body_str).decode("utf-8")
    try:
        return json.loads(body_str) if isinstance(body_str, str) else (body_str or {})
    except (json.JSONDecodeError, TypeError):
        return {}


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda entry point.

    Phase 1 (gateway): Validate, fire-and-forget async self-invocation, return 200.
    Phase 2 (process): Actually generate the customer-facing RCA.
    """
    phase = event.get("_phase", "gateway")
    logger.info("Handler invoked phase=%s", phase)

    secrets = _get_secrets()
    os.environ["NOTION_API_TOKEN"] = secrets.get("NOTION_API_TOKEN", "")
    os.environ["OPENAI_API_KEY"] = secrets.get("OPENAI_API_KEY", "")

    # --- Phase 2: async worker -------------------------------------------
    if phase == "process":
        page_id = event["page_id"]
        force = event.get("force", False)
        try:
            result = _process_rca(page_id, force)
            logger.info("RCA processed successfully: %s", result)
            return {"ok": True, **result}
        except Exception:
            logger.exception("Failed to process RCA for page_id=%s", page_id)
            return {"ok": False, "error": "processing failed"}

    # --- Phase 1: gateway (immediate ack) --------------------------------
    payload = _parse_body(event)
    page_id = _extract_page_id(payload)
    if not page_id:
        logger.warning("Missing page_id. Payload keys: %s", list(payload.keys()) if isinstance(payload, dict) else "N/A")
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"ok": False, "error": "missing page_id"}),
        }

    force = bool(
        payload.get("force") is True
        or str(payload.get("force", "")).lower() == "true"
    )

    # Fire async self-invocation for the heavy work
    lambda_client = boto3.client("lambda")
    async_payload = {
        "_phase": "process",
        "page_id": page_id,
        "force": force,
    }
    lambda_client.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(async_payload).encode("utf-8"),
    )
    logger.info("Async invocation dispatched for page_id=%s", page_id)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"ok": True, "accepted": True, "page_id": page_id}),
    }
