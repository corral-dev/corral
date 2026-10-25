// Execute the shipped TypeScript asset with only Pi's host API substituted.
import { readFileSync, writeFileSync } from "node:fs";
import { syncBuiltinESMExports } from "node:module";
import childProcess from "node:child_process";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const spec = JSON.parse(readFileSync(process.argv[3], "utf8"));
process.env.PI_CODING_AGENT_DIR = spec.agentDir;
process.env.CORRAL_PI_INSTANCE_ID = "contender";
delete process.env.CORRAL_PI_CLAIM_PATH;
process.env.TMUX = spec.tmux ?? "";
process.env.TMUX_TMPDIR = spec.agentDir;

// A regression fixture must never query the developer's real tmux sessions.
const realExec = childProcess.execFileSync;
childProcess.execFileSync = (command, args, options) => {
	if (command === "ps") {
		// Ownership checks read a process start time; the fixture answers it
		// directly instead of inspecting this machine's real processes.
		if (args.includes("lstart=")) {
			const pid = args[args.indexOf("-p") + 1];
			const started = spec.processStarts?.[pid];
			if (started === undefined) throw new Error(`ps: no such process: ${pid}`);
			return `${started}\n`;
		}
		return spec.parents;
	}
  if (command !== "tmux") return realExec(command, args, options);
  const socket = args[args.indexOf("-S") + 1];
  if (spec.slowTmux) throw new Error("fixture lookup timed out");
  return spec.panes?.[socket] ?? "";
};
syncBuiltinESMExports();

let source = readFileSync(process.argv[2], "utf8");
source = source.replace(
  /import \{[\s\S]*?\} from "@earendil-works\/pi-coding-agent";/,
  'const getAgentDir = () => process.env.PI_CODING_AGENT_DIR;',
);
const extensionPath = join(spec.agentDir, "fixture-extension.mts");
writeFileSync(extensionPath, source);
const module = await import(pathToFileURL(extensionPath).href);
const handlers = new Map();
module.default({ on: (name, handler) => handlers.set(name, handler) });
const notifications = [];
let shutdown = false;
const ctx = {
  mode: "tui", cwd: spec.agentDir,
  sessionManager: {
    getHeader: () => ({}), getCwd: () => spec.agentDir,
    getSessionId: () => spec.sessionId,
    getSessionFile: () => spec.sessionFile,
  },
  ui: { notify: (message, level) => notifications.push({ message, level }) },
  shutdown: () => { shutdown = true; },
};
await handlers.get("session_start")({ reason: "startup" }, ctx);
let result;
if (spec.target) {
  result = await handlers.get("session_before_switch")({ reason: "resume", targetSessionFile: spec.target }, ctx);
}
const input = await handlers.get("input")({}, ctx);
console.log(JSON.stringify({ result, notifications, shutdown, input, pid: process.pid }));
