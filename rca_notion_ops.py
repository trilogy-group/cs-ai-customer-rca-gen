from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from notion_client import Client as NotionClient


RCA_DATABASE_ID = "18b85e927d3180c3890eceac97a51cb0"
CUSTOMER_RCA_LINK_TEXT = "Customer-Facing RCA Document"
CUSTOMER_RCA_DOC_PROP = "Customer RCA Doc"
CUSTOMER_RCA_VIEW_URL = (
    "https://www.notion.so/trilogy-enterprises/"
    "18b85e927d3180c3890eceac97a51cb0?v=31485e927d3180dfab6e000ced0953a9"
)


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


def _is_customer_rca_link_block(block: Dict[str, Any]) -> bool:
    """True if block is a paragraph containing a Customer-Facing RCA link."""
    if block.get("type") != "paragraph":
        return False
    for rt in (block.get("paragraph", {}).get("rich_text", []) or []):
        plain = (rt.get("plain_text") or "").strip()
        if plain in (CUSTOMER_RCA_LINK_TEXT, "Updated Customer-Facing RCA Document") and (rt.get("href") or ((rt.get("text", {}) or {}).get("link") or {}).get("url")):
            return True
    return False


def _is_customer_rca_section_heading(block: Dict[str, Any]) -> bool:
    """True if block is the 'Customer-Facing RCA Draft' heading."""
    if block.get("type") != "heading_2":
        return False
    text = _plain_text_from_rich_text(block.get("heading_2", {}).get("rich_text", []))
    return text.strip() == "Customer-Facing RCA Draft"


_CUSTOMER_RCA_SECTION_HEADINGS = {"What Happened", "Root Cause", "What We Did", "What We're Doing Long-Term"}


def remove_old_customer_rca_blocks(page_id: str) -> int:
    """Delete all blocks belonging to the customer-facing RCA section from the parent page.

    Walks the page blocks and removes:
      - The "Customer-Facing RCA Draft" heading and everything below it that
        belongs to the generated section (headings, paragraphs, bullets, dividers, links).
      - Any standalone divider + link-paragraph pairs that were appended by
        append_customer_rca_link_only().
      - Review callout blocks inserted by the automation.

    Returns the number of blocks deleted.
    """
    notion = get_notion_client()
    blocks = get_page_blocks(page_id)
    ids_to_delete: List[str] = []
    in_section = False

    for i, b in enumerate(blocks):
        bid = b.get("id", "")
        btype = b.get("type", "")

        if _is_customer_rca_section_heading(b):
            in_section = True
            # Also grab the divider immediately before the heading if present
            if i > 0 and blocks[i - 1].get("type") == "divider":
                prev_id = blocks[i - 1].get("id", "")
                if prev_id and prev_id not in ids_to_delete:
                    ids_to_delete.append(prev_id)
            ids_to_delete.append(bid)
            continue

        if in_section:
            text = ""
            if btype in ("heading_2", "heading_3"):
                text = _plain_text_from_rich_text(b.get(btype, {}).get("rich_text", []))

            if btype == "divider":
                ids_to_delete.append(bid)
                continue
            if btype in ("heading_2", "heading_3") and text.strip() in _CUSTOMER_RCA_SECTION_HEADINGS:
                ids_to_delete.append(bid)
                continue
            if btype in ("paragraph", "bulleted_list_item", "numbered_list_item"):
                ids_to_delete.append(bid)
                continue
            if _is_customer_rca_link_block(b):
                ids_to_delete.append(bid)
                continue
            # Hit a block that doesn't belong to the section \u2014 stop
            in_section = False

        # Standalone link blocks (from append_customer_rca_link_only)
        if not in_section and _is_customer_rca_link_block(b):
            # Also grab the divider immediately before it
            if i > 0 and blocks[i - 1].get("type") == "divider":
                prev_id = blocks[i - 1].get("id", "")
                if prev_id and prev_id not in ids_to_delete:
                    ids_to_delete.append(prev_id)
            ids_to_delete.append(bid)

        # Review callout blocks added by append_customer_rca_link_only
        if not in_section and _is_review_callout_block(b):
            if i > 0 and blocks[i - 1].get("type") == "divider":
                prev_id = blocks[i - 1].get("id", "")
                if prev_id and prev_id not in ids_to_delete:
                    ids_to_delete.append(prev_id)
            ids_to_delete.append(bid)

    for bid in ids_to_delete:
        try:
            notion.blocks.delete(block_id=bid)
        except Exception:
            pass

    return len(ids_to_delete)


def archive_old_customer_rca_child(page_id: str) -> Optional[str]:
    """Archive (soft-delete) the old customer-facing RCA child page.

    Reads the current Customer RCA Doc URL property, extracts the page ID,
    and archives it. Returns the archived page ID or None.
    """
    old_url = ""
    try:
        old_url = get_page_url_property(page_id, CUSTOMER_RCA_DOC_PROP)
    except Exception:
        pass
    if not old_url:
        return None

    # Extract page ID from Notion URL (last 32 hex chars)
    match = re.search(r"([0-9a-f]{32})$", old_url.rstrip("/"))
    if not match:
        return None

    child_page_id = _format_uuid(match.group(1))
    try:
        notion = get_notion_client()
        notion.pages.update(page_id=child_page_id, archived=True)
        return child_page_id
    except Exception:
        return None


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


_REVIEW_CALLOUT_MARKER = "Review Action Required: "


def _is_review_callout_block(block: Dict[str, Any]) -> bool:
    """True if block is the review-action-required callout we insert."""
    if block.get("type") != "callout":
        return False
    text = _plain_text_from_rich_text(block.get("callout", {}).get("rich_text", []))
    return text.startswith(_REVIEW_CALLOUT_MARKER)


def _review_callout_block(child_url: Optional[str] = None) -> Dict[str, Any]:
    """Callout reminding the reviewer to mark 'Customer RCA Ready' in the database view."""
    rich_text: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": {"content": _REVIEW_CALLOUT_MARKER},
            "annotations": {"bold": True},
        },
        {
            "type": "text",
            "text": {"content": "Please review the "},
        },
    ]
    if child_url:
        rich_text.append({
            "type": "text",
            "text": {"content": "Customer-Facing RCA", "link": {"url": child_url}},
            "annotations": {"bold": True, "underline": True},
        })
    else:
        rich_text.append({
            "type": "text",
            "text": {"content": "Customer-Facing RCA"},
            "annotations": {"bold": True},
        })
    rich_text.extend([
        {
            "type": "text",
            "text": {"content": " and once approved, mark the \"Customer RCA Ready\" checkbox in the "},
        },
        {
            "type": "text",
            "text": {"content": "Customer RCA View", "link": {"url": CUSTOMER_RCA_VIEW_URL}},
            "annotations": {"bold": True, "underline": True},
        },
        {
            "type": "text",
            "text": {"content": "."},
        },
    ])
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "icon": {"type": "emoji", "emoji": "\u26a0\ufe0f"},
            "color": "yellow_background",
            "rich_text": rich_text,
        },
    }


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
    blocks.append(_review_callout_block(child_url))
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
