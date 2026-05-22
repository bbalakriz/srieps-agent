#!/usr/bin/env python3
"""
Ingest exported KCS articles into LlamaStack via the vector_stores file-upload API.

Uses the same path as the docling RAG pipeline:
  files.create() + vector_stores.files.create()
Searchable via client.vector_stores.search() (/v1/vector_stores/{id}/search).

Run this after export_kcs.py, pointing at your LlamaStack instance.

Usage:
  export LLAMA_STACK_URL=http://lsd-llama-milvus-service:8321
  python ingest_kcs.py --input kcs-articles.ndjson

Options:
  --input            Path to NDJSON file from export_kcs.py (default: kcs-articles.ndjson)
  --llama-stack-url  LlamaStack service URL (or set LLAMA_STACK_URL env var)
  --vector-db-id     Target vector store name (default: kcs_vector_id)
  --embed-model      Provider resource ID of the embedding model
"""

import os
import json
import time
import argparse

from llama_stack_client import LlamaStackClient

KCS_VECTOR_DB_ID = os.getenv("KCS_VECTOR_DB_ID", "kcs_vector_id")
EMBED_MODEL_ID = os.getenv("EMBED_MODEL_ID", "ibm-granite/granite-embedding-125m-english")
MAX_FIELD_CHARS = 8000     # truncate very large fields before upload
MAX_RETRIES = 3
RETRY_BACKOFF = 5          # seconds between retries
CHUNK_TOKENS = 2048        # large chunks to keep most articles in one chunk
CHUNK_OVERLAP = 100        # token overlap between chunks


def to_str(val, *, max_chars: int = MAX_FIELD_CHARS) -> str:
    """Normalize a field that may be a string or list-of-strings, then truncate."""
    if isinstance(val, list):
        text = "\n".join(str(v) for v in val)
    else:
        text = str(val) if val else ""
    return text[:max_chars]


def format_article(record: dict) -> str:
    """
    Format a KCS article as structured plain text with a parseable header.

    The header lines (KCS_ID:, Title:, View_URI:, Product:) are placed first so
    the MCP server can extract them from any chunk returned by a vector search.
    With CHUNK_TOKENS=2048 most articles fit in a single chunk; when splitting
    does occur the MCP server falls back to the chunk that carries the header.
    """
    parts = [
        f"KCS_ID: {record.get('id', '')}",
        f"Title: {to_str(record.get('title', ''))}",
        f"View_URI: {to_str(record.get('view_uri', ''), max_chars=500)}",
        f"Product: {to_str(record.get('product', ''), max_chars=500)}",
        "",
    ]
    if record.get("issue"):
        parts += ["Issue:", to_str(record["issue"]), ""]
    if record.get("resolution"):
        parts += ["Resolution:", to_str(record["resolution"]), ""]
    if record.get("root_cause"):
        parts += ["Root Cause:", to_str(record["root_cause"]), ""]
    return "\n".join(parts)


def ensure_vector_store(client: LlamaStackClient, name: str, embed_model_id: str) -> str:
    """
    Return the UUID for the named vector store, creating it only if needed.
    vector_stores.create() is NOT idempotent — check first to avoid duplicates.
    """
    stores = client.vector_stores.list()
    existing = next((s for s in stores.data if s.name == name), None)
    if existing:
        print(f"Using existing vector store '{existing.id}' (name='{name}')")
        return existing.id

    models = client.models.list()
    embed_model = next(
        (m for m in models if m.provider_resource_id == embed_model_id), None
    )
    if not embed_model:
        available = [m.provider_resource_id for m in models]
        raise SystemExit(
            f"Embedding model '{embed_model_id}' not found in LlamaStack.\n"
            f"Available models: {available}"
        )

    store = client.vector_stores.create(
        name=name,
        extra_body={
            "provider_id": "milvus-remote",
            "embedding_model": embed_model.identifier,
            "embedding_dimension": embed_model.metadata["embedding_dimension"],
        },
    )
    print(f"Created vector store '{store.id}' with model '{embed_model.identifier}'")
    return store.id


def upload_article(client: LlamaStackClient, vector_store_uuid: str, record: dict):
    """Upload a single KCS article as a plain-text file and attach it to the vector store."""
    kcs_id = record.get("id", "")
    content = format_article(record)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            file_obj = client.files.create(
                file=(f"kcs-{kcs_id}.txt", content.encode("utf-8"), "text/plain"),
                purpose="assistants",
            )
            client.vector_stores.files.create(
                vector_store_id=vector_store_uuid,
                file_id=file_obj.id,
                chunking_strategy={
                    "type": "static",
                    "static": {
                        "max_chunk_size_tokens": CHUNK_TOKENS,
                        "chunk_overlap_tokens": CHUNK_OVERLAP,
                    },
                },
            )
            return
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            print(f"\n  Retry {attempt}/{MAX_RETRIES} after error: {e}", flush=True)
            time.sleep(RETRY_BACKOFF * attempt)


def ingest(llama_stack_url: str, input_path: str, vector_db_id: str, embed_model_id: str):
    client = LlamaStackClient(base_url=llama_stack_url)
    vector_store_uuid = ensure_vector_store(client, vector_db_id, embed_model_id)

    total = 0
    skipped = 0
    t_start = time.time()

    with open(input_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if "_meta" in record:
                continue
            if not record.get("id"):
                skipped += 1
                continue

            upload_article(client, vector_store_uuid, record)
            total += 1

            if total % 10 == 0:
                elapsed = time.time() - t_start
                rate = total / elapsed * 60
                print(f"  {total} articles  ({rate:.0f} art/min)   ", end="\r", flush=True)

    elapsed = time.time() - t_start
    print(
        f"\nDone. Ingested {total} articles into '{vector_store_uuid}' "
        f"in {elapsed/60:.1f} min ({skipped} skipped, no id)"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ingest KCS NDJSON into LlamaStack via vector_stores file-upload API"
    )
    parser.add_argument("--input", default="kcs-articles.ndjson")
    parser.add_argument("--llama-stack-url", default=os.getenv("LLAMA_STACK_URL", ""))
    parser.add_argument("--vector-db-id", default=KCS_VECTOR_DB_ID)
    parser.add_argument("--embed-model", default=EMBED_MODEL_ID)
    args = parser.parse_args()

    if not args.llama_stack_url:
        raise SystemExit("ERROR: Set --llama-stack-url or LLAMA_STACK_URL env var")

    ingest(args.llama_stack_url, args.input, args.vector_db_id, args.embed_model)
