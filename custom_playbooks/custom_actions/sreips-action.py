from robusta.api import *
from llama_stack_client import LlamaStackClient
from llama_stack_client import Agent, AgentEventLogger
import uuid

@action
def lls_agent_action(event: PodEvent):
    # we have full access to the pod on which the alert fired
    pod = event.get_pod()
    pod_name = pod.metadata.name
    pod_logs = pod.get_logs()

    client = LlamaStackClient(base_url="https://lls-route-llamastack.apps.cluster-5tptd.5tptd.sandbox2399.opentlc.com/")
    client.models.list()
    models = client.models.list()

    vector_db_id = "sreips_vector_id"
    model_id = next(m for m in models if m.model_type == "llm").identifier
    print(models)
    print(client.vector_dbs.list())

    client.toolgroups.register(
        toolgroup_id="mcp::rh-kcs-mcp",
        provider_id="model-context-protocol",
        mcp_endpoint={"uri" : "https://rh-kcs-mcp-servers.apps.cluster-5tptd.5tptd.sandbox2399.opentlc.com/sse"},
    )
    client.toolgroups.list()

    rag_agent = Agent(
        client,
        model=model_id,
        instructions="You are a helpful assistant",
        tools=[
            {
                "name": "builtin::rag/knowledge_search",
                "args": {"vector_db_ids": [vector_db_id]},
            }, 
            "mcp::rh-kcs-mcp",
        ],
        max_infer_iters=10
    )

    prompt = "Use the given rag search and find what's the resolution of volume attachment failures in kubernetes?"
    prompt = "Use the given rag search and find what's the resolution for pod crashloop backoff issues in kubernetes?"
    print("prompt>", prompt)

    session_id = rag_agent.create_session(session_name=f"s{uuid.uuid4().hex}")

    response = rag_agent.create_turn(
        messages=[{"role": "user", "content": prompt}],
        session_id=session_id,
        stream=True,

    )

    # for log in AgentEventLogger().log(response):
        # print(log)
        # log.print()

    rag_output = []
    for log in AgentEventLogger().log(response):
        if log.role != "inference" and log.role != "tool_execution":
            rag_output.append(log)

    if rag_output:
        print("FINAL RAG RESPONSE:\n")
        rag_results = "".join(str(x) for x in rag_output) 
        print(rag_results)
    else:
        print("No final RAG response found.")

    mcp_agent = Agent(
        client,
        model=model_id,
        instructions="You are a helpful assistant",
        tools=[
            "mcp::rh-kcs-mcp",
        ],max_infer_iters=100
    )

    input_prompt = "Find relevant knowledge articles for 'volume attachment failures in kubernetes'"
    input_prompt = "Find relevant knowledge articles for 'what's the resolution for pod crashloop backoff failures in kubernetes'?"

    session_id = mcp_agent.create_session(session_name=f"s{uuid.uuid4().hex}")

    response = mcp_agent.create_turn(
            messages=[{"role": "user","content": input_prompt}],
            session_id=session_id,
            stream=True,
        )

    import re, json

    mcp_output = []
    for log in AgentEventLogger().log(response):
        if log.role != "inference" and log.role != "tool_execution":
            mcp_output.append(log)

    if mcp_output:
        print("FINAL MCP RESPONSE:\n")
        mcp_results = "".join(str(x) for x in mcp_output) 
        print(mcp_results)
    else:
        print("No MCP response found.")    

    # mcp_output = []

    # for log in AgentEventLogger().log(response):
    #     if log.role == "tool_execution" and "Tool:search_kcs Response:" in log.content:
    #         # Extract the JSON array between the single quotes after 'TextContentItem(text='
    #         match = re.search(r"TextContentItem\(text='(.*?)', type='text'\)", log.content)
    #         if match:
    #             json_str = match.group(1)
    #             try:
    #                 data = json.loads(json_str)
    #                 for item in data:
    #                     title = item.get("title")
    #                     uri = item.get("view_uri")
    #                     mcp_output.append((title, " - ", uri))
    #                     # print(f"- {title}: {uri}")
    #             except json.JSONDecodeError as e:
    #                 print("Error decoding JSON:", e)  

    # mcp_results = "\n".join(f"{t[0]}{t[1]}{t[2]}" for t in mcp_output)

    # print(mcp_results)      

    # this is how you send data to slack or other destinations
    event.add_enrichment([
        # CallbackBlock(name="Pod Processes", callback=lambda: pod_processes),
        MarkdownBlock("*Oh no!* An alert occurred on " + pod_name),
        MarkdownBlock(rag_results + "\n\n" + mcp_results),
        FileBlock("crashing-pod.log", pod_logs)
    ])