---
name: sreips-evidence-ocp
description: Read-only OpenShift evidence gathering via ocp_mcp MCP tools for RCA.
version: 1.0.0
metadata:
  hermes:
    tags: [SREIPS, OpenShift, MCP]
---

# OCP evidence (read only)

## When to use

During `sreips-openshift-incident-rca` before stating root cause.

## Allowed tools (ocp_mcp)

Hermes exposes only these tools for RCA (see `hermes-config` in `hermes-agent/hermes-all-in-one.yaml`):

- `events_list`
- `pods_list`
- `pods_list_in_namespace`
- `pods_get`
- `pods_top`
- `pods_log`
- `resources_list`
- `resources_get`

Use them to inspect the reported namespace and resource. Prefer namespace-scoped calls when the incident namespace is known.

## Restricted tools (never use in RCA)

Do not call these even if they appear available:

- `configuration_view`
- `namespaces_list`, `projects_list`
- `nodes_log`, `nodes_stats_summary`, `nodes_top`
- `helm_install`, `helm_list`, `helm_uninstall`
- `pods_delete`, `pods_exec`, `pods_run`
- `resources_create_or_update`, `resources_delete`, `resources_scale`

## Forbidden actions

Do not delete, patch, scale, apply, exec into pods, or run new pods. Do not run remediation.

## Evidence quality

Record namespace, resource name, and exact API error text in `evidence` entries with `source: ocp_mcp`.
