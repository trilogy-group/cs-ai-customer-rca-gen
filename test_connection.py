from __future__ import annotations

import json

import rca_notion_ops as notion_ops


def main() -> None:
    print("Testing Notion integration for RCA database...")
    schema = notion_ops.get_database_schema()
    print(f"\nDatabase properties ({len(schema)}):")
    for name, info in schema.items():
        ptype = (info or {}).get("type", "unknown")
        print(f"- {name}: {ptype}")

    page = notion_ops.query_database_first_page()
    if not page:
        print("\nNo pages returned from the database query (is the database empty?).")
        return

    page_id = page.get("id", "")
    title = notion_ops.get_page_title(page_id)
    blocks = notion_ops.get_page_blocks(page_id)
    print("\nSample page:")
    print(f"- page_id: {page_id}")
    print(f"- title: {title}")
    print(f"- blocks: {len(blocks)}")

    preview = notion_ops.blocks_to_text(blocks)[:500]
    print("\nContent preview (first 500 chars):")
    print(preview or "(empty)")


if __name__ == "__main__":
    main()
