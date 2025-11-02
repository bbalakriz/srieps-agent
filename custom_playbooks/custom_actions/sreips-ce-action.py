from robusta.api import *
import requests
import os
import re

# SREIPS Agent API endpoint - externalized
SREIPS_AGENT_URL = os.getenv("SREIPS_AGENT_URL", "http://sreips-agent.sreips-agent.svc.cluster.local:8000")

# Prompt mapping based on common Kubernetes failure reasons
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
    "FailedScheduling": "pod scheduling failure OpenShift",
    "PersistentVolumeClaimNotBound": "PVC not bound OpenShift",
    "VolumeAttachFailed": "volume attachment failure OpenShift",
    "ProvisioningFailed": "failed pvc provisioning openShift",
    "FailedMount": "volume mount failure OpenShift",
    "FailedAttachVolume": "volume attachment failure OpenShift"
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

@action
def lls_agent_event_action(event: EventChangeEvent):
    try:
        # Get the Kubernetes event (using documented approach)
        k8s_event = event.obj
        
        # Safely extract event details with defaults
        event_reason = getattr(k8s_event, 'reason', 'Unknown')
        event_message = getattr(k8s_event, 'message', 'No message available')
        event_type = getattr(k8s_event, 'type', 'Warning')
        
        # Safely get involved object details
        involved_obj = getattr(k8s_event, 'involvedObject', None)
        if involved_obj:
            resource_kind = getattr(involved_obj, 'kind', 'Unknown')
            resource_name = getattr(involved_obj, 'name', 'Unknown')
            # Handle cluster-scoped resources that don't have namespaces
            resource_namespace = getattr(involved_obj, 'namespace', 'cluster-scoped')
        else:
            resource_kind = 'Unknown'
            resource_name = 'Unknown'
            resource_namespace = 'Unknown'
        
        # Map event reason to SREIPS prompt
        # Use existing PROMPT_MAPPINGS or construct a generic query
        prompt = PROMPT_MAPPINGS.get(event_reason, f"{event_reason} OpenShift troubleshooting")
        
        # Query SREIPS Agent
        results = query_sreips_agent(prompt)
        combined_results = results.get("combined_results", "No results returned from SREIPS Agent")
        
        # Parse and format results
        rag_results, mcp_results = parse_combined_results(combined_results)
        
        # Build enrichment with event-specific context
        enrichment_blocks = [
            MarkdownBlock(f"*🚨 Kubernetes Event:* `{event_reason}`"),
            MarkdownBlock(f"*📦 Resource:* {resource_kind} `{resource_name}` in `{resource_namespace}`"),
            MarkdownBlock(f"*💬 Message:* {event_message}"),
            DividerBlock(),
        ]
        
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
        event.add_enrichment(enrichment_blocks)
        
    except AttributeError as e:
        # Handle missing attributes gracefully
        print(f"AttributeError in lls_agent_event_action: {e}")        
    except Exception as e:
        # Catch any other unexpected errors
        print(f"Unexpected error in lls_agent_event_action: {e}")
