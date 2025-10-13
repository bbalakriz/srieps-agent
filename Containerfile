FROM robustadev/robusta-runner:0.28.1

COPY custom_playbooks/ /etc/robusta/playbooks/custom_playbooks
RUN python3 -m pip install --no-cache-dir /etc/robusta/playbooks/custom_playbooks
