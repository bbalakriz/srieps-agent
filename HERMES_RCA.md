# Hermes RCA Architecture

This document describes how SREIPS uses the Hermes agent and the Hermes RCA bridge to turn a raw OpenShift event into a structured root cause analysis that is posted back to Slack or Mattermost. It replaces the legacy standalone sreips agent and remediation agent path (that were used in the rhoai-3 and master branches), and is no longer deployed by `bootstrap.sh`.

## Overview

When an event fires in the cluster, sreips core calls a small HTTP bridge instead of talking to an LLM directly. The bridge builds a predefined system prompt, sends the incident payload to the Hermes agent chat completions endpoint and validates the JSON that comes back before handing a formatted Slack message to sreips core.

```
OpenShift event
      |
      v
sreips core (sreips-runner, custom_actions/sreips-action.py)
      |  POST /analyze
      v
hermes rca bridge (hermes-rca-bridge/bridge.py)
      |  POST /v1/chat/completions
      v
Hermes agent gateway (hermes-agent namespace)
      |
      +--> ocp_mcp (read only cluster evidence)
      +--> rh_kcs_mcp (Red Hat KCS articles)
      +--> sreips_rag (enterprise KB search via LlamaStack vector store)
      |
      v
Structured RCA JSON --> Slack markdown --> back through the bridge to sreips core
```

## Request flow

1. Runner detects an event such as CrashLoopBackOff, ImagePullBackOff, OOMKilled or a PVC failure and runs `lls_agent_action` or `lls_agent_action_generic` from `custom_playbooks/custom_actions/sreips-action.py`.
2. That handler extracts the event reason, message, resource kind, name, namespace and any pod logs, builds a short keyword search query and calls `POST {HERMES_RCA_URL}/analyze` on the Hermes RCA bridge.
3. The bridge (`hermes-rca-bridge/bridge.py`) wraps the incident into a predefined system prompt plus a JSON user message and calls the Hermes agent at `POST {HERMES_BASE_URL}/v1/chat/completions` with the configured `HERMES_MODEL`.
4. The Hermes agent gateway loads the SREIPS skills and uses its configured MCP servers to gather evidence:
   - `ocp_mcp` for live pod, event and resource state in the cluster (read only allowlist, see below)
   - `rh_kcs_mcp` for Red Hat knowledge base articles matching the symptoms
   - `sreips_rag` for the enterprise knowledge base ingested from internal runbooks
5. Hermes returns a single JSON object matching the RCA schema. The bridge parses and validates it with pydantic, converts it into Slack flavored markdown and a combined text block and returns all three (`report`, `slack_markdown`, `combined_results`) plus an `incident_id` to sreips core.
6. sreips core attaches the Slack markdown as enrichment blocks on the original alert, so the analysis appears directly under the CrashLoopBackOff or quota notification in the configured channel.

## SREIPS skills

The Hermes agent loads four skills from `hermes-skills/sreips/` (installed into the pod at startup by the `install-sreips-skills` init container):

- `sreips-openshift-incident-rca`: the main procedure. Defines the required JSON output schema and the order of operations (gather OCP evidence, search KCS, search the internal KB, then synthesize).
- `sreips-evidence-ocp`: lists the eight allowed `ocp_mcp` tools and the tools that must never be called during RCA.
- `sreips-kcs-enrichment`: rules for building KCS search queries and citing `view_uri` for every article referenced.
- `sreips-output-slack`: formatting rules for the Slack markdown response, section order and bold or link conventions.

## RCA output schema

Hermes must return one JSON object with no markdown fence around it. The bridge validates it against this schema (see `RcaReport` in `bridge.py`):

```json
{
  "summary": "string",
  "symptoms": ["string"],
  "evidence": [{"source": "string", "detail": "string"}],
  "root_cause": "string",
  "contributing_factors": ["string"],
  "recommended_actions": [{"priority": "immediate|verification|long-term", "action": "string", "verification": "string"}],
  "enterprise_kb": [{"title": "string", "excerpt": "string", "source": "string"}],
  "kcs_articles": [{"id": "string", "title": "string", "view_uri": "string", "relevance": "string"}],
  "confidence": "high|medium|low",
  "open_questions": ["string"]
}
```

If a field is missing or malformed, the pydantic model falls back to an empty default rather than failing the whole request, except for `confidence` which normalizes any unexpected value to `medium`.

## MCP tool policy

`ocp_mcp` is shared with other tools in the cluster but Hermes is restricted to a read only allowlist configured under `mcp_servers.ocp_mcp.tools.include` in `hermes-agent/hermes-all-in-one.yaml`:

Allowed: `events_list`, `pods_list`, `pods_list_in_namespace`, `pods_get`, `pods_top`, `pods_log`, `resources_list`, `resources_get`.

Restricted (must never be called for RCA): `configuration_view`, `namespaces_list`, `projects_list`, `nodes_log`, `nodes_stats_summary`, `nodes_top`, `helm_install`, `helm_list`, `helm_uninstall`, `pods_delete`, `pods_exec`, `pods_run`, `resources_create_or_update`, `resources_delete`, `resources_scale`.

This means Hermes RCA can inspect the cluster but cannot mutate it. There is no automated remediation path in this branch; every recommended action in the RCA report is informational and must be applied manually by an operator.

## Verifying the RCA path

These checks are specific to the Hermes RCA request flow and go beyond the general installation checks in `Readme.md`.

```bash
# health check on the bridge
oc -n hermes-agent exec deploy/hermes-rca-bridge -- curl -s localhost:8000/health

# confirm the skills were installed into the Hermes agent pod
oc -n hermes-agent exec deploy/hermes -- ls -laR /etc/hermes/skills/sreips 2>/dev/null \
  || oc -n hermes-agent exec deploy/hermes -- ls -laR /opt/data/skills

# send a sample incident straight to the bridge, bypassing sreips core
curl -X POST http://hermes-rca-bridge.hermes-agent.svc.cluster.local:8000/analyze \
  -H "Content-Type: application/json" \
  -d '{"event_reason":"CrashLoopBackOff","resource_kind":"Pod","search_query":"CrashLoopBackOff Pod OpenShift"}'
```

## Troubleshooting the RCA path

- Bridge returns 503 with `HERMES_BASE_URL not configured`: the `hermes-rca-bridge-config` ConfigMap is missing `HERMES_BASE_URL`, check that bootstrap captured the Hermes route before deploying the bridge.
- Bridge returns 502 with a JSON parse error: Hermes did not return valid JSON, check `oc -n hermes-agent logs deploy/hermes` for the raw model output and confirm the skills ConfigMap is mounted.
- No KCS articles in the report: confirm `rh-kcs-mcp` is reachable from Hermes and that the offline ingest workflow has been run if `KCS_MODE=offline` (see Readme.md).
- No enterprise KB hits: confirm `sreips-rag-mcp` resolved the `sreips_vector_id` vector store, `oc -n hermes-agent logs deploy/sreips-rag-mcp`.
- Request times out: both the bridge and the Hermes route are configured with a 600 second timeout (`HERMES_TIMEOUT_SEC`, HAProxy route annotation). Increase both together if the model consistently needs longer.
