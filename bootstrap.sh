#!/bin/bash

# ==============================================================================
# SREIPS Master Bootstrap Script
# ==============================================================================
# This script orchestrates the installation of all SREIPS components in sequence:
# 1. sreips-core
# 2. minio
# 3. rh-kcs-mcp
# 4. llamastack
# 5. sreips-agent
#
# Prerequisites:
# - OpenShift CLI (oc) installed and logged in
# - jq for JSON parsing
# - curl for API calls
# - config.env file with all required variables
# ==============================================================================

set -e  # Exit on error
set -u  # Exit on undefined variable
set -o pipefail  # Exit on pipe failure

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/config.env"

# ==============================================================================
# Utility Functions
# ==============================================================================

log_info() {
    echo -e "${BLUE}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} ℹ️  $1"
}

log_success() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} ✅ $1"
}

log_warning() {
    echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} ⚠️  $1"
}

log_error() {
    echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} ❌ $1"
}

log_step() {
    echo -e "\n${GREEN}===================================================${NC}"
    echo -e "${GREEN}$1${NC}"
    echo -e "${GREEN}===================================================${NC}\n"
}

# Error handler
error_handler() {
    log_error "Installation failed at line $1"
    log_error "Please check the error messages above and fix any issues"
    exit 1
}

trap 'error_handler $LINENO' ERR

# Wait for pod to be ready
wait_for_pod() {
    local namespace=$1
    local label=$2
    local timeout=${3:-300}
    
    log_info "Waiting for pod with label $label in namespace $namespace to be ready (timeout: ${timeout}s)..."
    
    local counter=0
    while [ $counter -lt $timeout ]; do
        if oc get pods -n "$namespace" -l "$label" --no-headers 2>/dev/null | grep -q "Running"; then
            local ready=$(oc get pods -n "$namespace" -l "$label" --no-headers 2>/dev/null | grep "Running" | awk '{print $2}')
            if [[ "$ready" == "1/1" ]] || [[ "$ready" == *"/"* && $(echo "$ready" | cut -d'/' -f1) -eq $(echo "$ready" | cut -d'/' -f2) ]]; then
                log_success "Pod is ready!"
                return 0
            fi
        fi
        sleep 5
        counter=$((counter + 5))
    done
    
    log_error "Timeout waiting for pod to be ready"
    return 1
}

# Wait for job to complete
wait_for_job() {
    local namespace=$1
    local job_name=$2
    local timeout=${3:-300}
    
    log_info "Waiting for job $job_name in namespace $namespace to complete (timeout: ${timeout}s)..."
    
    local counter=0
    while [ $counter -lt $timeout ]; do
        if oc get job "$job_name" -n "$namespace" -o jsonpath='{.status.succeeded}' 2>/dev/null | grep -q "1"; then
            log_success "Job completed successfully!"
            return 0
        fi
        
        # Check if job failed
        if oc get job "$job_name" -n "$namespace" -o jsonpath='{.status.failed}' 2>/dev/null | grep -q "[1-9]"; then
            log_error "Job failed!"
            return 1
        fi
        
        sleep 5
        counter=$((counter + 5))
    done
    
    log_error "Timeout waiting for job to complete"
    return 1
}

# ==============================================================================
# Prerequisites Check
# ==============================================================================

check_prerequisites() {
    log_step "1: Checking Prerequisites"
    
    # Check for oc CLI
    if ! command -v oc &> /dev/null; then
        log_error "OpenShift CLI (oc) is not installed or not in PATH"
        exit 1
    fi
    log_success "OpenShift CLI (oc) found: $(oc version --client | head -n1)"
    
    # Check if logged into OpenShift
    if ! oc whoami &> /dev/null; then
        log_error "Not logged into OpenShift cluster. Please run 'oc login' first"
        exit 1
    fi
    log_success "Logged into OpenShift as: $(oc whoami)"
    log_info "OpenShift server: $(oc whoami --show-server)"
    
    # Check for jq
    if ! command -v jq &> /dev/null; then
        log_error "jq is not installed. Please install jq for JSON parsing"
        exit 1
    fi
    log_success "jq found: $(jq --version)"
    
    # Check for curl
    if ! command -v curl &> /dev/null; then
        log_error "curl is not installed. Please install curl"
        exit 1
    fi
    log_success "curl found"
    
    # Check for config.env
    if [ ! -f "$CONFIG_FILE" ]; then
        log_error "Configuration file not found: $CONFIG_FILE"
        log_error "Please copy config.env.template to config.env and fill in your values"
        log_error "  cp config.env.template config.env"
        exit 1
    fi
    log_success "Configuration file found: $CONFIG_FILE"
    
    # Source config file
    log_info "Loading configuration from $CONFIG_FILE"
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
    
    # Validate required variables
    log_info "Validating required configuration variables..."
    local missing_vars=()
    
    # SREIPS Core variables
    [ -z "${SLACK_API_KEY:-}" ] && missing_vars+=("SLACK_API_KEY")
    [ -z "${SLACK_CHANNEL:-}" ] && missing_vars+=("SLACK_CHANNEL")
    [ -z "${SENTRY_DSN:-}" ] && missing_vars+=("SENTRY_DSN")
    [ -z "${CLUSTER_NAME:-}" ] && missing_vars+=("CLUSTER_NAME")
    
    # MinIO variables
    [ -z "${MINIO_ROOT_USER:-}" ] && missing_vars+=("MINIO_ROOT_USER")
    [ -z "${MINIO_ROOT_PASSWORD:-}" ] && missing_vars+=("MINIO_ROOT_PASSWORD")
    
    # RH KCS MCP variables
    [ -z "${RH_API_OFFLINE_TOKEN:-}" ] && missing_vars+=("RH_API_OFFLINE_TOKEN")
    
    # LlamaStack variables
    [ -z "${INFERENCE_MODEL:-}" ] && missing_vars+=("INFERENCE_MODEL")
    [ -z "${VLLM_URL:-}" ] && missing_vars+=("VLLM_URL")
    [ -z "${VLLM_TLS_VERIFY:-}" ] && missing_vars+=("VLLM_TLS_VERIFY")
    [ -z "${VLLM_API_TOKEN:-}" ] && missing_vars+=("VLLM_API_TOKEN")
    
    # SREIPS Agent variables
    [ -z "${VECTOR_DB_ID:-}" ] && missing_vars+=("VECTOR_DB_ID")
    
    if [ ${#missing_vars[@]} -gt 0 ]; then
        log_error "Missing required configuration variables:"
        for var in "${missing_vars[@]}"; do
            log_error "  - $var"
        done
        log_error "Please update your config.env file"
        exit 1
    fi
    
    log_success "All required configuration variables are set"
}

# ==============================================================================
# Module Installation Functions
# ==============================================================================

install_sreips_core() {
    log_step "2: Installing SREIPS Core"
    
    cd "${SCRIPT_DIR}/sreips-core" || exit 1
    
    log_info "Creating sreips-core namespace..."
    oc new-project sreips-core || oc project sreips-core
    
    log_info "Creating sreips-playbooks-config-secret with values from config.env..."
    # Read the playbooks config YAML, update values, and base64 encode BEFORE applying sreips-setup.yaml
    if [ -f "sreips-playbooks-config-secret.yaml" ]; then
        log_info "Processing sreips-playbooks-config-secret.yaml..."
        
        # Create a temporary file with updated values
        sed -e "s|api_key:.*|api_key: ${SLACK_API_KEY}|g" \
            -e "s|slack_channel:.*|slack_channel: ${SLACK_CHANNEL}|g" \
            -e "s|cluster_name:.*|cluster_name: ${CLUSTER_NAME}|g" \
            -e "s|clusterName:.*|clusterName: ${CLUSTER_NAME}|g" \
            sreips-playbooks-config-secret.yaml > /tmp/sreips-playbooks-config-updated.yaml
        
        # Base64 encode the updated config (cross-platform: works on both macOS and Linux)
        if base64 --wrap 2>&1 | grep -q "invalid option"; then
            # macOS (no --wrap flag)
            PLAYBOOKS_CONFIG_B64=$(base64 < /tmp/sreips-playbooks-config-updated.yaml | tr -d '\n')
        else
            # Linux (use --wrap=0 to prevent line breaks)
            PLAYBOOKS_CONFIG_B64=$(base64 --wrap=0 < /tmp/sreips-playbooks-config-updated.yaml)
        fi
        
        # Create the secret with updated configuration FIRST, before applying sreips-setup.yaml
        cat <<EOF | oc apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: sreips-playbooks-config-secret
  namespace: sreips-core
type: Opaque
data:
  active_playbooks.yaml: ${PLAYBOOKS_CONFIG_B64}
EOF
        
        # Clean up temp file
        rm -f /tmp/sreips-playbooks-config-updated.yaml
        
        log_success "Created sreips-playbooks-config-secret with Slack API key, channel, and cluster name"
    else
        log_warning "sreips-playbooks-config-secret.yaml not found"
    fi
    
    log_info "Applying sreips-setup.yaml (excluding sreips-playbooks-config-secret which we already created)..."
    # Split sreips-setup.yaml into individual resources and skip sreips-playbooks-config-secret
    cat sreips-setup.yaml | awk '
        BEGIN { 
            RS="---"
            in_secret=0
        }
        {
            # Check if this resource is the problematic secret
            if ($0 ~ /name: sreips-playbooks-config-secret/ && $0 ~ /kind: Secret/) {
                in_secret=1
            } else {
                in_secret=0
            }
            
            # Print the resource if it is not the problematic secret
            if (!in_secret && NF > 0) {
                print "---"
                print $0
            }
        }
    ' | oc apply -f -
    
    log_info "Patching sreips-runner-secret with Sentry DSN from config.env..."
    oc create secret generic sreips-runner-secret \
        --from-literal=SENTRY_DSN="$SENTRY_DSN" \
        -n sreips-core \
        --dry-run=client -o yaml | oc apply -f -
    
    log_info "Restarting sreips-runner deployment to pick up updated secrets..."
    oc rollout restart deployment/sreips-runner -n sreips-core
    oc rollout status deployment/sreips-runner -n sreips-core --timeout=300s
    
    log_info "Waiting for sreips-runner to be ready..."
    wait_for_pod "sreips-core" "app=sreips-runner" 300
    
    log_info "Waiting for sreips-forwarder to be ready..."
    wait_for_pod "sreips-core" "app=sreips-forwarder" 300
    
    log_success "SREIPS Core installation completed"
}

install_minio() {
    log_step "3: Installing MinIO"
    
    cd "${SCRIPT_DIR}/minio" || exit 1
    
    log_info "Creating minio namespace..."
    oc new-project minio || oc project minio
    
    log_info "Creating minio-secret with credentials from config.env..."
    oc create secret generic minio-secret \
        --from-literal=minio_root_user="$MINIO_ROOT_USER" \
        --from-literal=minio_root_password="$MINIO_ROOT_PASSWORD" \
        -n minio \
        --dry-run=client -o yaml | oc apply -f -
    
    log_info "Applying MinIO manifests..."
    oc apply -f all-in-one.yaml -n minio
    
    log_info "Waiting for MinIO deployment to be ready..."
    oc rollout status deployment/minio -n minio --timeout=300s
    
    log_info "Waiting for MinIO pod to be ready..."
    wait_for_pod "minio" "app=minio" 300
    
    log_info "Checking MinIO bucket creation job status..."
    # The job may have already run with old credentials, so we check and recreate if needed
    if oc get job minio-bucket-create -n minio &>/dev/null; then
        JOB_STATUS=$(oc get job minio-bucket-create -n minio -o jsonpath='{.status.succeeded}')
        if [ "$JOB_STATUS" != "1" ]; then
            log_info "Recreating bucket creation job with new credentials..."
            oc delete job minio-bucket-create -n minio --ignore-not-found=true
            sleep 5
            oc apply -f all-in-one.yaml -n minio
        fi
    fi
    
    log_info "Waiting for MinIO bucket creation job to complete..."
    wait_for_job "minio" "minio-bucket-create" 300
    
    log_success "MinIO installation completed"
}

install_rh_kcs_mcp() {
    log_step "4: Installing Red Hat KCS MCP Server"
    
    cd "${SCRIPT_DIR}/rh-kcs-mcp" || exit 1
    
    log_info "Creating mcp-servers namespace..."
    oc new-project mcp-servers || oc project mcp-servers
    
    log_info "Applying RH KCS MCP manifests..."
    oc apply -f all-in-one.yaml -n mcp-servers
    
    log_info "Patching redhat-api-token secret with token from config.env..."
    oc create secret generic redhat-api-token \
        --from-literal=RH_API_OFFLINE_TOKEN="$RH_API_OFFLINE_TOKEN" \
        -n mcp-servers \
        --dry-run=client -o yaml | oc apply -f -
    
    log_info "Restarting MCP deployment to pick up new token..."
    oc rollout restart deployment/redhat-api-mcp -n mcp-servers
    oc rollout status deployment/redhat-api-mcp -n mcp-servers --timeout=300s
    
    log_info "Waiting for MCP server pod to be ready..."
    wait_for_pod "mcp-servers" "app=redhat-api-mcp" 300
    
    log_info "Capturing MCP endpoint route..."
    MCP_ROUTE=$(oc get route rh-kcs -n mcp-servers -o jsonpath='{.spec.host}')
    export MCP_ENDPOINT="https://${MCP_ROUTE}/sse"
    log_success "MCP Endpoint: $MCP_ENDPOINT"
    
    log_success "Red Hat KCS MCP Server installation completed"
}

install_llamastack() {
    log_step "5: Installing LlamaStack"
    
    cd "${SCRIPT_DIR}/llamastack" || exit 1
    
    log_info "Applying OpenShift Data Science operators setup..."
    oc apply -f operators-setup.yaml
    
    log_info "Waiting for operators to be installed (60 seconds)..."
    sleep 60
    
    log_info "Creating llamastack namespace..."
    oc new-project llamastack || oc project llamastack
    
    log_info "Creating llama-stack-inference-model-secret with config.env values..."
    oc create secret generic llama-stack-inference-model-secret \
        --from-literal=INFERENCE_MODEL="$INFERENCE_MODEL" \
        --from-literal=VLLM_URL="$VLLM_URL" \
        --from-literal=VLLM_TLS_VERIFY="$VLLM_TLS_VERIFY" \
        --from-literal=VLLM_API_TOKEN="$VLLM_API_TOKEN" \
        -n llamastack \
        --dry-run=client -o yaml | oc apply -f -
    
    log_info "Applying LlamaStack manifests..."
    oc apply -f llamastack-distribution.yaml -n llamastack
    oc apply -f all-in-one.yaml -n llamastack
    
    log_info "Patching dashboard-dspa-secret with MinIO credentials from config.env..."
    # The credentials should match the actual MinIO username/password, not base64 encoded
    oc create secret generic dashboard-dspa-secret \
        --from-literal=AWS_ACCESS_KEY_ID="$MINIO_ROOT_USER" \
        --from-literal=AWS_SECRET_ACCESS_KEY="$MINIO_ROOT_PASSWORD" \
        -n llamastack \
        --dry-run=client -o yaml | oc apply -f -
    
    oc label secret dashboard-dspa-secret opendatahub.io/dashboard=true -n llamastack --overwrite
    
    log_info "Waiting for Data Science Pipeline server to be ready (this may take up to 6 minutes)..."
    sleep 360
    
    log_info "Capturing LlamaStack route..."
    LLAMA_ROUTE=$(oc get route lsd-llama-milvus-service -n llamastack -o jsonpath='{.spec.host}')
    export LLAMA_STACK_URL="https://${LLAMA_ROUTE}/"
    log_success "LlamaStack URL: $LLAMA_STACK_URL"
    
    log_info "Getting Data Science Pipeline route and token..."
    DS_PIPELINE_ROUTE=$(oc get route -n llamastack ds-pipeline-dspa -o jsonpath='{.spec.host}')
    OC_TOKEN=$(oc whoami -t)
    
    log_info "Uploading ingestion pipeline..."
    PIPELINE_UPLOAD_RESPONSE=$(curl -s -X POST "https://${DS_PIPELINE_ROUTE}/apis/v2beta1/pipelines/upload" \
        -H "Authorization: Bearer ${OC_TOKEN}" \
        -F "uploadfile=@./docling-pipeline_compiled.yaml" \
        -F "name=ingestion-pipeline" \
        -F "display_name=Enterprise KB Ingestion Pipeline" \
        -F "description=Pipeline for converting PDFs to markdown and ingesting into Milvus" \
        -F "namespace=llamastack")
    
    PIPELINE_ID=$(echo "$PIPELINE_UPLOAD_RESPONSE" | jq -r '.pipeline_id')
    log_success "Pipeline uploaded with ID: $PIPELINE_ID"
    
    log_info "Creating experiment..."
    EXPERIMENT_RESPONSE=$(curl -s -X POST "https://${DS_PIPELINE_ROUTE}/apis/v2beta1/experiments" \
        -H "Authorization: Bearer ${OC_TOKEN}" \
        -H "Content-Type: application/json" \
        -d '{
            "experiment_id": "auto-trigger-experiment",
            "display_name": "auto-trigger-experiment",
            "description": "Experiment for recurring PDF ingestion runs",
            "namespace": "llamastack"
        }')
    
    EXPERIMENT_ID=$(echo "$EXPERIMENT_RESPONSE" | jq -r '.experiment_id')
    log_success "Experiment created with ID: $EXPERIMENT_ID"
    
    log_info "Creating recurring run (every 12 hours)..."
    JSON_PAYLOAD=$(jq -n \
        --arg pipeline_id "$PIPELINE_ID" \
        --arg experiment_id "$EXPERIMENT_ID" \
        '{
            "display_name": "auto-run-every-12h",
            "description": "Trigger this pipeline every 12 hours",
            "pipeline_version_reference": {
                "pipeline_id": $pipeline_id
            },
            "experiment_id": $experiment_id,
            "max_concurrency": "1",
            "no_catchup": true,
            "trigger": {
                "cron_schedule": {
                    "cron": "0 0 */12 * * ?"
                }
            },
            "mode": "ENABLE"
        }')
    
    RECURRING_RUN_RESPONSE=$(curl -s -X POST "https://${DS_PIPELINE_ROUTE}/apis/v2beta1/recurringruns" \
        -H "Authorization: Bearer ${OC_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "$JSON_PAYLOAD")
    
    RECURRING_RUN_ID=$(echo "$RECURRING_RUN_RESPONSE" | jq -r '.recurring_run_id')
    log_success "Recurring run created with ID: $RECURRING_RUN_ID"
    
    log_info "Triggering initial ingestion run immediately..."
    INITIAL_RUN_RESPONSE=$(curl -s -X POST "https://${DS_PIPELINE_ROUTE}/apis/v2beta1/runs" \
        -H "Authorization: Bearer ${OC_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{
            \"pipeline_version_reference\": {
                \"pipeline_id\": \"${PIPELINE_ID}\"
            },
            \"display_name\": \"initial-ingestion-run\"
        }")
    
    INITIAL_RUN_ID=$(echo "$INITIAL_RUN_RESPONSE" | jq -r '.run_id')
    log_success "Initial ingestion run triggered with ID: $INITIAL_RUN_ID"
    
    log_success "LlamaStack installation completed"
}

install_sreips_agent() {
    log_step "6: Installing SREIPS Agent"
    
    cd "${SCRIPT_DIR}/sreips-agent" || exit 1
    
    log_info "Creating sreips-agent namespace..."
    oc new-project sreips-agent || oc project sreips-agent
    
    log_info "Applying SREIPS Agent manifests..."
    oc apply -f all-in-one.yaml -n sreips-agent
    
    log_info "Patching sreips-agent-config ConfigMap with captured URLs and config.env values..."
    oc create configmap sreips-agent-config \
        --from-literal=LLAMA_STACK_URL="$LLAMA_STACK_URL" \
        --from-literal=MCP_ENDPOINT="$MCP_ENDPOINT" \
        --from-literal=VECTOR_DB_ID="$VECTOR_DB_ID" \
        -n sreips-agent \
        --dry-run=client -o yaml | oc apply -f -
    
    log_info "Restarting SREIPS Agent deployment to pick up new configuration..."
    oc rollout restart deployment/sreips-agent -n sreips-agent
    oc rollout status deployment/sreips-agent -n sreips-agent --timeout=300s
    
    log_info "Waiting for SREIPS Agent pod to be ready..."
    wait_for_pod "sreips-agent" "app=sreips-agent" 300
    
    log_info "Capturing SREIPS Agent route..."
    SREIPS_AGENT_ROUTE=$(oc get route sreips-agent -n sreips-agent -o jsonpath='{.spec.host}')
    export SREIPS_AGENT_URL="https://${SREIPS_AGENT_ROUTE}"
    log_success "SREIPS Agent URL: $SREIPS_AGENT_URL"
    
    log_success "SREIPS Agent installation completed"
}

# ==============================================================================
# Main Installation Flow
# ==============================================================================

main() {
    log_step "SREIPS Master Bootstrap Script"
    log_info "Starting installation of all SREIPS components..."
    log_info "This process will install: sreips-core, minio, rh-kcs-mcp, llamastack, sreips-agent"
    
    # Check prerequisites
    check_prerequisites
    
    # Install components in sequence
    install_sreips_core
    install_minio
    install_rh_kcs_mcp
    install_llamastack
    install_sreips_agent
    
    # Final summary
    log_step "Installation Complete! 🎉"
    log_success "All SREIPS components have been successfully installed"
    echo ""
    log_info "Component URLs:"
    log_info "  - SREIPS Agent: $SREIPS_AGENT_URL"
    log_info "  - LlamaStack: $LLAMA_STACK_URL"
    log_info "  - MCP Server: $MCP_ENDPOINT"
    echo ""
    log_info "You can now test the SREIPS agent with:"
    echo "  curl -X POST $SREIPS_AGENT_URL/query \\"
    echo "    -H \"Content-Type: application/json\" \\"
    echo "    -d '{\"query\": \"CrashLoopBackOff OpenShift pod\"}'"
    echo ""
    log_success "Installation completed successfully!"
}

# Run main function
main "$@"

