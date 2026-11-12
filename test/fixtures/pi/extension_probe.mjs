// Drives CCB's Pi state extension (lib/pi_extension/ccb-state.ts) through PI'S OWN LOADER.
//
// Usage: node extension_probe.mjs <scenario.json> <extension.ts> <pi-dist-dir>
//
// <pi-dist-dir> is the installed `dist/` of @earendil-works/pi-coding-agent. Loading goes through
// `loadExtensionsCached` and events through `ExtensionRunner`, so the probe exercises the real
// cache-by-cwd behaviour (a runtime with another cwd re-evaluates the module) and the real
// `ctx.mode` / `ctx.hasUI` bindings that decide who may publish.
//
// The scenario is {"steps": [...]}. Each step is one of:
//   {"op": "runtime", "cwd": "/w", "mode": "tui"|"print"|"rpc", "reload": false, "editor": "",
//    "idle": true, "session": "s-1", "file": "/sessions/s-1.jsonl"}
//     - "reload": true calls Pi's clearExtensionCache() first, as a reload does.
//   {"event": "session_start", "runtime": 0, "reason": "startup", "session": ..., "file": ...,
//    "previous": ..., "editor": ..., "idle": true, "mode": ..., "hasUI": ...}
//     - "mode"/"hasUI" rebind the runtime's UI context before the event, as bindExtensions does.
// and the probe appends {"op": <event>, "state": <snapshot or null>} to its JSON output.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [scenarioPath, modulePath, piDist] = process.argv.slice(2);
if (!piDist) throw new Error("usage: extension_probe.mjs <scenario.json> <extension.ts> <pi-dist-dir>");

const distUrl = pathToFileURL(piDist.endsWith("/") ? piDist : `${piDist}/`).href;
const { clearExtensionCache, createExtensionRuntime, loadExtensionsCached } = await import(
  `${distUrl}core/extensions/loader.js`
);
const { createEventBus } = await import(`${distUrl}core/event-bus.js`);
const { ExtensionRunner } = await import(`${distUrl}core/extensions/runner.js`);

const scenario = JSON.parse(readFileSync(scenarioPath, "utf8"));
const statePath = process.env.CCB_PI_STATE_FILE;
const processRuntime = createExtensionRuntime();
const eventBus = createEventBus();

function readState() {
  if (!statePath) return null;
  try {
    return JSON.parse(readFileSync(statePath, "utf8"));
  } catch {
    return null;
  }
}

function sessionManager(record) {
  return {
    getSessionId: () => record.state.session,
    getSessionFile: () => record.state.file,
  };
}

/** A minimal TUI context: only getEditorText is read; the runner wraps the prompt methods itself. */
function uiContext(record) {
  return {
    getEditorText: () => record.event.editor,
    select: async () => undefined,
    confirm: async () => false,
    input: async () => undefined,
    editor: async () => undefined,
    custom: async () => undefined,
  };
}

const runtimes = [];
const results = [];
for (const step of scenario.steps) {
  if (step.op === "runtime") {
    if (step.reload) clearExtensionCache();
    const cwd = step.cwd ?? process.cwd();
    const loaded = await loadExtensionsCached([modulePath], cwd, eventBus, processRuntime);
    if (loaded.errors.length > 0) throw new Error(`extension failed to load: ${JSON.stringify(loaded.errors)}`);
    // `state` is what Pi's runner holds across events (session identity, bound mode); `event` is
    // the live view Pi reports for the event being emitted (editor text, idle).
    const record = {
      runner: null,
      state: { session: step.session ?? "s-1", file: step.file ?? "/sessions/s-1.jsonl" },
      event: { editor: step.editor ?? "", idle: true },
    };
    const runner = new ExtensionRunner(loaded.extensions, loaded.runtime, cwd, sessionManager(record), undefined);
    record.runner = runner;
    const mode = step.mode ?? "tui";
    runner.setUIContext(mode === "tui" ? uiContext(record) : undefined, mode);
    runtimes.push(record);
    continue;
  }
  const record = runtimes[step.runtime ?? 0];
  if (!record) throw new Error("scenario used a runtime that was never created");
  if (step.session !== undefined) record.state.session = step.session;
  if (step.file !== undefined) record.state.file = step.file;
  record.event = { editor: step.editor ?? "", idle: step.idle ?? true };
  if (step.mode !== undefined || step.hasUI !== undefined) {
    const mode = step.mode ?? record.runner.mode;
    const withUI = step.hasUI ?? mode === "tui";
    record.runner.setUIContext(withUI ? uiContext(record) : undefined, mode);
  }
  record.runner.isIdleFn = () => record.event.idle;
  await record.runner.emit({
    type: step.event,
    reason: step.reason,
    previousSessionFile: step.previous,
  });
  results.push({ op: step.event, state: readState() });
}
process.stdout.write(JSON.stringify(results));
// The heartbeat interval would otherwise keep this process alive.
process.exit(0);
