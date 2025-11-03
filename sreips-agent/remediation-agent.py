from llama_stack_client import LlamaStackClient
from llama_stack_client import Agent, AgentEventLogger
import os
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any
import json
import logging
import re
import threading
import requests
import uuid
import uvicorn

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="SREIPS Remediation Agent API")

LLAMA_STACK_URL = os.getenv("LLAMA_STACK_URL", "https://lsd-llama-milvus-service-llamastack.apps.cluster-wvkq8.wvkq8.sandbox3266.opentlc.com/")
OCP_MCP_ENDPOINT = os.getenv("OCP_MCP_ENDPOINT", "https://ocp-mcp-mcp-servers.apps.cluster-wvkq8.wvkq8.sandbox3266.opentlc.com/sse")

client = None
model_id = None

class RemediationRequest(BaseModel):
    issue_type: str
    namespace: str
    resource: Dict[str, str]
    event_reason: str
    quota_details: Dict[str, str]
    remediation_strategy: str = "auto"

class RemediationResponse(BaseModel):
    status: str
    message: str
    details: Optional[Dict[str, Any]] = None

def initialize_client():
    """Initialize the LlamaStack client and register OCP MCP toolgroup"""
    global client, model_id
    
    if client is None:
        client = LlamaStackClient(base_url=LLAMA_STACK_URL)
        models = client.models.list()
        model_id = next(m for m in models if m.model_type == "llm").identifier
        print(f"Initialized with model: {model_id}")
        
        # Check if model is suitable for tool calling
        # *********************************************************************
        # WARNING: Using smaller models like Llama4-Scout-17B or similar WILL
        # result in UNPREDICTABLE TOOL EXECUTION BEHAVIOR and AGENT FAILURES.
        # For consistent and robust agentic functionalities (tool calls, sequences),
        # use larger models like claude sonnet or any specialized models.
        #  At the time of writing this, the integration of Anthropic models is broken with llamastack.
        #  See https://github.com/llamastack/llama-stack/issues/2504 for more details.
        # *********************************************************************
        if "17B" in model_id or "Scout" in model_id:
            print("⚠️  WARNING: Smaller models (17B) may have inconsistent tool execution behavior.")
            print("    For production, consider using Llama-3.1-70B or larger models trained for tool use.")
        
        # Register OCP MCP toolgroup
        try:
            client.toolgroups.register(
                toolgroup_id="mcp::ocp-mcp",
                provider_id="model-context-protocol",
                mcp_endpoint={"uri": OCP_MCP_ENDPOINT},
            )
            print("Successfully registered OCP MCP toolgroup")
            
            # List available tools to verify registration
            tools = client.tools.list(toolgroup_id="mcp::ocp-mcp")
            print(f"Available OCP MCP tools: {[t.identifier for t in tools]}")
        except Exception as e:
            print(f"Toolgroup registration (may already exist): {e}")

def execute_quota_remediation(remediation_request: RemediationRequest) -> Dict[str, Any]:
    """
    Execute quota remediation using OCP MCP agent
    
    This function creates an agent that:
    1. Analyzes the quota issue
    2. Calls appropriate tools from ocp-mcp to update resource quotas
    3. Returns the result of the remediation
    """
    
    namespace = remediation_request.namespace
    quota_name = remediation_request.quota_details.get("quota_name", "unknown")
    resource_type = remediation_request.quota_details.get("resource_type", "unknown")
    requested = remediation_request.quota_details.get("requested", "unknown")
    current_limit = remediation_request.quota_details.get("current_limit", "unknown")
    event_reason = remediation_request.event_reason
    
    print(f"\n=== Remediation Request ===")
    print(f"Namespace: {namespace}")
    print(f"Quota Name: {quota_name}")
    print(f"Resource Type: {resource_type}")
    print(f"Requested: {requested}")
    print(f"Current Limit: {current_limit}")
    print(f"Event Reason: {event_reason}")
    
    try:
        available_tools = client.tools.list(toolgroup_id="mcp::ocp-mcp")
        tool_identifiers = [t.identifier for t in available_tools]
        print(f"Available tools in mcp::ocp-mcp: {tool_identifiers}")
    except Exception as e:
        print(f"Error listing tools: {e}")
        return {
            "status": "error",
            "message": f"Failed to list available tools: {str(e)}"
        }
    
    # Using delete-then-recreate to avoid server-side apply conflicts
    # Extract resource info for triggering retry
    resource_kind = remediation_request.resource.get("kind", "unknown")
    resource_name = remediation_request.resource.get("name", "unknown")
    
    remediation_agent = Agent(
        client,
        model=model_id,
        instructions=f"""You are a Kubernetes resource quota remediation agent. Execute tools directly without explanation.

CONTEXT:
- Namespace: {namespace}
- ResourceQuota Name: {quota_name}
- Resource Type Exceeded: {resource_type}
- Requested Amount: {requested}
- Current Limit: {current_limit}
- Affected Resource: {resource_kind}/{resource_name}
- Event Reason: {event_reason}

OBJECTIVE:
Remediate the ResourceQuota '{quota_name}' in namespace '{namespace}' to allow the requested resource amount.

APPROACH:
1. Retrieve the current ResourceQuota to understand its configuration
2. Delete the existing ResourceQuota (to avoid server-side apply conflicts)
3. Create a new ResourceQuota with updated limits that accommodate the requested amount:
   - Set the limit for '{resource_type}' to a value that exceeds '{requested}'
   - Maintain reasonable limits for all other resource types (cpu, memory)
   - Use conservative scaling: set new limits 1.5-2x the requested amount
4. Trigger a retry by deleting the affected {resource_kind} resource so it can be recreated

IMPORTANT:
- Ensure all quota spec.hard fields include both requests and limits for cpu and memory
- Calculate appropriate values based on the context provided
- Execute all tool calls without asking for confirmation
""",
        tools=["mcp::ocp-mcp"],
        max_infer_iters=10
    )
    
    print(f"Created remediation agent")
    
    session_id = remediation_agent.create_session(session_name=f"remediation-{uuid.uuid4().hex}")
    
    simple_prompt = "Execute the steps"
    
    print(f"Executing remediation with prompt: {simple_prompt}")
    
    try:
        response = remediation_agent.create_turn(
            messages=[{"role": "user", "content": simple_prompt}],
            session_id=session_id,
            stream=True,
        )
        
        assistant_messages = []
        tool_executions = []
        streamed_content = []
        errors = []
        error_counts = {} 
        
        for log in AgentEventLogger().log(response):
            log.print()
            
            if log.role == "assistant":
                assistant_messages.append(log)
            elif log.role == "tool_execution":
                tool_executions.append(log)
                error_msg = None
                
                if hasattr(log, 'error') and log.error:
                    error_msg = str(log.error)
                elif hasattr(log, 'content'):
                    content_str = str(log.content)
                    if 'failed' in content_str.lower() or 'error' in content_str.lower():
                        error_msg = content_str
                
                if error_msg:
                    errors.append(error_msg)
                    error_counts[error_msg] = error_counts.get(error_msg, 0) + 1
                    
                    # Exit early if same error repeats 2+ times
                    if error_counts[error_msg] >= 2:
                        print(f"ERROR: Same error repeated {error_counts[error_msg]} times. Stopping agent.")
                        break
            elif log.role is None or log.role == "":
                if hasattr(log, 'content'):
                    streamed_content.append(str(log.content))
        
        print(f"\n=== Remediation Summary ===")
        print(f"Tool executions: {len(tool_executions)}")
        print(f"Assistant messages: {len(assistant_messages)}")
        print(f"Errors: {len(errors)}")
        
        if errors:
            return {
                "status": "error",
                "message": f"Tool execution failed: {'; '.join(errors)}",
                "details": {
                    "namespace": namespace,
                    "resource_type": resource_type,
                    "tool_executions": len(tool_executions)
                }
            }
        
        # Extract final response
        final_message = ""
        if assistant_messages:
            last_message = assistant_messages[-1]
            final_message = str(last_message.content) if hasattr(last_message, 'content') else str(last_message)
        elif streamed_content:
            final_message = "".join(streamed_content)
        
        if tool_executions:
            return {
                "status": "success",
                "message": f"Successfully remediated quota issue. {final_message}",
                "details": {
                    "namespace": namespace,
                    "resource_type": resource_type,
                    "requested": requested,
                    "previous_limit": current_limit,
                    "tool_executions": len(tool_executions),
                    "agent_response": final_message[:500]  # Truncate long responses
                }
            }
        else:
            return {
                "status": "warning",
                "message": f"Agent responded but did not execute tools. Response: {final_message[:200]}",
                "details": {
                    "namespace": namespace,
                    "resource_type": resource_type,
                    "agent_response": final_message
                }
            }
            
    except Exception as e:
        print(f"Error during remediation: {e}")
        return {
            "status": "error",
            "message": f"Remediation failed with error: {str(e)}",
            "details": {
                "namespace": namespace,
                "resource_type": resource_type
            }
        }

@app.on_event("startup")
async def startup_event():
    """Initialize client on startup"""
    initialize_client()

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "SREIPS Remediation Agent API is running",
        "endpoints": ["/remediate", "/health"],
        "supported_issues": ["resource_quota"]
    }

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "client_initialized": client is not None,
        "model": model_id if model_id else "not initialized"
    }

# This extraction step is necessary due to the less reliable output formatting of the 
# llama4-scout-17b model from litemass. If using more robust models like Claude Sonnet, 
# this workaround is not needed. This is a temporary solution to enable successful 
# tool calling with llama4-scout-17b.
# Note: This workaround is required because of https://github.com/llamastack/llama-stack/issues/2504,
# due to which the Claude Sonnet integration with llamastack is broken.
def extract_from_slack_message(message_blocks: list) -> Dict[str, str]:
    """
    Extract quota information from Slack message blocks
    """
    data = {
        "namespace": "unknown",
        "resource_kind": "unknown",
        "resource_name": "unknown",
        "event_reason": "unknown",
        "quota_name": "unknown",
        "quota_resource_type": "unknown",
        "quota_requested": "unknown",
        "quota_limit": "unknown"
    }
    
    for block in message_blocks:
        if block.get("type") == "section" and "text" in block:
            text = block["text"].get("text", "")
            
            # Extract Resource info: "Resource: ReplicaSet `name` in `namespace`"
            resource_match = re.search(r'Resource:\*\*?\s+(\w+)\s+`([^`]+)`\s+in\s+`([^`]+)`', text)
            if resource_match:
                data["resource_kind"] = resource_match.group(1)
                data["resource_name"] = resource_match.group(2)
                data["namespace"] = resource_match.group(3)
                logger.info(f"Extracted resource: {data['resource_kind']}/{data['resource_name']} in {data['namespace']}")
            
            # Extract Event Reason: "Resource Quota Issue: `FailedCreate`"
            reason_match = re.search(r'Resource Quota Issue:\*\*?\s+`([^`]+)`', text)
            if reason_match:
                data["event_reason"] = reason_match.group(1)
                logger.info(f"Extracted reason: {data['event_reason']}")
            
            # Extract Quota Details block
            if "Quota Details:" in text:
                # Quota Name: `test-quota`
                name_match = re.search(r'Quota Name:\s+`([^`]+)`', text)
                if name_match:
                    data["quota_name"] = name_match.group(1)
                
                # Resource Type: `requests.cpu`
                type_match = re.search(r'Resource Type:\s+`([^`]+)`', text)
                if type_match:
                    data["quota_resource_type"] = type_match.group(1)
                
                req_match = re.search(r'Requested:\s+`([^`]+)`', text)
                if req_match:
                    data["quota_requested"] = req_match.group(1)
                
                limit_match = re.search(r'Limit:\s+`([^`]+)`', text)
                if limit_match:
                    data["quota_limit"] = limit_match.group(1)
                
                logger.info(f"Extracted quota: name={data['quota_name']}, type={data['quota_resource_type']}, requested={data['quota_requested']}, limit={data['quota_limit']}")
    
    return data

def run_remediation_async(remediation_data: RemediationRequest, response_url: str):
    """Execute remediation in background and post results to Slack"""
    # Send immediate confirmation
    if response_url:
        try:
            requests.post(response_url, json={
                "replace_original": True,
                "text": f"Remediation triggered for namespace *{remediation_data.namespace}*"
            }, headers={"Content-Type": "application/json"})
            logger.info("Posted immediate confirmation to Slack")
        except Exception as e:
            logger.error(f"Failed to post immediate confirmation: {e}")
    
    try:
        logger.info(f"Starting async remediation for namespace: {remediation_data.namespace}")
        result = execute_quota_remediation(remediation_data)
        
        if result.get("status") == "success":
            message = "Remediation completed successfully"
        else:
            message = f"Remediation failed: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error(f"Remediation error: {e}")
        message = f"Remediation error: {str(e)}"
    
    # Post result to Slack
    if response_url:
        try:
            requests.post(response_url, json={
                "replace_original": True,
                "text": message
            }, headers={"Content-Type": "application/json"})
            logger.info(f"Posted result to Slack: {message}")
        except Exception as e:
            logger.error(f"Failed to post to Slack: {e}")

@app.post("/remediate")
async def remediate(http_request: Request, payload: Optional[str] = Form(None)):
    """
    Execute automated remediation for the given issue.
    Handles both JSON and Slack form-encoded payloads.
    Currently supports: resource_quota issues
    """
    logger.info("=" * 60)
    logger.info("RAW REMEDIATION REQUEST RECEIVED")
    logger.info(f"Method: {http_request.method}")
    logger.info(f"URL: {http_request.url}")
    logger.info(f"Content-Type: {http_request.headers.get('content-type')}")
    logger.info(f"Has form payload: {payload is not None}")
    if payload:
        logger.info(f"Payload (first 500 chars): {payload[:500]}")
    logger.info("=" * 60)
    
    remediation_data = None
    response_url = None
    
    if payload:
        logger.info("Detected Slack form payload")
        try:
            slack_data = json.loads(payload)
            logger.info(f"Slack payload type: {slack_data.get('type')}")
            
            # Extract response_url for async callback
            response_url = slack_data.get("response_url")
            logger.info(f"Slack response_url: {response_url}")
            
            if "message" in slack_data and "blocks" in slack_data["message"]:
                extracted = extract_from_slack_message(slack_data["message"]["blocks"])
                
                remediation_data = RemediationRequest(
                    issue_type="resource_quota",
                    namespace=extracted["namespace"],
                    resource={
                        "kind": extracted["resource_kind"],
                        "name": extracted["resource_name"]
                    },
                    event_reason=extracted["event_reason"],
                    quota_details={
                        "quota_name": extracted["quota_name"],
                        "resource_type": extracted["quota_resource_type"],
                        "requested": extracted["quota_requested"],
                        "current_limit": extracted["quota_limit"]
                    },
                    remediation_strategy="auto"
                )
                logger.info("Successfully parsed Slack payload")
            else:
                raise HTTPException(status_code=400, detail="No message blocks found in Slack payload")
                
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Slack JSON: {e}")
            raise HTTPException(status_code=400, detail=f"Invalid JSON in payload: {e}")
    else:
        logger.info("Attempting to parse as JSON body")
        try:
            json_body = await http_request.json()
            remediation_data = RemediationRequest(**json_body)
            logger.info("Successfully parsed JSON body")
        except Exception as e:
            logger.error(f"Failed to parse as JSON: {e}")
            raise HTTPException(status_code=400, detail=f"Could not parse request: {e}")
    
    logger.info("PARSED REMEDIATION REQUEST:")
    logger.info(f"Issue Type: {remediation_data.issue_type}")
    logger.info(f"Namespace: {remediation_data.namespace}")
    logger.info(f"Resource: {remediation_data.resource}")
    logger.info(f"Event Reason: {remediation_data.event_reason}")
    logger.info(f"Quota Details: {remediation_data.quota_details}")
    logger.info("=" * 60)
    
    # If Slack request with response_url, run async and return immediately
    if response_url:
        logger.info("Launching async remediation for Slack")
        thread = threading.Thread(
            target=run_remediation_async,
            args=(remediation_data, response_url),
            daemon=True
        )
        thread.start()
        return {"text": "Remediation in progress...", "response_type": "ephemeral"}
    
    # Otherwise, run synchronously (for direct API calls)
    try:
        if not client:
            logger.error("Client not initialized")
            raise HTTPException(status_code=503, detail="Client not initialized")
        
        if remediation_data.issue_type != "resource_quota":
            logger.warning(f"Unsupported issue type: {remediation_data.issue_type}")
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported issue type: {remediation_data.issue_type}. Currently only 'resource_quota' is supported."
            )
        
        if not remediation_data.namespace or not remediation_data.namespace.strip():
            logger.warning("Empty namespace provided")
            raise HTTPException(status_code=400, detail="Namespace cannot be empty")
        
        if remediation_data.issue_type == "resource_quota":
            logger.info("Executing quota remediation...")
            result = execute_quota_remediation(remediation_data)
            
            logger.info(f"Remediation result: {result['status']}")
            return RemediationResponse(
                status=result["status"],
                message=result["message"],
                details=result.get("details")
            )
        else:
            logger.error(f"Invalid issue type: {remediation_data.issue_type}")
            raise HTTPException(status_code=400, detail="Invalid issue type")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error in remediate endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Error processing remediation: {str(e)}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)

