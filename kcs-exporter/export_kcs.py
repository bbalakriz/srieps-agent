#!/usr/bin/env python3
"""
Export Red Hat KCS articles for offline ingestion into Milvus.

The RH KCS Solr API caps pagination at ~300 results per query, so a single
query cannot retrieve all 7000+ OCP/RHOAI articles. This script works around
that by chunking the query into quarterly date ranges, each of which returns
fewer than 300 results, and deduplicating by article ID.

Run this BEFORE going air-gapped, with valid RH API credentials.

Usage:
  export RH_API_OFFLINE_TOKEN=<your_token>
  python export_kcs.py
  python export_kcs.py --output kcs-articles.ndjson --products ocp,rhoai
"""

import os
import json
import asyncio
import argparse
from datetime import datetime, timedelta, timezone

import httpx
from bs4 import BeautifulSoup

SSO_URL = "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
KCS_URL = "https://access.redhat.com/hydra/rest/search/v2/kcs"

PRODUCT_FILTERS = {
    "ocp": [
        "OpenShift Container Platform",
        "Red Hat OpenShift",
        "OpenShift",
    ],
    "rhoai": [
        "Red Hat OpenShift AI",
        "Red Hat OpenShift Data Science",
        "RHOAI",
        "RHODS",
    ],
    "rhel": [
        "Red Hat Enterprise Linux",
        "RHEL",
    ],
}

CONTENT_FIELDS = ",".join([
    "id", "allTitle", "view_uri", "standard_product",
    "issue", "solution_resolution", "solution_rootcause",
    "documentKind", "lastModifiedDate", "language",
])

PAGE_SIZE = 100
# KCS API caps Solr pagination at ~300 results; use date-range chunks to stay under
MAX_PAGE_OFFSET = 200

# OCP 4.x launched May 2019 — pre-2019 articles are OCP 2.x/3.x era, not relevant
START_YEAR = 2019
TOKEN_REFRESH_BUFFER_SECONDS = 60


def quarters_between(start_year: int, end_dt: datetime):
    """Yield (from_str, to_str) pairs for each quarter from start_year to end_dt."""
    months = [(1, "01-01", "03-31"), (2, "04-01", "06-30"),
              (3, "07-01", "09-30"), (4, "10-01", "12-31")]
    year = start_year
    while True:
        for _, m_start, m_end in months:
            from_str = f"{year}-{m_start}T00:00:00Z"
            to_str = f"{year}-{m_end}T23:59:59Z"
            yield from_str, to_str
            if year >= end_dt.year and _ >= (end_dt.month - 1) // 3 + 1:
                return
        year += 1


def strip_html(text: str) -> str:
    if not text or "<" not in text:
        return text or ""
    return BeautifulSoup(text, "html.parser").get_text(separator=" ", strip=True)


class RHAuth:
    def __init__(self, offline_token: str):
        self.offline_token = offline_token
        self.access_token: str | None = None
        self.expiry: datetime | None = None

    async def token(self) -> str:
        now = datetime.now(timezone.utc)
        if self.access_token and self.expiry and now < self.expiry:
            return self.access_token
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(SSO_URL, data={
                "grant_type": "refresh_token",
                "client_id": "rhsm-api",
                "refresh_token": self.offline_token,
            }, headers={"Content-Type": "application/x-www-form-urlencoded"})
            resp.raise_for_status()
            data = resp.json()
            self.access_token = data["access_token"]
            self.expiry = now + timedelta(seconds=data["expires_in"] - TOKEN_REFRESH_BUFFER_SECONDS)
            return self.access_token


async def fetch_page(client: httpx.AsyncClient, auth: RHAuth,
                     q: str, date_from: str, date_to: str, start: int) -> dict:
    token = await auth.token()
    date_fq = f"lastModifiedDate:[{date_from}%20TO%20{date_to}]"
    expression = (
        "sort=lastModifiedDate%20ASC"
        "&fq=documentKind%3A(%22Article%22%20OR%20%22Solution%22)"
        "%20AND%20accessState%3A(%22active%22)"
        "%20AND%20language%3Aen"
        f"&fq={date_fq}"
        f"&fl={CONTENT_FIELDS}"
        "&showRetired=false"
    )
    resp = await client.post(KCS_URL, json={
        "q": q,
        "rows": PAGE_SIZE,
        "start": start,
        "expression": expression,
        "clientName": "kcs-exporter",
    }, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }, timeout=60)
    resp.raise_for_status()
    return resp.json()


def build_product_query(product_keys: list[str]) -> str:
    names = []
    for key in product_keys:
        if key in PRODUCT_FILTERS:
            names.extend(PRODUCT_FILTERS[key])
    quoted = " OR ".join(f'"{n}"' for n in names)
    return f"standard_product:({quoted})"


def normalize_record(doc: dict) -> dict | None:
    issue = strip_html(doc.get("issue", ""))
    resolution = strip_html(doc.get("solution_resolution", ""))
    root_cause = strip_html(doc.get("solution_rootcause", ""))
    if not any([issue, resolution, root_cause]):
        return None
    return {
        "id": doc.get("id", ""),
        "title": strip_html(doc.get("allTitle", "")),
        "view_uri": doc.get("view_uri", ""),
        "product": doc.get("standard_product", ""),
        "issue": issue,
        "resolution": resolution,
        "root_cause": root_cause,
        "last_modified": doc.get("lastModifiedDate", ""),
    }


async def export(offline_token: str, output_path: str, product_keys: list[str]):
    auth = RHAuth(offline_token)
    q = build_product_query(product_keys)
    print(f"Product query: {q}")
    print(f"Splitting into quarterly date ranges to stay under API pagination limit...")

    now = datetime.now(timezone.utc)
    seen_ids: set[str] = set()
    written = 0
    skipped_no_content = 0
    skipped_duplicate = 0
    quarter_count = 0

    with open(output_path, "w") as f:
        f.write(json.dumps({"_meta": {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "products": product_keys,
        }}) + "\n")

        async with httpx.AsyncClient(timeout=60) as client:
            for date_from, date_to in quarters_between(START_YEAR, now):
                # Quick probe: get total for this quarter
                first = await fetch_page(client, auth, q, date_from, date_to, start=0)
                total_in_quarter = first.get("response", {}).get("numFound", 0)
                if total_in_quarter == 0:
                    continue

                quarter_count += 1
                q_written = 0

                # Process all pages within this quarter
                start = 0
                current_page = first
                while True:
                    docs = current_page.get("response", {}).get("docs", [])
                    if not docs:
                        break

                    for doc in docs:
                        art_id = doc.get("id", "")
                        if art_id in seen_ids:
                            skipped_duplicate += 1
                            continue
                        seen_ids.add(art_id)

                        record = normalize_record(doc)
                        if record:
                            f.write(json.dumps(record) + "\n")
                            written += 1
                            q_written += 1
                        else:
                            skipped_no_content += 1

                    start += PAGE_SIZE
                    if start > MAX_PAGE_OFFSET or start >= total_in_quarter:
                        break

                    current_page = await fetch_page(client, auth, q, date_from, date_to, start)

                print(
                    f"  {date_from[:7]}: {total_in_quarter:4d} found, {q_written:3d} new | "
                    f"total so far: {written} articles",
                    flush=True,
                )

    print(f"\nDone. Exported {written} articles across {quarter_count} quarters.")
    print(f"Skipped: {skipped_no_content} (no content), {skipped_duplicate} (duplicates)")
    print(f"Output: {output_path}  ({os.path.getsize(output_path)/1024/1024:.1f} MB)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="kcs-articles.ndjson")
    parser.add_argument("--products", default="ocp,rhoai")
    args = parser.parse_args()

    token = os.getenv("RH_API_OFFLINE_TOKEN")
    if not token:
        raise SystemExit("ERROR: Set RH_API_OFFLINE_TOKEN environment variable")

    product_keys = [p.strip() for p in args.products.split(",") if p.strip()]
    unknown = [p for p in product_keys if p not in PRODUCT_FILTERS]
    if unknown:
        raise SystemExit(f"ERROR: Unknown product keys: {unknown}. Valid: {list(PRODUCT_FILTERS)}")

    asyncio.run(export(token, args.output, product_keys))
