from llama_stack_client import LlamaStackClient
from llama_stack_client import Agent, AgentEventLogger
import uuid
import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any
import uvicorn
import json

# Initialize FastAPI app
app = FastAPI(title="SREIPS Remediation Agent API")

# Global client and configuration - externalized via environment variables
LLAMA_STACK_URL = os.getenv("LLAMA_STACK_URL", "")
OCP_MCP_ENDPOINT = os.getenv("OCP_MCP_ENDPOINT", "")

# Initialize client globally
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
        # it is strongly recommended to use larger models such as Llama-3.1-70B
        # or any specialized models explicitly trained for tool usage.
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
            # Toolgroup might already be registered
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
    resource_type = remediation_request.quota_details.get("resource_type", "unknown")
    requested = remediation_request.quota_details.get("requested", "unknown")
    current_limit = remediation_request.quota_details.get("current_limit", "unknown")
    event_reason = remediation_request.event_reason
    
    print(f"\n=== Remediation Request ===")
    print(f"Namespace: {namespace}")
    print(f"Resource Type: {resource_type}")
    print(f"Requested: {requested}")
    print(f"Current Limit: {current_limit}")
    print(f"Event Reason: {event_reason}")
    
    # List available tools before creating agent (for debugging)
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
    
    # Create remediation agent with very explicit, direct instructions
    # *************************************************************************
    # CRITICAL for Llama4-Scout-17B: Instructions MUST be SHORT and DIRECT
    # Long, complex instructions confuse smaller models and prevent tool use.
    # *************************************************************************
    remediation_agent = Agent(
        client,
        model=model_id,
        instructions=f"""Update OpenShift resource quota in namespace {namespace}.

Resource: {resource_type}
Current Limit: {current_limit}
Requested: {requested}

Task: Increase the quota limit to accommodate the requested amount.

Use the tools available to:
1. Get current quota settings
2. Update quota to new value
3. Confirm the update

Be direct and use the tools immediately.""",
        tools=["mcp::ocp-mcp"],
        max_infer_iters=100
    )
    
    print(f"Created remediation agent")
    
    session_id = remediation_agent.create_session(session_name=f"remediation-{uuid.uuid4().hex}")
    
    # Build a very simple, direct prompt for the agent
    # Smaller models respond better to imperative commands
    simple_prompt = f"Update resource quota for {resource_type} in namespace {namespace} from {current_limit} to accommodate {requested}"
    
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
        
        # Process agent response logs
        for log in AgentEventLogger().log(response):
            log.print()
            
            if log.role == "assistant":
                assistant_messages.append(log)
            elif log.role == "tool_execution":
                tool_executions.append(log)
                # Check for tool execution errors
                if hasattr(log, 'error') and log.error:
                    errors.append(str(log.error))
            elif log.role is None or log.role == "":
                if hasattr(log, 'content'):
                    streamed_content.append(str(log.content))
        
        print(f"\n=== Remediation Summary ===")
        print(f"Tool executions: {len(tool_executions)}")
        print(f"Assistant messages: {len(assistant_messages)}")
        print(f"Errors: {len(errors)}")
        
        # Determine success based on tool execution and responses
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
        
        # If we had tool executions and no errors, consider it successful
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
            # No tool executions - agent didn't use tools
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

@app.post("/remediate", response_model=RemediationResponse)
async def remediate(request: RemediationRequest):
    """
    Execute automated remediation for the given issue.
    Currently supports: resource_quota issues
    """
    try:
        if not client:
            raise HTTPException(status_code=503, detail="Client not initialized")
        
        # Validate request
        if request.issue_type != "resource_quota":
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported issue type: {request.issue_type}. Currently only 'resource_quota' is supported."
            )
        
        if not request.namespace or not request.namespace.strip():
            raise HTTPException(status_code=400, detail="Namespace cannot be empty")
        
        # Execute remediation based on issue type
        if request.issue_type == "resource_quota":
            result = execute_quota_remediation(request)
            
            return RemediationResponse(
                status=result["status"],
                message=result["message"],
                details=result.get("details")
            )
        else:
            raise HTTPException(status_code=400, detail="Invalid issue type")
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Unexpected error in remediate endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Error processing remediation: {str(e)}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)

