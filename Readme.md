# SREIPS Deployment Guide

## Quick Start

Use the master bootstrap script to install all components automatically:

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

#### Step 4: Add Bot to Channel
1. Create or choose a Slack channel (e.g., `#sreips-helper`)
2. In the channel, type `/invite @SREIPS Bot` (or your bot name)
3. The bot name you use here is your `SLACK_CHANNEL` for `config.env`

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
- **Sentry DSN** - (Optional) For error tracking from https://sentry.io
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

The SREIPS platform consists of 5 main components that are installed in sequence:

1. **sreips-core**: Core SREIPS monitoring and automation framework based on Robusta
2. **minio**: Object storage for data pipeline artifacts
3. **rh-kcs-mcp**: Red Hat Knowledge Centered Service MCP server for KB access
4. **llamastack**: AI/ML pipeline infrastructure with Milvus vector database
5. **sreips-agent**: Main SREIPS agent that orchestrates troubleshooting workflows

## Manual Deployment (Alternative)

If you prefer to install components individually or need to re-run specific steps:

```bash
# Source the configuration and functions
source config.env
source bootstrap.sh

# Run individual installation functions
install_sreips_core    # Step 2
install_minio          # Step 3
install_rh_kcs_mcp     # Step 4
install_llamastack     # Step 5
install_sreips_agent   # Step 6
```

Note: Manual deployment requires that you run steps in sequence as later components depend on earlier ones.

## Troubleshooting

If installation fails:

1. Check that you're logged into OpenShift: `oc whoami`
2. Verify all required variables are set in `config.env`
3. Check pod status: `oc get pods -n <namespace>`
4. View pod logs: `oc logs -n <namespace> <pod-name>`
5. The script will provide detailed error messages indicating where the failure occurred

## Using SREIPS

To test SREIPS event detection and notification, apply the sample manifests in `./test-manifests`. These manifests will generate simulated issues or failures. SREIPS will detect the resulting events, send detailed notifications to your configured Slack channel and include enriched solutions based on data from your enterprise knowledge base and Red Hat KCS.