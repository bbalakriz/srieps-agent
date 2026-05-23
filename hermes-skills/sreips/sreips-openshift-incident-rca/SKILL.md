---
name: sreips-openshift-incident-rca
description: OpenShift incident root cause analysis using OCP MCP evidence, enterprise KB, and Red Hat KCS.
version: 1.0.0
metadata:
  hermes:
    tags: [SREIPS, OpenShift, RCA]
    related_skills: [sreips-evidence-ocp, sreips-kcs-enrichment, sreips-output-slack]
---

# SREIPS OpenShift incident RCA

## When to use

Any Kubernetes or OpenShift alert enrichment: CrashLoopBackOff, ImagePullBackOff, OOM, PVC binding, quota warnings.

## Procedure

1. Load `sreips-evidence-ocp` and gather cluster evidence using only the eight allowed `ocp_mcp` tools (see that skill).
2. Load `sreips-kcs-enrichment` and search KCS for matching symptoms.
3. Call `sreips_rag` tool `search_internal_kb` with a short keyword query from the incident.
4. Synthesize findings. Do not mutate cluster state in this skill.

## Required JSON output

Return a single JSON object (no markdown fence) with these keys:

- `summary` (string)
- `symptoms` (array of strings)
- `evidence` (array of `{source, detail}`)
- `root_cause` (string)
- `contributing_factors` (array of strings)
- `recommended_actions` (array of `{priority, action, verification}`)
- `enterprise_kb` (array of `{title, excerpt, source}`)
- `kcs_articles` (array of `{id, title, view_uri, relevance}`)
- `confidence` (`high` | `medium` | `low`)
- `open_questions` (array of strings)

## Rules

- Cite every KCS article with `view_uri`.
- Separate facts (evidence) from inference (root_cause).
- If evidence is insufficient, set confidence to `low` and list `open_questions`.
