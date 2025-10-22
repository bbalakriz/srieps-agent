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
        
        # Register MCP toolgroup
        try:
            client.toolgroups.register(
                toolgroup_id="mcp::rh-kcs-mcp",
                provider_id="model-context-protocol",
                mcp_endpoint={"uri": MCP_ENDPOINT},
            )
        except Exception as e:
            # Toolgroup might already be registered
            print(f"Toolgroup registration: {e}")

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
    # prompt = "Use the given rag search and find what's the resolution for pod crashloop backoff issues in kubernetes?"

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
    mcp_agent = Agent(
        client,
        model=model_id,
        instructions="You are a helpful assistant",
        tools=[
            "mcp::rh-kcs-mcp",
        ],
        max_infer_iters=100
    )

    session_id = mcp_agent.create_session(session_name=f"s{uuid.uuid4().hex}")
    # prompt = "Find relevant knowledge articles for 'what's the resolution for pod crashloop backoff failures in kubernetes'?"

    response = mcp_agent.create_turn(
        messages=[{"role": "user", "content": prompt}],
        session_id=session_id,
        stream=True,
    )

    mcp_output = []
    for log in AgentEventLogger().log(response):
        log.print()
        if log.role != "inference" and log.role != "tool_execution":
            mcp_output.append(log)

    if mcp_output:
        mcp_results = "".join(str(x) for x in mcp_output)
        return mcp_results
    else:
        return "No MCP response found."

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
