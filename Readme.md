# SREIPS Deployment Guide

## Quick Start

Use the master bootstrap script to install all components automatically:

> **⚠️ Important**: The target OpenShift cluster must NOT have RHOAI (Red Hat OpenShift AI) installed before using this bootstrap. SREIPS has its own AI/ML infrastructure and conflicts may occur with existing RHOAI installations.

### 1. Set up Slack Integration (Required)

Before deployment, you need to create a Slack Bot/App for SREIPS notifications:

#### Step 1: Create a Slack App
1. Go to your Slack workspace: https://api.slack.com/apps
2. Click **Create New App** → **From scratch**
3. Name it (e.g., "SREIPS Bot") and select your workspace
4. Click **Create App**

#### Step 2: Configure Permissions
1. In your app settings, go to **OAuth & Permissions**
2. Scroll down to **Scopes** section
3. Under **Bot Token Scopes**, add the following permissions:
   - `chat:write` - Send messages as SREIPS Bot
   - `chat:write.public` - Send messages to channels that SREIPS Bot isn't a member of
   - `files:write` - Upload, edit and delete files as SREIPS Bot
   - `incoming-webhook` - Post messages to specific channels in Slack

#### Step 3: Install App to Workspace
1. Scroll up to **OAuth Tokens for Your Workspace**
2. Click **Install to Workspace**
3. Review permissions and click **Allow**
4. Copy the **Bot User OAuth Token** (starts with `xoxb-...`)
   - This is your `SLACK_API_KEY` for `config.env`

#### Step 4: Get Signing Secret
1. In your app settings, go to **Basic Information**
2. Scroll down to **App Credentials** section
3. Copy the **Signing Secret**
   - This is your `SIGNING_KEY` for `config.env`
   - This is used to verify that requests to your remediation agent are coming from Slack

#### Step 5: Configure Interactivity & Shortcuts
1. In your app settings, go to **Interactivity & Shortcuts**
2. Toggle **Interactivity** to **On**
3. Set the **Request URL** to: `<remediation-agent-route-url>/remediate`
   - Example: `https://remediation-agent-sreips-agent.apps.your-cluster.com/remediate`
   - To get the route URL after deployment, run:
     ```bash
     oc get route remediation-agent -n sreips-agent -o jsonpath='{.spec.host}'
     ```
   - Then use: `https://<route-host>/remediate`
4. Click **Save Changes**

**Note**: You'll need to update this URL after deploying SREIPS, as the route won't exist until the remediation agent is deployed.

#### Step 6: Add Bot to Channel
1. Create or choose a Slack channel (e.g., `#sreips-helper`)
2. In the channel, type `/invite @SREIPS Bot` (or your bot name)
3. The channel name you use here is your `SLACK_CHANNEL` for `config.env`

### 2. Set up Mattermost Integration (Optional Alternative to Slack)

Mattermost is a self-hosted messaging alternative to Slack. When `MATTERMOST_ENABLED=true` is set in `config.env`, the bootstrap script handles the Mattermost deployment automatically as step 2 (before sreips-core), because the Mattermost bot token must be available when sreips-core's playbooks config secret is created. This mirrors how Slack requires its bot token to be pre-configured before the bootstrap runs, except that Mattermost itself is deployed by the bootstrap.

#### Step 0: Configure config.env for Mattermost

Before running `./bootstrap.sh`, update `config.env` with the Mattermost database credentials:

```bash
export MATTERMOST_ENABLED="true"
export MATTERMOST_MYSQL_ROOT_PASSWORD="your-strong-root-password"
export MATTERMOST_MYSQL_PASSWORD="your-mattermost-db-password"
export MATTERMOST_CHANNEL="sreips-helper"
# leave these blank - the bootstrap will prompt you to fill them in after deploying
export MATTERMOST_BOT_TOKEN=""
export MATTERMOST_BOT_TOKEN_ID=""
```

Then run `./bootstrap.sh`. The script will deploy Mattermost, wait for it to be ready, print the URL and then pause with instructions to create the bot in the Mattermost UI. Once you update `config.env` with the bot token and press Enter, the bootstrap continues with the remaining components.

#### Step 1: Enable Bot Account Creation

1. Log in to Mattermost using an account with **System Admin** privileges
2. Click the **Product/Main Menu** icon (top-left corner, usually a grid or ☰ icon) and go to **System Console**
3. On the left navigation panel, scroll down to **Integrations** and select **Bot Accounts** (or Integration Management)
4. Set **Enable Bot Account Creation** to **true**
5. Click **Save**

#### Step 2: Create the Bot and Get the Tokens

1. Click the top-left menu again to leave the System Console, then go to **Integrations > Bot Accounts**
2. Click the **Add Bot Account** button
3. Fill in the details:
   - **Username**: e.g., `sreips-bot` (Must begin with a letter, and contain between 3 to 22 lowercase characters/numbers)
   - **Display Name / Description**: Enter a recognizable name
   - **Role**: To ensure it can post seamlessly to both public and private channels without requiring team ID routing, change the role to **System Admin**
   - **Additional Permissions**: Select the option to allow the bot to **post to all Mattermost channels** (the postall permission)
4. Click **Create Bot Account**

**CRITICAL**: The next screen will display "Setup Successful" and show a long Token and a Token ID.
- **Copy both values immediately and save them to a secure text file**
- Mattermost will only show the main token this one time
- If you close this screen without copying it, you will have to generate a new token

#### Step 3: Add the Bot to Your Mattermost Team

Before a bot can join a channel, it first needs to be invited to the overarching Mattermost "Team":

1. Navigate back to your main Mattermost chat interface
2. Click on your team name at the top-left corner and select **Invite People** from the dropdown menu
3. Select the option to **Invite Members**
4. In the search box, type the username of the bot you just created
5. Select the bot and click **Invite Members** (or **Invite**)
   - The bot is now officially part of your Mattermost team workspace

#### Step 4: Create the Channel and Invite the Bot

Finally, get the bot into the specific channel where you want SREIPS to send alerts:

1. In the left-hand sidebar of Mattermost, click the **+** icon next to the "Channels" header and select **Create New Channel**
2. Name the channel (e.g., `sreips-helper`) and choose whether it should be **Public** or **Private**, then click **Create**
3. Once you are inside the newly created channel, click the channel's name at the top of the screen to open the channel menu
4. Select **Add Members**
5. Search for your bot's username, select it, and click **Add**

**You are now completely finished with the Mattermost UI side.** Add the token and token_id you saved in Step 2 to `config.env`:

```bash
export MATTERMOST_BOT_TOKEN="<your-token>"
export MATTERMOST_BOT_TOKEN_ID="<your-token-id>"
```

Then press Enter in the bootstrap terminal to continue. The bootstrap will automatically substitute these values into the `sreips-playbooks-config-secret.yaml` before applying it to the cluster. No manual YAML editing required.

### 4. Configure your environment

```bash
# Copy the configuration template
cp config.env.template config.env

# Edit config.env and fill in your values
# Make sure to set SLACK_API_KEY and SLACK_CHANNEL from the steps above
vim config.env
```

### 5. Run the master bootstrap script

```bash
./bootstrap.sh
```

This will automatically install all SREIPS components in the correct sequence with proper dependency handling.

## Configuration Required

Before running the bootstrap script, you need to configure the following in `config.env`:

### SREIPS Core
- **Slack API key** - Obtained from steps above (starts with `xoxb-`)
- **Slack channel** - Channel name where notifications will be sent (e.g., `sreips-helper`)
- **Signing key** - Slack signing secret for verifying requests from Slack (from Basic Information → App Credentials)
- **Cluster name** - Your OpenShift cluster identifier

### MinIO
- **Root username** - MinIO admin username (minimum 3 characters)
- **Root password** - MinIO admin password (minimum 8 characters)

### Red Hat KCS MCP
- **RH API Offline Token** - Required for online mode and for exporting KCS articles before offline ingest. Get from https://access.redhat.com/management/api
  1. Log in with your Red Hat account
  2. Navigate to API Tokens section
  3. Generate or copy your offline token
- **KCS mode** - `KCS_MODE` in `config.env` (default `offline`). Bootstrap applies it to `redhat-api-mcp` together with `LLAMA_STACK_URL`. Run the KCS ingest workflow in Post-Deployment Steps before KCS queries will work.

### LlamaStack
- **Inference model** - LLM model name (e.g., `Qwen3.6-35B-A3B`)
- **VLLM URL** - Your vLLM inference endpoint
- **VLLM API token** - Authentication token for vLLM
- **VLLM TLS verify** - Set to `true` or `false` for SSL verification

### SREIPS Agent
- **Vector database ID** - Defaults to `sreips_vector_id` in `config.env`. The SREIPS agent resolves this store name to the LlamaStack UUID at runtime after the enterprise KB ingestion pipeline creates it. No post-deployment update is required.

See `config.env.template` for detailed descriptions and example values.

## Prerequisites

- OpenShift CLI (`oc`) installed and logged in to your cluster
- `jq` for JSON parsing
- `curl` for API calls
- Valid credentials for all services (Slack, Red Hat API, VLLM, etc.)

## Component Overview

The SREIPS platform consists of 7 main components (plus an optional Mattermost component) that are installed in sequence:

1. **mattermost** (optional): Self-hosted messaging platform, an alternative to Slack for SREIPS notifications. Deployed before sreips-core because the bot token is required by the sreips-core configuration.
2. **sreips-core**: Core SREIPS monitoring and automation framework based on Robusta
3. **minio**: Object storage for data pipeline artifacts
4. **ocp-mcp**: OpenShift MCP server that provides cluster management capabilities for the remediation agent
5. **rh-kcs-mcp**: Red Hat Knowledgebase Content Services MCP server for KB access
6. **llamastack**: AI/ML pipeline infrastructure with Milvus vector database
7. **sreips-agent**: Main SREIPS agent that orchestrates troubleshooting workflows
8. **remediation-agent**: Automated remediation agent for self-healing capabilities with interactive Slack or Mattermost buttons

For detailed architecture and data flow diagrams, see [ARCHITECTURE.md](https://docs.google.com/presentation/d/1mDIUx_LKE_zHQxduarDN1AXC6TIuSUj9XT8eVc2P2AQ)

## Manual Deployment (Alternative)

If you prefer to install components individually or need to re-run specific steps:

```bash
# Source the configuration and functions
source config.env
source bootstrap.sh

# Run individual installation functions
install_mattermost     # Step 2: Mattermost (optional - only when MATTERMOST_ENABLED=true)
install_sreips_core    # Step 3: Core monitoring framework
install_minio          # Step 4: Object storage
install_ocp_mcp        # Step 5: OpenShift MCP server for remediation agent
install_rh_kcs_mcp     # Step 6: Red Hat KCS MCP server
install_llamastack     # Step 7: AI/ML pipeline infrastructure
install_sreips_agent   # Step 8: SREIPS and Remediation agents
```

Note: Manual deployment requires that you run steps in sequence as later components depend on earlier ones. If using Mattermost, it must be installed before sreips-core so the bot token is available when the playbooks config secret is created. The remediation agent specifically requires the OCP MCP server (step 5) to perform cluster operations.

## Troubleshooting

If installation fails:

1. Check that you're logged into OpenShift: `oc whoami`
2. Verify all required variables are set in `config.env`
3. Check pod status: `oc get pods -n <namespace>`
4. View pod logs: `oc logs -n <namespace> <pod-name>`
5. The script will provide detailed error messages indicating where the failure occurred

## Post-Deployment Steps

After the bootstrap script completes successfully, you need to complete the following steps:

> ⚠️ **CRITICAL**: LlamaStack Restart Limitation
> 
> **DO NOT restart LlamaStack pods** after the initial bootstrap unless absolutely necessary. Due to a bug in the Milvus remote provider (in the LlamaStack version used here), restarting LlamaStack does not restore the operational registry on startup. This breaks both:
> - **Persisted KCS data** ingested via offline pipeline
> - **Internal knowledge base** uploaded via RAG pipeline
> 
> The vector database connections are lost and cannot be recovered without manual re-ingestion of all data. If restart becomes necessary, you will need to re-run the data ingestion pipelines to restore service functionality.

### 1. Update Slack App Configuration

1. **Get the Remediation Agent Route URL:**
   ```bash
   oc get route remediation-agent -n sreips-agent -o jsonpath='{.spec.host}'
   ```

2. **Update Slack App Interactivity URL:**
   - Go back to your Slack app settings at https://api.slack.com/apps
   - Navigate to **Interactivity & Shortcuts**
   - Update the **Request URL** to: `https://<route-from-step-1>/remediate`
   - Click **Save Changes**

This enables the interactive remediation buttons in Slack notifications.

### 2. KCS offline ingest (required)

The Red Hat KCS MCP server (`deployment/redhat-api-mcp` in namespace `mcp-servers`) defaults to offline mode. It reads from the local `kcs_vector_id` Milvus store via LlamaStack RAG instead of access.redhat.com.

| Mode | Behavior | `RH_API_OFFLINE_TOKEN` at runtime |
|------|----------|-----------------------------------|
| `offline` (default) | Queries local `kcs_vector_id` via LlamaStack RAG | Not used |
| `online` | Queries access.redhat.com in real time | Required |

Bootstrap deploys offline mode (`KCS_MODE=offline` in `config.env` and `rh-kcs-mcp/all-in-one.yaml`) and patches `LLAMA_STACK_URL` plus `KCS_MODE` from `config.env` after LlamaStack is up. KCS search will not return useful results until you complete the ingest steps below.

Set `export KCS_MODE="offline"` in `config.env` (default in `config.env.template`). To use online mode instead, set `KCS_MODE="online"` before bootstrap or switch after deploy (see below).

#### KCS ingest workflow

**Step 1: Export KCS articles (internet connected machine)**

```bash
export RH_API_OFFLINE_TOKEN="<your-offline-token-from-https://access.redhat.com/management/api>"
cd kcs-exporter
pip install -r requirements.txt
python export_kcs.py --output kcs-articles.ndjson --products ocp
```

Optional product filter: `python export_kcs.py --output kcs-articles.ndjson --products ocp,rhoai`

**Step 2: Transfer `kcs-articles.ndjson` to your cluster admin host**

**Step 3: Stage the NDJSON onto the cluster PVC (`llamastack` namespace)**

```bash
# from the repo root
oc -n llamastack apply -f kcs-exporter/kcs-data-pvc.yaml
oc -n llamastack apply -f kcs-exporter/kcs-stage-deployment.yaml

# wait for the staging pod
oc -n llamastack wait --for=condition=Ready pod -l app=kcs-stage --timeout=120s

POD_NAME=$(oc -n llamastack get pods -l app=kcs-stage -o jsonpath='{.items[0].metadata.name}')
oc -n llamastack cp kcs-exporter/kcs-articles.ndjson "${POD_NAME}:/data/kcs-articles.ndjson"

# remove the staging deployment when the copy succeeds
oc -n llamastack delete deployment kcs-stage
```

**Step 4: Trigger offline KCS ingestion into Milvus**

Each run creates a new Job (`generateName: kcs-ingest-`). Re-run this step after re-exporting articles or after a LlamaStack restart that lost persisted data.

```bash
oc -n llamastack create -f kcs-exporter/kcs-ingest-job.yaml

# monitor until the job completes
oc -n llamastack logs -l app=kcs-ingest -f
```

The job reads `/data/kcs-articles.ndjson` from the `kcs-data` PVC and ingests into the `kcs_vector_id` vector store via the in-cluster LlamaStack service URL.

#### Switching to online mode (optional)

Use when the cluster has outbound access to access.redhat.com and you prefer live KCS over the local snapshot:

```bash
oc set env deployment/redhat-api-mcp KCS_MODE=online -n mcp-servers
oc rollout status deployment/redhat-api-mcp -n mcp-servers
```

Update `config.env`: `export KCS_MODE="online"`. Ensure `RH_API_OFFLINE_TOKEN` is valid in the `redhat-api-token` secret (bootstrap creates this from `config.env`).

#### Switching back to offline mode

```bash
oc set env deployment/redhat-api-mcp KCS_MODE=offline -n mcp-servers
oc rollout status deployment/redhat-api-mcp -n mcp-servers
```

Update `config.env`: `export KCS_MODE="offline"`. Ensure KCS ingest has been completed so `kcs_vector_id` is populated.

#### Re-triggering KCS ingest

To refresh offline KCS data: repeat Steps 1 through 4 (re-export, re-stage, `oc -n llamastack create -f kcs-exporter/kcs-ingest-job.yaml`). Do not restart LlamaStack pods unless unavoidable (see warning above).

## Using SREIPS

To test SREIPS event detection and notification, apply the sample manifests in `./test-manifests`. These manifests will generate simulated issues or failures. SREIPS will detect the resulting events, send detailed notifications to your configured Slack channel and include enriched solutions based on data from your enterprise knowledge base and Red Hat KCS. 

### Automated Remediation

The **remediation-agent** provides self-healing capabilities for resource quota issues:

- **AI powered analysis**: Automatically analyzes quota violations using LlamaStack
- **One click fixes**: Interactive Slack buttons to trigger automated remediation
- **Safe operations**: Uses the OCP MCP server to perform auditable cluster operations
- **Real time feedback**: Immediate success/failure notifications back to Slack
- **Secure**: Request verification using Slack signing secret to ensure authenticity

#### Testing Different Scenarios

The `test-manifests/` directory contains various test scenarios to validate SREIPS detection and notification capabilities:

##### 1. CrashLoop Detection
```bash
oc apply -f test-manifests/01-crashloop-pod.yaml
```
Tests detection of pods stuck in CrashLoopBackOff state. SREIPS will analyze container logs and provide troubleshooting guidance.

##### 2. ImagePullBackOff Detection
```bash
oc apply -f test-manifests/02-imagepull-pod.yaml
```
Tests detection of image pull failures. SREIPS will identify the missing or inaccessible image and suggest resolution steps.

##### 3. Out of Memory (OOM) Detection
```bash
oc apply -f test-manifests/03-oom-pod.yaml
```
Tests detection of OOM killed containers. SREIPS will analyze memory usage patterns and recommend appropriate resource limits.

##### 4. PVC Failure Detection
```bash
oc apply -f test-manifests/05-pvc-failure.yaml
```
Tests detection of persistent volume claim binding failures. SREIPS will analyze storage class availability and quota issues.

##### 5. Quota Exceeded with Automated Remediation
```bash
oc apply -f test-manifests/06-quota-exceeded-pod.yaml
```
Tests the automated remediation feature for resource quota violations. This will:
1. Create a namespace with restrictive resource quotas
2. Attempt to deploy a pod that exceeds the quota
3. Trigger SREIPS to detect the quota violation
4. Send a Slack notification with an interactive "Remediate" button
5. Click the button to trigger automated quota adjustment via the remediation-agent
