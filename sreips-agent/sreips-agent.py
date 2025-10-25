from llama_stack_client import LlamaStackClient
from llama_stack_client import Agent, AgentEventLogger
import uuid
import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import uvicorn

# Initialize FastAPI app
app = FastAPI(title="SREIPS Agent API")

# Global client and configuration - externalized via environment variables
LLAMA_STACK_URL = os.getenv("LLAMA_STACK_URL", "")
MCP_ENDPOINT = os.getenv("MCP_ENDPOINT", "")
VECTOR_DB_ID = os.getenv("VECTOR_DB_ID", "sreips_vector_id")

# Initialize client globally
client = None
model_id = None

class QueryRequest(BaseModel):
    query: str
    
class QueryResponse(BaseModel):
    combined_results: str

def initialize_client():
    """Initialize the LlamaStack client and register toolgroups"""
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
        
        # Register MCP toolgroup
        try:
            client.toolgroups.register(
                toolgroup_id="mcp::rh-kcs-mcp",
                provider_id="model-context-protocol",
                mcp_endpoint={"uri": MCP_ENDPOINT},
            )
            print("Successfully registered MCP toolgroup")
            
            # List available tools to verify registration
            tools = client.tools.list(toolgroup_id="mcp::rh-kcs-mcp")
            print(f"Available MCP tools: {[t.identifier for t in tools]}")
        except Exception as e:
            # Toolgroup might already be registered
            print(f"Toolgroup registration (may already exist): {e}")

def query_rag_agent(prompt: str) -> str:
    """Query the RAG agent with the given prompt"""
    rag_agent = Agent(
        client,
        model=model_id,
        instructions="You are a helpful assistant",
        tools=[
            {
                "name": "builtin::rag/knowledge_search",
                "args": {"vector_db_ids": [VECTOR_DB_ID]},
            }, 
        ],
        max_infer_iters=100
    )

    session_id = rag_agent.create_session(session_name=f"s{uuid.uuid4().hex}")
    
    response = rag_agent.create_turn(
        messages=[{"role": "user", "content": prompt}],
        session_id=session_id,
        stream=True,
    )

    rag_output = []
    for log in AgentEventLogger().log(response):
        log.print()
        if log.role != "inference" and log.role != "tool_execution":
            rag_output.append(log)

    if rag_output:
        rag_results = "".join(str(x) for x in rag_output)
        return rag_results
    else:
        return "No RAG response found."

def query_mcp_agent(prompt: str) -> str:
    """Query the MCP agent with the given prompt"""

    print("Received prompt for MCP agent:", prompt)
    
    # List available tools before creating agent (for debugging)
    try:
        available_tools = client.tools.list(toolgroup_id="mcp::rh-kcs-mcp")
        tool_identifiers = [t.identifier for t in available_tools]
        print(f"Available tools in mcp::rh-kcs-mcp: {tool_identifiers}")
    except Exception as e:
        print(f"Error listing tools: {e}")

    # Create agent with the toolgroup name (not individual tool names)
    # Keep instructions minimal for smaller models
    mcp_agent = Agent(
        client,
        model=model_id,
        instructions="""You are a helpful assistant. Search for relevant Red Hat knowledge articles.

Format each result as:
Title: [article title]
Link: [full view_uri URL]

Show the complete URL for each article so users can easily access them.""",
        tools=["mcp::rh-kcs-mcp"],
        max_infer_iters=100
    )
    
    print(f"Created agent with ID: {mcp_agent.agent_id if hasattr(mcp_agent, 'agent_id') else 'N/A'}")

    session_id = mcp_agent.create_session(session_name=f"s{uuid.uuid4().hex}")
    
    # Simplified prompt to trigger tool use more reliably with smaller models
    # More direct phrasing seems to work better
    enhanced_prompt = f"Find Red Hat solutions for: {prompt}"
    print(f"Prompt before calling MCP agent: {enhanced_prompt}")

    # Try streaming first (more responsive)
    try:
        response = mcp_agent.create_turn(
            messages=[{"role": "user", "content": enhanced_prompt}],
            session_id=session_id,
            stream=True,
        )

        mcp_output = []
        assistant_messages = []
        tool_responses = []
        streamed_content = []
        
        # Process all logs from the agent
        for log in AgentEventLogger().log(response):
            log.print()
            
            # Collect assistant messages (complete responses)
            if log.role == "assistant":
                assistant_messages.append(log)
            # Collect tool execution responses
            elif log.role == "tool_execution":
                tool_responses.append(log)
            # Collect streaming content tokens (role=None)
            elif log.role is None or log.role == "":
                # These are streaming tokens that form the complete response
                if hasattr(log, 'content'):
                    streamed_content.append(str(log.content))
            # Capture other non-inference logs
            elif log.role != "inference":
                mcp_output.append(log)

        print(f"\n=== Response Summary ===")
        print(f"Tool executions: {len(tool_responses)}")
        print(f"Streamed tokens: {len(streamed_content)}")

        # Try to extract the final response in order of preference
        if assistant_messages:
            # Get the last assistant message which should have the final formatted response
            last_message = assistant_messages[-1]
            result = str(last_message.content) if hasattr(last_message, 'content') else str(last_message)
            print(f"Returning assistant message ({len(result)} chars)")
            return result
        elif streamed_content:
            # Reconstruct the complete response from streaming tokens
            complete_response = "".join(streamed_content)
            print(f"Returning streamed content ({len(complete_response)} chars)")
            return complete_response
        elif mcp_output:
            mcp_results = "\n".join(str(x) for x in mcp_output)
            print(f"Returning other output ({len(mcp_results)} chars)")
            return mcp_results
        else:
            return "No response generated. The search may have returned empty results. Try different search terms."
    except Exception as e:
        print(f"Error during agent turn: {e}")
        return f"Error querying MCP agent: {str(e)}"

@app.on_event("startup")
async def startup_event():
    """Initialize client on startup"""
    initialize_client()

@app.get("/")
async def root():
    """Root endpoint"""
    return {"message": "SREIPS Agent API is running", "endpoints": ["/query"]}

@app.post("/query", response_model=QueryResponse)
async def query_agents(request: QueryRequest):
    """
    Query both RAG and MCP agents with the provided query string.
    Returns combined results from both agents.
    """
    try:
        if not request.query or not request.query.strip():
            raise HTTPException(status_code=400, detail="Query cannot be empty")
        
        # Query both agents
        rag_results = query_rag_agent(request.query)
        mcp_results = query_mcp_agent(request.query)
        
        # Combine results
        combined_results = f"=== RAG Results ===\n{rag_results}\n\n=== MCP Results ===\n{mcp_results}"
        
        return QueryResponse(
            combined_results=combined_results
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing query: {str(e)}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
