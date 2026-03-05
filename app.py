from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import uuid
from typing import Any, Dict, Optional

from flask import Flask, jsonify, request

import rca_notion_ops as notion_ops
from rca_generator import generate_customer_rca


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
app = Flask(__name__)
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)


def _process_rca(page_id: str, force: bool) -> Dict[str, Any]:
    already_processed = notion_ops.has_existing_customer_rca(page_id)

    logging.info("Processing RCA page_id=%s", page_id)
    blocks = notion_ops.get_page_blocks(page_id)
    rca_text = notion_ops.blocks_to_text(blocks)
    title_hint = notion_ops.build_title_hint(page_id)

    rca_data = generate_customer_rca(rca_text=rca_text, title_hint=title_hint)
    _, child_url = notion_ops.create_customer_rca_child_page(page_id, rca_data)

    # Regenerate on every trigger even if already processed.
    # This supports the "rejected -> updated -> Awaiting QC again" workflow.
    if already_processed or force:
        notion_ops.append_customer_rca_link_only(page_id, child_url, label="Updated Customer-Facing RCA Document")
    else:
        notion_ops.append_customer_rca_section_and_link(page_id, rca_data, child_url)

    notion_ops.set_page_url_property(page_id, notion_ops.CUSTOMER_RCA_DOC_PROP, child_url)
    return {"child_url": child_url, "regenerated": bool(already_processed or force)}


def _log_future_result(job_id: str, fut: concurrent.futures.Future) -> None:
    try:
        result = fut.result()
        logging.info("Job %s completed: %s", job_id, result)
    except Exception:
        logging.exception("Job %s failed", job_id)


def _extract_page_id(payload: Dict[str, Any]) -> Optional[str]:
    candidates = [
        ("page_id",),
        ("pageId",),
        ("data", "page_id"),
        ("data", "pageId"),
        # Notion automations send the page object under "data" with an "id" field.
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


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.post("/notion-webhook")
def notion_webhook():
    payload = request.get_json(silent=True) or {}
    page_id = _extract_page_id(payload)
    if not page_id:
        logging.warning("Webhook missing page_id. Payload keys: %s", list(payload.keys()))
        return jsonify({"ok": False, "error": "missing page_id"}), 400

    force = bool(payload.get("force") is True or str(payload.get("force", "")).lower() == "true")

    job_id = str(uuid.uuid4())
    fut = _executor.submit(_process_rca, page_id, force)
    fut.add_done_callback(lambda f: _log_future_result(job_id, f))

    # Ack immediately so Notion/ngrok doesn't time out.
    return jsonify({"ok": True, "accepted": True, "job_id": job_id, "page_id": page_id}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    # Disable the reloader to avoid duplicate background processing in dev.
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
