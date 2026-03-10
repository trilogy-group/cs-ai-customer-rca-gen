from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import OpenAI


PLACEHOLDER = "_No information available at this time._"
SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "customer_rca_system_prompt.txt"


def _find_token_file(start: Path, filename: str = "token.json", max_up: int = 5) -> Optional[Path]:
    current = start
    for _ in range(max_up + 1):
        candidate = current / filename
        if candidate.exists():
            return candidate
        if current.parent == current:
            break
        current = current.parent
    return None


def load_tokens() -> Dict[str, Any]:
    token_path = _find_token_file(Path(__file__).resolve().parent, "token.json")
    if token_path:
        return json.loads(token_path.read_text(encoding="utf-8"))
    # Fallback: read from environment variables (Lambda / Secrets Manager path)
    import os
    return {
        "NOTION_API_TOKEN": os.environ.get("NOTION_API_TOKEN", ""),
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
    }


def _coerce_str(value: Any) -> str:
    if value is None:
        return PLACEHOLDER
    s = str(value).strip()
    return s if s else PLACEHOLDER


def _coerce_bullets(value: Any, min_items: int, max_items: int) -> List[str]:
    items: List[str] = []
    if isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
    elif isinstance(value, str) and value.strip():
        items = [line.strip("- ").strip() for line in value.splitlines() if line.strip()]

    if not items:
        items = [PLACEHOLDER]
    items = items[:max_items]
    while len(items) < min_items:
        items.append(PLACEHOLDER)
    return items


def _normalize_title(title_hint: str) -> str:
    hint = (title_hint or "").strip() or "Untitled Incident"
    return f"RCA - {hint}"


def _system_prompt() -> str:
    if SYSTEM_PROMPT_PATH.exists():
        text = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        return text.replace("{{PLACEHOLDER}}", PLACEHOLDER)

    return (
        "Generate a customer-facing Root Cause Analysis (RCA) for customers.\n"
        "Keep details high-level and non-technical. Do not state any commitments.\n"
        "Report only facts present in the RCA content.\n\n"
        "Return ONLY a JSON object with exactly these keys:\n"
        '{\n'
        '  "title": "RCA - <Relevant Title Excluding JIRA Ticket Numbers>",\n'
        '  "what_happened": "<text>",\n'
        '  "root_cause": "<text>",\n'
        '  "what_we_did": ["<bullet 1>", "<bullet 2>", "<bullet 3>"],\n'
        '  "what_were_doing_long_term": ["<bullet 1>", "<bullet 2>"]\n'
        "}\n\n"
        f"If information is missing, use exactly: {PLACEHOLDER}\n"
        "Ensure what_we_did has 3-5 bullets, and what_were_doing_long_term has 2-5 bullets.\n"
        "Title must start with 'RCA - '.\n"
    )


def generate_customer_rca(rca_text: str, title_hint: str) -> Dict[str, Any]:
    tokens = load_tokens()
    api_key = tokens.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY missing from token.json")

    client = OpenAI(api_key=api_key)
    desired_title = _normalize_title(title_hint)

    resp = client.chat.completions.create(
        model="gpt-5.2",
        response_format={"type": "json_object"},
        temperature=0.2,
        messages=[
            {"role": "system", "content": _system_prompt()},
            {
                "role": "user",
                "content": (
                    f"Desired title: {desired_title}\n\n"
                    "RCA content follows.\n"
                    "-----\n"
                    f"{rca_text}\n"
                    "-----\n"
                ),
            },
        ],
    )

    raw = (resp.choices[0].message.content or "").strip()
    data = json.loads(raw)

    out: Dict[str, Any] = {}
    raw_title = _coerce_str(data.get("title"))
    # Strip placeholder text that may appear as a date suffix (e.g. "RCA - Foo - _No information...")
    raw_title = re.sub(r"\s*-?\s*_No information available at this time\._\s*$", "", raw_title).rstrip(" -")
    out["title"] = raw_title if raw_title.startswith("RCA - ") else desired_title
    out["what_happened"] = _coerce_str(data.get("what_happened"))
    out["root_cause"] = _coerce_str(data.get("root_cause"))
    out["what_we_did"] = _coerce_bullets(data.get("what_we_did"), min_items=3, max_items=5)
    out["what_were_doing_long_term"] = _coerce_bullets(
        data.get("what_were_doing_long_term"), min_items=2, max_items=5
    )
    return out
