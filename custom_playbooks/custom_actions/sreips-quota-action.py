from robusta.api import *
import requests
import os
import re
from pydantic import BaseModel

# SREIPS Agent API endpoint - externalized
SREIPS_AGENT_URL = os.getenv("SREIPS_AGENT_URL", "http://sreips-agent.sreips-agent.svc.cluster.local:8000")

# Remediation action URL - externalized (points to remediation agent)
REMEDIATION_ACTION_URL = os.getenv("REMEDIATION_ACTION_URL", "http://remediation-agent.sreips-agent.svc.cluster.local:8080/remediate")

# Prompt mapping for ResourceQuota-related failures
# ********************************************************************************************
# WARNING: The below mapping of Kubernetes failure reasons to short search-optimized prompts
# is REQUIRED **ONLY** because Llama4-Scout-17B (or other compact/lite models) struggles with
# tool invocation and reasoning if you use more natural, verbose language.
# 
# If you use larger, more sophisticated models (e.g., Llama-3.1-70B or similar), you DO NOT
# need this brittle mapping; the agent will understand direct, full prompts naturally.
#
# THIS MAPPING IS A WORKAROUND FOR Llama4-Scout-17B/LiteMass limitations!
# ********************************************************************************************

PROMPT_MAPPINGS = {
    "FailedCreate": "pod creation failed resource quota exceeded OpenShift",
    "ExceededQuota": "resource quota exceeded OpenShift",
    "LimitExceeded": "resource limit exceeded OpenShift",
    "InsufficientMemory": "insufficient memory quota OpenShift",
    "InsufficientCPU": "insufficient CPU quota OpenShift",
    "QuotaExceeded": "namespace quota exceeded OpenShift"
}

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
    - Keep bullets and numbered lists as-is
    - Preserve code blocks with backticks
    """
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
    """Parameters for quota remediation callback"""
    namespace: str = "unknown"
    resource_kind: str = "unknown"
    resource_name: str = "unknown"
    event_reason: str = "unknown"
    quota_resource_type: str = "unknown"
    quota_requested: str = "unknown"
    quota_limit: str = "unknown"

def extract_quota_details(event_message: str) -> dict:
    """
    Extract resource quota details from event message
    Returns dict with quota information for remediation
    """
    details = {
        "resource_type": "unknown",
        "requested": "unknown",
        "limit": "unknown"
    }
    
    # Common patterns in quota-related messages
    # Example: "pods \"mypod\" is forbidden: exceeded quota: compute-resources, requested: cpu=2, used: cpu=8, limited: cpu=10"
    
    # Extract resource type (cpu, memory, pods, etc.)
    resource_match = re.search(r'requested:\s+([^=]+)=', event_message)
    if resource_match:
        details["resource_type"] = resource_match.group(1).strip()
    
    # Extract requested amount
    requested_match = re.search(r'requested:\s+[^=]+=([^,\s]+)', event_message)
    if requested_match:
        details["requested"] = requested_match.group(1).strip()
    
    # Extract limit
    limit_match = re.search(r'limited:\s+[^=]+=([^,\s]+)', event_message)
    if limit_match:
        details["limit"] = limit_match.group(1).strip()
    
    return details

@action
def lls_agent_quota_action(event: EventChangeEvent):
    try:
        # Get the Kubernetes event
        k8s_event = event.obj
        
        # DEBUG: Log the event structure
        print(f"DEBUG: Event type: {type(k8s_event)}")
        print(f"DEBUG: Event dir: {dir(k8s_event)}")
        print(f"DEBUG: Event dict (if available): {k8s_event.__dict__ if hasattr(k8s_event, '__dict__') else 'No __dict__'}")
        
        # Safely extract event details with defaults
        event_reason = getattr(k8s_event, 'reason', 'Unknown')
        event_message = getattr(k8s_event, 'message', 'No message available')
        event_type = getattr(k8s_event, 'type', 'Warning')
        
        print(f"DEBUG: event_reason={event_reason}, event_message={event_message}")
        
        # Safely get involved object details
        involved_obj = getattr(k8s_event, 'involvedObject', None)
        print(f"DEBUG: involved_obj={involved_obj}, type={type(involved_obj) if involved_obj else None}")
        
        if involved_obj:
            print(f"DEBUG: involved_obj dir: {dir(involved_obj)}")
            print(f"DEBUG: involved_obj dict: {involved_obj.__dict__ if hasattr(involved_obj, '__dict__') else 'No __dict__'}")
            
            resource_kind = getattr(involved_obj, 'kind', 'Unknown')
            resource_name = getattr(involved_obj, 'name', 'Unknown')
            resource_namespace = getattr(involved_obj, 'namespace', 'cluster-scoped')
            
            print(f"DEBUG: Extracted - kind={resource_kind}, name={resource_name}, namespace={resource_namespace}")
        else:
            resource_kind = 'Unknown'
            resource_name = 'Unknown'
            resource_namespace = 'Unknown'
            print(f"DEBUG: No involvedObject found!")
        
        # Extract quota details from message
        quota_details = extract_quota_details(event_message)
        
        # Map event reason to SREIPS prompt
        prompt = PROMPT_MAPPINGS.get(event_reason, f"{event_reason} resource quota OpenShift troubleshooting")
        
        # Query SREIPS Agent
        results = query_sreips_agent(prompt)
        combined_results = results.get("combined_results", "No results returned from SREIPS Agent")
        
        # Parse and format results
        rag_results, mcp_results = parse_combined_results(combined_results)
        
        # Build enrichment with event-specific context
        enrichment_blocks = [
            MarkdownBlock(f"*🚨 Resource Quota Issue:* `{event_reason}`"),
            MarkdownBlock(f"*📦 Resource:* {resource_kind} `{resource_name}` in `{resource_namespace}`"),
            MarkdownBlock(f"*💬 Message:* {event_message}"),
            DividerBlock(),
        ]
        
        # Add quota details if extracted
        if quota_details["resource_type"] != "unknown":
            quota_info = (
                f"*📊 Quota Details:*\n"
                f"• Resource Type: `{quota_details['resource_type']}`\n"
                f"• Requested: `{quota_details['requested']}`\n"
                f"• Limit: `{quota_details['limit']}`"
            )
            enrichment_blocks.append(MarkdownBlock(quota_info))
            enrichment_blocks.append(DividerBlock())
        
        # Add RAG results if available
        if rag_results:
            enrichment_blocks.append(
                MarkdownBlock(f"*📚 Knowledge Base Resolution:*\n{rag_results}")
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
            "namespace": resource_namespace,
            "resource_kind": resource_kind,
            "resource_name": resource_name,
            "event_reason": event_reason,
            "quota_resource_type": quota_details["resource_type"],
            "quota_requested": quota_details["requested"],
            "quota_limit": quota_details["limit"]
        }
        
        enrichment_blocks.append(
            CallbackBlock(
                {
                    "🔧 Trigger Auto-Remediation": CallbackChoice(
                        action=remediate_quota_issue,
                        action_params=remediation_params
                    )
                }
            )
        )
        
        # Send enrichment to destinations
        event.add_enrichment(enrichment_blocks)
        
    except AttributeError as e:
        # Handle missing attributes gracefully
        print(f"AttributeError in lls_agent_quota_action: {e}")        
    except Exception as e:
        # Catch any other unexpected errors
        print(f"Unexpected error in lls_agent_quota_action: {e}")

@action
def remediate_quota_issue(event: EventChangeEvent, params: RemediationParams):
    """
    Callback action triggered when user clicks the remediation button
    Sends remediation request to SREIPS remediation service
    """
    try:
        namespace = params.namespace
        resource_kind = params.resource_kind
        resource_name = params.resource_name
        event_reason = params.event_reason
        quota_resource_type = params.quota_resource_type
        quota_requested = params.quota_requested
        quota_limit = params.quota_limit
        
        # Prepare remediation request payload
        remediation_payload = {
            "issue_type": "resource_quota",
            "namespace": namespace,
            "resource": {
                "kind": resource_kind,
                "name": resource_name
            },
            "event_reason": event_reason,
            "quota_details": {
                "resource_type": quota_resource_type,
                "requested": quota_requested,
                "current_limit": quota_limit
            },
            "remediation_strategy": "auto"
        }
        
        # Call remediation service
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
            
            if status == "success":
                event.add_enrichment([
                    MarkdownBlock(f"*✅ Remediation Triggered Successfully*"),
                    MarkdownBlock(f"*Response:* {message}"),
                ])
            else:
                event.add_enrichment([
                    MarkdownBlock(f"*⚠️ Remediation Request Status: {status}*"),
                    MarkdownBlock(f"*Details:* {message}"),
                ])
                
        except requests.exceptions.Timeout:
            event.add_enrichment([
                MarkdownBlock("*❌ Remediation Failed*"),
                MarkdownBlock("Request to remediation service timed out"),
            ])
        except requests.exceptions.ConnectionError:
            event.add_enrichment([
                MarkdownBlock("*❌ Remediation Failed*"),
                MarkdownBlock(f"Could not connect to remediation service at {REMEDIATION_ACTION_URL}"),
            ])
        except Exception as e:
            event.add_enrichment([
                MarkdownBlock("*❌ Remediation Failed*"),
                MarkdownBlock(f"Error: {str(e)}"),
            ])
            
    except Exception as e:
        print(f"Error in remediate_quota_issue callback: {e}")
        event.add_enrichment([
            MarkdownBlock("*❌ Remediation Error*"),
            MarkdownBlock(f"Unexpected error: {str(e)}"),
        ])

