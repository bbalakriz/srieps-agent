#!/bin/bash
# manual Hermes SREIPS integration steps (bootstrap.sh automates all of this)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${SCRIPT_DIR}/config.env" 2>/dev/null || true

echo "Hermes agent install is handled by bootstrap.sh install_hermes_rca_stack:"
echo "  hermes-sreips-skills ConfigMap from hermes-skills/sreips/"
echo "  hermes-all-in-one.yaml (SA, secrets, config, Deployment, Route, PVC)"
echo "  bootstrap substitutes REPLACE_* from config.env before oc apply"
