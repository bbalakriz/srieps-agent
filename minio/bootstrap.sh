cd ../minio
oc new-project minio
oc apply -f all-in-one.yaml -n minio