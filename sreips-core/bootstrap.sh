############## create sreips-runner and sreips-forwarder. ##############
cd ../sreips-core
oc new-project sreips-core
oc apply -f sreips-setup.yaml 

############## slack integration. ##############
# Creating a New Slack Bot / App

# - Go to your Slack workspace: https://api.slack.com/apps
# - Click Create New App → From scratch.
# - Name it and select your workspace.
# - Go to OAuth & Permissions:
# - Under Scopes, add:
#     - chat:write
#     - chat:write.public
# - Copy the Bot User OAuth Token (xoxb-...) and set it in sreips-playbooks-config-secret.yaml

############## basic testing sreips ##############
oc apply -f image-pull-backoff-pod.yaml -n sreips-core
oc apply -f https://gist.githubusercontent.com/robusta-lab/283609047306dc1f05cf59806ade30b6/raw -n sreips-core
oc delete deploy/crashpod -n sreips-core
oc run badpod --image=doesnotexist/broken:latest -n sreips-core


#deploy rh-kcs-mcp
cd rh-kcs-mcp
oc new-project mcp-servers
oc apply -f all-in-one.yaml -n mcp-servers
