############## obsolete --- deploy sreips-agent ##############
oc new-app https://github.com/bbalakriz/sreips-agent.git --strategy=source --name=agent -n sreips
#  oc delete is/agent bc/agent deploy/agent svc/agent
oc set env deployment/agent SLACK_WEBHOOK_URL=