#!/usr/bin/env python3
"""Hermes RCA bridge: structured incident in, validated RCA JSON out."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("hermes-rca-bridge")

app = FastAPI(title="Hermes RCA Bridge", version="1.0.0")

HERMES_BASE_URL = os.getenv("HERMES_BASE_URL", "").rstrip("/")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "")
HERMES_MODEL = os.getenv("HERMES_MODEL", "Qwen3.6-35B-A3B")
HERMES_TIMEOUT_SEC = int(os.getenv("HERMES_TIMEOUT_SEC", "600"))
CLUSTER_NAME = os.getenv("CLUSTER_NAME", "openshift")


class IncidentPayload(BaseModel):
    event_reason: str = ""
    event_message: str = ""
    resource_kind: str = ""
    resource_name: str = ""
    resource_namespace: str = ""
    resource_logs: str = ""
    search_query: str = ""


class RecommendedAction(BaseModel):
    priority: str = "medium"
    action: str = ""
    verification: str = ""


class KbHit(BaseModel):
    title: str = ""
    excerpt: str = ""
    source: str = ""


class KcsArticle(BaseModel):
    id: str = ""
    title: str = ""
    view_uri: str = ""
    relevance: str = ""


class EvidenceItem(BaseModel):
    source: str = ""
    detail: str = ""


class RcaReport(BaseModel):
    summary: str = ""
    symptoms: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    root_cause: str = ""
    contributing_factors: list[str] = Field(default_factory=list)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    enterprise_kb: list[KbHit] = Field(default_factory=list)
    kcs_articles: list[KcsArticle] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"
    open_questions: list[str] = Field(default_factory=list)

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, v: Any) -> str:
        if v in ("high", "medium", "low"):
            return v
        return "medium"


class AnalyzeResponse(BaseModel):
    incident_id: str
    report: RcaReport
    combined_results: str
    slack_markdown: str


SYSTEM_PROMPT = """You are SREIPS, an expert OpenShift SRE performing root cause analysis.

Use Hermes skills: sreips-openshift-incident-rca, sreips-evidence-ocp, sreips-kcs-enrichment.
Use MCP servers: ocp_mcp (read only; allowed tools: events_list, pods_list, pods_list_in_namespace, pods_get, pods_top, pods_log, resources_list, resources_get), rh_kcs_mcp, sreips_rag (search_internal_kb).
Never use ocp_mcp delete, patch, scale, exec, run, helm, or configuration_view tools.

You MUST return ONLY valid JSON matching this schema (no markdown code fence):
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
"""


def _build_user_message(incident: IncidentPayload) -> str:
    logs = incident.resource_logs
    if logs and len(logs) > 12000:
        logs = logs[-12000:]
    return json.dumps(
        {
            "cluster": CLUSTER_NAME,
            "event_reason": incident.event_reason,
            "event_message": incident.event_message,
            "resource": {
                "kind": incident.resource_kind,
                "name": incident.resource_name,
                "namespace": incident.resource_namespace,
            },
            "search_query": incident.search_query,
            "logs_tail": logs,
        },
        indent=2,
    )


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def _call_hermes(messages: list[dict[str, str]]) -> str:
    if not HERMES_BASE_URL:
        raise HTTPException(status_code=503, detail="HERMES_BASE_URL not configured")
    url = f"{HERMES_BASE_URL}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if HERMES_API_KEY:
        headers["Authorization"] = f"Bearer {HERMES_API_KEY}"
    body = {
        "model": HERMES_MODEL,
        "messages": messages,
        "temperature": 0.2,
    }
    with httpx.Client(timeout=HERMES_TIMEOUT_SEC) as client:
        resp = client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
    choice = data.get("choices") or []
    if not choice:
        raise HTTPException(status_code=502, detail="Hermes returned no choices")
    msg = choice[0].get("message") or {}
    content = msg.get("content") or ""
    if not content:
        raise HTTPException(status_code=502, detail="Hermes returned empty content")
    return content


def _report_to_slack(report: RcaReport) -> str:
    sections: list[str] = []

    if report.summary:
        sections.extend(["*Summary*", report.summary, ""])

    if report.root_cause:
        sections.extend(["*Root Cause*", report.root_cause, ""])

    if report.confidence:
        sections.extend([f"*Confidence:* {report.confidence}", ""])

    if report.symptoms:
        sections.append("*Symptoms*")
        sections.extend(f"• {s}" for s in report.symptoms[:6])
        sections.append("")

    if report.contributing_factors:
        sections.append("*Contributing Factors*")
        sections.extend(f"• {f}" for f in report.contributing_factors[:6])
        sections.append("")

    if report.evidence:
        sections.append("*Evidence*")
        for e in report.evidence[:8]:
            detail = e.detail[:400]
            source = e.source or "cluster"
            sections.append(f"• _{source}_ — {detail}")
        sections.append("")

    if report.recommended_actions:
        sections.append("*Recommended Actions*")
        for a in report.recommended_actions[:6]:
            line = f"• *[{a.priority}]* {a.action}"
            sections.append(line)
            if a.verification:
                sections.append(f"  _verify:_ {a.verification[:200]}")
        sections.append("")

    if report.enterprise_kb:
        sections.append("*Enterprise Knowledge Base*")
        for k in report.enterprise_kb[:4]:
            title = k.title or "KB article"
            excerpt = (k.excerpt or "").strip()
            if excerpt:
                sections.append(f"• *{title}*")
                sections.append(f"  {excerpt[:300]}")
            else:
                sections.append(f"• *{title}*")
        sections.append("")

    if report.kcs_articles:
        sections.append("*Red Hat KCS*")
        for k in report.kcs_articles[:6]:
            title = k.title or "KCS article"
            kcs_id = k.id or ""
            uri = k.view_uri or (f"https://access.redhat.com/solutions/{kcs_id}" if kcs_id else "")
            if uri:
                id_suffix = f" · `{kcs_id}`" if kcs_id else ""
                sections.append(f"• <{uri}|{title}>{id_suffix}")
            else:
                sections.append(f"• {title}" + (f" · `{kcs_id}`" if kcs_id else ""))
            relevance = (k.relevance or "").strip()
            if relevance:
                sections.append(f"  _relevance:_ {relevance[:200]}")
        sections.append("")

    if report.open_questions:
        sections.append("*Open Questions*")
        sections.extend(f"• {q}" for q in report.open_questions[:4])
        sections.append("")

    while sections and sections[-1] == "":
        sections.pop()

    return "\n".join(sections)


def _report_to_combined(report: RcaReport, slack_md: str) -> str:
    kb = "\n".join(f"- {k.title}: {k.excerpt[:500]}" for k in report.enterprise_kb[:5]) or "No KB hits."
    kcs = "\n".join(
        f"- {k.title} ({k.view_uri or k.id})" for k in report.kcs_articles[:5]
    ) or "No KCS hits."
    return (
        f"=== RCA Summary ===\n{report.summary}\n\n"
        f"=== Root Cause ===\n{report.root_cause}\n\n"
        f"=== RAG Results ===\n{kb}\n\n"
        f"=== MCP Results ===\n{kcs}\n\n"
        f"=== Full Report ===\n{slack_md}"
    )


@app.get("/")
@app.get("/health")
def health():
    return {"status": "ok", "hermes_configured": bool(HERMES_BASE_URL)}


@app.post("/analyze", response_model=AnalyzeResponse)
@app.post("/query", response_model=AnalyzeResponse)
def analyze(incident: IncidentPayload) -> AnalyzeResponse:
    incident_id = str(uuid.uuid4())
    log.info("incident_id=%s kind=%s name=%s ns=%s reason=%s", incident_id, incident.resource_kind, incident.resource_name, incident.resource_namespace, incident.event_reason)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(incident)},
    ]
    try:
        raw = _call_hermes(messages)
        parsed = _extract_json(raw)
        report = RcaReport.model_validate(parsed)
    except json.JSONDecodeError as e:
        log.error("incident_id=%s json_parse_error=%s", incident_id, e)
        raise HTTPException(status_code=502, detail=f"Failed to parse Hermes JSON: {e}") from e
    except httpx.HTTPStatusError as e:
        log.error("incident_id=%s hermes_http=%s", incident_id, e.response.status_code)
        raise HTTPException(status_code=502, detail=f"Hermes API error: {e}") from e
    except Exception as e:
        log.error("incident_id=%s error=%s", incident_id, e)
        raise HTTPException(status_code=500, detail=str(e)) from e

    slack_md = _report_to_slack(report)
    combined = _report_to_combined(report, slack_md)
    log.info("incident_id=%s confidence=%s", incident_id, report.confidence)
    return AnalyzeResponse(
        incident_id=incident_id,
        report=report,
        combined_results=combined,
        slack_markdown=slack_md,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
