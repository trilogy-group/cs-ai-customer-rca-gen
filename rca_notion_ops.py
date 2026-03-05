from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from notion_client import Client as NotionClient


RCA_DATABASE_ID = "18b85e927d3180c3890eceac97a51cb0"
CUSTOMER_RCA_LINK_TEXT = "Customer-Facing RCA Document"
CUSTOMER_RCA_DOC_PROP = "Customer RCA Doc"


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
    if not token_path:
        token_path = _find_token_file(Path(__file__).resolve().parent, "token-template.json")
    if token_path:
        return json.loads(token_path.read_text(encoding="utf-8"))
    # Fallback: read from environment variables (Lambda / Secrets Manager path)
    import os
    return {
        "NOTION_API_TOKEN": os.environ.get("NOTION_API_TOKEN", ""),
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
    }


def _format_uuid(value: str) -> str:
    compact = value.replace("-", "").strip()
    if len(compact) == 32:
        return f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:]}"
    return value


def get_notion_client() -> NotionClient:
    tokens = load_tokens()
    api_key = tokens.get("NOTION_API_TOKEN")
    if not api_key:
        raise RuntimeError("NOTION_API_TOKEN missing from token.json")
    return NotionClient(auth=api_key)


def get_database_schema(database_id: str = RCA_DATABASE_ID) -> Dict[str, Any]:
    notion = get_notion_client()
    return notion.databases.retrieve(database_id=_format_uuid(database_id)).get("properties", {})


def query_database_first_page(database_id: str = RCA_DATABASE_ID) -> Optional[Dict[str, Any]]:
    notion = get_notion_client()
    db = notion.databases.retrieve(database_id=_format_uuid(database_id))
    parent_ids = {_format_uuid(database_id)}
    for ds in db.get("data_sources") or []:
        if isinstance(ds, dict) and ds.get("id"):
            parent_ids.add(ds["id"])

    cursor: Optional[str] = None
    while True:
        resp = notion.search(
            query="",
            filter={"property": "object", "value": "page"},
            sort={"direction": "descending", "timestamp": "last_edited_time"},
            start_cursor=cursor,
            page_size=20,
        )
        for page in resp.get("results", []):
            parent = page.get("parent") or {}
            if parent.get("database_id") in parent_ids or parent.get("data_source_id") in parent_ids:
                return page
        if not resp.get("has_more"):
            return None
        cursor = resp.get("next_cursor")


def get_page_title(page_id: str) -> str:
    notion = get_notion_client()
    page = notion.pages.retrieve(page_id=_format_uuid(page_id))
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            title = prop.get("title", [])
            if title:
                return title[0].get("plain_text", "").strip() or "Untitled Incident"
    return "Untitled Incident"


def get_page_blocks(page_id: str) -> List[Dict[str, Any]]:
    notion = get_notion_client()
    block_id = _format_uuid(page_id)
    out: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        resp = notion.blocks.children.list(block_id=block_id, start_cursor=cursor)
        out.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return out


def _plain_text_from_rich_text(rich_text: Iterable[Dict[str, Any]]) -> str:
    return "".join(rt.get("plain_text", "") for rt in rich_text or [])


def blocks_to_text(blocks: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for b in blocks:
        btype = b.get("type")
        payload = b.get(btype, {}) if btype else {}
        if btype in {"paragraph", "heading_1", "heading_2", "heading_3"}:
            text = _plain_text_from_rich_text(payload.get("rich_text", []))
            if btype == "heading_1":
                parts.append(f"# {text}")
            elif btype == "heading_2":
                parts.append(f"## {text}")
            elif btype == "heading_3":
                parts.append(f"### {text}")
            else:
                parts.append(text)
        elif btype == "bulleted_list_item":
            text = _plain_text_from_rich_text(payload.get("rich_text", []))
            parts.append(f"- {text}")
        elif btype == "numbered_list_item":
            text = _plain_text_from_rich_text(payload.get("rich_text", []))
            parts.append(f"1. {text}")
        elif btype == "to_do":
            text = _plain_text_from_rich_text(payload.get("rich_text", []))
            parts.append(f"- [ ] {text}")
        elif btype == "toggle":
            text = _plain_text_from_rich_text(payload.get("rich_text", []))
            parts.append(text)
        elif btype == "quote":
            text = _plain_text_from_rich_text(payload.get("rich_text", []))
            parts.append(f"> {text}")
        elif btype == "callout":
            text = _plain_text_from_rich_text(payload.get("rich_text", []))
            parts.append(text)
        elif btype == "divider":
            parts.append("---")
        else:
            continue
    return "\n".join(p for p in parts if p is not None)


def _rich_text_contains_link_text(block: Dict[str, Any], needle: str) -> bool:
    btype = block.get("type")
    payload = block.get(btype, {}) if btype else {}
    for rt in payload.get("rich_text", []) or []:
        if (rt.get("plain_text") or "") == needle and rt.get("href"):
            return True
        text = (rt.get("text", {}) or {}).get("content", "")
        if text == needle and ((rt.get("text", {}) or {}).get("link") or {}).get("url"):
            return True
    return False


def has_existing_customer_rca(page_id: str) -> bool:
    # Fast path: if the database URL property is already set, consider it processed.
    try:
        url = get_page_url_property(page_id, CUSTOMER_RCA_DOC_PROP)
        if url:
            return True
    except Exception:
        pass

    for b in get_page_blocks(page_id):
        if b.get("type") == "paragraph" and _rich_text_contains_link_text(b, CUSTOMER_RCA_LINK_TEXT):
            return True
    return False


def get_page_url_property(page_id: str, prop_name: str) -> str:
    notion = get_notion_client()
    page = notion.pages.retrieve(page_id=_format_uuid(page_id))
    props = page.get("properties", {}) or {}
    prop = props.get(prop_name) or {}
    if (prop.get("type") or "") == "url":
        return (prop.get("url") or "").strip()
    return ""


def set_page_url_property(page_id: str, prop_name: str, url: str) -> None:
    notion = get_notion_client()
    notion.pages.update(
        page_id=_format_uuid(page_id),
        properties={
            prop_name: {"url": url},
        },
    )


def _strip_ticket_numbers(title: str) -> str:
    cleaned = re.sub(r"\b[A-Z]{2,10}-\d+\b", "", title).strip()
    cleaned = re.sub(r"\b\d{5,}\b", "", cleaned).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -\u2013\u2014")
    return cleaned or "Untitled Incident"


def _to_rich_text(content: str, link_url: Optional[str] = None, bold: bool = False) -> List[Dict[str, Any]]:
    text_obj: Dict[str, Any] = {"content": content}
    if link_url:
        text_obj["link"] = {"url": link_url}
    rt: Dict[str, Any] = {"type": "text", "text": text_obj}
    if bold:
        rt["annotations"] = {"bold": True}
    return [rt]


def _heading_block(text: str, level: int = 2) -> Dict[str, Any]:
    key = f"heading_{level}"
    return {"object": "block", "type": key, key: {"rich_text": _to_rich_text(text)}}


def _paragraph_block(text: str) -> Dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _to_rich_text(text)}}


def _bullets_block(items: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in items:
        out.append(
            {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _to_rich_text(item)},
            }
        )
    return out


def create_customer_rca_child_page(parent_page_id: str, rca_data: Dict[str, Any]) -> Tuple[str, str]:
    notion = get_notion_client()
    title = rca_data.get("title") or "RCA - Untitled Incident"
    children: List[Dict[str, Any]] = []
    children.append(_heading_block("What Happened", 2))
    children.append(_paragraph_block(rca_data.get("what_happened") or "No information available at this time."))
    children.append(_heading_block("Root Cause", 2))
    children.append(_paragraph_block(rca_data.get("root_cause") or "No information available at this time."))
    children.append(_heading_block("What We Did", 2))
    children.extend(_bullets_block(rca_data.get("what_we_did") or ["No information available at this time."]))
    children.append(_heading_block("What We're Doing Long-Term", 2))
    children.extend(_bullets_block(rca_data.get("what_were_doing_long_term") or ["No information available at this time."]))

    page = notion.pages.create(
        parent={"page_id": _format_uuid(parent_page_id)},
        properties={"title": [{"type": "text", "text": {"content": title}}]},
        children=children,
    )
    return page.get("id", ""), page.get("url", "")


def append_customer_rca_section_and_link(page_id: str, rca_data: Dict[str, Any], child_url: str) -> None:
    notion = get_notion_client()
    blocks: List[Dict[str, Any]] = [{"object": "block", "type": "divider", "divider": {}}]
    blocks.append(_heading_block("Customer-Facing RCA Draft", 2))

    blocks.append(_heading_block("What Happened", 2))
    blocks.append(_paragraph_block(rca_data.get("what_happened") or "No information available at this time."))
    blocks.append(_heading_block("Root Cause", 2))
    blocks.append(_paragraph_block(rca_data.get("root_cause") or "No information available at this time."))
    blocks.append(_heading_block("What We Did", 2))
    blocks.extend(_bullets_block(rca_data.get("what_we_did") or ["No information available at this time."]))
    blocks.append(_heading_block("What We're Doing Long-Term", 2))
    blocks.extend(_bullets_block(rca_data.get("what_were_doing_long_term") or ["No information available at this time."]))

    blocks.append({"object": "block", "type": "divider", "divider": {}})
    blocks.append(
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": _to_rich_text(CUSTOMER_RCA_LINK_TEXT, link_url=child_url, bold=True)},
        }
    )
    notion.blocks.children.append(block_id=_format_uuid(page_id), children=blocks)


def append_customer_rca_link_only(page_id: str, child_url: str, label: str = CUSTOMER_RCA_LINK_TEXT) -> None:
    notion = get_notion_client()
    blocks: List[Dict[str, Any]] = [{"object": "block", "type": "divider", "divider": {}}]
    blocks.append(
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": _to_rich_text(label, link_url=child_url, bold=True)},
        }
    )
    notion.blocks.children.append(block_id=_format_uuid(page_id), children=blocks)


def build_title_hint(page_id: str) -> str:
    title = get_page_title(page_id)
    return _strip_ticket_numbers(title)
