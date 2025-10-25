export INFERENCE_MODEL="Llama-4-Scout-17B-16E-W4A16"
export VLLM_URL="https://litellm-litemaas.apps.prod.rhoai.rh-aiservices-bu.com/v1"
export VLLM_TLS_VERIFY="true" 
export VLLM_API_TOKEN=""

oc apply -f operators-setup.yaml
oc new-project llamastack 
oc create secret generic llama-stack-inference-model-secret -n llamastack \
  --from-literal INFERENCE_MODEL="$INFERENCE_MODEL" \
  --from-literal VLLM_URL="$VLLM_URL" \
  --from-literal VLLM_TLS_VERIFY="$VLLM_TLS_VERIFY" \
  --from-literal VLLM_API_TOKEN="$VLLM_API_TOKEN"

oc apply -f llamastack-distribution.yaml -n llamastack
oc apply -f all-in-one.yaml -n llamastack

sleep 360 # wait for the DS pipeline server pods to be ready

export ROUTE=$(oc get route -n llamastack ds-pipeline-dspa --template='{{ .spec.host }}')
export TOKEN=$(oc whoami -t)

export PIPELINE_ID=$(curl -X POST "https://${ROUTE}/apis/v2beta1/pipelines/upload" \
  -H "Authorization: Bearer ${TOKEN}" \
  -F "uploadfile=@./docling-pipeline_compiled.yaml" \
  -F "name=ingestion-pipeline" \
  -F "display_name=Enterprise KB Ingestion Pipeline" \
  -F "description=Pipeline for converting PDFs to markdown and ingesting into Milvus" \
  -F "namespace=llamastack" | jq -r '.pipeline_id')
echo "Extracted PIPELINE_ID: $PIPELINE_ID"

export EXPERIMENT_ID=$(curl -X POST "https://${ROUTE}/apis/v2beta1/experiments" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "experiment_id": "auto-trigger-experiment",
    "display_name": "auto-trigger-experiment",
    "description": "Experiment for recurring PDF ingestion runs",
    "namespace": "llamastack"
  }' | jq -r '.experiment_id')
  echo "Extracted EXPERIMENT_ID: $EXPERIMENT_ID"

# Build JSON payload using jq for proper variable substitution
JSON_PAYLOAD=$(jq -n \
  --arg pipeline_id "$PIPELINE_ID" \
  --arg experiment_id "$EXPERIMENT_ID" \
  '{
    "display_name": "auto-run-every-2h",
    "description": "Trigger this pipeline every 2 hours",
    "pipeline_version_reference": {
      "pipeline_id": $pipeline_id
    },
    "experiment_id": $experiment_id,
    "max_concurrency": "1",
    "no_catchup": true,
    "trigger": {
      "cron_schedule": {
        "cron": "0 */2 * * *"
      }
    },
    "mode": "ENABLE"
  }')

export RECURRING_RUN_ID=$(curl -X POST "https://${ROUTE}/apis/v2beta1/recurringruns" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$JSON_PAYLOAD" | jq -r '.recurring_run_id')
echo "Created RecurringRun: $RECURRING_RUN_ID"
