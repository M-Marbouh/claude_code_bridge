/**
 * CCB's Pi state publisher.
 *
 * Pi readiness ("is it safe to type a prompt into this pane?") comes from Pi itself through this
 * extension, not from reading the terminal screen: the screen cannot tell a draft from an idle
 * editor, and every layout change in Pi (extension widgets, statuses, selectors) has to be
 * re-learned. Pi knows its own state, so this extension publishes it as one small JSON snapshot
 * that CCB reads.
 *
 * CCB launches Pi with `--extension <this file>` plus two environment variables:
 *   CCB_PI_STATE_FILE     absolute path of the snapshot CCB reads
 *   CCB_PI_LAUNCH_TOKEN   random per launch; CCB only trusts a snapshot carrying its own token
 * Without both variables the extension stays completely inert, so loading it by hand is harmless.
 *
 * The snapshot is replaced atomically (temporary file in the same directory, then rename) and is
 * mode 0600. It is rewritten on every lifecycle event below and on a 500 ms timer, because editor
 * typing produces no event. Every write and every handler swallows its own errors: nothing here
 * may ever throw into Pi or slow its lifecycle down.
 *
 * Scope: this reports readiness only. CCB still tracks completion and replies from Pi's own session
 * transcript. Only the runtime instance that claimed this process's publisher writes; child and
 * retired instances never write.
 */
import { chmodSync, mkdirSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const SNAPSHOT_VERSION = 1;
const HEARTBEAT_MS = 500;

type Snapshot = {
  v: number;
  launch_token: string;
  runtime_instance_id: string;
  pid: number;
  mode: string;
  has_ui: boolean;
  idle: boolean;
  editor_empty: boolean;
  prompt_open: boolean;
  seq: number;
  updated_at: number;
};

type Publisher = {
  runtimeInstanceId: string;
  active: boolean;
  retired: boolean;
  seq: number;
  promptDepth: number;
  ctx: ExtensionContext | null;
  /** Clears the heartbeat interval; null when no timer is running. */
  timer: (() => void) | null;
  /** Hand the pane over to a newer runtime: stop publishing, never write again. */
  retire: () => void;
};

/**
 * The publisher that owns this process's snapshot.
 *
 * Pi re-evaluates an extension module for a runtime with another cwd (its extension cache is keyed
 * by cwd), so this claim cannot live in module scope: a child would get its own and overwrite the
 * pane's report. It sits on `globalThis` instead, shared by every module instance in the process.
 */
const OWNER_SLOT = Symbol.for("ccb.pi-state.owner");
type OwnerSlot = { current: Publisher | null };
// SAFETY: `globalThis` holds this slot under a symbol key this file names once; nothing else in the
// process writes that key, so the value at it is always the slot this module created.
const globalScope = globalThis as unknown as { [key: symbol]: OwnerSlot | undefined };
const ownerSlot: OwnerSlot = (globalScope[OWNER_SLOT] ??= { current: null });

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** A string field of an event payload; "" for anything else. */
function field(event: unknown, name: string): string {
  if (typeof event !== "object" || event === null) return "";
  return text(Reflect.get(event, name));
}

export default function ccbState(pi: ExtensionAPI): void {
  const stateFile = text(process.env.CCB_PI_STATE_FILE).trim();
  const launchToken = text(process.env.CCB_PI_LAUNCH_TOKEN).trim();
  if (!stateFile || !launchToken) {
    // Not launched by CCB (or launched without its state channel): publish nothing at all.
    return;
  }

  const publisher: Publisher = {
    runtimeInstanceId: `${process.pid}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`,
    active: false,
    retired: false,
    seq: 0,
    promptDepth: 0,
    ctx: null,
    timer: null,
    retire: () => {
      publisher.retired = true;
      publisher.active = false;
      publisher.promptDepth = 0;
      publisher.ctx = null;
      clearTimer();
    },
  };

  const clearTimer = (): void => {
    const stop = publisher.timer;
    publisher.timer = null;
    if (stop === null) return;
    try {
      stop();
    } catch {
      /* a timer that cannot be cleared is already gone */
    }
  };

  /**
   * Pause publishing WITHOUT giving up the claim.
   *
   * Releasing it here is what let a rejected child take the pane after the main paused: ownership
   * only ever moves to a newer TUI runtime, in `claim`.
   */
  const stopPublishing = (): void => {
    clearTimer();
    publisher.active = false;
    publisher.promptDepth = 0;
  };

  /**
   * Decide whether this instance may publish.
   *
   * Only Pi's interactive TUI runtime may: it is the one bound to the pane's terminal, so it is
   * the only runtime whose `mode` is "tui" with a UI. In-process children (subagents) run headless
   * in print mode, whatever their cwd, so they never claim. A newer TUI instance claiming - a
   * reload, or the runtime Pi starts after /new, /resume or /fork - takes ownership and retires the
   * previous publisher. Which conversation Pi is in stays irrelevant: the launch token already
   * proves this is the Pi CCB started.
   */
  const claim = (ctx: ExtensionContext): boolean => {
    if (publisher.retired) return false;
    if (text(ctx.mode) !== "tui" || ctx.hasUI !== true) return false;
    const previous = ownerSlot.current;
    if (previous !== null && previous !== publisher) previous.retire();
    ownerSlot.current = publisher;
    publisher.active = true;
    publisher.ctx = ctx;
    return true;
  };

  const publish = (): void => {
    if (!publisher.active) return;
    const ctx = publisher.ctx;
    if (!ctx) return;
    let mode = "";
    let hasUI = false;
    let idle = false;
    let editorEmpty = false;
    try {
      mode = text(ctx.mode);
      hasUI = Boolean(ctx.hasUI);
      idle = Boolean(ctx.isIdle());
      // getEditorText() is the only API for "is a draft sitting in the editor". Treat whitespace
      // as empty: a spaces-only editor cannot be concatenated into anything meaningful.
      editorEmpty = hasUI && text(ctx.ui.getEditorText()).trim().length === 0;
    } catch {
      // Any unreadable context is reported as "not ready" rather than guessed.
      return;
    }
    const snapshot: Snapshot = {
      v: SNAPSHOT_VERSION,
      launch_token: launchToken,
      runtime_instance_id: publisher.runtimeInstanceId,
      pid: process.pid,
      mode,
      has_ui: hasUI,
      idle,
      editor_empty: editorEmpty,
      prompt_open: publisher.promptDepth > 0,
      seq: ++publisher.seq,
      updated_at: Date.now(),
    };
    writeSnapshot(stateFile, snapshot);
  };

  const startTimer = (): void => {
    clearTimer();
    const interval = setInterval(publish, HEARTBEAT_MS);
    publisher.timer = () => clearInterval(interval);
  };

  const guarded = (handler: () => void): void => {
    try {
      handler();
    } catch {
      /* never throw into Pi */
    }
  };

  // session_start / session_shutdown: claim, refresh, and release the published state.
  pi.on("session_start", async (_event, ctx) => {
    guarded(() => {
      if (!claim(ctx)) return;
      startTimer();
      publish();
    });
  });

  pi.on("session_shutdown", async (event) => {
    guarded(() => {
      // "quit" ends this runtime for good. A reload/replacement hands this instance a new
      // session_start instead, and it re-claims there; a non-quit shutdown only pauses the timer,
      // so the pane is never handed to a headless runtime in the meantime.
      if (field(event, "reason") === "quit") {
        publisher.retire();
        return;
      }
      publisher.ctx = null;
      stopPublishing();
    });
  });

  // Agent lifecycle: busy starts, busy may continue after agent_end, settled is final.
  pi.on("agent_start", async (_event, ctx) => {
    guarded(() => {
      if (!publisher.active) return;
      publisher.ctx = ctx;
      publish();
    });
  });

  pi.on("agent_end", async (_event, ctx) => {
    guarded(() => {
      if (!publisher.active) return;
      publisher.ctx = ctx;
      publish();
    });
  });

  pi.on("agent_settled", async (_event, ctx) => {
    guarded(() => {
      if (!publisher.active) return;
      publisher.ctx = ctx;
      publish();
    });
  });

  // Extension prompts: Pi is blocked on a person, not merely busy.
  pi.on("ui_prompt_start", async (_event, ctx) => {
    guarded(() => {
      if (!publisher.active) return;
      publisher.ctx = ctx;
      publisher.promptDepth += 1;
      publish();
    });
  });

  pi.on("ui_prompt_end", async (_event, ctx) => {
    guarded(() => {
      if (!publisher.active) return;
      publisher.ctx = ctx;
      publisher.promptDepth = Math.max(0, publisher.promptDepth - 1);
      publish();
    });
  });

  // Input: the editor was just emptied (or a steer landed), so the previous snapshot is stale.
  pi.on("input", async (_event, ctx) => {
    guarded(() => {
      if (!publisher.active) return;
      publisher.ctx = ctx;
      publish();
    });
  });
}

function writeSnapshot(stateFile: string, snapshot: Snapshot): void {
  const temporary = `${stateFile}.${process.pid}.tmp`;
  try {
    mkdirSync(dirname(stateFile), { recursive: true, mode: 0o700 });
    writeFileSync(temporary, JSON.stringify(snapshot) + "\n", { encoding: "utf8", mode: 0o600 });
    try {
      chmodSync(temporary, 0o600);
    } catch {
      /* the file already carries mode 0600 through writeFileSync's mode */
    }
    renameSync(temporary, stateFile);
  } catch {
    /* A state file CCB cannot read only makes CCB refuse; never disturb Pi for it. */
  }
}
