from robusta.api import *

@action
def my_action(event: PodEvent):
    # we have full access to the pod on which the alert fired
    pod = event.get_pod()
    pod_name = pod.metadata.name
    pod_logs = pod.get_logs()
    pod_processes = pod.exec("ps aux")

    print("Pod processes:\n" + pod_processes)

    # this is how you send data to slack or other destinations
    event.add_enrichment([
        MarkdownBlock("*Oh no!* An alert occurred on " + pod_name),
        FileBlock("crashing-pod.log", pod_logs)
    ])
