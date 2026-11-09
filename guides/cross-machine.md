# Cross-machine messaging (preview)

CCB can deliver a one-way message from a Claude or Codex pane on one machine to a
Claude or Codex pane in a CCB project on another machine, over SSH.

It is **preview** software, and deliberately narrow:

- **One-way.** The destination agent receives your text and acts on it; it does
  **not** send a reply back through CCB yet. If you want an answer, the receiving
  agent has to reach you some other way.
- **Delivery only to Claude and Codex.** No other provider is advertised.
- **No `ccb remote setup` yet.** You set it up by hand, once per direction, with
  the steps below.
- **Linux only.**
- **Each direction is a separate opt-in.** Enabling laptop → server does not let
  the server send anything to the laptop.
- **Role-enforcing destinations accept remote messages for `lead` only.** A
  remote sender has no CCB credential on the destination host, so it is treated
  as external.

Nothing here opens a port or starts a listener: your existing SSH server is the
transport, and one fixed command runs on the destination.

---

## Security first — read this before you start

- **A delivered message makes that agent act as the destination user**, with the
  files, tools and credentials that user already has. The project allow-list
  below limits *where* a message may go. It does not limit *what* the receiving
  agent can do, and it is not a sandbox.
- **Use a dedicated key per direction**, held only for this purpose. Do not reuse
  your login key or an agent-forwarded identity.
- **Restrict the key line** with `restrict,command="…"` (below). That is what
  makes the key unable to run anything except the CCB gate: no shell, no PTY, no
  port forwarding, no SFTP.
- **A relayed message is a request, never an approval.** Whatever the text says
  about permissions, the destination's own rules still apply.
- **Pin the destination host key and check the fingerprint** before your first
  message (Step 5). Without that check, SSH would happily talk to an impostor.

---

## 1. Prerequisites

- CCB installed on **both** machines with a release that ships the two cross-machine
  commands. Both machines must have `ccb-remote-gate` and `ccb-remote`:
  `command -v ccb-remote ccb-remote-gate` should print two paths on each.
- **SSH from A to B works** (you can `ssh` to the destination host).
- **The destination account has a real login shell**, not `nologin`/`false`.
  sshd starts the forced command through that shell, so a `nologin` shell stops
  the gate from ever running. Check on B: `getent passwd bob | cut -d: -f7`
  should print something like `/bin/bash`.
- **The destination account must be allowed to log in with a key.** Two things
  can stop that before your key is even considered: the account is *locked*, or
  sshd's `AllowUsers`/`AllowGroups` excludes it. **Do not pre-emptively change
  account passwords** — run Step 8 first and read the failure instead.

  Status without exposing hashes:

  ```bash
  # on B
  sudo passwd -S bob          # P usable password, L locked/no password, NP none
  sudo chage -l bob           # expiry fields only
  sudo sshd -T | grep -i -E 'allowusers|allowgroups|denyusers|usepam'
  ```

  - If B's auth log says `User bob not allowed because account is locked`, and
    this account is meant to be **key-only** (nobody logs in with its password),
    you may unlock it for key login:

    ```bash
    # on B — replaces bob's password hash with a non-matching "*"; anyone using
    # that account's PASSWORD will lose it. Do not do this for a shared account.
    sudo usermod -p '*' bob
    ```

    Only do this if that fits: `passwd -S` also reports `L` for an already
    key-only account, and whether sshd refuses a locked account at all depends
    on its `UsePAM` setting. The auth log is the authority; the connection
    attempt is the test.
  - If it says `not listed in AllowUsers`, add the account **without removing
    the existing list** and validate before reloading:

    ```bash
    # on B
    sudo cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak
    sudo sed -i '/^AllowUsers /s/$/ bob/' /etc/ssh/sshd_config   # append, keep the rest
    sudo sshd -t && sudo systemctl reload ssh                    # service may be "sshd" on your distro
    ```

    `sshd -t` must pass before you reload; if it fails, restore the backup. (A
    directive inside a `Match` block needs editing by hand instead.)

Throughout, these names are examples — substitute your own:

| Name | Meaning | Example |
| --- | --- | --- |
| `laptop` / `server` | the two machines | A sends, B receives |
| `HOST` | the hostname or IP A uses to reach B | `server.example.com`, `192.0.2.10` |
| `PORT` | B's SSH port | `22` |
| `alice` / `DEST_USER` | the users on each machine | `alice` on A, `bob` on B |
| `server` | the **CCB host alias** you type (`--peer server::demo`) | `[a-z0-9][a-z0-9-]{0,31}` |
| `demo` | the CCB project on B | the directory B runs CCB in |
| `laptop-a` | the **principal** name you give this key on B | lowercase, `-`/`_` allowed |
| `alice1` | the **issuer token** (a shared, non-secret label) | lowercase letters/digits |

`HOST` must be a **resolvable hostname or IP address**, with `PORT` written
explicitly: CCB runs `ssh -F /dev/null`, so it never reads your `~/.ssh/config`
and an alias defined there will **not** resolve. The CCB host alias (`server`
above) is only a name inside CCB's own `hosts.json`.

Set these once in the shell you use on A (the later commands use them):

```bash
# on A
HOST=server.example.com     # resolvable name or IP of B
PORT=22
DEST_USER=bob               # the account on B
CCB_HOST=server             # the alias you type: --peer server::demo
PROJECT=demo
PRINCIPAL=laptop-a
TOKEN=alice1
```

---

## 2. On the laptop (A): make a dedicated key

```bash
mkdir -p ~/.ccb/remote/keys
ssh-keygen -t ed25519 -f ~/.ccb/remote/keys/server -N ''
```

That creates `~/.ccb/remote/keys/server` (private, keep it) and
`~/.ccb/remote/keys/server.pub` (the line you install on B). Give the private key
no passphrase so an unattended `ask` can use it; the key is restricted to one
command on B, which is what makes that acceptable.

If you build the key somewhere else, the paths you later put in
`hosts.json` must be **absolute and already exist**. A leading `~` is expanded
for you (`~/.ccb/remote/keys/server` works); other shell syntax
(`$HOME`, `~bob`) is not, so write the full path for those.

---

## 3. On the server (B): install the restricted key line

Find the gate's absolute path **on B** and create the `authorized_keys` line:

```bash
GATE="$(readlink -f "$(command -v ccb-remote-gate)")"
echo "$GATE"                       # e.g. /home/bob/.local/share/codex-dual/bin/ccb-remote-gate
```

The **principal name** is chosen by you, here `laptop-a`. It must match
`[a-z0-9_-]{1,64}`: lowercase letters, digits, `-` and `_`.

Paste the laptop's public key into that line. Run this **on B**, from the laptop's
public key (copy it over, or paste the text when asked):

```bash
GATE="$(readlink -f "$(command -v ccb-remote-gate)")"
PRINCIPAL=laptop-a
PUBKEY='ssh-ed25519 AAAA...alice@laptop'
mkdir -p ~/.ssh && chmod 700 ~/.ssh
printf 'restrict,command="%s --principal %s" %s\n' "$GATE" "$PRINCIPAL" "$PUBKEY" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
tail -1 ~/.ssh/authorized_keys
```

The line must look exactly like this (one line):

```
restrict,command="/home/bob/.local/share/codex-dual/bin/ccb-remote-gate --principal laptop-a" ssh-ed25519 AAAA... alice@laptop
```

`restrict` turns off PTY, agent/X11 forwarding, port forwarding and `~/.ssh/rc`
for this key; `command=` fixes what the key can run. Nothing else is exposed.

## 4. On the server (B): the destination config

B needs to know which principal may reach which project. Create
`~/.ccb/remote/config.json`:

```bash
mkdir -p ~/.ccb/remote && chmod 700 ~/.ccb/remote
cat > ~/.ccb/remote/config.json <<'JSON'
{
  "principals": {
    "laptop-a": { "issuer_token": "alice1", "allowed_aliases": ["demo"] }
  },
  "aliases": {
    "demo": { "work_dir": "/home/bob/demo", "providers": ["codex", "claude"] }
  }
}
JSON
chmod 600 ~/.ccb/remote/config.json
```

- `principals.<name>.issuer_token` — a label, not a secret. It must match
  `[a-z0-9]{4,32}`, and the **same value** goes in A's `hosts.json`. It namespaces
  request ids: `<id>-h<token>`.
- `principals.<name>.allowed_aliases` — the **only** project aliases this
  principal may address.
- `aliases.<alias>.work_dir` — the absolute directory B runs CCB in for that project.
- `aliases.<alias>.providers` — which providers a remote message may target.
  Only `claude` and `codex` are honoured.
- `issuer_token` must be present for every principal and `work_dir` for every
  alias. **Unknown fields here are ignored**, not refused — a typo'd principal
  or alias name simply never matches anything, so it refuses instead of
  erroring. A missing file means *deny everything*; a malformed one is reported
  as a config error and everything is refused until you fix it.

To give another machine access later, add a second principal and its own key
line — never share the first key.

## 5. On the laptop (A): pin the server's host key, with verification

Do **not** let SSH trust on first use: `ssh-keyscan` is unauthenticated, so the
only thing that makes the pinned key trustworthy is comparing its fingerprint
out-of-band.

Step 1 — read the fingerprint **on B**, from a path you trust (a console, or an
SSH connection you have already verified another way — never over the connection
you are about to pin):

```bash
# on B
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
# 256 SHA256:AbCd... root@server (ED25519)
```

Step 2 — fetch it on A into a **private temporary directory**, and compare:

```bash
# on A
PIN="$(mktemp -d)" && chmod 700 "$PIN"
ssh-keyscan -p "$PORT" -t ed25519 "$HOST" > "$PIN/hostkey" 2>/dev/null
ssh-keygen -lf "$PIN/hostkey"
```

The two fingerprints must be identical, character for character. If they differ,
stop: something is intercepting the connection.

Step 3 — only then install the pin and clean up (`ssh-keyscan -p` already prints
the `[host]:port` form a non-default port needs):

```bash
# on A
mkdir -p ~/.ccb/remote && chmod 700 ~/.ccb/remote
cp "$PIN/hostkey" ~/.ccb/remote/known_hosts
chmod 600 ~/.ccb/remote/known_hosts
cat ~/.ccb/remote/known_hosts       # server.example.com ssh-ed25519 AAAA...  ([host]:port on a non-22 port)
rm -rf "$PIN"
```

This file is the only host-key source that cross-machine delivery uses; the
user's `~/.ssh/config` and global known-hosts files are deliberately ignored.

## 6. On the laptop (A): the host config

`~/.ccb/remote/hosts.json` names the host alias you will type, and the key,
token and pinned host key to use for it:

```bash
mkdir -p ~/.ccb/remote && chmod 700 ~/.ccb/remote
cat > ~/.ccb/remote/hosts.json <<JSON
{
  "hosts": {
    "$CCB_HOST": {
      "ssh_target": "$DEST_USER@$HOST",
      "port": $PORT,
      "identity_file": "~/.ccb/remote/keys/server",
      "known_hosts": "~/.ccb/remote/known_hosts",
      "issuer_token": "$TOKEN"
    }
  }
}
JSON
chmod 600 ~/.ccb/remote/hosts.json
cat ~/.ccb/remote/hosts.json
```

(`$HOST` must be the resolvable name or IP — not an `~/.ssh/config` alias — and
`port` must be explicit, because CCB runs `ssh -F /dev/null`.)

Rules (the loader is strict on purpose):

- the alias must match `[a-z0-9][a-z0-9-]{0,31}`;
- `ssh_target` is `[user@]host` and may not start with `-`;
- `port` is 1–65535;
- `identity_file` and `known_hosts` must be **absolute paths that exist**. A
  leading `~` (your home) or `~user` is expanded as the shell would expand it —
  so `~you/...` is the safe form; the expanded path must still exist;
- `issuer_token` must equal the one you gave this principal on B;
- unknown fields in a host entry **are** refused here (unlike B's config).

## 7. On the server (B): have CCB running in the project

Cross-machine delivery hands the message to B's own `askd` and lets B inject it
into the live pane. So B must already be running CCB for `demo`, with the target
provider live:

```bash
# on B
cd /home/bob/demo
ccb codex        # or: ccb claude
```

Leave that session running. The gate answers `daemon_unavailable` when B has no
reachable CCB daemon, and `destination_unavailable` when the project has no live
session for that provider.

## 8. Check the connection

```bash
# on A
ccb-remote list server
```

Expected (one line per allowed alias, providers with presence):

```
demo	codex=present, claude=absent
```

If you see `no destinations authorized on this host`, the principal, token or
alias rules do not line up — compare Step 3/4 on B with Step 6 on A.

## 9. Send a message

```bash
# on A
ask codex --peer server::demo "hello from the laptop"
```

A remote delivery is always one-way, so its intent is `notify` whether or not
you pass `--notify` (`--notify` remains accepted and means the same thing here).
Flags that assume a reply — an explicit `--wait`, `--foreground` or
`--reply-to` — are refused for a cross-machine target.

Expected:

```
id=20261109-182536-922-2626995-halice1 state=accepted destination=accepted-pending
```

Use the id `ask` printed, exactly as printed.

The message is now durably accepted on B and queued for the exact live pane. On
B you will see it appear in that pane, wrapped with a source label:

```
CCB_REMOTE: this message was relayed from another machine by CCB.
...
CCB_REPLY_EXPECTED: no
CCB_REMOTE_REQUEST_ID: 20261109-182536-922-2626995-halice1
CCB_REMOTE_INTENT: notify
...
hello from the laptop
```

Then confirm the terminal state from A:

```bash
# on A — use the id ask printed
ccb-remote status <id-from-ask>
```

Expected:

```
id=20261109-182536-922-2626995-halice1 state=delivered destination=delivered
```

Right after a send the state can still be `accepted-pending` or
`dispatch-in-progress`; run the same `ccb-remote status` again a moment later.
`delivered` means the text reached the pane. It does not mean the agent read it,
answered it, or did what it asked.

---

## 10. What the results mean

**Sender states** (in `ask` output, and in `~/.ccb/remote/outbound/<id>.json`):

| State | Exit | Meaning |
| --- | --- | --- |
| `accepted` | 0 | The destination recorded the request durably. Delivery follows. |
| `not-delivered` | 1 | Nothing was accepted: SSH failed, the destination refused before accepting, or the receipt could not be written. Safe to fix and retry. |
| `acceptance-unknown` | 2 | The request was written but no trustworthy acknowledgement came back. It **may** be accepted. Do not resend. |

**Destination states** (shown by `ccb-remote status`, which prints the state
only — the *reason* for a `failed`/`outcome-unknown` request is in B's journal
record, see below):

| State | Meaning |
| --- | --- |
| `accepted-pending` | Queued; nothing has been sent to the pane yet. |
| `dispatch-in-progress` | The durable intent to inject was recorded immediately before the injection; this is not yet proof that injection started. |
| `delivered` | Injected into the exact pane (terminal for this preview). |
| `failed` | Refused before injection (destination gone, role policy, lock timeout). The reason is in B's journal record. |
| `outcome-unknown` | Something failed at or after the injection; it may or may not have been delivered. |

**Never resend an `acceptance-unknown` message.** Run
`ccb-remote status <id>` first; it asks the destination what actually happened
and updates the receipt. There is no automatic retry, by design: a resend could
deliver twice.

**Where the files are:**

- A: `~/.ccb/remote/hosts.json`, `~/.ccb/remote/keys/`, `~/.ccb/remote/known_hosts`,
  and one receipt per request in `~/.ccb/remote/outbound/<request-id>.json`.
  Receipts hold the id, target, digest, caller identity and state — never the message text.
- B: `~/.ccb/remote/config.json` and one journal record per request in
  `~/.ccb/remote/journal/<request-id>.json` (including the text, for the
  destination's own audit).

Both directories are private (`0700`, files `0600`).

**Reading a failure reason.** `ccb-remote status` reports the state, not why.
B's journal record holds the reason. On B:

```bash
# on B
ID=<id-from-ask>
python3 - "$ID" <<'READ'
import json, os, sys
path = os.path.expanduser(f"~/.ccb/remote/journal/{sys.argv[1]}.json")
record = json.load(open(path))
print(record["state"], "|", record["history"][-1].get("reason", "(no reason recorded)"))
READ
```

---

## 11. Troubleshooting

Every row below is a message the sender prints (`ask` / `ccb-remote`) or a state
you then see from `ccb-remote status`. Reasons for a destination `failed` /
`outcome-unknown` state are in **B's journal record** (see "Reading a failure
reason" in §10); `ccb-remote status` itself prints the state only.

| What you see | What it means | What to do |
| --- | --- | --- |
| `state=not-delivered (ssh exited 255: bob@server: Permission denied (publickey).)` | SSH refused the login. The sender shows ssh's own last lines, so this is the real cause. | Check, in order: the `authorized_keys` line (Step 3) and the key path in `hosts.json`; the shell (`getent passwd bob` → not `nologin`/`false`); the account's lock state (`sudo passwd -S bob`); and `AllowUsers`/`AllowGroups` (`sudo sshd -T \| grep -i allow`). B's auth log names the cause: `account is locked`, `not listed in AllowUsers`, or `Failed publickey`. Only for a key-only account, `sudo usermod -p '*' bob` clears the lock (it replaces the password hash — see Step 1). |
| `state=not-delivered (ssh exited 255: … Host key for [host]:port has changed … \| Host key verification failed.)` | The pinned host key does not match. | Re-verify out-of-band (Step 5) before touching `known_hosts`. Never "just" delete the line. |
| `state=not-delivered (ssh exited 255: ssh: connect to host … port …: Connection refused)` | SSH is not reachable. | No bytes were sent. Fix SSH or the port, retry. |
| `state=not-delivered (malformed response: frame_too_large \| response text: This account is currently not available.)` | The account's shell is `nologin`/`false`; its notice arrives on the protocol stream instead of the forced command. | Give B's account a real login shell (Step 1), e.g. `sudo usermod -s /bin/bash bob`. |
| `state=not-delivered (the destination closed the connection without a response)` (no `ssh exited` part) | ssh was still running when the connection dropped, so there is no exit status to quote. | Fall back to plain SSH to see the whole picture: `ssh -v -i ~/.ccb/remote/keys/server -o IdentitiesOnly=yes -o UserKnownHostsFile=~/.ccb/remote/known_hosts -o StrictHostKeyChecking=yes -p "$PORT" "$DEST_USER@$HOST"`. |
| `[WARN] CCB_CALLER not set; using 'manual'.` | You ran `ask` outside a CCB pane. | Harmless for delivery (the caller is recorded as an audit claim), or run it from a CCB pane / set `CCB_CALLER=claude` (or `codex`). |
| `state=not-delivered (the destination refused the request: unauthorized (…))` | The principal, token or alias did not line up. | Compare the principal name and `issuer_token` in Step 3/4 on B with Step 6 on A, and that the alias is in `allowed_aliases`. |
| `… unsupported_provider (provider is not allowed for that destination)` | The alias does not list that provider. | Add it to `aliases.<alias>.providers` on B. |
| `state=not-delivered (… daemon_unavailable …)` | B has no reachable CCB daemon. | Start CCB on B in the project (Step 7). |
| `state=not-delivered (… destination_unavailable (no live codex session for the destination project))` | The project has no live session for that provider. | Start that provider on B. |
| `… destination_unavailable (more than one live codex session for the destination project; select one with dest_session)` | Ambiguous target. | Pass the exact session id: `ask codex --peer "$CCB_HOST::$PROJECT" --live-id <live-id> "…"` (`ccb-list --json` on B shows it). |
| `state=not-delivered (… journal_failure …)` | B could not record the request. | Disk/permissions on B's `~/.ccb/remote`; nothing was accepted. |
| `state=acceptance-unknown (…)` | No trustworthy acknowledgement. | **Do not resend.** Run `ccb-remote status <id>`. |
| `state=unknown-or-not-authorized` from `ccb-remote status` | The destination has no such request for this principal. | Check the id; another principal's request looks exactly the same. |
| `ccb-remote status` → `state=failed`; B's journal reason `role policy refused: external senders may only send to a session holding the lead role…` | B enforces role policy and the target is not `lead`. | Give the target session the `lead` role on B (`ccb-role set lead` in that pane), or send to one that has it. |
| `ccb-remote status` → `state=failed`; B's journal reason `destination replaced` | B's pane changed between acceptance and injection. | Nothing was sent to the new pane. Send a new message if still needed. |
| `ccb-remote status` → `state=outcome-unknown` | Failure at or after injection; may or may not have arrived. | Read the reason in B's journal and check the pane. Do not resend blindly. |

## 12. Turning it off

Do this on B, in two steps, and understand what each one covers:

1. **Remove the key line** from the destination account's
   `~/.ssh/authorized_keys` — delete the one line that begins
   `restrict,command="… --principal laptop-a"`. This stops **new** connections
   from authenticating. It does **not** terminate a connection that is already
   authenticated: the gate on that connection keeps serving frames.
2. **Remove the principal** from B's `~/.ccb/remote/config.json` (delete its
   `principals.<name>` entry, and its alias from any ACLs). Every request is
   authorized against the config as loaded for that request, so this makes any
   further request — including on an already-open connection — fail with
   `unauthorized`.

The same two steps cover "revoke this machine" and "pause the feature".

What this does **not** do:

- **Already-accepted messages can still be delivered.** Requests that B has
  durably accepted (and any work already queued for a pane) continue; there is
  no cancel in this preview.
- Removing the config file entirely denies everything, including requests that
  were accepted earlier — their records remain in B's journal and are still
  visible to `ccb-remote status`.

On A, delete `~/.ccb/remote/hosts.json`, the private key under
`~/.ccb/remote/keys/` and `known_hosts` if you will not use this direction again.
Old receipts in `~/.ccb/remote/outbound/` can be deleted at any time.

## 13. Limitations and roadmap

Not yet, by design of this preview:

- **No replies.** A destination answer does not travel back through CCB.
- **No setup command.** Manual steps (this guide) only.
- **No retention or cleanup policy.** Journal records and receipts stay until you
  remove them; a request is never replayed automatically.
- **No cancellation.** Once delivered, the receiving agent may already have acted.
- **Linux only.** macOS and Windows are untested for this path.
- **Not a sandbox.** The allow-list limits destinations, not the agent's powers.
