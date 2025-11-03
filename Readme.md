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
   - Example: `https://sreips-remediation-agent-sreips-agent.apps.your-cluster.com/remediate`
   - To get the route URL after deployment, run:
     ```bash
     oc get route sreips-remediation-agent -n sreips-agent -o jsonpath='{.spec.host}'
     ```
   - Then use: `https://<route-host>/remediate`
4. Click **Save Changes**

**Note**: You'll need to update this URL after deploying SREIPS, as the route won't exist until the remediation agent is deployed.

#### Step 6: Add Bot to Channel
1. Create or choose a Slack channel (e.g., `#sreips-helper`)
2. In the channel, type `/invite @SREIPS Bot` (or your bot name)
3. The channel name you use here is your `SLACK_CHANNEL` for `config.env`

### 2. Configure your environment

```bash
# Copy the configuration template
cp config.env.template config.env

# Edit config.env and fill in your values
# Make sure to set SLACK_API_KEY and SLACK_CHANNEL from the steps above
vim config.env
```

### 3. Run the master bootstrap script

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
- **RH API Offline Token** - Get from https://access.redhat.com/management/api
  1. Log in with your Red Hat account
  2. Navigate to API Tokens section
  3. Generate or copy your offline token

### LlamaStack
- **Inference model** - LLM model name (e.g., `Llama-4-Scout-17B-16E-W4A16`)
- **VLLM URL** - Your vLLM inference endpoint
- **VLLM API token** - Authentication token for vLLM
- **VLLM TLS verify** - Set to `true` or `false` for SSL verification

### SREIPS Agent
- **Vector database ID** - Identifier for your vector database (e.g., `sreips_vector_id`)

See `config.env.template` for detailed descriptions and example values.

## Prerequisites

- OpenShift CLI (`oc`) installed and logged in to your cluster
- `jq` for JSON parsing
- `curl` for API calls
- Valid credentials for all services (Slack, Red Hat API, VLLM, etc.)

## Component Overview

The SREIPS platform consists of 7 main components that are installed in sequence:

1. **sreips-core**: Core SREIPS monitoring and automation framework based on Robusta
2. **minio**: Object storage for data pipeline artifacts
3. **ocp-mcp**: OpenShift MCP server that provides cluster management capabilities for the remediation agent
4. **rh-kcs-mcp**: Red Hat Knowledgebase Content Services MCP server for KB access
5. **llamastack**: AI/ML pipeline infrastructure with Milvus vector database
6. **sreips-agent**: Main SREIPS agent that orchestrates troubleshooting workflows
7. **remediation-agent**: Automated remediation agent for self-healing capabilities with interactive Slack buttons

For detailed architecture and data flow diagrams, see [ARCHITECTURE.md](https://docs.google.com/presentation/d/1mDIUx_LKE_zHQxduarDN1AXC6TIuSUj9XT8eVc2P2AQ)

## Manual Deployment (Alternative)

If you prefer to install components individually or need to re-run specific steps:

```bash
# Source the configuration and functions
source config.env
source bootstrap.sh

# Run individual installation functions
install_sreips_core    # Step 2: Core monitoring framework
install_minio          # Step 3: Object storage
install_ocp_mcp        # Step 4: OpenShift MCP server for remediation agent
install_rh_kcs_mcp     # Step 5: Red Hat KCS MCP server
install_llamastack     # Step 6: AI/ML pipeline infrastructure
install_sreips_agent   # Step 7: SREIPS and Remediation agents
```

Note: Manual deployment requires that you run steps in sequence as later components depend on earlier ones. The remediation agent specifically requires the OCP MCP server (step 4) to perform cluster operations.

## Troubleshooting

If installation fails:

1. Check that you're logged into OpenShift: `oc whoami`
2. Verify all required variables are set in `config.env`
3. Check pod status: `oc get pods -n <namespace>`
4. View pod logs: `oc logs -n <namespace> <pod-name>`
5. The script will provide detailed error messages indicating where the failure occurred

## Post-Deployment Steps

After the bootstrap script completes successfully, you need to update your Slack app configuration:

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

