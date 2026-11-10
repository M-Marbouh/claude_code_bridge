# Cross-machine CCB rig ("machine B")

A disposable Docker host that runs its own sshd, tmux and CCB installation,
plus a stub `codex`/`claude` pane that records everything it receives. The
source side is this working tree, run under a temporary HOME.

    CCB_DOCKER_RIG=1 pytest test/test_docker_rig.py -q

Rows live in `test/test_docker_rig.py`; evidence is read from the destination
(journal records under `/home/b/.ccb/remote/journal/`, and the stub log at
`/srv/demo/.stub-agent.log`) and from the isolated source HOME's receipts, never
from sender output alone.

Reply rows (M2) script the stub through `/srv/demo/.stub-reply.json`
(`Rig.set_stub_config`); when a delivered prompt carries
`CCB_REMOTE_REPLY_COMMAND`, the stub runs that exact command from its own pane
environment. `Rig.start_source_side()` builds the isolated "machine A": its own
tmux server, a fake launch registry for the asking pane, and (for
`--background`) a real host-side `askd`.

Knobs:

- `CCB_RIG_SKIP_BUILD=1` reuse the existing `ccb-m1-rig:latest` image
- `CCB_RIG_KEEP=1` keep the container and temp dir after a failure

Files:

- `Dockerfile`, `sshd_config`, `entrypoint.sh` — the container (user `b`,
  key-only sshd, host keys generated at start)
- `stub_agent.py` — the recording pane agent (and, scripted, the replying
  agent), installed as `codex`/`claude`
- `protocol_probe.py` — raw framed-protocol driver (fixed ids, controlled
  hangs) for the duplicate-id and lost-acknowledgement rows
- `rig.py` — harness: build/start, install for `b` (this tree, or another git
  ref for the M1↔M2 row), authorize the restricted keys, launch the destination,
  read the journal/stub log, and build the isolated reply source side
