oc new-project llamastack 
export INFERENCE_MODEL="Llama-4-Scout-17B-16E-W4A16"
export VLLM_URL="https://litellm-litemaas.apps.prod.rhoai.rh-aiservices-bu.com/v1"
export VLLM_TLS_VERIFY="true" 
export VLLM_API_TOKEN=""

oc create secret generic llama-stack-inference-model-secret -n llamastack \
  --from-literal INFERENCE_MODEL="$INFERENCE_MODEL" \
  --from-literal VLLM_URL="$VLLM_URL" \
  --from-literal VLLM_TLS_VERIFY="$VLLM_TLS_VERIFY" \
  --from-literal VLLM_API_TOKEN="$VLLM_API_TOKEN"

oc apply -f llamastack-distribution.yaml -n llamastack
oc apply -f all-in-one.yaml -n llamastack

