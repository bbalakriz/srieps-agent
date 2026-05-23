#!/bin/bash

# ==============================================================================
# SREIPS Master Bootstrap Script
# ==============================================================================
# This script orchestrates the installation of all SREIPS components in sequence:
# 1. prerequisites check
# 2. mattermost (optional - only when MATTERMOST_ENABLED=true in config.env)
# 3. sreips-core
# 4. minio
# 5. ocp-mcp
# 6. rh-kcs-mcp
# 7. milvus
# 8. postgres
# 9. llamastack
# 10. patch rh-kcs-mcp with llamastack URL
# 11. sreips-rag-mcp
# 12. hermes agent + hermes-rca-bridge
#
# Mattermost is deployed before sreips-core because sreips-core needs the
# Mattermost bot token in its playbooks config secret. After Mattermost is
# deployed the script pauses so the bot can be created in the UI and the
# token saved to config.env - exactly like how Slack tokens are pre-configured
# before the bootstrap runs, except Mattermost is self-hosted and deployed here.
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

# strip quotes/whitespace from a config.env RHS value
strip_config_value() {
    local val="$1"
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    if [[ "$val" =~ ^\"(.*)\"$ ]]; then
        val="${BASH_REMATCH[1]}"
    elif [[ "$val" =~ ^\'(.*)\'$ ]]; then
        val="${BASH_REMATCH[1]}"
    fi
    printf '%s' "$val"
}

# read the last assignment for VAR from config.env (supports export or plain VAR=)
read_config_var_from_file() {
    local var_name="$1"
    local line rhs
    line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${var_name}=" "$CONFIG_FILE" 2>/dev/null \
        | grep -v '^[[:space:]]*#' | tail -1 || true)
    if [ -z "$line" ]; then
        printf ''
        return 0
    fi
    rhs="${line#*=}"
    strip_config_value "$rhs"
}

# reload Mattermost bot vars from disk (re-read after the bootstrap pause)
reload_mattermost_bot_tokens() {
    MATTERMOST_BOT_TOKEN="$(read_config_var_from_file MATTERMOST_BOT_TOKEN)"
    MATTERMOST_BOT_TOKEN_ID="$(read_config_var_from_file MATTERMOST_BOT_TOKEN_ID)"
    export MATTERMOST_BOT_TOKEN MATTERMOST_BOT_TOKEN_ID
}

mattermost_bot_tokens_configured() {
    reload_mattermost_bot_tokens
    [ -n "${MATTERMOST_BOT_TOKEN:-}" ] && [ -n "${MATTERMOST_BOT_TOKEN_ID:-}" ] \
        && [ "${MATTERMOST_BOT_TOKEN}" != "<your-token>" ] \
        && [ "${MATTERMOST_BOT_TOKEN_ID}" != "<your-token-id>" ]
}

# re-source full config.env; use set -a so non-export assignments are exported
reload_config_env() {
    set +u
    set -a
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
    set +a
    set -u
    reload_mattermost_bot_tokens
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
    [ -z "${SIGNING_KEY:-}" ] && missing_vars+=("SIGNING_KEY")
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
    [ -z "${OPENAI_BASE_URL:-}" ] && missing_vars+=("OPENAI_BASE_URL")
    [ -z "${OPENAI_API_KEY:-}" ] && missing_vars+=("OPENAI_API_KEY")
    [ -z "${POSTGRES_DB_USER:-}" ] && missing_vars+=("POSTGRES_DB_USER")
    [ -z "${POSTGRES_DB_PASSWORD:-}" ] && missing_vars+=("POSTGRES_DB_PASSWORD")
    [ -z "${POSTGRES_DB_NAME:-}" ] && missing_vars+=("POSTGRES_DB_NAME")
    
    # SREIPS Agent variables
    [ -z "${VECTOR_DB_ID:-}" ] && missing_vars+=("VECTOR_DB_ID")

    # Hermes agent
    [ -z "${HERMES_MODEL:-}" ] && missing_vars+=("HERMES_MODEL")
    
    # Mattermost variables (only required when MATTERMOST_ENABLED=true)
    if [ "${MATTERMOST_ENABLED:-false}" = "true" ]; then
        [ -z "${MATTERMOST_MYSQL_ROOT_PASSWORD:-}" ] && missing_vars+=("MATTERMOST_MYSQL_ROOT_PASSWORD")
        [ -z "${MATTERMOST_MYSQL_PASSWORD:-}" ] && missing_vars+=("MATTERMOST_MYSQL_PASSWORD")
    fi
    
    if [ ${#missing_vars[@]} -gt 0 ]; then
        log_error "Missing required configuration variables:"
        for var in "${missing_vars[@]}"; do
            log_error "  - $var"
        done
        log_error "Please update your config.env file"
        exit 1
    fi
    
    log_success "All required configuration variables are set"
    
    # Set optional Milvus password (default: auto-generated)
    MILVUS_PASSWORD="${MILVUS_PASSWORD:-}"
    export MILVUS_PASSWORD
}

# ==============================================================================
# Module Installation Functions
# ==============================================================================

install_mattermost() {
    log_step "2: Installing Mattermost (Optional)"
    
    cd "${SCRIPT_DIR}/mattermost" || exit 1
    
    log_info "Creating mattermost namespace..."
    oc new-project mattermost 2>/dev/null || oc project mattermost
    
    log_info "Creating MySQL secret with credentials from config.env..."
    oc create secret generic mattermost-team-edition-mysql \
        --from-literal=mysql-root-password="${MATTERMOST_MYSQL_ROOT_PASSWORD}" \
        --from-literal=mysql-password="${MATTERMOST_MYSQL_PASSWORD}" \
        -n mattermost \
        --dry-run=client -o yaml | oc apply -f -
    
    log_info "Creating Mattermost DB connection secret..."
    # connection string uses the mattermost db user (hardcoded as 'mattermost' in the mysql deployment)
    DB_CONN_STR="mysql://mattermost:${MATTERMOST_MYSQL_PASSWORD}@tcp(mattermost-team-edition-mysql:3306)/mattermost?charset=utf8mb4,utf8&readTimeout=30s&writeTimeout=30s"
    if base64 --wrap 2>&1 | grep -q "invalid option"; then
        DB_CONN_B64=$(echo -n "${DB_CONN_STR}" | base64 | tr -d '\n')
    else
        DB_CONN_B64=$(echo -n "${DB_CONN_STR}" | base64 --wrap=0)
    fi
    cat <<EOF | oc apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: mattermost-team-edition-mattermost-dbsecret
  namespace: mattermost
type: Opaque
data:
  mattermost.dbsecret: ${DB_CONN_B64}
EOF
    
    log_info "Applying Mattermost manifests (skipping placeholder secret resources)..."
    # the two secrets with <<your-value>> placeholders are skipped here because
    # we already created them above with real values from config.env
    cat mm-all-in-one.yaml | awk '
        BEGIN { RS="---"; in_secret=0 }
        {
            if (($0 ~ /name: mattermost-team-edition-mattermost-dbsecret/ ||
                 $0 ~ /name: mattermost-team-edition-mysql/) && $0 ~ /kind: Secret/) {
                in_secret=1
            } else {
                in_secret=0
            }
            if (!in_secret && NF > 0) {
                print "---"
                print $0
            }
        }
    ' | oc apply -f -
    
    log_info "Waiting for MySQL pod to be ready..."
    wait_for_pod "mattermost" "app=mattermost-team-edition-mysql" 300
    
    log_info "Waiting for Mattermost pod to be ready..."
    wait_for_pod "mattermost" "app.kubernetes.io/name=mattermost-team-edition" 300
    
    log_info "Capturing Mattermost route..."
    MM_ROUTE=$(oc get route mattermost-team-edition -n mattermost -o jsonpath='{.spec.host}')
    export MATTERMOST_URL="https://${MM_ROUTE}"
    log_success "Mattermost URL: $MATTERMOST_URL"
    
    # if the bot token is already in config.env (re-run scenario) skip the pause
    if mattermost_bot_tokens_configured; then
        log_success "Mattermost bot token already configured in config.env, skipping setup prompt"
        log_success "Mattermost installation completed"
        return 0
    fi
    
    echo ""
    log_warning "ACTION REQUIRED: Mattermost bot setup"
    log_info "Mattermost is now running at: $MATTERMOST_URL"
    log_info "Complete these steps in the Mattermost UI, then update config.env:"
    log_info "  1. Log in and go to System Console > Integrations > Bot Accounts"
    log_info "  2. Enable bot account creation and save"
    log_info "  3. Go to Integrations > Bot Accounts > Add Bot Account"
    log_info "  4. Create the bot (role: System Admin, postall permission enabled)"
    log_info "  5. Copy the Token and Token ID from the success screen (shown only once)"
    log_info "  6. Invite the bot to your Mattermost team:"
    log_info "       - Main menu > click your team name > Invite People > Invite Members"
    log_info "       - Search for the bot username, select it, and click Invite"
    log_info "  7. Create the alerts channel (name should match MATTERMOST_CHANNEL in config.env):"
    log_info "       - Sidebar: + next to Channels > Create New Channel"
    log_info "       - Name it ${MATTERMOST_CHANNEL:-sreips-helper} (Public or Private) > Create"
    log_info "  8. Add the bot to that channel:"
    log_info "       - Open the channel > click the channel name at the top > Add Members"
    log_info "       - Search for the bot username, select it, and click Add"
    log_info "  9. Edit this file (save before pressing Enter):"
    log_info "       $CONFIG_FILE"
    log_info "       export MATTERMOST_BOT_TOKEN=\"<your-token>\""
    log_info "       export MATTERMOST_BOT_TOKEN_ID=\"<your-token-id>\""
    log_info "  See Readme.md for the full Mattermost setup guide"
    echo ""
    read -r -p "Press Enter once config.env is saved with the Mattermost bot token to continue..."
    
    # re-read from disk (plain source can miss vars with set -u or missing export)
    reload_config_env
    
    if ! mattermost_bot_tokens_configured; then
        log_error "MATTERMOST_BOT_TOKEN and MATTERMOST_BOT_TOKEN_ID must be set in config.env"
        log_error "File: $CONFIG_FILE"
        if grep -qE '^[[:space:]]*(export[[:space:]]+)?MATTERMOST_BOT_TOKEN=' "$CONFIG_FILE" 2>/dev/null; then
            log_error "  MATTERMOST_BOT_TOKEN line found but value is empty or still a placeholder"
        else
            log_error "  MATTERMOST_BOT_TOKEN assignment not found (check variable name spelling)"
        fi
        if grep -qE '^[[:space:]]*(export[[:space:]]+)?MATTERMOST_BOT_TOKEN_ID=' "$CONFIG_FILE" 2>/dev/null; then
            log_error "  MATTERMOST_BOT_TOKEN_ID line found but value is empty or still a placeholder"
        else
            log_error "  MATTERMOST_BOT_TOKEN_ID assignment not found (check variable name spelling)"
        fi
        log_error "Use export MATTERMOST_BOT_TOKEN=\"...\" and export MATTERMOST_BOT_TOKEN_ID=\"...\" then re-run bootstrap"
        exit 1
    fi
    
    log_success "Mattermost installation completed"
}

install_sreips_core() {
    log_step "3: Installing SREIPS Core"
    
    cd "${SCRIPT_DIR}/sreips-core" || exit 1
    
    log_info "Creating sreips-core namespace..."
    oc new-project sreips-core || oc project sreips-core
    
    log_info "Applying sreips-setup.yaml (excluding sreips-playbooks-config-secret)..."
    # apply setup first so sreips-runner-service-account exists before we
    # generate the Prometheus token and build the config secret
    cat sreips-setup.yaml | awk '
        BEGIN { 
            RS="---"
            in_secret=0
        }
        {
            if ($0 ~ /name: sreips-playbooks-config-secret/ && $0 ~ /kind: Secret/) {
                in_secret=1
            } else {
                in_secret=0
            }
            if (!in_secret && NF > 0) {
                print "---"
                print $0
            }
        }
    ' | oc apply -f -
    
    log_info "Granting cluster-monitoring-view to sreips-runner-service-account..."
    oc adm policy add-cluster-role-to-user cluster-monitoring-view \
        -z sreips-runner-service-account -n sreips-core
    
    log_info "Generating Prometheus auth token (1-year duration)..."
    PROMETHEUS_TOKEN=$(oc create token sreips-runner-service-account -n sreips-core --duration=8760h)
    log_success "Prometheus auth token generated"
    
    log_info "Creating sreips-playbooks-config-secret with values from config.env..."
    if [ -f "sreips-playbooks-config-secret.yaml" ]; then
        log_info "Processing sreips-playbooks-config-secret.yaml..."
        
        # substitute all credentials and the generated Prometheus token in one pass
        sed -e "s|api_key:.*|api_key: ${SLACK_API_KEY}|g" \
            -e "s|slack_channel:.*|slack_channel: ${SLACK_CHANNEL}|g" \
            -e "s|signing_key:.*|signing_key: \"${SIGNING_KEY}\"|g" \
            -e "s|cluster_name:.*|cluster_name: ${CLUSTER_NAME}|g" \
            -e "s|clusterName:.*|clusterName: ${CLUSTER_NAME}|g" \
            -e "s|prometheus_auth: \"Bearer <INSERT_YOUR_TOKEN_HERE>\"|prometheus_auth: \"Bearer ${PROMETHEUS_TOKEN}\"|g" \
            sreips-playbooks-config-secret.yaml > /tmp/sreips-playbooks-config-updated.yaml
        
        # when Mattermost is enabled, substitute the bot token, token_id and channel
        # into the mattermost_sink section of the playbooks config
        if [ "${MATTERMOST_ENABLED:-false}" = "true" ]; then
            log_info "Substituting Mattermost bot token into playbooks config..."
            sed -e "s|    token: \"[^\"]*\"|    token: \"${MATTERMOST_BOT_TOKEN}\"|g" \
                -e "s|    token_id: \"[^\"]*\"|    token_id: \"${MATTERMOST_BOT_TOKEN_ID}\"|g" \
                -e "s|    channel: \"[^\"]*\"|    channel: \"${MATTERMOST_CHANNEL:-sreips-helper}\"|g" \
                /tmp/sreips-playbooks-config-updated.yaml > /tmp/sreips-playbooks-config-mm.yaml
            mv /tmp/sreips-playbooks-config-mm.yaml /tmp/sreips-playbooks-config-updated.yaml
        fi
        
        # Base64 encode the updated config (cross-platform: works on both macOS and Linux)
        if base64 --wrap 2>&1 | grep -q "invalid option"; then
            # macOS (no --wrap flag)
            PLAYBOOKS_CONFIG_B64=$(base64 < /tmp/sreips-playbooks-config-updated.yaml | tr -d '\n')
        else
            # Linux (use --wrap=0 to prevent line breaks)
            PLAYBOOKS_CONFIG_B64=$(base64 --wrap=0 < /tmp/sreips-playbooks-config-updated.yaml)
        fi
        
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
        
        rm -f /tmp/sreips-playbooks-config-updated.yaml
        
        if [ "${MATTERMOST_ENABLED:-false}" = "true" ]; then
            log_success "Created sreips-playbooks-config-secret with Slack, Mattermost, Prometheus auth, signing key, and cluster name"
        else
            log_success "Created sreips-playbooks-config-secret with Slack, Prometheus auth, signing key, and cluster name"
        fi
    else
        log_warning "sreips-playbooks-config-secret.yaml not found"
    fi
    
    log_info "Restarting sreips-runner deployment to pick up the config secret..."
    oc rollout restart deployment/sreips-runner -n sreips-core
    oc rollout status deployment/sreips-runner -n sreips-core --timeout=300s
    
    log_info "Waiting for sreips-runner to be ready..."
    wait_for_pod "sreips-core" "app=sreips-runner" 300
    
    log_info "Waiting for sreips-forwarder to be ready..."
    wait_for_pod "sreips-core" "app=sreips-forwarder" 300
    
    log_success "SREIPS Core installation completed"
}

install_minio() {
    log_step "4: Installing MinIO"
    
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

install_ocp_mcp() {
    log_step "5: Installing OpenShift MCP Server"
    
    cd "${SCRIPT_DIR}/ocp-mcp" || exit 1
    
    log_info "Creating mcp-servers namespace (if not exists)..."
    oc new-project mcp-servers 2>/dev/null || oc project mcp-servers
    
    log_info "Applying OCP MCP manifests..."
    oc apply -f all-in-one.yaml -n mcp-servers
    
    log_info "Waiting for OCP MCP server pod to be ready..."
    wait_for_pod "mcp-servers" "app=ocp-mcp-server" 300
    
    log_info "Capturing OCP MCP endpoint route..."
    export OCP_MCP_ENDPOINT="http://ocp-mcp-server.mcp-servers.svc.cluster.local:8000/sse"
    log_success "OCP MCP Endpoint (internal): $OCP_MCP_ENDPOINT"
    
    log_success "OpenShift MCP Server installation completed"
}

install_rh_kcs_mcp() {
    log_step "6: Installing Red Hat KCS MCP Server"
    
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
    export MCP_ENDPOINT="http://redhat-api-mcp.mcp-servers.svc.cluster.local:8000/sse"
    log_success "RH KCS MCP Endpoint (internal): $MCP_ENDPOINT"
    
    log_success "Red Hat KCS MCP Server installation completed"
}

install_milvus() {
    log_step "7: Installing Milvus Vector Database"
    
    cd "${SCRIPT_DIR}/milvus" || exit 1
    
    log_info "Creating llamastack namespace..."
    oc new-project llamastack 2>/dev/null || oc project llamastack
    
    log_info "Creating milvus-secret with credentials from config.env..."
    oc create secret generic milvus-secret \
        --from-literal=MILVUS_ENDPOINT="tcp://milvus-service:19530" \
        --from-literal=MILVUS_TOKEN="$MILVUS_PASSWORD" \
        --from-literal=MILVUS_CONSISTENCY_LEVEL="Bounded" \
        -n llamastack \
        --dry-run=client -o yaml | oc apply -f -
    
    log_info "Applying Milvus manifests..."
    oc apply -f all-in-one.yaml -n llamastack
    
    log_info "Waiting for etcd deployment to be ready..."
    oc rollout status deployment/etcd-deployment -n llamastack --timeout=300s
    
    log_info "Waiting for Milvus standalone deployment to be ready..."
    oc rollout status deployment/milvus-standalone -n llamastack --timeout=600s
    
    log_info "Waiting for Milvus pod to be ready..."
    wait_for_pod "llamastack" "app=milvus-standalone" 600
    
    log_success "Milvus installation completed"
}

install_postgres() {
    log_step "8: Installing PostgreSQL for LlamaStack"
    
    cd "${SCRIPT_DIR}/llamastack" || exit 1
    
    log_info "Creating llamastack namespace..."
    oc new-project llamastack 2>/dev/null || oc project llamastack
    
    log_info "Creating postgres-llamastack secret with credentials from config.env..."
    oc create secret generic postgres-llamastack \
        --from-literal=database-user="$POSTGRES_DB_USER" \
        --from-literal=database-password="$POSTGRES_DB_PASSWORD" \
        --from-literal=database-name="$POSTGRES_DB_NAME" \
        -n llamastack \
        --dry-run=client -o yaml | oc apply -f -
    
    log_info "Applying PostgreSQL manifests..."
    oc apply -f postgres-llamastack.yaml -n llamastack
    
    log_info "Waiting for PostgreSQL deployment to be ready..."
    oc rollout status deployment/postgres-llamastack -n llamastack --timeout=300s
    
    log_info "Waiting for PostgreSQL pod to be ready..."
    wait_for_pod "llamastack" "app=postgres-llamastack" 300
    
    log_success "PostgreSQL installation completed"
}

install_llamastack() {
    log_step "9: Installing LlamaStack"
    
    cd "${SCRIPT_DIR}/llamastack" || exit 1
    
    log_info "Applying rhoai-operator.3.4.0 operator setup..."
    oc apply -f operators-setup.yaml

    log_info "Waiting for operator to be installed (60 seconds)..."
    sleep 60 

    log_info "Approving rhoai-operator.3.4.0 operator install plan..."
    INSTALL_PLAN=$(oc get installplan -n redhat-ods-operator \
            -o jsonpath='{.items[?(@.spec.clusterServiceVersionNames[0]=="rhods-operator.3.4.0")].metadata.name}')

    oc patch installplan $INSTALL_PLAN \
        -n redhat-ods-operator \
        --type merge \
        --patch '{"spec":{"approved":true}}'
    
    log_info "Waiting for all relevant rhoai components to be installed (120 seconds)..."
    sleep 30

    log_info "Applying data science cluster setup..."
    oc apply -f dsc-setup.yaml

    log_info "Waiting for all dsc pods to be installed (180 seconds)..."
    sleep 30
    
    log_info "Creating llamastack namespace..."
    oc new-project llamastack || oc project llamastack
    
    log_info "Creating llama-stack-inference-model-secret with config.env values..."
    oc create secret generic llama-stack-inference-model-secret \
        --from-literal=OPENAI_BASE_URL="$OPENAI_BASE_URL" \
        --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY" \
        --from-literal=INFERENCE_MODEL="$INFERENCE_MODEL" \
        --from-literal=VLLM_URL="$VLLM_URL" \
        --from-literal=VLLM_TLS_VERIFY="$VLLM_TLS_VERIFY" \
        --from-literal=VLLM_API_TOKEN="$VLLM_API_TOKEN" \
        -n llamastack \
        --dry-run=client -o yaml | oc apply -f -
    
    log_info "Creating llamastack-run-config ConfigMap..."
    oc apply -f llamastack-run-config.yaml -n llamastack

    log_info "Applying LlamaStack distribution and supporting manifests..."
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
    sleep 30
    
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

patch_rh_kcs_mcp_with_llamastack_url() {
    log_step "10: Patching RH KCS MCP with LlamaStack URL"
    
    log_info "Updating rh-kcs-mcp deployment with LLAMA_STACK_URL and KCS_MODE..."
    oc set env deployment/redhat-api-mcp \
        LLAMA_STACK_URL="$LLAMA_STACK_URL" \
        KCS_MODE="${KCS_MODE:-offline}" \
        -n mcp-servers
    
    log_info "Waiting for rh-kcs-mcp deployment to rollout..."
    oc rollout status deployment/redhat-api-mcp -n mcp-servers --timeout=300s
    
    log_info "Waiting for updated rh-kcs-mcp pod to be ready..."
    wait_for_pod "mcp-servers" "app=redhat-api-mcp" 300
    
    log_success "RH KCS MCP patched with LlamaStack URL"
}

capture_hermes_base_url() {
    local hermes_route
    hermes_route=$(oc get route hermes -n hermes-agent -o jsonpath='{.spec.host}' 2>/dev/null)
    if [ -z "$hermes_route" ]; then
        log_error "Hermes route 'hermes' not found in hermes-agent namespace"
        return 1
    fi
    export HERMES_BASE_URL="https://${hermes_route}"
    log_success "Hermes URL captured from route: $HERMES_BASE_URL"
}

install_sreips_rag_mcp() {
    log_step "11: Installing SREIPS RAG MCP (enterprise KB bridge)"

    cd "${SCRIPT_DIR}/sreips-rag-mcp" || exit 1

    oc new-project hermes-agent 2>/dev/null || oc project hermes-agent

    log_info "Creating sreips-rag-mcp-config..."
    oc create configmap sreips-rag-mcp-config \
        --from-literal=LLAMA_STACK_URL="$LLAMA_STACK_URL" \
        --from-literal=VECTOR_DB_ID="${VECTOR_DB_ID:-sreips_vector_id}" \
        -n hermes-agent \
        --dry-run=client -o yaml | oc apply -f -

    log_info "Applying SREIPS RAG MCP manifests..."
    oc apply -f all-in-one.yaml -n hermes-agent

    log_info "Waiting for sreips-rag-mcp pod..."
    wait_for_pod "hermes-agent" "app=sreips-rag-mcp" 300

    export SREIPS_RAG_MCP_INTERNAL="http://sreips-rag-mcp.hermes-agent.svc.cluster.local:8000/sse"
    log_success "SREIPS RAG MCP ready: $SREIPS_RAG_MCP_INTERNAL"
}

create_hermes_sreips_skills_configmap() {
    local skills_dir="${SCRIPT_DIR}/hermes-skills/sreips"
    local cm_args=()
    local skill_dir

    if [ ! -d "$skills_dir" ]; then
        log_error "Skills directory not found: $skills_dir"
        return 1
    fi

    for skill_dir in "${skills_dir}"/*/; do
        [ -f "${skill_dir}SKILL.md" ] || continue
        cm_args+=( "--from-file=$(basename "$skill_dir")=${skill_dir}SKILL.md" )
    done

    if [ "${#cm_args[@]}" -eq 0 ]; then
        log_error "No SKILL.md files under ${skills_dir}"
        return 1
    fi

    oc create configmap hermes-sreips-skills \
        "${cm_args[@]}" \
        -n hermes-agent \
        --dry-run=client -o yaml | oc apply -f -
}

apply_hermes_all_in_one() {
    local manifest ocp_sse kcs_sse rag_sse model openrouter_key rendered
    manifest="${SCRIPT_DIR}/hermes-agent/hermes-all-in-one.yaml"

    ocp_sse="${OCP_MCP_ENDPOINT:-http://ocp-mcp-server.mcp-servers.svc.cluster.local:8000/sse}"
    kcs_sse="${MCP_ENDPOINT:-http://redhat-api-mcp.mcp-servers.svc.cluster.local:8000/sse}"
    rag_sse="${SREIPS_RAG_MCP_INTERNAL:-http://sreips-rag-mcp.hermes-agent.svc.cluster.local:8000/sse}"
    model="${HERMES_MODEL:-${INFERENCE_MODEL}}"
    openrouter_key="${OPENROUTER_APIKEY:-${OPENROUTER_API_TOKEN:-}}"

    if [ -z "${OCP_MCP_ENDPOINT:-}" ] || [ -z "${MCP_ENDPOINT:-}" ]; then
        log_warning "OCP_MCP_ENDPOINT or MCP_ENDPOINT not set; Hermes MCP URLs may be wrong"
    fi
    if [ -z "$openrouter_key" ]; then
        log_warning "OPENROUTER_APIKEY not set; hermes-openrouter-secret will use placeholder"
        openrouter_key="replace-me"
    fi

    if [ ! -f "$manifest" ]; then
        log_error "Hermes manifest not found: $manifest"
        return 1
    fi

    rendered=$(mktemp)
    sed -e "s|REPLACE_VLLM_URL|${VLLM_URL}|g" \
        -e "s|REPLACE_HERMES_MODEL|${model}|g" \
        -e "s|REPLACE_OCP_MCP_SSE|${ocp_sse}|g" \
        -e "s|REPLACE_RH_KCS_SSE|${kcs_sse}|g" \
        -e "s|REPLACE_SREIPS_RAG_SSE|${rag_sse}|g" \
        -e "s|REPLACE_VLLM_API_TOKEN|${VLLM_API_TOKEN}|g" \
        -e "s|REPLACE_OPENROUTER_API_KEY|${openrouter_key}|g" \
        -e "s|REPLACE_HERMES_API_KEY|${HERMES_API_KEY}|g" \
        "$manifest" >"$rendered"

    oc apply -f "$rendered" -n hermes-agent
    rm -f "$rendered"
    log_success "Hermes agent manifests applied"
}

ensure_hermes_api_key() {
    if [ -n "${HERMES_API_KEY:-}" ]; then
        return 0
    fi
    HERMES_API_KEY="$(openssl rand -hex 32)"
    export HERMES_API_KEY
    log_warning "HERMES_API_KEY was not set; generated a new API key for Hermes agent and RCA bridge"
    log_info "Add to config.env: export HERMES_API_KEY=\"${HERMES_API_KEY}\""
}

install_hermes_agent() {
    ensure_hermes_api_key
    create_hermes_sreips_skills_configmap

    if oc get deployment hermes -n hermes-agent &>/dev/null; then
        log_info "Hermes agent already deployed; applying updated manifests"
    else
        log_info "Deploying Hermes agent (hermes-all-in-one.yaml)..."
    fi

    apply_hermes_all_in_one

    oc adm policy add-scc-to-user anyuid -z hermes -n hermes-agent 2>/dev/null \
        || log_warning "Could not add anyuid SCC to hermes SA (may already exist)"

    # always restart so pods pick up SCC on first install (apply runs before SCC grant)
    oc rollout restart deployment/hermes -n hermes-agent
    oc rollout status deployment/hermes -n hermes-agent --timeout=300s

    log_info "Waiting for Hermes agent pod..."
    wait_for_pod "hermes-agent" "app=hermes" 300

    capture_hermes_base_url || return 1
    log_success "Hermes agent deployed"
}

apply_hermes_rca_bridge() {
    local manifest model cluster_name rendered
    manifest="${SCRIPT_DIR}/hermes-rca-bridge/all-in-one.yaml"
    model="${HERMES_MODEL:-Qwen3.6-35B-A3B}"
    cluster_name="${CLUSTER_NAME:-openshift}"

    if [ ! -f "$manifest" ]; then
        log_error "Hermes RCA bridge manifest not found: $manifest"
        return 1
    fi

    if [ -z "${HERMES_BASE_URL:-}" ]; then
        log_error "HERMES_BASE_URL is empty; Hermes route must exist before deploying the RCA bridge"
        return 1
    fi

    rendered=$(mktemp)
    sed -e "s|REPLACE_HERMES_BASE_URL|${HERMES_BASE_URL}|g" \
        -e "s|REPLACE_HERMES_MODEL|${model}|g" \
        -e "s|REPLACE_CLUSTER_NAME|${cluster_name}|g" \
        -e "s|REPLACE_HERMES_API_KEY|${HERMES_API_KEY:-}|g" \
        "$manifest" >"$rendered"

    oc apply -f "$rendered" -n hermes-agent
    rm -f "$rendered"
    log_success "Hermes RCA bridge manifests applied"
}

install_hermes_rca_stack() {
    log_step "12: Installing Hermes agent, RCA bridge, and SREIPS skills"

    oc new-project hermes-agent 2>/dev/null || oc project hermes-agent

    install_hermes_agent || return 1

    log_info "Deploying Hermes RCA bridge (all-in-one.yaml)..."
    apply_hermes_rca_bridge

    log_info "Waiting for hermes-rca-bridge pod..."
    wait_for_pod "hermes-agent" "app=hermes-rca-bridge" 300

    export HERMES_RCA_URL="http://hermes-rca-bridge.hermes-agent.svc.cluster.local:8000"
    log_success "Hermes RCA bridge URL: $HERMES_RCA_URL"

    log_info "Verify Hermes agent:"
    log_info "  oc -n hermes-agent exec deploy/hermes -- ls -laR /etc/hermes/skills/sreips"
}

# print offline KCS ingest steps when KCS_MODE is offline (default)
print_offline_kcs_ingest_reminder() {
    local kcs_mode
    kcs_mode="$(read_config_var_from_file KCS_MODE)"
    kcs_mode="${kcs_mode:-offline}"
    kcs_mode="$(echo "$kcs_mode" | tr '[:upper:]' '[:lower:]')"
    if [ "$kcs_mode" != "offline" ]; then
        return 0
    fi

    echo ""
    log_warning "Next step: offline KCS ingest (KCS_MODE=offline)"
    log_info "The RH KCS MCP server is in offline mode. Run the ingest workflow before KCS queries will work."
    log_info "Full details: ${SCRIPT_DIR}/Readme.md (section: KCS offline ingest)"
    echo ""
    log_info "  1. On an internet connected machine, export articles:"
    echo "       export RH_API_OFFLINE_TOKEN=\"<token-from-access.redhat.com/management/api>\""
    echo "       cd ${SCRIPT_DIR}/kcs-exporter"
    echo "       pip install -r requirements.txt"
    echo "       python export_kcs.py --output kcs-articles.ndjson --products ocp"
    echo ""
    log_info "  2. Copy kcs-articles.ndjson to this host (repo root: ${SCRIPT_DIR})"
    echo ""
    log_info "  3. Stage onto the cluster PVC:"
    echo "       oc -n llamastack apply -f ${SCRIPT_DIR}/kcs-exporter/kcs-data-pvc.yaml"
    echo "       oc -n llamastack apply -f ${SCRIPT_DIR}/kcs-exporter/kcs-stage-deployment.yaml"
    echo "       oc -n llamastack wait --for=condition=Ready pod -l app=kcs-stage --timeout=120s"
    echo "       POD_NAME=\$(oc -n llamastack get pods -l app=kcs-stage -o jsonpath='{.items[0].metadata.name}')"
    echo "       oc -n llamastack cp ${SCRIPT_DIR}/kcs-exporter/kcs-articles.ndjson \"\${POD_NAME}:/data/kcs-articles.ndjson\""
    echo "       oc -n llamastack delete deployment kcs-stage"
    echo ""
    log_info "  4. Trigger ingest into Milvus (kcs_vector_id store):"
    echo "       oc -n llamastack create -f ${SCRIPT_DIR}/kcs-exporter/kcs-ingest-job.yaml"
    echo "       oc -n llamastack logs -l app=kcs-ingest -f"
    echo ""
    log_info "  Do not restart LlamaStack pods after ingest unless necessary (see Readme.md)."
}

# ==============================================================================
# Main Installation Flow
# ==============================================================================

main() {
    log_step "SREIPS Master Bootstrap Script"
    log_info "Starting installation of all SREIPS components..."
    
    # check_prerequisites sources config.env, making MATTERMOST_ENABLED available
    check_prerequisites
    
    # now config.env is sourced - log the component list and sequence
    if [ "${MATTERMOST_ENABLED:-false}" = "true" ]; then
        log_info "This process will install: mattermost, sreips-core, minio, ocp-mcp, rh-kcs-mcp, milvus, postgres, llamastack, sreips-rag-mcp, hermes agent, hermes-rca-bridge"
    else
        log_info "This process will install: sreips-core, minio, ocp-mcp, rh-kcs-mcp, milvus, postgres, llamastack, sreips-rag-mcp, hermes agent, hermes-rca-bridge"
    fi
    
    # deploy Mattermost before sreips-core so the bot token is available
    # when the sreips-core playbooks config secret is created
    if [ "${MATTERMOST_ENABLED:-false}" = "true" ]; then
        install_mattermost
    fi
    
    # Install components in sequence
    install_sreips_core
    install_minio
    install_ocp_mcp
    install_rh_kcs_mcp
    install_milvus
    install_postgres
    install_llamastack
    patch_rh_kcs_mcp_with_llamastack_url

    install_sreips_rag_mcp
    install_hermes_rca_stack
    
    # Final summary
    log_step "Installation Complete!"
    log_success "All SREIPS components have been successfully installed"
    echo ""
    log_info "Component URLs:"
    log_info "  - LlamaStack: $LLAMA_STACK_URL"
    log_info "  - RH KCS MCP Server: $MCP_ENDPOINT"
    log_info "  - OCP MCP Server: $OCP_MCP_ENDPOINT"
    log_info "  - Hermes RCA bridge: ${HERMES_RCA_URL:-http://hermes-rca-bridge.hermes-agent.svc.cluster.local:8000}"
    log_info "  - Hermes agent: ${HERMES_BASE_URL:-not captured yet}"
    if [ "${MATTERMOST_ENABLED:-false}" = "true" ]; then
        log_info "  - Mattermost: $MATTERMOST_URL"
    fi
    echo ""
    echo "  curl -X POST ${HERMES_RCA_URL:-http://hermes-rca-bridge.hermes-agent.svc.cluster.local:8000}/analyze \\"
    echo "    -H \"Content-Type: application/json\" \\"
    echo "    -d '{\"event_reason\":\"CrashLoopBackOff\",\"resource_kind\":\"Pod\",\"search_query\":\"CrashLoopBackOff Pod OpenShift\"}'"
    echo ""

    print_offline_kcs_ingest_reminder

    log_success "Installation completed successfully!"
}

# Run main function
main "$@"

