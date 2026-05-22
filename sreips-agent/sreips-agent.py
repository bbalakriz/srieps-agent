from llama_stack_client import LlamaStackClient
from llama_stack_client import Agent
import uuid
import os
import asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
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
_vector_store_uuid = None  # Cached UUID after lazy resolution

class QueryRequest(BaseModel):
    query: str
    
class QueryResponse(BaseModel):
    combined_results: str

def get_vector_store_uuid() -> str:
    """Lazily resolve vector store name to UUID on first use (cached thereafter)."""
    global _vector_store_uuid
    if _vector_store_uuid is None:
        if '-' not in VECTOR_DB_ID:
            stores = client.vector_stores.list()
            matching = next((s for s in stores.data if s.name == VECTOR_DB_ID), None)
            if not matching:
                raise ValueError(f"Vector store '{VECTOR_DB_ID}' not found. Is the pipeline still running?")
            _vector_store_uuid = matching.id
        else:
            _vector_store_uuid = VECTOR_DB_ID
    return _vector_store_uuid

def initialize_client():
    """Initialize the LlamaStack client and register toolgroups"""
    global client, model_id
    
    if client is None:
        client = LlamaStackClient(base_url=LLAMA_STACK_URL)
        models = client.models.list()
        model_id = os.getenv("MODEL_ID", "gpt-5.2")
        print(f"Initialized with model: {model_id}")
                
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
            print(f"Available MCP tools: {[t.name for t in tools]}")
        except Exception as e:
            # Toolgroup might already be registered
            print(f"Toolgroup registration (may already exist): {e}")

def query_rag_agent(prompt: str) -> str:
    """Query the RAG agent with the given prompt"""
    print("Received prompt for RAG agent:", prompt)
    
    vector_store_uuid = get_vector_store_uuid()
    
    rag_agent = Agent(
        client,
        model=model_id,
        instructions="You are a helpful assistant. Use the tool to search the knowledge base for the best answer.",
        tools=[
            {
                "type": "file_search",
                "vector_store_ids": [vector_store_uuid],
            },
        ],
    )

    session_id = rag_agent.create_session(session_name=f"s{uuid.uuid4().hex}")
    
    response = rag_agent.create_turn(
        messages=[{"role": "user", "content": prompt}],
        session_id=session_id,
        stream=True,
    )

    # Process streaming response - 0.3.1 API
    output_text = ""
    streamed_text = ""  # capture text as it streams (includes file tokens)
    
    for chunk in response:
        if hasattr(chunk, 'event') and hasattr(chunk.event, 'event_type'):
            event_type = chunk.event.event_type
            
            # extract text from step_progress events (incremental text)
            if event_type == "step_progress":
                if hasattr(chunk.event, 'delta') and hasattr(chunk.event.delta, 'text'):
                    text = chunk.event.delta.text
                    streamed_text += text  # capture with file tokens
                    print(text, end='', flush=True)
            
            # extract final text from turn_completed event
            elif event_type == "turn_completed":
                if hasattr(chunk.event, 'final_text'):
                    output_text = chunk.event.final_text
                    print(f"\n\n=== RAG Turn Completed ===")
                    break

    # analyze the streamed text for file references
    print(f"\n{'='*60}")
    print(f"RAG RESPONSE ANALYSIS")
    print(f"{'='*60}")
    
    import re
    # extract file reference tokens from streamed text (not final_text which strips them)
    file_refs = re.findall(r'<\|file-([a-f0-9]+)\|>', streamed_text)
    
    if file_refs:
        print(f"\n FILE REFERENCE TOKENS FOUND: {len(file_refs)} references")
        print(f"These document IDs prove the LLM is citing vector database content:\n")
        for idx, ref in enumerate(file_refs, 1):
            print(f"  {idx}. file-{ref}")
    else:
        print(f"\n✗ WARNING: No file reference tokens found")
        print(f"LLM may not be using vector database content")
    
    print(f"\nResponse length: {len(output_text)} chars")
    print(f"{'='*60}\n")
    
    if output_text:
        return output_text
    else:
        print("No RAG response found.")
        return "No RAG response found."

def query_mcp_agent(prompt: str) -> str:
    """Query the MCP agent with the given prompt"""

    print("Received prompt for MCP agent:", prompt)
    
    # List available tools before creating agent (for debugging)
    try:
        available_tools = client.tools.list(toolgroup_id="mcp::rh-kcs-mcp")
        tool_identifiers = [t.name for t in available_tools]
        print(f"Available tools in mcp::rh-kcs-mcp: {tool_identifiers}")
    except Exception as e:
        print(f"Error listing tools: {e}")

    # Create agent with MCP tool specification
    # Keep instructions minimal for smaller models
    mcp_agent = Agent(
        client,
        model=model_id,
        instructions="""You are a helpful assistant. Search for relevant Red Hat knowledge articles.

Format each result as:
Title: [article title]
Link: [full view_uri URL]

Show the complete URL for each article so users can easily access them.""",
        tools=[
            {
                "type": "mcp",
                "server_url": MCP_ENDPOINT,
                "server_label": "mcp::rh-kcs-mcp",
            }
        ],
    )
    
    print(f"Created agent with ID: {mcp_agent.agent_id if hasattr(mcp_agent, 'agent_id') else 'N/A'}")

    session_id = mcp_agent.create_session(session_name=f"s{uuid.uuid4().hex}")
    
    # Simplified prompt to trigger tool use more reliably with smaller models
    # More direct phrasing seems to work better
    enhanced_prompt = f"Find Red Hat solutions for: {prompt}"
    print(f"Prompt before calling MCP agent: {enhanced_prompt}")

    # Process streaming response - 0.3.1 API
    try:
        response = mcp_agent.create_turn(
            messages=[{"role": "user", "content": enhanced_prompt}],
            session_id=session_id,
            stream=True,
        )

        output_text = ""
        tool_executions = []
        
        # Process streaming response directly - 0.3.1 API
        for chunk in response:
            if hasattr(chunk, 'event') and hasattr(chunk.event, 'event_type'):
                event_type = chunk.event.event_type
                
                # Extract text from step_progress events (incremental text)
                if event_type == "step_progress":
                    if hasattr(chunk.event, 'delta') and hasattr(chunk.event.delta, 'text'):
                        text = chunk.event.delta.text
                        output_text += text
                        print(text, end='', flush=True)
                
                # Extract final text from turn_completed event
                elif event_type == "turn_completed":
                    if hasattr(chunk.event, 'final_text'):
                        output_text = chunk.event.final_text
                        print(f"\n=== Turn Completed ===")
                        break
                    # Fallback: if final_text not available, use accumulated text
                    elif output_text:
                        break

        print(f"\n=== Response Summary ===")
        print(f"Final text length: {len(output_text)} chars")

        if output_text:
            return output_text
        else:
            return "No response generated. The search may have returned empty results. Try different search terms."
            
    except Exception as e:
        print(f"Error during agent turn: {e}")
        import traceback
        traceback.print_exc()
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

        curl -X POST "http://localhost:8000/query" \
         -H "Content-Type: application/json" \
         -d '{"query": "CrashLoopBackOff OpenShift pod"}'   
    """
    try:
        if not request.query or not request.query.strip():
            raise HTTPException(status_code=400, detail="Query cannot be empty")
        
        # run blocking sync functions in a thread pool so the event loop
        # stays free to serve liveness probes during long LlamaStack calls
        rag_results = await asyncio.to_thread(query_rag_agent, request.query)
        mcp_results = await asyncio.to_thread(query_mcp_agent, request.query)
        
        # Combine results
        combined_results = f"=== RAG Results ===\n{rag_results}\n\n=== MCP Results ===\n{mcp_results}"
        
        return QueryResponse(
            combined_results=combined_results
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing query: {str(e)}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
