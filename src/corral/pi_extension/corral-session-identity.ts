/**
 * Corral 会话身份桥（corral-session-identity）
 *
 * 职责：把「当前 Pi TUI 进程正在展示哪条会话」写成本机 claim 文件，供
 * Corral 精确绑定分屏；并上报本轮 agent 相位（working / waiting / idle），
 * 供侧栏绿点/黄点在历史尚未落盘时跟上 TUI 的 Working 状态。
 * 不注册模型/工具/命令，不读取对话正文，不联网。只在 `ctx.mode === "tui"`
 * 时写入——SDK/RPC/JSON/print 与 subagent 都不参与。
 *
 * Claim 协议 v1 字段见 Corral 设计文档
 * docs/design/PI_SESSION_IDENTITY_EXTENSION_DESIGN.md。
 */
import {
	getAgentDir,
	type ExtensionAPI,
	type ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import {
	closeSync,
	fsyncSync,
	mkdirSync,
	openSync,
	readFileSync,
	readdirSync,
	realpathSync,
	renameSync,
	unlinkSync,
	writeFileSync,
} from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";

const PROTOCOL_VERSION = 1;
const EXTENSION_VERSION = "1.2.0";
const IDENTITY_DIRNAME = "corral-session-identity";
/** 心跳间隔；Corral 读取侧按 4 个周期（60s）判定 claim 过期。 */
const HEARTBEAT_MS = 15_000;

type AgentPhase = "idle" | "working" | "waiting";

interface ClaimIdentity {
	sessionId: string;
	sessionFile: string | null;
	cwd: string;
	parentSession: string | null;
}

export default function (pi: ExtensionAPI): void {
	// 扩展 reload（/new、/resume、/fork、/reload 后 Pi 会重载扩展实例）时
	// globalThis 存续：nonce 保持不变，配合 env 里的 instanceId 让 Corral 仍认得
	// 这是同一个进程。
	const globalCache = globalThis as {
		__corralPiIdentity?: { nonce: string; nativeInstanceId: string };
	};
	const processIdentity = (globalCache.__corralPiIdentity ??= {
		nonce: randomUUID(),
		nativeInstanceId: `native-${randomUUID()}`,
	});
	const nonce = processIdentity.nonce;
	const processStartedAt = new Date(Date.now() - process.uptime() * 1000).toISOString();

	// 托管 Pi 的 instance 来自 Corral env；裸 Pi 使用进程级缓存，保证
	// /new、/resume、/fork、/reload 重载扩展后仍写同一 claim 文件。
	const instanceId = process.env.CORRAL_PI_INSTANCE_ID || processIdentity.nativeInstanceId;
	let claimPath = process.env.CORRAL_PI_CLAIM_PATH || "";
	let sequence = 0;
	let lastIdentity: ClaimIdentity | null = null;
	let lastState: "" | "active" | "switching" | "shutdown" = "";
	let heartbeat: ReturnType<typeof setInterval> | undefined;
	let lastCtx: ExtensionContext | undefined;
	let ownerPath = "";
	let ownershipToken = "";
	let ownershipHeld = false;
	let blockedByOwner = false;
	// Pi 只在整轮助手输出完成后才把 assistant 写入 jsonl；Working 期间历史常停在
	// 用户消息。相位只来自扩展生命周期事件，不读正文，供 Corral 画绿/黄点。
	let agentPhase: AgentPhase = "idle";
	let agentPhaseEvent = "startup";
	let agentPhaseAt = new Date().toISOString();

	function resolveClaimPath(): string {
		if (!claimPath) {
			// 裸 Pi（未经 Corral 托管）也写 claim：Corral 能精确识别它，
			// 但只认这个 namespace，不读会话正文。
			claimPath = join(
				getAgentDir(),
				"corral-session-identity",
				"claims",
				"v1",
				`${instanceId}.json`,
			);
		}
		return claimPath;
	}

	function canonicalSessionFile(sessionFile: string): string {
		const absolute = resolve(sessionFile);
		try {
			return realpathSync(absolute);
		} catch {
			let parent = dirname(absolute);
			try {
				parent = realpathSync(parent);
			} catch {
				// 文件和父目录都尚未创建时，绝对规范路径仍可稳定参与 hash。
			}
			return join(parent, basename(absolute));
		}
	}

	function ownerFile(sessionFile: string): { canonical: string; path: string } {
		const canonical = canonicalSessionFile(sessionFile);
		const hash = createHash("sha256").update(canonical, "utf-8").digest("hex");
		return {
			canonical,
			path: join(getAgentDir(), IDENTITY_DIRNAME, "owners", "v1", `${hash}.lock`),
		};
	}

	function tmuxSocket(): string | undefined {
		// TMUX ends in ,server-pid,session-index; the socket path may contain commas.
		return process.env.TMUX?.match(/^(.*),\d+,\d+$/)?.[1];
	}

	function processAlive(pid: number): boolean {
		if (!Number.isInteger(pid) || pid <= 0) return false;
		try {
			process.kill(pid, 0);
			return true;
		} catch (error) {
			// EPERM 表示进程存在但无权发信号；同样视为存活。
			return (error as { code?: string }).code === "EPERM";
		}
	}

	function readOwner(path: string): Record<string, unknown> | null {
		try {
			const value = JSON.parse(readFileSync(path, "utf-8"));
			return value && typeof value === "object" ? value as Record<string, unknown> : null;
		} catch {
			return null;
		}
	}

	function removeDeadOwner(path: string): boolean {
		const owner = readOwner(path);
		// 损坏 owner 且无法证明原进程已死时保守冲突，不擅自删除。
		if (!owner) return false;
		const pid = Number(owner.pid ?? 0);
		if (processAlive(pid)) return false;
		try {
			// 删除前再回读 token，避免 stale 检查后锁已被后来者替换。
			const current = readOwner(path);
			if (!current || current.ownershipToken !== owner.ownershipToken) return false;
			unlinkSync(path);
			return true;
		} catch {
			return false;
		}
	}

	function acquireOwnership(identity: ClaimIdentity): boolean {
		if (!identity.sessionFile) return true;
		const location = ownerFile(identity.sessionFile);
		mkdirSync(dirname(location.path), { recursive: true });
		for (let attempt = 0; attempt < 2; attempt += 1) {
			const token = randomUUID();
			const owner = {
				protocolVersion: PROTOCOL_VERSION,
				canonicalSessionFile: location.canonical,
				instanceId,
				pid: process.pid,
				processStartedAt,
				instanceNonce: nonce,
				claimPath: resolveClaimPath(),
				ownershipToken: token,
				acquiredAt: new Date().toISOString(),
				tmuxSocket: tmuxSocket(),
			};
			let fd: number | undefined;
			try {
				fd = openSync(location.path, "wx", 0o600);
				writeFileSync(fd, JSON.stringify(owner), "utf-8");
				fsyncSync(fd);
				closeSync(fd);
				fd = undefined;
				ownerPath = location.path;
				ownershipToken = token;
				ownershipHeld = true;
				return true;
			} catch (error) {
				if (fd !== undefined) {
					try { closeSync(fd); } catch { /* 已关闭 */ }
				}
				if ((error as { code?: string }).code !== "EEXIST") return false;
				const current = readOwner(location.path);
				if (
					current?.instanceId === instanceId &&
					current?.instanceNonce === nonce &&
					current?.pid === process.pid
				) {
					ownerPath = location.path;
					ownershipToken = String(current.ownershipToken ?? "");
					ownershipHeld = Boolean(ownershipToken);
					return ownershipHeld;
				}
				if (!removeDeadOwner(location.path)) return false;
			}
		}
		return false;
	}

	function ownershipConflict(sessionFile: string | null | undefined): boolean {
		if (!sessionFile) return false;
		const path = ownerFile(sessionFile).path;
		if (ownershipHeld && path === ownerPath) return false;
		const current = readOwner(path);
		if (!current) return false;
		if (!processAlive(Number(current.pid ?? 0)) && removeDeadOwner(path)) return false;
		return true;
	}

	function releaseOwnership(): void {
		if (!ownershipHeld || !ownerPath || !ownershipToken) return;
		try {
			const current = readOwner(ownerPath);
			if (
				current?.instanceId === instanceId &&
				current?.instanceNonce === nonce &&
				current?.ownershipToken === ownershipToken
			) {
				unlinkSync(ownerPath);
			}
		} catch {
			// 退出清理失败由下一位 writer 的死进程回收处理。
		} finally {
			ownerPath = "";
			ownershipToken = "";
			ownershipHeld = false;
		}
	}

	function locateOwnerPane(owner: Record<string, unknown>): { socket: string; target: string; label: string } | null {
		const pid = Number(owner.pid);
		if (!Number.isInteger(pid) || pid <= 1 || !processAlive(pid)) return null;
		// Only the error path probes tmux. Bound the entire read-only lookup, not
		// each socket separately, so an unavailable server cannot freeze /resume.
		const deadline = Date.now() + 750;
		function query(command: string, args: string[]): string {
			if (Date.now() >= deadline) return "";
			try {
				return execFileSync(command, args, {
					encoding: "utf-8", timeout: Math.max(1, deadline - Date.now()),
					maxBuffer: 512 * 1024, stdio: ["ignore", "pipe", "ignore"],
				});
			} catch { return ""; }
		}
		// Match the writer or one of its parents to pane_pid. Session names and
		// cwd alone are not evidence, including when two tmux servers share names.
		const parents = new Map<number, number>();
		for (const line of query("ps", ["-A", "-o", "pid=,ppid="]).trim().split("\n")) {
			const [child, parent] = line.trim().split(/\s+/).map(Number);
			if (child > 1 && parent > 0) parents.set(child, parent);
		}
		const ancestors = new Set<number>();
		for (let current = pid; current > 1 && !ancestors.has(current) && ancestors.size < 20;) {
			ancestors.add(current);
			current = parents.get(current) ?? 0;
		}
		const sockets = new Set<string>();
		if (typeof owner.tmuxSocket === "string") sockets.add(owner.tmuxSocket);
		const currentSocket = tmuxSocket();
		if (currentSocket) sockets.add(currentSocket);
		// Legacy 1.1 owners have no socket metadata. Discover existing sockets
		// in the current user's tmux directories without starting any server.
		const uid = process.getuid?.();
		if (uid !== undefined) {
			for (const base of new Set([process.env.TMUX_TMPDIR, "/tmp", tmpdir()])) {
				if (!base) continue;
				const directory = join(base, `tmux-${uid}`);
				try {
					for (const entry of readdirSync(directory, { withFileTypes: true })) {
						if (entry.isSocket()) sockets.add(join(directory, entry.name));
					}
				} catch { /* No readable tmux directory. */ }
			}
		}
		for (const socket of sockets) {
			const output = query("tmux", ["-N", "-S", socket, "list-panes", "-a", "-F",
				"#{pane_pid}\t#{session_id}:#{window_index}.#{pane_index}\t#{session_name}:#{window_index}.#{pane_index}"]);
			for (const line of output.trim().split("\n")) {
				const [panePid, target, label] = line.split("\t");
				if (ancestors.has(Number(panePid)) && /^\$\d+:\d+\.\d+$/.test(target ?? "")) {
					return { socket, target, label };
				}
			}
		}
		return null;
	}

	function notifyOwnerConflict(ctx: ExtensionContext, sessionFile: string | null = lastIdentity?.sessionFile ?? null): void {
		const owner = sessionFile ? readOwner(ownerFile(sessionFile).path) : null;
		// Strip terminal controls from paths and tmux names before displaying them.
		const display = (value: unknown): string => String(value ?? "").replace(/[\x00-\x1f\x7f-\x9f]/g, "?");
		const quote = (value: string): string => `'${value.replace(/'/g, "'\\''")}'`;
		const lines = [owner
			? "该 Pi 会话已在另一个窗口运行，已阻止重复写入。/ This Pi session is already open; duplicate writing was blocked."
			: "无法确认 Pi 会话写入权限，已阻止重复写入。/ Pi session ownership could not be verified; writing was blocked."];
		if (sessionFile) lines.push(`Session: ${display(basename(sessionFile))}`);
		if (owner) lines.push(`Owner PID: ${display(owner.pid)}`);
		const pane = owner ? locateOwnerPane(owner) : null;
		if (pane) {
			lines.push(`Tmux: ${display(pane.label)}`);
			lines.push("在另一终端返回原窗口 / Return from another terminal:");
			// Numeric tmux IDs avoid name ambiguity. Never emit a command containing
			// terminal controls; PID-only guidance remains available in that case.
			if (display(pane.socket) === pane.socket) {
				lines.push(`tmux -S ${quote(pane.socket)} attach-session -t ${quote(pane.target)}`);
			}
		} else {
			lines.push("请返回原 Pi 窗口；未能定位 tmux 面板。/ Return to the original Pi window; its tmux pane could not be located.");
		}
		lines.push("要在此恢复，先在原窗口退出 Pi，再重试 /resume。/ To resume here, exit Pi in the original window, then retry /resume.");
		ctx.ui.notify(lines.join("\n"), "error");
	}

	function setAgentPhase(phase: AgentPhase, event: string): void {
		agentPhase = phase;
		agentPhaseEvent = event;
		agentPhaseAt = new Date().toISOString();
		if (lastState === "active" && lastIdentity) {
			writeClaim("active", event, lastIdentity);
		}
	}

	function writeClaim(
		state: "active" | "switching" | "shutdown",
		reason: string,
		identity: ClaimIdentity | null,
		targetSessionFile: string | null = null,
	): void {
		const ctx = lastCtx;
		if (!ctx || !identity || !identity.sessionId) return;
		const claim = {
			protocolVersion: PROTOCOL_VERSION,
			extensionVersion: EXTENSION_VERSION,
			instanceId,
			pid: process.pid,
			processStartedAt,
			instanceNonce: nonce,
			state,
			sessionId: identity.sessionId,
			sessionFile: identity.sessionFile,
			cwd: identity.cwd,
			parentSession: identity.parentSession,
			reason,
			targetSessionFile,
			// Optional v1 fields: Corral readers ignore unknowns; 1.1+ consume these
			// so sidebar dots track TUI Working before jsonl catches up.
			agentPhase,
			agentPhaseEvent,
			agentPhaseAt,
			updatedAt: new Date().toISOString(),
			sequence: ++sequence,
		};
		const path = resolveClaimPath();
		try {
			mkdirSync(dirname(path), { recursive: true });
			// 同目录临时文件 + 原子替换，读侧永远看不到半份 JSON。
			const tmp = `${path}.tmp-${process.pid}-${sequence}`;
			writeFileSync(tmp, JSON.stringify(claim), "utf-8");
			renameSync(tmp, path);
		} catch {
			// claim 写失败只影响 Corral 精确关联，绝不干扰 Pi 本体。
		}
	}

	function currentIdentity(ctx: ExtensionContext): ClaimIdentity | null {
		const sm = ctx.sessionManager;
		try {
			const header = sm.getHeader() as { parentSession?: string } | undefined;
			return {
				// getSessionFile() 对尚未落盘的会话返回 undefined：照实写 null，
				// Corral 保持 provisional，等文件出现后转正。
				sessionId: sm.getSessionId() ?? "",
				sessionFile: sm.getSessionFile() ?? null,
				cwd: sm.getCwd() ?? ctx.cwd,
				parentSession: header?.parentSession ?? null,
			};
		} catch {
			return null;
		}
	}

	function stopHeartbeat(): void {
		if (heartbeat !== undefined) {
			clearInterval(heartbeat);
			heartbeat = undefined;
		}
	}

	function startHeartbeat(): void {
		if (heartbeat !== undefined) return;
		heartbeat = setInterval(() => {
			if (lastState === "active" && lastIdentity) {
				writeClaim("active", "heartbeat", lastIdentity);
			}
		}, HEARTBEAT_MS);
		// 心跳只服务 Corral 读取侧的时效校验，不得阻止进程退出。
		if (typeof heartbeat.unref === "function") heartbeat.unref();
	}

	/** 跨扩展 reload 续接 sequence：新实例不得把序号写回比磁盘更小的值。 */
	function resumeSequence(): void {
		try {
			const raw = readFileSync(resolveClaimPath(), "utf-8");
			const parsed = JSON.parse(raw) as { sequence?: number; instanceId?: string };
			if (
				parsed &&
				typeof parsed.sequence === "number" &&
				parsed.instanceId === instanceId &&
				parsed.sequence > sequence
			) {
				sequence = parsed.sequence;
			}
		} catch {
			// 首次运行或旧 claim 缺失/损坏：从 0 起步即可。
		}
	}

	pi.on("session_start", async (event, ctx) => {
		if (ctx.mode !== "tui") return;
		lastCtx = ctx;
		resumeSequence();
		const identity = currentIdentity(ctx);
		if (!identity) return;
		lastIdentity = identity;
		if (!acquireOwnership(identity)) {
			blockedByOwner = true;
			lastState = "shutdown";
			notifyOwnerConflict(ctx);
			ctx.shutdown();
			return;
		}
		blockedByOwner = false;
		lastState = "active";
		agentPhase = "idle";
		agentPhaseEvent = event.reason ?? "startup";
		agentPhaseAt = new Date().toISOString();
		writeClaim("active", event.reason ?? "startup", identity);
		startHeartbeat();
	});

	// Working 条出现在 agent_start；要等 agent_settled（含重试/压缩/排队续跑都结束）
	// 才清成 idle。不要用 agent_end：那会在自动续跑间隙把绿点打灭。
	pi.on("agent_start", async (_event, ctx) => {
		if (ctx.mode !== "tui") return;
		lastCtx = ctx;
		setAgentPhase("working", "agent_start");
	});

	pi.on("agent_settled", async (_event, ctx) => {
		if (ctx.mode !== "tui") return;
		lastCtx = ctx;
		setAgentPhase("idle", "agent_settled");
	});

	// 扩展弹出的结构化 UI 提问：黄点优先于绿点。
	pi.on("ui_prompt_start", async (_event, ctx) => {
		if (ctx.mode !== "tui") return;
		lastCtx = ctx;
		setAgentPhase("waiting", "ui_prompt_start");
	});

	pi.on("ui_prompt_end", async (_event, ctx) => {
		if (ctx.mode !== "tui") return;
		lastCtx = ctx;
		const stillRunning =
			typeof ctx.isIdle === "function" ? !ctx.isIdle() : agentPhase === "working";
		setAgentPhase(stillRunning ? "working" : "idle", "ui_prompt_end");
	});

	pi.on("session_before_switch", async (event, ctx) => {
		if (ctx.mode !== "tui") return;
		lastCtx = ctx;
		if (event.targetSessionFile && ownershipConflict(event.targetSessionFile)) {
			notifyOwnerConflict(ctx, event.targetSessionFile);
			return { cancel: true };
		}
		if (!lastIdentity) return;
		// /new、/resume 切换中：保留旧身份写 switching，targetSessionFile 只作
		// 诊断；Corral 维持原 pane 归属，等新 session_start 覆盖成 active。
		lastState = "switching";
		writeClaim(
			"switching",
			event.reason ?? "switch",
			lastIdentity,
			event.targetSessionFile ?? null,
		);
	});

	pi.on("input", async (_event, ctx) => {
		if (!blockedByOwner) return { action: "continue" as const };
		notifyOwnerConflict(ctx);
		return { action: "handled" as const };
	});

	pi.on("session_shutdown", async (event, ctx) => {
		if (ctx.mode !== "tui") return;
		stopHeartbeat();
		releaseOwnership();
		lastCtx = ctx;
		if (!lastIdentity) return;
		const reason = event.reason ?? "quit";
		if (reason === "quit") {
			// 真正退出：写 shutdown，Corral 把这个 pane 转为静态历史。
			lastState = "shutdown";
			writeClaim("shutdown", reason, lastIdentity);
		} else {
			// new/resume/fork/reload：进程还在换会话，扩展实例即将被重载。
			// 写 switching 而不是 shutdown，避免读取侧在替换窗口把 pane 判成
			// 已结束或改绑别人；新实例的 session_start 会覆盖成 active。
			lastState = "switching";
			writeClaim(
				"switching",
				reason,
				lastIdentity,
				event.targetSessionFile ?? null,
			);
		}
	});
}
