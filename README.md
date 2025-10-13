Creating a New Slack Bot / App

- Go to your Slack workspace: https://api.slack.com/apps
- Click Create New App → From scratch.
- Name it and select your workspace.
- Go to OAuth & Permissions:
- Under Scopes, add:
    - chat:write
    - chat:write.public
- Copy the Bot User OAuth Token (xoxb-...) and set it in the helm values.yaml


## build custom runner with custom actions
cd /Users/bbalasub/Projects/sreips/30-Sep-2025
podman build -t quay.io/balki404/sreips-runner:0.0.2 . --platform=linux/amd64 --no-cache --ignorefile .podmanignore

