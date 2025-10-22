oc new-project sreips-agent


curl -X POST "http://localhost:8000/query" \
  -H "Content-Type: application/json" \
  -d '{"query": "what is the resolution for pod crashloop backoff issues in kubernetes?"}'