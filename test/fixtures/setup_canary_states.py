"""Sanitized production states from the 2026-11-17 cross-machine canary.

These are the *real* failure shapes the guided setup has to catch, with real
hostnames, addresses and keys removed. The passing control is the effective
answer a correctly hardened host gives for the intended connection.

`sshd -T -C user=...,host=...,addr=...,lport=...` is the authority: these
fixtures are what that command prints, not what the files on disk look like.
"""

#: (a) The on-disk shape that fooled a file-based check: the Include is
#: present, the included drop-in is empty, and sshd is still password-enabled.
EMPTY_DROPIN_CONFIG = """\
Include /etc/ssh/sshd_config.d/*.conf
# /etc/ssh/sshd_config.d/50-cloud-init.conf is empty
"""

#: (a) The EFFECTIVE answer for that machine. This must FAIL the check.
EMPTY_DROPIN_EFFECTIVE = """\
port 22
addressfamily any
listenaddress [::]:22
listenaddress 0.0.0.0:22
usepam yes
permitrootlogin prohibit-password
pubkeyauthentication yes
passwordauthentication yes
kbdinteractiveauthentication yes
challengeresponseauthentication no
permitemptypasswords no
x11forwarding yes
allowtcpforwarding yes
"""

#: (d) The passing control: key-only for the actual user/address/port.
HARDENED_EFFECTIVE = """\
port 22
addressfamily any
listenaddress [::]:22
listenaddress 0.0.0.0:22
usepam yes
permitrootlogin prohibit-password
pubkeyauthentication yes
passwordauthentication no
kbdinteractiveauthentication no
challengeresponseauthentication no
permitemptypasswords no
x11forwarding yes
allowtcpforwarding yes
"""

#: The same clean profile as expanded for a Match block keyed by LocalPort.
LPORT_MATCH_EFFECTIVE = """\
# sshd -T -C user=bob,host=laptop,addr=198.51.100.20,lport=2222
port 2222
pubkeyauthentication yes
passwordauthentication no
kbdinteractiveauthentication no
"""

#: A Match LocalAddress/LocalPort rule changed the effective answer: password
#: auth is allowed only for this local address/port combination.
LADDR_MATCH_EFFECTIVE = """\
# sshd -T -C user=bob,host=198.51.100.20,addr=198.51.100.20,laddr=192.0.2.5,lport=2222
port 2222
pubkeyauthentication yes
passwordauthentication yes
kbdinteractiveauthentication no
"""

#: Password auth is off but keyboard-interactive is still on: NOT enough.
KBD_INTERACTIVE_STILL_ON = """\
port 22
pubkeyauthentication yes
passwordauthentication no
kbdinteractiveauthentication yes
"""

#: (b) ufw active, default-deny incoming, and no rule for the SSH port.
UFW_ACTIVE_DENY_SSH = """\
Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
"""

#: The control for (b): the same firewall with the SSH port allowed.
UFW_ACTIVE_ALLOW_SSH = """\
Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW IN    Anywhere
22/tcp (v6)                ALLOW IN    Anywhere (v6)
"""

#: A status word the parser does not understand must fail closed.
UFW_STATUS_UNKNOWN = """\
Status: unknown
Default: deny (incoming), allow (outgoing), disabled (routed)
"""

#: "inactive" must not be read as active by a substring match.
UFW_STATUS_INACTIVE = """\
Status: inactive
Default: deny (incoming), allow (outgoing), disabled (routed)
"""

#: firewalld is outside the tested remediation set.
FIREWALLD_LIST_ALL_NO_SSH = """\
public (active)
  target: default
  icmp-block-inversion: no
  interfaces: eth0
  services: dhcpv6-client
  ports:
  protocols:
  masquerade: no
  forward-ports:
"""

#: (c) The receiving account cannot start the gate through this shell.
NOLOGIN_SHELL = "/usr/sbin/nologin"
REAL_SHELL = "/bin/bash"

#: The receiver's local loopback probe, as the canary observed it: sshd was up
#: with a real login shell; the failures were elsewhere.
LOOPBACK_SSHD_UP = True
LOOPBACK_NOTHING_LISTENING = False

#: (b) What the sending machine sees when the receiver's firewall drops it.
BLOCKED_REACHABILITY_DETAIL = (
    "ssh: connect to host 198.51.100.10 port 22: Connection timed out"
)
