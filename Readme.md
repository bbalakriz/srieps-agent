# SREIPS Deployment Guide

Use the master bootstrap script to install all components automatically:

### 1. Configure your environment

```bash
# Copy the configuration template
cp config.env.template config.env

# Edit config.env and fill in your values
vim config.env
```

### 2. Run the master bootstrap script

```bash
./bootstrap.sh
```

This will automatically install all SREIPS components in the correct sequence with proper dependency handling.

## Configuration Required

Before running the bootstrap script, you need to configure the following in `config.env`:

- **SREIPS Core**: Slack API key, Slack channel, Sentry DSN, cluster name
- **MinIO**: Root username and password
- **RH KCS MCP**: Red Hat API offline token
- **LlamaStack**: Inference model, VLLM URL, VLLM API token, MinIO credentials
- **SREIPS Agent**: Vector database ID

See `config.env.template` for detailed descriptions and example values.

## Prerequisites

- OpenShift CLI (`oc`) installed and logged in to your cluster
- `jq` for JSON parsing
- `curl` for API calls
- Valid credentials for all services (Slack, Red Hat API, VLLM, etc.)

## Component Overview

- **sreips-core**: Core SREIPS monitoring and automation framework based on Robusta
- **minio**: Object storage for data pipeline artifacts
- **rh-kcs-mcp**: Red Hat Knowledge Centered Service MCP server for KB access
- **llamastack**: AI/ML pipeline infrastructure with Milvus vector database
- **sreips-agent**: Main SREIPS agent that orchestrates troubleshooting workflows

## Troubleshooting

If installation fails:

1. Check that you're logged into OpenShift: `oc whoami`
2. Verify all required variables are set in `config.env`
3. Check pod status: `oc get pods -n <namespace>`
4. View pod logs: `oc logs -n <namespace> <pod-name>`
5. The script will provide detailed error messages indicating where the failure occurred