### miluvs remote with lls is failing due to the bug with files_api integration as noted here https://github.com/llamastack/llama-stack/issues/2626
#oc new-project milvus
#helm repo add milvus https://zilliztech.github.io/milvus-helm/
#helm repo update
#curl -o milvus-openshift-value.yaml https://raw.githubusercontent.com/rh-aiservices-bu/llm-on-openshift/main/vector-databases/milvus/openshift-values.yaml
#helm template -f milvus-openshift-values.yaml vectordb --set cluster.enabled=false --set etcd.replicaCount=1 --set minio.mode=standalone --set pulsar.enabled=false milvus/milvus > milvus_manifest_standalone.yaml
#yq '(select(.kind == "StatefulSet" and .metadata.name == "vectordb-etcd") | .spec.template.spec.securityContext) = {}' -i milvus_manifest_standalone.yaml
#yq '(select(.kind == "StatefulSet" and .metadata.name == "vectordb-etcd") | .spec.template.spec.containers[0].securityContext) = {"capabilities": {"drop": ["ALL"]}, "runAsNonRoot": true, "allowPrivilegeEscalation": false}' -i milvus_manifest_standalone.yaml
#yq '(select(.kind == "Deployment" and .metadata.name == "vectordb-minio") | .spec.template.spec.containers[0].securityContext) = {"capabilities": {"drop": ["ALL"]}, "runAsNonRoot": true, "allowPrivilegeEscalation": false, "seccompProfile": {"type": "RuntimeDefault"} }' -i milvus_manifest_standalone.yaml
#yq '(select(.kind == "Deployment" and .metadata.name == "vectordb-minio") | .spec.template.spec.securityContext) = {}' -i milvus_manifest_standalone.yaml
#yq '(select(.kind == "StatefulSet" and .metadata.name == "vectordb-pulsarv3-zookeeper") | .spec.template.spec.securityContext) = {}' -i milvus_manifest_standalone.yaml
#yq '(select(.kind == "StatefulSet" and .metadata.name == "vectordb-pulsarv3-zookeeper") | .spec.template.spec.containers[0].securityContext) = {"capabilities": {"drop": ["ALL"]}, "runAsNonRoot": true, "allowPrivilegeEscalation": false}' -i milvus_manifest_standalone.yaml
#yq '(select(.kind == "StatefulSet" and .metadata.name == "vectordb-pulsarv3-bookie") | .spec.template.spec.securityContext) = {}' -i milvus_manifest_standalone.yaml
#yq '(select(.kind == "StatefulSet" and .metadata.name == "vectordb-pulsarv3-bookie") | .spec.template.spec.containers[0].securityContext) = {"capabilities": {"drop": ["ALL"]}, "runAsNonRoot": true, "allowPrivilegeEscalation": false}' -i milvus_manifest_standalone.yaml
#oc apply -f milvus_manifest_standalone.yaml
#oc apply -f https://raw.githubusercontent.com/rh-aiservices-bu/llm-on-openshift/main/vector-databases/milvus/attu-deployment.yaml
################################

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

oc apply -f llamastack-run-config.yaml -n llamastack
oc apply -f llamastack-distribution.yaml -n llamastack

