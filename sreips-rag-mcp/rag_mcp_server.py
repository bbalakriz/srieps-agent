#!/usr/bin/env python3
"""MCP server exposing enterprise KB search via LlamaStack vector_stores API."""

import os
from typing import Any

from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("SREIPS Enterprise KB RAG")

LLAMA_STACK_URL = os.getenv("LLAMA_STACK_URL", "")
VECTOR_DB_ID = os.getenv("VECTOR_DB_ID", "sreips_vector_id")
_vector_store_uuid: str | None = None


def _resolve_vector_store_uuid() -> str:
    global _vector_store_uuid
    if _vector_store_uuid is not None:
        return _vector_store_uuid
    if not LLAMA_STACK_URL:
        raise RuntimeError("LLAMA_STACK_URL is required")
    from llama_stack_client import LlamaStackClient

    client = LlamaStackClient(base_url=LLAMA_STACK_URL)
    stores = client.vector_stores.list()
    match = next((s for s in stores.data if s.name == VECTOR_DB_ID), None)
    if not match:
        available = [s.name for s in stores.data]
        raise RuntimeError(f"Vector store '{VECTOR_DB_ID}' not found. Available: {available}")
    _vector_store_uuid = match.id
    return _vector_store_uuid


def _chunk_text(item: Any) -> str:
    parts = []
    for c in item.content or []:
        if hasattr(c, "text") and c.text:
            parts.append(c.text)
    return "\n".join(parts)


@mcp.tool()
def search_internal_kb(query: str, top_k: int = 8) -> list[dict]:
    """
    Search the enterprise knowledge base (Milvus via LlamaStack) for OpenShift runbooks and solutions.
    """
    from llama_stack_client import LlamaStackClient

    store_id = _resolve_vector_store_uuid()
    client = LlamaStackClient(base_url=LLAMA_STACK_URL)
    result = client.vector_stores.search(store_id, query=query, max_num_results=min(top_k, 20))
    hits = []
    for item in result.data:
        text = _chunk_text(item)
        meta = getattr(item, "metadata", None) or {}
        title = ""
        source = ""
        if isinstance(meta, dict):
            title = str(meta.get("title") or meta.get("filename") or "")
            source = str(meta.get("source") or meta.get("file_id") or "")
        if not title and text:
            title = text[:120].replace("\n", " ")
        hits.append(
            {
                "title": title,
                "excerpt": text[:2000] if text else "",
                "source": source or VECTOR_DB_ID,
                "score": getattr(item, "score", None),
            }
        )
    return hits


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
