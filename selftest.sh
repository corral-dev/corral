#!/usr/bin/env bash
# corral 端到端自测（Textual 界面层）：隔离 HOME 与 tmux socket，不碰真实会话。
#
# Real-terminal keyboard smoke: hosting, forwarding, search, reattachment,
# Ctrl+Q exit, direct launch, IME cursor anchoring and OSC 52 copy.
# Mouse close/card interactions use Pilot tests with isolated real tmux panes.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTER="corral-timeline-test-$$"
KEEPALIVE="corral-keepalive"
TMP="$(mktemp -d /tmp/corral-selftest.XXXXXX)"
PASS=0

ok() { PASS=$((PASS + 1)); echo "PASS  $1"; }
cap() { tmux -L "$OUTER" capture-pane -p -t tui 2>/dev/null; }
sessions() { tmux -L "$KEEPALIVE" list-sessions -F '#{session_name}' 2>/dev/null; }
wait_for() {
  local text="$1" tries="${2:-30}"
  local i
  for ((i = 0; i < tries; i++)); do
    cap | grep -qF -- "$text" && return 0
    sleep 0.25
  done
  echo "未等到：$text" >&2
  cap >&2 || true
  return 1
}
cleanup() {
  tmux -L "$OUTER" kill-server 2>/dev/null || true
  tmux -L "$KEEPALIVE" kill-session -t corral-claude-aaaa1111 2>/dev/null || true
  tmux -L "$KEEPALIVE" kill-session -t corral-claude-bbbb2222 2>/dev/null || true
  # direct_session/cursor_session 是直启子命令生成的随机 uuid 会话名，运行到
  # 对应步骤才会被赋值；trap 在函数体内引用是延迟求值，EXIT 时能拿到当时的值。
  [[ -n "${direct_session:-}" ]] && tmux -L "$KEEPALIVE" kill-session -t "$direct_session" 2>/dev/null || true
  [[ -n "${cursor_session:-}" ]] && tmux -L "$KEEPALIVE" kill-session -t "$cursor_session" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

FIXTURE_TIMESTAMP="$(python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')"
mkdir -p "$TMP/home/.claude/projects/demo" "$TMP/workA" "$TMP/workB" "$TMP/fakebin" "$TMP/home/.cache/corral"
cat > "$TMP/home/.cache/corral/titles.json" <<'EOF'
{"claude:aaaa1111":{"title":"修复切换体验","fp":"seed"},"claude:bbbb2222":{"title":"第二个会话","fp":"seed"}}
EOF
cat > "$TMP/home/.claude/projects/demo/aaaa1111.jsonl" <<EOF
{"type":"user","message":{"content":"修复切换体验"},"timestamp":"$FIXTURE_TIMESTAMP","cwd":"$TMP/workA","sessionId":"aaaa1111"}
{"type":"assistant","message":{"content":[{"type":"text","text":"会话 A 回复"}]},"timestamp":"$FIXTURE_TIMESTAMP","sessionId":"aaaa1111"}
EOF
cat > "$TMP/home/.claude/projects/demo/bbbb2222.jsonl" <<EOF
{"type":"user","message":{"content":"第二个会话"},"timestamp":"$FIXTURE_TIMESTAMP","cwd":"$TMP/workB","sessionId":"bbbb2222"}
{"type":"assistant","message":{"content":[{"type":"text","text":"会话 B 回复"}]},"timestamp":"$FIXTURE_TIMESTAMP","sessionId":"bbbb2222"}
EOF
# The first card must be A even when fresh fixture timestamps are identical.
touch "$TMP/home/.claude/projects/demo/aaaa1111.jsonl"
cat > "$TMP/fakebin/claude" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then echo "fake"; exit 0; fi
sid=""
previous=""
for arg in "$@"; do [[ "$previous" == "--resume" ]] && sid="$arg"; previous="$arg"; done
if [[ "$sid" == "cursortest" ]]; then
  # 光标探测专用：不带换行地打印提示符，光标停在提示符末尾（模拟真实 shell/
  # agent 等待输入时的光标位置），用于验证外层终端的真实硬件光标是否精确
  # 落在这里——这是 IME 候选框定位依赖的同一套机制。
  printf 'PROMPT> '
  while true; do sleep 1; done
fi
echo "FAKE-CLAUDE --resume $sid"
while IFS= read -r line; do echo "ECHO: $line"; done
EOF
chmod +x "$TMP/fakebin/claude"

TMUX_DIR="$(dirname "$(command -v tmux)")"
# textual/rich 是 pip --user 装的：HOME 一旦被隔离成 $TMP/home，--user 站点包
# 路径会跟着 HOME 走而失效（真机排查过的坑，不是猜测）。这里把当前解释器实际
# 能看到的 sys.path 原样透传，绕开这个问题；真正 pip install 到系统/venv 的
# 用户不受影响。
PYWORKAROUND_PATH="$(python3 -c 'import sys; print(":".join(p for p in sys.path if p))')"
ENVV="HOME=$TMP/home PYTHONPATH=$REPO/src:$PYWORKAROUND_PATH PATH=$TMP/fakebin:$TMUX_DIR:/usr/local/bin:/usr/bin:/bin TERM=xterm-256color CORRAL_TITLE_GENERATOR=none CORRAL_LANG=zh CORRAL_ISOLATE_MANAGED_HOSTS=1"
tmux -L "$OUTER" new-session -d -s tui -x 180 -y 42
tmux -L "$OUTER" set-option -t tui mouse on
tmux -L "$OUTER" send-keys -t tui "cd $REPO && env $ENVV python3 -m corral --limit 5" Enter

wait_for "workA 修复切换体验" 60
wait_for "workB 第二个会话" 60
ok "首屏是跨运行时统一时间线"

# 进入 corral 默认就高亮第一张会话卡（「＋ 新建」被跳过），右栏应直接展示完整
# 对话预览（选中即预览，不再依赖 Space）。**这里不能再按 Down**——那会落到第二
# 张卡（workB），后面的托管断言全部对不上 aaaa1111。
wait_for "● 你" 60
wait_for "修复切换体验" 60
ok "选中未托管会话时右栏展示完整对话预览"

tmux -L "$OUTER" send-keys -t tui Enter
wait_for "FAKE-CLAUDE --resume aaaa1111" 60
sessions | grep -qx "corral-claude-aaaa1111"
ok "回车把会话托管进后台 tmux 并在右栏展示实时画面"

# 回车 = 明确意图：输入应当已经在右栏，直接打字就该到达托管会话（不再需要先
# 点一下右栏 / 按 Tab）。这一步同时验证自动聚焦生效和键盘真实转发。
tmux -L "$OUTER" send-keys -t tui -l "smoke-input"
tmux -L "$OUTER" send-keys -t tui Enter
wait_for "ECHO: smoke-input" 40
sleep 0.5
ok "回车后无需点鼠标，键盘输入直接转发进托管会话"

# Leave the live terminal before using the sidebar search shortcut.
tmux -L "$OUTER" send-keys -t tui C-\\
sleep 0.5
tmux -L "$OUTER" send-keys -t tui /
sleep 0.3
tmux -L "$OUTER" send-keys -t tui -l "workA"
filtered=0
for _ in {1..40}; do
  if ! cap | grep -q "workB 第二个会话"; then
    filtered=1
    break
  fi
  sleep 0.15
done
if [[ "$filtered" != "1" ]]; then
  echo "Search did not filter out workB" >&2
  cap >&2
  exit 1
fi
ok "Ctrl+\\ 返回列表后搜索过滤生效"
# Escape clears the query. Enter first leaves the search input for the list;
# a second Enter explicitly reopens the selected hosted session.
tmux -L "$OUTER" send-keys -t tui Escape
wait_for "workB 第二个会话" 20
tmux -L "$OUTER" send-keys -t tui Enter
sleep 0.4
tmux -L "$OUTER" send-keys -t tui Enter
sleep 0.4
tmux -L "$OUTER" send-keys -t tui -l "reattach-input"
tmux -L "$OUTER" send-keys -t tui Enter
wait_for "ECHO: reattach-input" 40
[[ "$(sessions | grep -c '^corral-claude-aaaa1111$')" == "1" ]]
ok "重新回车接回同一托管会话并真实转发输入"

# The current global quit binding must leave the managed terminal alive.
tmux -L "$OUTER" send-keys -t tui C-q
for _ in {1..40}; do
  [[ "$(tmux -L "$OUTER" display-message -p -t tui '#{pane_current_command}')" != "python3" ]] && break
  sleep 0.1
done
if [[ "$(tmux -L "$OUTER" display-message -p -t tui '#{pane_current_command}')" == "python3" ]]; then
  echo "Ctrl+Q did not quit corral" >&2
  cap >&2
  exit 1
fi
sessions | grep -qx "corral-claude-aaaa1111"
ok "Ctrl+Q 退出界面，后台托管会话继续存活"

# ---- 直启子命令：corral claude --resume <id> 直接带进 TUI 侧边栏并托管 ----
# 直启的 ident 是 keepalive.new_session_ident() 生成的随机 uuid 片段，与
# --resume 后面的参数无关，因此新会话名靠"托管前后 diff 出唯一新增项"识别，
# 不能假设成 corral-claude-<--resume 的参数>。
before_direct="$(sessions | grep '^corral-claude-' || true)"
tmux -L "$OUTER" new-window -t tui -n direct
tmux -L "$OUTER" send-keys -t direct "cd $REPO && env $ENVV python3 -m corral claude --resume directcccc" Enter
wait_for_direct() {
  local text="$1" tries="${2:-40}"
  local i
  for ((i = 0; i < tries; i++)); do
    tmux -L "$OUTER" capture-pane -p -t direct 2>/dev/null | grep -qF -- "$text" && return 0
    sleep 0.25
  done
  echo "未等到（direct 窗口）：$text" >&2
  tmux -L "$OUTER" capture-pane -p -t direct >&2 || true
  return 1
}
wait_for_direct "FAKE-CLAUDE --resume directcccc" 60
direct_session="$(comm -13 <(echo "$before_direct" | sort) <(sessions | grep '^corral-claude-' | sort))"
[[ -n "$direct_session" ]]
ok "直启子命令自动托管新会话并在侧边栏展示"

# 直启会自动聚焦右栏（与侧边栏点选不抢焦点的语义不同）。
tmux -L "$OUTER" send-keys -t direct -l "direct-smoke-input"
tmux -L "$OUTER" send-keys -t direct Enter
wait_for_direct "ECHO: direct-smoke-input" 40
ok "直启场景键盘输入真实转发进托管会话"

# ---- IME 光标锚定：真实终端硬件光标必须精确落在托管 pane 内的真实光标位置 ----
# （这是 IME 候选框/emoji 弹出框定位依赖的同一套机制，真机没有输入法没法直接
# 看候选框位置，但可以验证底层依据——外层终端光标坐标——算得准不准）。
# 快照必须在新建 cursor 窗口之前拍，diff 才有意义（曾经错误地在 wait_for_cursor
# 成功之后才拍快照，那时新会话早已存在，diff 出来永远是空，是脚本自身的 bug，
# 不是产品的 bug）。
before_cursor="$(sessions | grep '^corral-claude-' || true)"
tmux -L "$OUTER" new-window -t tui -n cursor
tmux -L "$OUTER" send-keys -t cursor "cd $REPO && env $ENVV python3 -m corral claude --resume cursortest" Enter
wait_for_cursor() {
  local text="$1" tries="${2:-40}"
  local i
  for ((i = 0; i < tries; i++)); do
    tmux -L "$OUTER" capture-pane -p -t cursor 2>/dev/null | grep -qF -- "$text" && return 0
    sleep 0.25
  done
  echo "未等到（cursor 窗口）：$text" >&2
  tmux -L "$OUTER" capture-pane -p -t cursor >&2 || true
  return 1
}
wait_for_cursor "PROMPT>" 60
cursor_session="$(comm -13 <(echo "$before_cursor" | sort -u) <(sessions | grep '^corral-claude-' | sort -u) | head -1)"
if [[ -z "$cursor_session" ]]; then
  echo "未能识别出 cursor 测试新建的托管会话名" >&2
  sessions >&2
  exit 1
fi
# 直启已自动聚焦右栏；等外层光标跟上内嵌 pane 光标即可。
expected_x=""
outer_cursor=""
inner_cursor=""
for _ in {1..40}; do
  inner_cursor="$(tmux -L "$KEEPALIVE" display-message -p -t "$cursor_session" '#{cursor_x},#{cursor_y}')"
  outer_cursor="$(tmux -L "$OUTER" display-message -p -t cursor '#{cursor_x},#{cursor_y}')"
  outer_x="${outer_cursor%,*}"; outer_y="${outer_cursor#*,}"
  inner_x="${inner_cursor%,*}"; inner_y="${inner_cursor#*,}"
  # 左栏固定宽度 39，右栏前保留 1 列空隙，因此面板起点在第 40 列；
  # 真实外层光标列 = 40 + pane 内真实光标列；顶栏与格标题各占 1 行，因此外层行
  # = 2 + pane 内真实光标行。算错了说明光标坐标换算或时机有问题。
  expected_x=$((40 + inner_x))
  expected_y=$((2 + inner_y))
  if [[ "$outer_x" == "$expected_x" && "$outer_y" == "$expected_y" ]]; then
    break
  fi
  sleep 0.15
done
outer_x="${outer_cursor%,*}"; outer_y="${outer_cursor#*,}"
inner_x="${inner_cursor%,*}"; inner_y="${inner_cursor#*,}"
expected_x=$((40 + inner_x))
expected_y=$((2 + inner_y))
if [[ "$outer_x" == "$expected_x" && "$outer_y" == "$expected_y" ]]; then
  ok "外层终端真实光标精确落在托管 pane 内的真实光标位置（内 ${inner_cursor} -> 外 ${outer_cursor}，IME 候选框定位依据的机制验证通过）"
else
  echo "光标锚定坐标不匹配：inner=${inner_cursor} outer=${outer_cursor} expected=${expected_x},${expected_y}" >&2
  exit 1
fi

# ---- IME 关键回归：外层真实光标必须「可见」（DECTCEM 打开），不能只是位置对 ----
# Textual 全屏运行期默认 `\e[?25l` 藏掉真实光标，只移动一个看不见的光标——位置
# 再准，IME 也没有可见锚点，用户在内嵌 Agent 里根本打不出中文（真机反馈）。
# `#{cursor_flag}` 反映托管 corral 当前有没有把真实光标显示出来：聚焦内嵌 pane
# 且有可见光标时必须为 1。EmbedPane._set_real_cursor 就是补这一步的。
outer_cursor_flag="$(tmux -L "$OUTER" display-message -p -t cursor '#{cursor_flag}')"
if [[ "$outer_cursor_flag" == "1" ]]; then
  ok "外层终端真实光标处于「可见」状态（cursor_flag=1）——IME 有可见锚点，中文合成才能激活"
else
  echo "外层真实光标被隐藏（cursor_flag=${outer_cursor_flag}）：位置对但不可见，IME 仍无法工作" >&2
  exit 1
fi

# ---- 划词选中 + Ctrl+C 复制：验证真实 OSC 52 写入到达外层终端（tmux 充当） ----
tmux -L "$OUTER" send-keys -t cursor -l "$(printf '\033[<0;40;3M')"
sleep 0.05
tmux -L "$OUTER" send-keys -t cursor -l "$(printf '\033[<32;70;3M')"
sleep 0.05
tmux -L "$OUTER" send-keys -t cursor -l "$(printf '\033[<0;70;3m')"
sleep 0.3
tmux -L "$OUTER" set-option -t cursor set-clipboard on
tmux -L "$OUTER" send-keys -t cursor C-c
sleep 0.5
copied="$(tmux -L "$OUTER" show-buffer 2>/dev/null || true)"
if [[ "$copied" == *"PROMPT>"* ]]; then
  ok "划词选中托管 pane 画面文字后 Ctrl+C 复制，真实 OSC 52 写入到达外层终端"
else
  echo "划词复制未生效，tmux 缓冲区内容：${copied@Q}" >&2
  exit 1
fi

echo "==== $PASS passed, 0 failed ===="
