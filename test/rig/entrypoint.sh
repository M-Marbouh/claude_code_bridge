#!/bin/bash
# Generate (once) and publish the host keys, then run sshd in the foreground.
set -euo pipefail
mkdir -p /run/sshd
if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
  ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/ssh_host_ed25519_key
  ssh-keygen -q -t rsa -b 3072 -N '' -f /etc/ssh/ssh_host_rsa_key
fi
exec /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config
