############## create sreips-runner and sreips-forwarder. ##############
oc new-project robusta
# helm template robusta robusta/robusta --version 0.28.1 -f values.yaml --set clusterName=openshift-prod --debug > robusta-setup.yaml
oc apply -f robusta-setup.yaml 

############## slack integration. ##############
# Creating a New Slack Bot / App

# - Go to your Slack workspace: https://api.slack.com/apps
# - Click Create New App → From scratch.
# - Name it and select your workspace.
# - Go to OAuth & Permissions:
# - Under Scopes, add:
#     - chat:write
#     - chat:write.public
# - Copy the Bot User OAuth Token (xoxb-...) and set it in the helm values.yaml

############## testing sreips-playbooks ##############
oc apply -f image-pull-backoff-pod.yaml 
oc apply -f https://gist.githubusercontent.com/robusta-lab/283609047306dc1f05cf59806ade30b6/raw -n default
oc delete deploy/crashpod -n default
oc run badpod --image=doesnotexist/broken:latest


############## obsolete --- deploy sreips-agent ##############
oc new-app https://github.com/bbalakriz/sreips-agent.git --strategy=source --name=agent -n robusta
#  oc delete is/agent bc/agent deploy/agent svc/agent
oc set env deployment/agent SLACK_WEBHOOK_URL=

#deploy rh-kcs-mcp
cd rh-kcs-mcp
oc new-project mcp-servers
oc apply -f all-in-one.yaml -n mcp-servers
