# "Machine B" for the cross-machine acceptance rig: a disposable SSH host that
# runs its own CCB installation and its own askd. Generic names only.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      openssh-server tmux python3 rsync procps ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# The destination account. A LOGIN shell (not nologin): sshd runs the forced
# command through it, so a nologin shell would stop the gate from starting.
# `useradd` leaves the account locked ("!" in shadow), and sshd refuses a locked
# account even for public-key auth. '*' is a non-matching hash, so the account
# is usable while no password can ever authenticate (PasswordAuthentication no).
RUN useradd -m -s /bin/bash b \
 && usermod -p '*' b \
 && mkdir -p /run/sshd /home/b/.ssh \
 && chown b:b /home/b/.ssh \
 && chmod 700 /home/b/.ssh

# Stub provider agents: real panes, real tmux, recorded input. `codex` and
# `claude` are the only providers M1 advertises for remote delivery.
COPY stub_agent.py /usr/local/bin/stub-agent
RUN chmod 0755 /usr/local/bin/stub-agent \
 && ln -sf stub-agent /usr/local/bin/codex \
 && ln -sf stub-agent /usr/local/bin/claude \
 && mkdir -p /srv/demo && chown b:b /srv/demo

COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /usr/local/bin/rig-entrypoint
RUN chmod 0755 /usr/local/bin/rig-entrypoint

EXPOSE 22
ENTRYPOINT ["/usr/local/bin/rig-entrypoint"]
