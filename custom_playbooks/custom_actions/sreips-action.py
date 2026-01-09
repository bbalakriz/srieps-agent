from robusta.api import *
import requests
import os
import re

# SREIPS Agent API endpoint - externalized
SREIPS_AGENT_URL = os.getenv("SREIPS_AGENT_URL", "http://sreips-agent.sreips-agent.svc.cluster.local:8000")

def build_sreips_prompt(event_reason: str, event_message: str, resource_kind: str) -> str:
    """
    Build a search-optimized prompt for SREIPS Agent based on event reason, message, and resource kind.
    Generic approach that works for any Kubernetes issue type.
    
    Creates short, keyword-focused prompts optimized for search while maintaining generic applicability.
    """
    prompt_parts = []
    
    # Add event reason (most descriptive and specific)
    if event_reason and event_reason != "Unknown":
        prompt_parts.append(event_reason)
    
    # Add resource kind for context (e.g., Pod, Node, PersistentVolumeClaim)
    if resource_kind and resource_kind != "Unknown":
        prompt_parts.append(resource_kind)
    
    # Extract key terms from message (generic keyword extraction)
    if event_message and event_message != "No message available":
        # Extract important keywords from the message
        # Look for common Kubernetes/resource-related terms
        keywords = set()
        
        # Common resource-related terms
        resource_terms = ["cpu", "memory", "storage", "quota", "limit", "image", "container", 
                         "pod", "node", "volume", "pvc", "persistentvolume", "network", "disk"]
        message_lower = event_message.lower()
        for term in resource_terms:
            if term in message_lower:
                keywords.add(term)
        
        # Common error-related terms
        error_terms = ["error", "failed", "timeout", "unauthorized", "forbidden", "not found", 
                      "unknown", "crash", "oom", "evicted", "backoff", "pull"]
        for term in error_terms:
            if term in message_lower:
                keywords.add(term)
        
        # Add unique keywords (avoid duplicates)
        prompt_parts.extend(list(keywords))
    
    # Add OpenShift context (always include for platform-specific search)
    prompt_parts.append("OpenShift")
    
    # Join with spaces for a natural search query
    # Remove duplicates while preserving order
    seen = set()
    unique_parts = []
    for part in prompt_parts:
        part_lower = part.lower()
        if part_lower not in seen:
            seen.add(part_lower)
            unique_parts.append(part)
    
    return " ".join(unique_parts)

def extract_event_info_from_pod_event(pod_event) -> tuple:
    """
    Extract event information from a PodEvent.
    Returns (event_reason, event_message, resource_kind, resource_name, resource_namespace, pod_logs)
    """
    try:
        pod = pod_event.get_pod()
        pod_name = pod.metadata.name
        pod_namespace = pod.metadata.namespace
        
        # Try to get logs
        try:
            pod_logs = pod.get_logs()
        except Exception as e:
            print(f"Could not fetch logs for pod {pod_name}: {e}")
            pod_logs = ""
        
        # Extract failure reason from pod status
        event_reason = "Unknown"
        event_message = ""
        
        # Check container statuses for failure reasons
        if pod.status and hasattr(pod.status, 'containerStatuses') and pod.status.containerStatuses:
            for container_status in pod.status.containerStatuses:
                # Check waiting state
                if (container_status.state and 
                    container_status.state.waiting and 
                    container_status.state.waiting.reason):
                    event_reason = container_status.state.waiting.reason
                    if hasattr(container_status.state.waiting, 'message'):
                        event_message = container_status.state.waiting.message
                    break
                
                # Check terminated state
                if (container_status.state and 
                    container_status.state.terminated and 
                    container_status.state.terminated.reason):
                    event_reason = container_status.state.terminated.reason
                    if hasattr(container_status.state.terminated, 'message'):
                        event_message = container_status.state.terminated.message
                    break
        
        # Check pod conditions as fallback
        if event_reason == "Unknown" and pod.status and hasattr(pod.status, 'conditions') and pod.status.conditions:
            for condition in pod.status.conditions:
                if (hasattr(condition, 'status') and 
                    hasattr(condition, 'reason') and
                    condition.status == "False" and 
                    condition.reason):
                    event_reason = condition.reason
                    if hasattr(condition, 'message'):
                        event_message = condition.message
                    break
        
        # Fallback to log analysis
        if event_reason == "Unknown" and pod_logs:
            log_lower = pod_logs.lower()
            if "out of memory" in log_lower or "oom" in log_lower:
                event_reason = "OOMKilled"
            elif "image pull" in log_lower or "imagepullbackoff" in log_lower:
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
    """
    Extract event information from an EventChangeEvent.
    Returns (event_reason, event_message, resource_kind, resource_name, resource_namespace, resource_logs)
    """
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
            resource_kind = 'Unknown'
            resource_name = 'Unknown'
            resource_namespace = 'Unknown'
        
        # Try to get logs if it's a pod
        resource_logs = get_resource_logs(resource_kind, resource_name, resource_namespace)
        
        return event_reason, event_message, resource_kind, resource_name, resource_namespace, resource_logs
        
    except Exception as e:
        print(f"Error extracting info from EventChangeEvent: {e}")
        return "Unknown", "Error extracting event information", "Unknown", "Unknown", "Unknown", ""

def get_resource_logs(resource_kind: str, resource_name: str, resource_namespace: str) -> str:
    """
    Attempt to get logs for a resource if it's a Pod.
    Returns empty string for non-pod resources or if logs cannot be retrieved.
    """
    if resource_kind != "Pod":
        return ""
    
    try:
        # Try to get the pod and its logs using Robusta's Pod API
        pod = Pod.find_pod(resource_name, resource_namespace)
        if pod:
            return pod.get_logs()
    except Exception as e:
        print(f"Could not fetch logs for pod {resource_name} in {resource_namespace}: {e}")
    
    return ""

def query_sreips_agent(query: str) -> dict:
    """
    Call the SREIPS Agent API with the given query
    Returns the combined results or error message
    """
    try:
        response = requests.post(
            f"{SREIPS_AGENT_URL}/query",
            json={"query": query},
            timeout=600
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.Timeout:
        return {"combined_results": "Error: Request to SREIPS Agent timed out"}
    except requests.exceptions.ConnectionError:
        return {"combined_results": f"Error: Could not connect to SREIPS Agent at {SREIPS_AGENT_URL}"}
    except Exception as e:
        return {"combined_results": f"Error querying SREIPS Agent: {str(e)}"}

def convert_markdown_to_slack(text: str) -> str:
    """
    Convert standard markdown to Slack-compatible markdown
    - **bold** → *bold* (Slack uses single asterisks for bold)
    - Headers (##, ###) → *bold text* (Slack doesn't support headers)
    - Remove language tags from code blocks (```bash → ```)
    - Keep bullets and numbered lists as-is
    - Preserve code blocks with backticks
    """
    # Remove language tags from code blocks (```bash → ```, ```python → ```, etc.)
    text = re.sub(r'```(\w+)\n', '```\n', text)
    
    # Convert headers to bold text
    # ## Header → *Header*
    text = re.sub(r'^##+\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    
    # Convert double asterisks (standard markdown bold) to single asterisks (Slack bold)
    text = re.sub(r'\*\*([^\*]+)\*\*', r'*\1*', text)
    
    # Ensure proper spacing around bullets for better readability
    text = re.sub(r'^\*\s+', '• ', text, flags=re.MULTILINE)
    
    return text

def parse_combined_results(combined_results: str) -> tuple:
    """
    Parse the combined results from SREIPS Agent into RAG and MCP sections
    Returns (rag_results, mcp_results) tuple, both converted to Slack markdown
    """
    try:
        # Split by section headers
        if "=== RAG Results ===" in combined_results and "=== MCP Results ===" in combined_results:
            parts = combined_results.split("=== MCP Results ===")
            rag_part = parts[0].replace("=== RAG Results ===", "").strip()
            mcp_part = parts[1].strip() if len(parts) > 1 else ""
            
            # Convert to Slack markdown format
            rag_part = convert_markdown_to_slack(rag_part)
            mcp_part = convert_markdown_to_slack(mcp_part)
            
            return rag_part, mcp_part
        else:
            # If no sections found, return all as RAG results
            converted = convert_markdown_to_slack(combined_results)
            return converted, ""
    except Exception as e:
        print(f"Error parsing combined results: {e}")
        return combined_results, ""

def _process_lls_agent_action(event_reason: str, event_message: str, resource_kind: str, 
                              resource_name: str, resource_namespace: str, resource_logs: str, 
                              event_obj) -> None:
    """
    Core logic for processing SREIPS agent action.
    Shared by both PodEvent and EventChangeEvent handlers.
    """
    # Build generic SREIPS agent prompt from event data
    prompt = build_sreips_prompt(event_reason, event_message, resource_kind)
    
    # Query the SREIPS Agent
    results = query_sreips_agent(prompt)
    combined_results = results.get("combined_results", "No results returned from SREIPS Agent")
    
    # Parse the results into separate sections
    rag_results, mcp_results = parse_combined_results(combined_results)
    
    # Build enrichment blocks
    enrichment_blocks = [
        MarkdownBlock(f"*🚨 Alert:* {resource_kind} `{resource_name}` in namespace `{resource_namespace}` is experiencing issues"),
        MarkdownBlock(f"*🔍 Detected Issue:* `{event_reason}`"),
        MarkdownBlock(f"*💬 Event Message:* {event_message}"),
    ]
    
    # Only add logs file if logs are available (for pods)
    if resource_logs:
        enrichment_blocks.append(FileBlock(f"{resource_name}-logs.log", resource_logs))
    
    enrichment_blocks.append(DividerBlock())
    
    # Add RAG results if available
    if rag_results:
        enrichment_blocks.append(
            MarkdownBlock(f"*📚 Matching Enterprise Knowledge Base Solution:*\n{rag_results}")
        )
        enrichment_blocks.append(DividerBlock())
    
    # Add MCP results if available
    if mcp_results:
        enrichment_blocks.append(
            MarkdownBlock(f"*🔗 Red Hat KCS Articles:*\n{mcp_results}")
        )
    
    # Send enrichment to destinations
    event_obj.add_enrichment(enrichment_blocks)

@action
def lls_agent_action(event: PodEvent):
    """
    Action handler for PodEvent (triggered by on_pod_crash_loop, on_image_pull_backoff, 
    on_pod_oom_killed, etc.).
    Extracts pod information and processes through SREIPS agent.
    
    """
    try:
        if hasattr(event, 'get_pod'):  
            (
                event_reason,
                event_message,
                resource_kind,
                resource_name,
                resource_namespace,
                resource_logs
            ) = extract_event_info_from_pod_event(event)
        else:  
            # Fallback handling  
            return 
        
        _process_lls_agent_action(event_reason, event_message, resource_kind, resource_name, 
                                  resource_namespace, resource_logs, event)
        
    except AttributeError as e:
        print(f"AttributeError in lls_agent_action: {e}")
    except Exception as e:
        print(f"Unexpected error in lls_agent_action: {e}")

@action
def lls_agent_action_generic(event: EventChangeEvent):
    """
    Generic action handler for EventChangeEvent (triggered by on_kubernetes_warning_event_create, 
    on_pod_update, etc.).
    Works with pods, nodes, PVCs, and all other Kubernetes resources.
    
    """
    try:
        # Standard EventChangeEvent handling
        event_reason, event_message, resource_kind, resource_name, resource_namespace, resource_logs = \
            extract_event_info_from_event_change(event)
    
        _process_lls_agent_action(event_reason, event_message, resource_kind, resource_name, 
                                  resource_namespace, resource_logs, event)
        
    except AttributeError as e:
        print(f"AttributeError in lls_agent_action_generic: {e}")
    except Exception as e:
        print(f"Unexpected error in lls_agent_action_generic: {e}")