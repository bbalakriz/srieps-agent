cd ../sreips-agent
oc new-project sreips-agent
oc apply -f all-in-one.yaml -n sreips-agent

curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "CrashLoopBackOff OpenShift pod"}'