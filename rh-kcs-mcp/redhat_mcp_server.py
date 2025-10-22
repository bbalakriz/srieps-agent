#!/usr/bin/env python3
"""
Red Hat API MCP Server

This MCP server implements tools to interact with Red Hat APIs:
1. Search Red Hat KCS Solutions
2. Get a Solution by ID
3. Search Red Hat Cases
4. Get Case details

The server uses the Model Context Protocol (MCP) to expose these tools to LLM applications.
"""

import os
import json
from typing import Optional, List, Dict, Any, Union
import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# Load environment variables
load_dotenv()

# Create MCP server
mcp = FastMCP("RedHat KCS API")

# Configuration
class RedHatAPI:
    """Red Hat API client with authentication and request handling."""
    
    def __init__(self):
        self.base_url = "https://access.redhat.com"
        self.sso_url = "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
        self.offline_token = os.getenv("RH_API_OFFLINE_TOKEN")
        if not self.offline_token:
            raise ValueError("RH_API_OFFLINE_TOKEN environment variable is required")
        
        self.access_token = None
        self.token_expiry = None
        
    async def get_access_token(self) -> str:
        """Get a valid access token, refreshing if necessary."""
        if self.access_token and self.token_expiry and datetime.now() < self.token_expiry:
            return self.access_token
        
        # Refresh token
        async with httpx.AsyncClient() as client:
            payload = {
                "grant_type": "refresh_token",
                "client_id": "rhsm-api",
                "refresh_token": self.offline_token
            }
            headers = {
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            response = await client.post(self.sso_url, data=payload, headers=headers)
            response.raise_for_status()
            
            data = response.json()
            self.access_token = data["access_token"]
            # Set expiry time slightly before actual expiry to be safe
            self.token_expiry = datetime.now() + timedelta(seconds=data["expires_in"] - 60)
            
            return self.access_token
    
    async def make_request(self, method: str, path: str, data: Optional[Dict] = None) -> Dict:
        """Make an authenticated request to the Red Hat API."""
        token = await self.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        url = f"{self.base_url}{path}"
        
        async with httpx.AsyncClient() as client:
            if method.lower() == "get":
                response = await client.get(url, headers=headers)
            elif method.lower() == "post":
                response = await client.post(url, json=data, headers=headers)
            else:
                raise ValueError(f"Unsupported method: {method}")
            
            response.raise_for_status()
            
            # Handle both JSON responses and text responses
            if "application/json" in response.headers.get("content-type", ""):
                return response.json()
            return {"content": response.text}

# Initialize API client
rhapi = RedHatAPI()

@mcp.tool()
async def search_kcs(query: str, rows: int = 50, start: int = 0, session_id: str = None) -> List[Dict]:
    """
    Search for Red Hat KCS Solutions and return a list with Solution IDs.
    
    Args:
        query: Search query string
        rows: Number of results to return (default: 50)
        start: Starting index for pagination (default: 0)
        
    Returns:
        List of solutions with their IDs and metadata
    
    By default, this tool returns only documents where documentKind is either "Article" or "Solution" and accessState is either "active" or "private".
    """
    path = "/hydra/rest/search/v2/kcs"
    
    data = {
        "q": query,
        "rows": rows,
        "expression": "sort=score%20DESC&fq=documentKind%3A(%22Article%22%20OR%20%22Solution%22)%20AND%20accessState%3A(%22active%22%20OR%20%22private%22)&fl=allTitle%2CcaseCount%2CdocumentKind%2C%5Belevated%5D%2ChasPublishedRevision%2Cid%2Clanguage%2ClastModifiedDate%2CModerationState%2Cscore%2Curi%2Cresource_uri%2Cview_uri%2CcreatedDate&showRetired=false",
        "start": start,
        "clientName": "mcp"
    }
    
    result = await rhapi.make_request("post", path, data)
    
    # Format the response to include only relevant information
    solutions = []
    if "response" in result and "docs" in result["response"]:
        for doc in result["response"]["docs"]:
            solution = {
                "id": doc.get("id"),
                "title": doc.get("allTitle"),
                "score": doc.get("score"),
                "view_uri": doc.get("view_uri")
            }
            solutions.append(solution)
    
    return solutions

@mcp.tool()
async def get_kcs(solution_id: str, session_id: str = None) -> Dict:
    """Get a specific solution by ID and extract structured content

    Args:
        solution_id: The ID of the solution to retrieve

    Returns:
        Dictionary with title, Environment, Issue, Resolution, and Root Cause
    """
    # Use the KCS search API to get the solution data
    path = "/hydra/rest/search/v2/kcs"
    
    data = {
        "q": f"id:{solution_id}",
}
    
    result = await rhapi.make_request("post", path, data)
    
    # Check if we got a result
    if not result or "response" not in result or "docs" not in result["response"] or not result["response"]["docs"]:
        return {
            "title": "",
            "environment": "",
            "issue": "",
            "resolution": "",
            "root_cause": ""
        }
    
    # Extract the solution data from the first document
    doc = result["response"]["docs"][0]
    
    # Initialize the result dictionary
    solution_data = {
        "title": doc.get("publishedTitle", ""),
        "environment": doc.get("standard_product", ""),
        "issue": doc.get("issue", ""),
        "resolution": doc.get("solution_resolution", ""),
        "root_cause": doc.get("solution_rootcause", ""),
    }
    
    return solution_data



@mcp.tool()
async def search_cases(query: str, rows: int = 10, start: int = 0, session_id: str = None) -> List[Dict]:
    """
    Search for Red Hat cases and return a list of case numbers.
    
    Args:
        query: Search query string
        rows: Number of results to return (default: 10)
        start: Starting index for pagination (default: 0)
        
    Returns:
        List of cases with their numbers and metadata
    """
    path = "/hydra/rest/search/v2/cases"
    
    data = {
        "q": query,
        "start": start,
        "rows": rows,
        "partnerSearch": False,
        "expression": "sort=case_lastModifiedDate%20desc&fl=case_createdByName%2Ccase_createdDate%2Ccase_lastModifiedDate%2Ccase_lastModifiedByName%2Cid%2Curi%2Ccase_summary%2Ccase_status%2Ccase_product%2Ccase_version%2Ccase_accountNumber%2Ccase_number%2Ccase_contactName%2Ccase_owner%2Ccase_severity"
    }
    
    result = await rhapi.make_request("post", path, data)
    
    # Format the response to include only relevant information
    cases = []
    if "response" in result and "docs" in result["response"]:
        for doc in result["response"]["docs"]:
            case = {
                "case_number": doc.get("case_number"),
                "summary": doc.get("case_summary"),
                "status": doc.get("case_status"),
                "product": doc.get("case_product"),
                "version": doc.get("case_version"),
                "severity": doc.get("case_severity"),
                "owner": doc.get("case_owner"),
                "created_date": doc.get("case_createdDate"),
                "created_by": doc.get("case_createdByName"),
                "last_modified_date": doc.get("case_lastModifiedDate"),
                "uri": doc.get("uri")
            }
            cases.append(case)
    
    return cases

@mcp.tool()
async def get_case(case_number: str, session_id: str = None) -> Dict:
    """
    Get case details by case number.
    
    Args:
        case_number: The case number (e.g., "04145487")
        
    Returns:
        Formatted case data with description, severity, issue, case number, and comments
    """
    path = f"/hydra/rest/v1/cases/{case_number}"
    data = await rhapi.make_request("get", path)
    
    # Extract comments from the main response
    comments = data.get("comments", [])
    
    # Format the response according to the specified schema
    formatted_result = {
        "summary": data.get("summary", data.get("title", "")),
        "description": data.get("description"),
        "severity": data.get("severity"),
        "comments": [
            {
                "createdDate": comment.get("createdDate"),
                "createdBy": comment.get("createdBy"),
                "commentBody": comment.get("commentBody", comment.get("text", ""))
            }
            for comment in reversed(comments)
        ]
    }
    
    # Add any additional useful fields from the original response
    if "status" in data:
        formatted_result["status"] = data.get("status")
    if "product" in data:
        formatted_result["product"] = data.get("product") 
    if "version" in data:
        formatted_result["version"] = data.get("version")
    if "ownerId" in data:
        formatted_result["ownerId"] = data.get("ownerId")
    if "createdDate" in data:
        formatted_result["createdDate"] = data.get("createdDate")
    if "openshiftClusterID" in data:
        formatted_result["openshiftClusterID"] = data.get("openshiftClusterID")
    if "openshiftClusterVersion" in data:
        formatted_result["openshiftClusterVersion"] = data.get("openshiftClusterVersion")
    # Add external trackers if present
    if "externalTrackers" in data and isinstance(data["externalTrackers"], list):
        formatted_result["external_trackers"] = [
            {
                "resourceKey": tracker.get("resourceKey"),
                "resourceURL": tracker.get("resourceURL"),
                "status": tracker.get("status"),
                "system": tracker.get("system"),
                "title": tracker.get("title"),
            }
            for tracker in data["externalTrackers"]
            if any(tracker.get(k) for k in ("resourceKey", "resourceURL", "status", "system", "title"))
        ]
    # Add case resource links if present
    if "caseResourceLinks" in data and isinstance(data["caseResourceLinks"], list):
        formatted_result["case_resource_links"] = [
            {
                "resourceType": link.get("resourceType"),
                "resourceViewURI": link.get("resourceViewURI"),
                "solutionTitle": link.get("solutionTitle"),
            }
            for link in data["caseResourceLinks"]
            if any(link.get(k) for k in ("resourceType", "resourceViewURI", "solutionTitle"))
        ]
    
    return formatted_result

@mcp.prompt(name="summarize_case_prompt", description="Summarize a Red Hat support case in C.A.S.E. markdown format.")
async def summarize_case_prompt(case_number: str) -> str:
    """
    Given a Red Hat support case number, instruct the LLM to fetch the case data and summarize it using the C.A.S.E. markdown template.
    """
    prompt = f'''
You are a Red Hat support expert. Given the following Red Hat support case number, first fetch the full case data using the appropriate tool or API, then analyze and summarize it using this markdown template into an artifact. 
Do not use bold, italics, or any markdown except for headings and bullet points. 
Be concise, actionable, and ensure all sections are filled if possible.

Case Number: {case_number}

## C.A.S.E. Update - [case number] - [title]

- Current Status:
  - [List the current issue(s) and status in bullet points]

- Actions:
  - [Summarize actions taken, outcomes, and any key commands or links in bullet points]

- Severity:
  - [Describe the business impact]

- Expectations:
  - [Outline next steps and expected timing in bullet points]

### ENVIRONMENT:
- [List relevant environmental factors: product, version, clusterID/ResourceID, and related links]

### NOTES:
- [Add any additional insights or actionable suggestions]

Ensure the summary is comprehensive yet concise, providing a clear overview and actionable next steps.
'''
    return prompt.strip()

@mcp.prompt(name="resolve_case_prompt", description="Red Hat Case Resolver Agent: investigation and solution workflow for a support case.")
async def resolve_case_prompt(case_number: str) -> str:
    """
    Given a Red Hat support case number, instruct the LLM to follow the Red Hat Case Resolver Bot workflow and output a markdown document with the required sections and rules.
    """
    prompt = f'''
## Investigation Workflow Round
    1. **Fetch the case**  
        • Get the Redhat with case number   
        • Summary and understand the current issue(s) and status

    2. **Keyword generation**
        • Build a rich keyword list (components, CVEs, error phrases, paraphrases).  
        • Produce at least three query variants.

    3. **Iterative search rounds** (cap=3). In **each** round:

    a. **Search**  
        • Search Red Hat KCS  
        • Search Jira ticket  
        • Search Red Hat cases for similar historical RedHat cases     
        • Search Internet

    b. **Retrieve & Read**  
        • KCS Solution and Articles
        • Jira ticket details, links, and comments 
        • Case history and resolution
        • Fetch any new hyperlinks inside those bodies.

    c. **Link Expansion**  
        • For every new URL try to fetch content  
        • Skip URLs already reviewed.
        
    

    d. **Reflect**  
        • Summarize what new evidence you gained.  
        • **Confidence:** *0 - 1*
        • Decide what is still missing and whether to start another round
        • Max three search rounds until confidence score >= 0.95.


## 2. Output Requirements
Produce **one markdown document** with these exact headings:

### Case Summary  
Concise restatement of the customer's problem.

### Analysis & Findings  
Bullet evidence from reviewed KCS, Jira, previous cases, and public sources.  
Cite inline: *(KCS 12345)* / *(JIRA ABC-42)* / *(CASE 987654)*.

### Proposed Solution  
Step by step fix or next action plan.  

### Sources  
Unordered list of **unique** URLs consulted—KCS first, then Jira, then previous cases (if publicly addressable), then public links. No duplicates.

## 3. Style Guide
* Be concise and factual; minimal fluff.  
* Quote exact error messages when they drive a search.  
* Prefer bullets; avoid unnecessary tables.

## 4. Hard Rules
* No JSON output—markdown only.  
* Never list the same link twice.  
* If still unsatisfied, recommend escalation.

# ================================================================
#                       ⬇️  START  ⬇️
# ================================================================

Case Number: {case_number}
'''
    return prompt.strip()

@mcp.prompt(name="resolve_case_prompt_v2", description="Red Hat Case Resolver Agent: investigation and solution workflow for a support case.")
async def resolve_case_prompt_v2(case_number: str) -> str:
    """
    Given a Red Hat support case number, instruct the LLM to fetch the case data and summarize it using the C.A.S.E. markdown template.
    """
    prompt = f'''

Case Number: {case_number}

# Agent Role and Goal

You are an AI assistant specialized in Red Hat support case resolution. Your primary goal is to comprehensively investigate a given Red Hat support case, gather all relevant information from internal Jira, Red Hat's Knowledge Centered Service (KCS), historical Red Hat cases, and the broader internet, and then synthesize this information into a clear, actionable summary and proposed solution. You must operate methodically through defined phases of fetching case details, iterative investigation, and reflective analysis, culminating in a markdown artifact that includes all relevant source links.

# Workflow and Instructions

Follow these steps precisely:

**Phase 1: Fetch and Understand Case**

1.  **Receive Case Number:** You will be given a Red Hat case number.
2.  **Get Case Details:** Use the `get_case` tool from the Red Hat API MCP server to retrieve the full details of the provided case number. Note any direct resource links provided in the case data.
3.  **Initial Summary & Understanding:**
    * Thoroughly analyze the retrieved case details.
    * Summarize the core problem(s), the current status of the case, the product(s) and version(s) involved, the customer's environment (if described), and any troubleshooting steps already taken or documented in the case.
    * Identify key entities, error messages, and technical terms.

**Phase 2: Iterative Investigation (Maximum 3 Rounds)**

This phase is iterative. You will conduct up to 3 rounds of investigation. A round may be cut short if the confidence threshold is met.

1.  **Keyword and Query Generation:**
    * Based on your initial understanding and the details from `get_case`, generate a rich and diverse list of keyword combinations and specific search queries.
    * Consider symptoms, error messages, product names, versions, components, technologies involved, and potential problem categories.
    * For Jira searches, formulate relevant JQL queries.

2.  **Iterative Search Round (Perform these actions in each round):**
    * **Search Red Hat KCS:** Use `search_kcs` with your generated keywords. Capture the KCS ID and `view_uri` for relevant results.
    * **Search Jira Issues:** Use `jira_search_issues` with appropriate JQL queries. Capture the Jira issue key for relevant results (this key implies the link).
    * **Search Historical Red Hat Cases:** Use `search_cases` with your keywords. Capture the case number for relevant results (this implies the link).
    * **Search Internet:** Perform targeted internet searches using your keywords. Capture the full URL for relevant public web pages.

3.  **Retrieve & Read Detailed Content:**
    * For the most promising results identified in the search phase:
        * **KCS:** Use `get_kcs` to retrieve the full content of relevant KCS solutions/articles using their IDs.
        * **Jira:** Use `jira_get_issue_details`, `jira_get_issue_comments`, and `jira_get_issue_links` to get comprehensive information about relevant Jira tickets using their keys. Note any further links found within these details.
        * **Red Hat Cases:** If highly relevant historical cases are found via `search_cases`, consider if using `get_case` on a very small number (1-2) of the *most* pertinent historical cases is warranted to understand their detailed resolution path. Note any resource links found.
        * **Internet Content:** Analyze the content from promising web links (URLs captured previously).
    * **Deep Dive & Link Traversal:**
        * Carefully examine all retrieved content.
        * Identify any **new** relevant URLs, KCS article IDs (and their `view_uri`s), Jira issue keys, or case numbers mentioned within the bodies of these documents.
        * **Maintain a list of already reviewed URLs/resources/issue keys/IDs** to avoid redundant fetching and processing in subsequent steps or rounds. Store the direct link/URI for each.
        * For newly discovered, unreviewed, and relevant links/IDs, fetch their content using the appropriate tools, ensuring you store their source link/URI.

**Phase 3: Reflect, Evaluate, and Decide**

1.  **Synthesize Information:** After each round of "Retrieve & Read," consolidate all new information gathered in that round with previously acquired knowledge. Ensure all source identifiers (IDs, Keys, URLs) are tracked.
2.  **Evaluate Sufficiency:** Critically assess whether the information you have gathered is sufficient to formulate a comprehensive and reliable resolution or a set of next steps for the original Red Hat case.
3.  **Confidence Score:**
    * Assign a **confidence score between 0.0 and 1.0** representing your certainty that you have enough information to resolve the case or provide definitive next steps.
    * Briefly justify this score based on the quality, relevance, and completeness of the information found and the supporting sources.
4.  **Decision for Next Round:**
    * **If confidence_score >= 0.95 OR you have completed 3 rounds:** Proceed to Phase 4 (Summarization).
    * **If confidence_score < 0.95 AND you have completed fewer than 3 rounds:**
        * Identify information gaps. What is still missing? What questions remain unanswered?
        * Refine your keyword strategy and search queries for the next round.
        * Initiate the next search round (Phase 2, Step 2).

**Phase 4: Summarize and Create Artifact**

Once the iterative investigation concludes:

1.  **Comprehensive Analysis:** Perform a final, holistic analysis of all information gathered throughout all rounds and its supporting sources.
2.  **Formulate Solution/Next Steps:** Based on your analysis, determine the likely root cause(s), a specific proposed solution, or logical next troubleshooting steps.
3.  **Generate Markdown Artifact:** Create a well-structured markdown document containing:
    * **Case ID:** The original Red Hat Case Number.
    * **Initial Case Summary:** Your summary from Phase 1, Step 3.
    * **Investigation Overview:** Briefly describe the search strategies employed in each round.
    * **Key Findings (each with its source identifier/link):**
        * Relevant KCS articles (ID, Title, `view_uri`, brief summary of relevance).
        * Relevant Jira issues (Key, Summary, Status, inferred Jira link, brief summary of relevance).
        * Relevant historical Red Hat cases (Case Number, Summary, inferred link, brief summary of relevance and its resolution if found).
        * Key insights from internet searches (Full URL, Page Title if available, brief summary of relevance).
    * **Correlation and Analysis:** Explain how these findings and their sources connect to the original case and to each other.
    * **Proposed Solution / Recommended Next Steps:** Clearly articulate the proposed solution or recommended steps.
    * **Confidence Score:** State your final confidence score.
    * **Remaining Open Questions / Areas for Further Manual Investigation:** If applicable.
    * **Comprehensive Source List (Key Resources Referenced):** This section must provide a complete list of all unique KCS IDs (with `view_uri`s), Jira Keys (implying direct links), Red Hat Case Numbers (implying direct links), and full URLs from internet searches that were consulted and deemed relevant during the investigation. Format clearly for easy access to the source material. Example:
        * KCS 12345: [Title of KCS] ([view_uri_link])
        * JIRA PROJECT-789: [Summary of Jira] ([link_to_jira_if_pattern_is_known_else_just_key])
        * Case 0098765: [Summary of Case] ([link_to_case_if_pattern_is_known_else_just_number])
        * Web: [Page Title] ([full_url_link])

# General Guidelines

* **Be Methodical:** Follow the workflow strictly.
* **Tool Usage Clarity:** Mentally (or if an option, explicitly state) which tool you are invoking and the key parameters.
* **Prioritization:** Prioritize information from Red Hat KCS, Red Hat Cases, and internal Jira.
* **Efficiency and Source Tracking:** Avoid redundant searches and meticulously track all source links/identifiers.
* **Focus on Resolution:** Aim to provide information that helps resolve the support case.
* **No `jira_create_issue`:** For this task, you will not use the `jira_create_issue` tool.

'''
    return prompt.strip()

# Run the server if executed directly
if __name__ == "__main__":
    mcp.run(transport="sse", host="0.0.0.0", port=8000)