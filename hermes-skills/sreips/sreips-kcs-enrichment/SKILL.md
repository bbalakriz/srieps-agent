---
name: sreips-kcs-enrichment
description: Red Hat KCS search and citation rules for SREIPS incident analysis.
version: 1.0.0
metadata:
  hermes:
    tags: [SREIPS, KCS, RedHat]
---

# KCS enrichment

## When to use

After initial triage, before final RCA JSON.

## Tools

Use **rh_kcs_mcp** `search_kcs` with queries built from:

- event reason (e.g. CrashLoopBackOff)
- resource kind
- error keywords from event message

Use `get_kcs` only for top 1 to 3 article IDs.

## Offline vs online

- Offline: Milvus store `kcs_vector_id` via LlamaStack (no live API).
- Online: access.redhat.com (requires token on server).

## Citation

Every KCS hit in output must include `id`, `title`, and `view_uri` in `kcs_articles`.
