from robusta.api import *
import requests
import os
import re
from pydantic import BaseModel
from typing import Optional, Dict, Any

# SREIPS Agent API endpoint - externalized
SREIPS_AGENT_URL = os.getenv("SREIPS_AGENT_URL", "http://sreips-agent.sreips-agent.svc.cluster.local:8000")

# Remediation action URL - externalized (points to remediation agent)
REMEDIATION_ACTION_URL = os.getenv("REMEDIATION_ACTION_URL", "http://remediation-agent.llamastack.svc.cluster.local:8080/remediate")

def detect_issue_type(event_reason: str, event_message: str, resource_kind: str) -> str:
    """
    Detect issue type from event reason using generic normalization.
    Uses the event reason as the primary source, normalizing it to a valid issue type identifier.
    Falls back to resource-based generic types if needed.
    """
    if not event_reason:
        # If no reason, use resource kind to create a generic type
        if resource_kind and resource_kind != "Unknown":
            return f"{resource_kind.lower()}_issue"
        return "unknown_issue"
    
    # Normalize the event reason to create a valid issue type identifier
    # Convert to lowercase, replace spaces and hyphens with underscores
    issue_type = event_reason.lower().replace(" ", "_").replace("-", "_")
    
    # Clean up any special characters (keep only alphanumeric and underscores)
    issue_type = re.sub(r'[^a-z0-9_]', '_', issue_type)
    
    # Remove multiple consecutive underscores
    issue_type = re.sub(r'_+', '_', issue_type)
    
    # Remove leading/trailing underscores
    issue_type = issue_type.strip('_')
    
    # If we ended up with an empty string, use a fallback
    if not issue_type:
        if resource_kind and resource_kind != "Unknown":
            return f"{resource_kind.lower()}_issue"
        return "unknown_issue"
    
    # For common well-known issue types, we can use standard names
    # But this is optional - the normalized reason works for any issue
    common_mappings = {
        "failedcreate": "quota_exceeded",  # Common for quota issues
        "exceededquota": "quota_exceeded",
        "oomkilled": "oom",
        "imagepullbackoff": "image_pull_backoff",
        "errimagepull": "image_pull_backoff",
        "crashloopbackoff": "pod_crashloop",
    }
    
    # Check if the normalized reason matches a common pattern
    if issue_type in common_mappings:
        return common_mappings[issue_type]
    
    # Otherwise, return the normalized event reason as the issue type
    # This makes it work for ANY event reason, not just the hardcoded ones
    return issue_type

def extract_issue_details(event_message: str, event_reason: str, resource_kind: str) -> Dict[str, Any]:
    """
    Extract generic issue details from event message.
    Uses pattern matching to extract common information without hardcoding specific issue types.
    Returns a dictionary with any relevant details found.
    """
    details = {}
    if not event_message:
        return details
    
    message_lower = event_message.lower()
    
    # Generic extraction patterns - work for any issue type
    
    # Extract container name if mentioned (common in pod-related issues)
    container_match = re.search(r'container[:\s]+([^\s,;]+)', event_message, re.IGNORECASE)
    if container_match:
        details["container_name"] = container_match.group(1).strip()
    
    # Extract image name if mentioned
    image_match = re.search(r'image[:\s]+([^\s,;]+)', event_message, re.IGNORECASE)
    if image_match:
        details["image_name"] = image_match.group(1).strip()
    
    # Extract exit code if mentioned
    exit_code_match = re.search(r'exit code[:\s]+(\d+)', event_message, re.IGNORECASE)
    if exit_code_match:
        details["exit_code"] = exit_code_match.group(1).strip()
    
    # Extract quota-related information (if present)
    quota_name_match = re.search(r'quota[:\s]+([^,;]+)', event_message, re.IGNORECASE)
    if quota_name_match:
        details["quota_name"] = quota_name_match.group(1).strip()
    
    # Extract resource type and values (for quota/limit issues)
    resource_match = re.search(r'(requests|limits)\.([^=,\s]+)\s*=\s*([^,\s]+)', event_message, re.IGNORECASE)
    if resource_match:
        details["resource_type"] = f"{resource_match.group(1).lower()}.{resource_match.group(2).strip()}"
        details["resource_value"] = resource_match.group(3).strip()
    
    # Extract memory/CPU values
    memory_match = re.search(r'(\d+[kmg]?i?b?)\s*(?:memory|ram|mem)', message_lower)
    if memory_match:
        details["memory_value"] = memory_match.group(1).strip()
    
    cpu_match = re.search(r'(\d+[m]?)\s*(?:cpu|cores?)', message_lower)
    if cpu_match:
        details["cpu_value"] = cpu_match.group(1).strip()
    
    # Extract error types (generic)
    if "unauthorized" in message_lower or "authentication" in message_lower:
        details["error_type"] = "unauthorized"
    elif "not found" in message_lower or "unknown" in message_lower or "manifest unknown" in message_lower:
        details["error_type"] = "not_found"
    elif "network" in message_lower or "timeout" in message_lower:
        details["error_type"] = "network_error"
    elif "permission" in message_lower or "forbidden" in message_lower:
        details["error_type"] = "permission_denied"
    
    # Extract any quoted strings (often contain important error messages)
    quoted_strings = re.findall(r'"([^"]+)"', event_message)
    if quoted_strings:
        details["error_messages"] = quoted_strings
    
    # Store the full event message for the remediation agent to parse if needed
    details["raw_event_message"] = event_message
    
    return details

def build_sreips_prompt(event_reason: str, event_message: str, resource_kind: str) -> str:
    """
    Build a search-optimized prompt for SREIPS Agent based on event reason and message.
    Generic approach that works for any issue type without hardcoding specific patterns.
    """
    prompt_parts = []
    
    # Add event reason (most descriptive)
    if event_reason:
        prompt_parts.append(event_reason)
    
    # Add resource kind for context
    if resource_kind and resource_kind != "Unknown":
        prompt_parts.append(resource_kind)
    
    # Extract key terms from message (generic keyword extraction)
    if event_message:
        # Extract important keywords from the message
        # Look for common Kubernetes/resource-related terms
        keywords = set()
        
        # Common resource-related terms
        resource_terms = ["cpu", "memory", "storage", "quota", "limit", "image", "container", "pod", "node", "volume", "pvc"]
        for term in resource_terms:
            if term in event_message.lower():
                keywords.add(term)
        
        # Common error-related terms
        error_terms = ["error", "failed", "timeout", "unauthorized", "forbidden", "not found", "unknown", "crash", "oom"]
        for term in error_terms:
            if term in event_message.lower():
                keywords.add(term)
        
        # Add unique keywords (avoid duplicates)
        prompt_parts.extend(list(keywords))
    
    # Add OpenShift context
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

class RemediationParams(BaseModel):
    """Generic parameters for remediation callback - matches RemediationRequest structure"""
    issue_type: str
    namespace: str
    resource_kind: str
    resource_name: str
    event_reason: Optional[str] = None
    issue_details: Dict[str, Any] = {}
    remediation_strategy: str = "auto"

@action
def lls_agent_remediation_action(event: EventChangeEvent):
    """
    Generic remediation action that handles any Kubernetes issue type.
    Detects issue type, queries SREIPS agent for solutions, and provides remediation button.
    """
    try:
        # Get the Kubernetes event
        k8s_event = event.obj
        
        event_reason = getattr(k8s_event, 'reason', 'Unknown')
        event_message = getattr(k8s_event, 'note', getattr(k8s_event, 'message', 'No message available'))
        event_type = getattr(k8s_event, 'type', 'Warning')
        involved_obj = getattr(k8s_event, 'regarding', getattr(k8s_event, 'involvedObject', None))
        
        if involved_obj:
            resource_kind = getattr(involved_obj, 'kind', 'Unknown')
            resource_name = getattr(involved_obj, 'name', 'Unknown')
            resource_namespace = getattr(involved_obj, 'namespace', 'cluster-scoped')
        else:
            resource_kind = 'Unknown'
            resource_name = 'Unknown'
            resource_namespace = 'Unknown'
        
        # Detect issue type using generic heuristics
        issue_type = detect_issue_type(event_reason, event_message, resource_kind)
        
        # Extract generic issue details (works for any issue type)
        issue_details = extract_issue_details(event_message, event_reason, resource_kind)
        
        # Build SREIPS agent prompt (generic, works for any issue)
        prompt = build_sreips_prompt(event_reason, event_message, resource_kind)
        
        # Query SREIPS Agent
        # results = query_sreips_agent(prompt)
        # combined_results = results.get("combined_results", "No results returned from SREIPS Agent")
        
        rag_results = ""
        mcp_results = ""
        # Parse and format results
        # rag_results, mcp_results = parse_combined_results(combined_results)
        
        # Build enrichment blocks
        enrichment_blocks = [
            MarkdownBlock(f"*🚨 Kubernetes Issue Detected:* `{issue_type.replace('_', ' ').title()}`"),
            MarkdownBlock(f"*📦 Resource:* {resource_kind} `{resource_name}` in `{resource_namespace}`"),
            MarkdownBlock(f"*🔍 Event Reason:* `{event_reason}`"),
            MarkdownBlock(f"*💬 Message:* {event_message}"),
            DividerBlock(),
        ]
        
        # Add issue details if extracted
        if issue_details:
            details_lines = []
            for key, value in issue_details.items():
                if value and value != "unknown":
                    # Format key nicely (e.g., "container_name" -> "Container Name")
                    formatted_key = key.replace('_', ' ').title()
                    details_lines.append(f"• {formatted_key}: `{value}`")
            
            if details_lines:
                details_info = f"*📊 Issue Details:*\n" + "\n".join(details_lines)
                enrichment_blocks.append(MarkdownBlock(details_info))
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
            enrichment_blocks.append(DividerBlock())
        
        # Add remediation callback button
        remediation_params = {
            "issue_type": issue_type,
            "namespace": resource_namespace,
            "resource_kind": resource_kind,
            "resource_name": resource_name,
            "event_reason": event_reason,
            "issue_details": issue_details,
            "remediation_strategy": "auto"
        }
        
        enrichment_blocks.append(
            CallbackBlock(
                {
                    "🔧 Trigger Auto-Remediation": CallbackChoice(
                        action=remediate_issue,
                        action_params=remediation_params
                    )
                }
            )
        )
        
        event.add_enrichment(enrichment_blocks)
        
    except AttributeError as e:
        print(f"AttributeError in lls_agent_remediation_action: {e}")
    except Exception as e:
        print(f"Unexpected error in lls_agent_remediation_action: {e}")

@action
def remediate_issue(event: EventChangeEvent, params: RemediationParams):
    """
    Callback action triggered when user clicks the remediation button.
    Sends remediation request to SREIPS remediation service.
    """
    try:
        # Build remediation payload matching RemediationRequest structure
        remediation_payload = {
            "issue_type": params.issue_type,
            "namespace": params.namespace,
            "resource": {
                "kind": params.resource_kind,
                "name": params.resource_name
            },
            "event_reason": params.event_reason,
            "issue_details": params.issue_details,
            "remediation_strategy": params.remediation_strategy
        }
        
        try:
            response = requests.post(
                REMEDIATION_ACTION_URL,
                json=remediation_payload,
                timeout=300
            )
            response.raise_for_status()
            result = response.json()
            
            status = result.get("status", "unknown")
            message = result.get("message", "No message returned")
            details = result.get("details", {})
            
            if status == "success" or status == "completed":
                # Build success message with details
                success_blocks = [
                    MarkdownBlock(f"*✅ Remediation Triggered Successfully*"),
                    MarkdownBlock(f"*Status:* {status}"),
                ]
                
                # Add tool execution count if available
                tool_executions = details.get("tool_executions", 0)
                if tool_executions > 0:
                    success_blocks.append(
                        MarkdownBlock(f"*Tool Executions:* {tool_executions}")
                    )
                
                # Add final message from agent
                if message and message != "No message returned":
                    # Truncate very long messages for Slack
                    if len(message) > 1000:
                        message = message[:1000] + "... (truncated)"
                    success_blocks.append(
                        MarkdownBlock(f"*Agent Response:*\n{message}")
                    )
                
                event.add_enrichment(success_blocks)
            else:
                # Build error/warning message
                error_blocks = [
                    MarkdownBlock(f"*⚠️ Remediation Request Status: {status}*"),
                ]
                
                if message and message != "No message returned":
                    error_blocks.append(
                        MarkdownBlock(f"*Details:* {message}")
                    )
                
                event.add_enrichment(error_blocks)
                
        except requests.exceptions.Timeout:
            event.add_enrichment([
                MarkdownBlock("*❌ Remediation Failed*"),
                MarkdownBlock("Request to remediation service timed out after 5 minutes"),
            ])
        except requests.exceptions.ConnectionError:
            event.add_enrichment([
                MarkdownBlock("*❌ Remediation Failed*"),
                MarkdownBlock(f"Could not connect to remediation service at {REMEDIATION_ACTION_URL}"),
            ])
        except requests.exceptions.HTTPError as e:
            event.add_enrichment([
                MarkdownBlock("*❌ Remediation Failed*"),
                MarkdownBlock(f"HTTP Error: {e.response.status_code} - {e.response.text[:200]}"),
            ])
        except Exception as e:
            event.add_enrichment([
                MarkdownBlock("*❌ Remediation Failed*"),
                MarkdownBlock(f"Error: {str(e)}"),
            ])
            
    except Exception as e:
        print(f"Error in remediate_issue callback: {e}")
        event.add_enrichment([
            MarkdownBlock("*❌ Remediation Error*"),
            MarkdownBlock(f"Unexpected error: {str(e)}"),
        ])

