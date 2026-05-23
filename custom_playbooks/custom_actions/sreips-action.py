from robusta.api import *
import requests
import os
import re

HERMES_RCA_URL = os.getenv(
    "HERMES_RCA_URL",
    "http://hermes-rca-bridge.hermes-agent.svc.cluster.local:8000",
)


def build_sreips_prompt(event_reason: str, event_message: str, resource_kind: str) -> str:
    """build a short keyword-focused search query for the RCA bridge."""
    parts = []
    if event_reason and event_reason != "Unknown":
        parts.append(event_reason)
    if resource_kind and resource_kind != "Unknown":
        parts.append(resource_kind)
    if event_message and event_message != "No message available":
        keywords = set()
        resource_terms = ["cpu", "memory", "storage", "quota", "limit", "image", "container",
                          "pod", "node", "volume", "pvc", "persistentvolume", "network", "disk"]
        error_terms = ["error", "failed", "timeout", "unauthorized", "forbidden", "not found",
                       "unknown", "crash", "oom", "evicted", "backoff", "pull"]
        msg_lower = event_message.lower()
        for term in resource_terms + error_terms:
            if term in msg_lower:
                keywords.add(term)
        parts.extend(list(keywords))
    parts.append("OpenShift")
    seen = set()
    unique = []
    for p in parts:
        if p.lower() not in seen:
            seen.add(p.lower())
            unique.append(p)
    return " ".join(unique)


def extract_event_info_from_pod_event(pod_event) -> tuple:
    """extract (reason, message, kind, name, namespace, logs) from a PodEvent."""
    try:
        pod = pod_event.get_pod()
        pod_name = pod.metadata.name
        pod_namespace = pod.metadata.namespace
        try:
            pod_logs = pod.get_logs()
        except Exception as e:
            print(f"Could not fetch logs for pod {pod_name}: {e}")
            pod_logs = ""

        event_reason = "Unknown"
        event_message = ""

        if pod.status and hasattr(pod.status, 'containerStatuses') and pod.status.containerStatuses:
            for cs in pod.status.containerStatuses:
                if cs.state and cs.state.waiting and cs.state.waiting.reason:
                    event_reason = cs.state.waiting.reason
                    event_message = getattr(cs.state.waiting, 'message', '') or ''
                    break
                if cs.state and cs.state.terminated and cs.state.terminated.reason:
                    event_reason = cs.state.terminated.reason
                    event_message = getattr(cs.state.terminated, 'message', '') or ''
                    break

        if event_reason == "Unknown" and pod.status and hasattr(pod.status, 'conditions') and pod.status.conditions:
            for cond in pod.status.conditions:
                if getattr(cond, 'status', None) == "False" and getattr(cond, 'reason', None):
                    event_reason = cond.reason
                    event_message = getattr(cond, 'message', '') or ''
                    break

        if event_reason == "Unknown" and pod_logs:
            log_lower = pod_logs.lower()
            if "out of memory" in log_lower or "oom" in log_lower:
                event_reason = "OOMKilled"
            elif "imagepullbackoff" in log_lower or "image pull" in log_lower:
                event_reason = "ImagePullBackOff"
            elif "crashloopbackoff" in log_lower or "crash loop" in log_lower:
                event_reason = "CrashLoopBackOff"
            elif "evicted" in log_lower:
                event_reason = "Evicted"

        if not event_message:
            event_message = f"Pod {pod_name} is experiencing {event_reason}"

        return event_reason, event_message, "Pod", pod_name, pod_namespace, pod_logs

    except Exception as e:
        print(f"Error extracting info from PodEvent: {e}")
        return "Unknown", "Error extracting pod information", "Pod", "Unknown", "Unknown", ""


def extract_event_info_from_event_change(event_change) -> tuple:
    """extract (reason, message, kind, name, namespace, logs) from an EventChangeEvent."""
    try:
        k8s_event = event_change.obj
        event_reason = getattr(k8s_event, 'reason', 'Unknown')
        event_message = getattr(k8s_event, 'note', getattr(k8s_event, 'message', 'No message available'))
        involved_obj = getattr(k8s_event, 'regarding', getattr(k8s_event, 'involvedObject', None))
        if involved_obj:
            resource_kind = getattr(involved_obj, 'kind', 'Unknown')
            resource_name = getattr(involved_obj, 'name', 'Unknown')
            resource_namespace = getattr(involved_obj, 'namespace', 'cluster-scoped')
        else:
            resource_kind = resource_name = resource_namespace = 'Unknown'

        resource_logs = ""
        if resource_kind == "Pod":
            try:
                pod = Pod.find_pod(resource_name, resource_namespace)
                if pod:
                    resource_logs = pod.get_logs()
            except Exception as e:
                print(f"Could not fetch logs for pod {resource_name}: {e}")

        return event_reason, event_message, resource_kind, resource_name, resource_namespace, resource_logs

    except Exception as e:
        print(f"Error extracting info from EventChangeEvent: {e}")
        return "Unknown", "Error extracting event information", "Unknown", "Unknown", "Unknown", ""


def query_hermes_rca(
    event_reason: str,
    event_message: str,
    resource_kind: str,
    resource_name: str,
    resource_namespace: str,
    resource_logs: str,
    search_query: str,
) -> dict:
    """call the Hermes RCA bridge and return the parsed response dict."""
    payload = {
        "event_reason": event_reason,
        "event_message": event_message,
        "resource_kind": resource_kind,
        "resource_name": resource_name,
        "resource_namespace": resource_namespace,
        "resource_logs": resource_logs or "",
        "search_query": search_query,
    }
    try:
        response = requests.post(
            f"{HERMES_RCA_URL.rstrip('/')}/analyze",
            json=payload,
            timeout=600,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.Timeout:
        return {"slack_markdown": "Error: request to Hermes RCA bridge timed out"}
    except requests.exceptions.ConnectionError:
        return {"slack_markdown": f"Error: could not connect to Hermes RCA bridge at {HERMES_RCA_URL}"}
    except Exception as e:
        return {"slack_markdown": f"Error querying Hermes RCA bridge: {str(e)}"}


def convert_markdown_to_slack(text: str) -> str:
    """convert standard markdown to Slack mrkdwn."""
    text = re.sub(r'```(\w+)\n', '```\n', text)
    text = re.sub(r'^##+\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*([^\*]+)\*\*', r'*\1*', text)
    text = re.sub(r'^\*\s+', '• ', text, flags=re.MULTILINE)
    return text


# slack/mattermost block text cap is 3000 chars; stay a little under
_MAX_BLOCK_CHARS = 2900


def _split_slack_blocks(text: str) -> list[str]:
    """split markdown into chunks that each fit within a single Slack block.

    prefers splitting on blank lines between sections so headers stay
    with their content; falls back to a hard newline split.
    """
    if len(text) <= _MAX_BLOCK_CHARS:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > _MAX_BLOCK_CHARS:
        # try to break at the last blank line before the limit
        split_at = remaining.rfind("\n\n", 0, _MAX_BLOCK_CHARS)
        if split_at < 1:
            split_at = remaining.rfind("\n", 0, _MAX_BLOCK_CHARS)
        if split_at < 1:
            split_at = _MAX_BLOCK_CHARS
        chunks.append(remaining[:split_at].rstrip("\n"))
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def _process_lls_agent_action(
    event_reason: str,
    event_message: str,
    resource_kind: str,
    resource_name: str,
    resource_namespace: str,
    resource_logs: str,
    event_obj,
) -> None:
    search_query = build_sreips_prompt(event_reason, event_message, resource_kind)
    results = query_hermes_rca(
        event_reason, event_message, resource_kind,
        resource_name, resource_namespace, resource_logs, search_query,
    )

    slack_md = convert_markdown_to_slack(results.get("slack_markdown", ""))
    incident_id = results.get("incident_id", "")

    enrichment_blocks = [
        MarkdownBlock(f"*Alert:* {resource_kind} `{resource_name}` in namespace `{resource_namespace}` is experiencing issues"),
        MarkdownBlock(f"*Detected Issue:* `{event_reason}`"),
        MarkdownBlock(f"*Event Message:* {event_message}"),
    ]
    if incident_id:
        enrichment_blocks.append(MarkdownBlock(f"*Analysis ID:* `{incident_id}`"))
    if resource_logs:
        enrichment_blocks.append(FileBlock(f"{resource_name}-logs.log", resource_logs))

    enrichment_blocks.append(DividerBlock())

    if slack_md:
        enrichment_blocks.append(MarkdownBlock("*Root Cause Analysis:*"))
        for chunk in _split_slack_blocks(slack_md):
            enrichment_blocks.append(MarkdownBlock(chunk))

    event_obj.add_enrichment(enrichment_blocks)


@action
def lls_agent_action(event: PodEvent):
    """handler for PodEvent (on_pod_crash_loop, on_image_pull_backoff, on_pod_oom_killed, etc.)."""
    try:
        if not hasattr(event, 'get_pod'):
            return
        (event_reason, event_message, resource_kind,
         resource_name, resource_namespace, resource_logs) = extract_event_info_from_pod_event(event)
        _process_lls_agent_action(
            event_reason, event_message, resource_kind,
            resource_name, resource_namespace, resource_logs, event,
        )
    except Exception as e:
        print(f"Unexpected error in lls_agent_action: {e}")


@action
def lls_agent_action_generic(event: EventChangeEvent):
    """handler for EventChangeEvent (on_kubernetes_warning_event_create, on_pod_update, etc.)."""
    try:
        (event_reason, event_message, resource_kind,
         resource_name, resource_namespace, resource_logs) = extract_event_info_from_event_change(event)
        _process_lls_agent_action(
            event_reason, event_message, resource_kind,
            resource_name, resource_namespace, resource_logs, event,
        )
    except Exception as e:
        print(f"Unexpected error in lls_agent_action_generic: {e}")
