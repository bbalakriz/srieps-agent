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