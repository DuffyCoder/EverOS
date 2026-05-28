#!/usr/bin/env bash
#
# build.sh — 改完 OpenViking-fork 后,一条命令跑一轮基于改动的 locomo 评测。
#
# 把 fork 两块源码都变成“活的”,全程零 image rebuild、不新建配置:
#   * OV server (Python)  —— editable 安装,重启进程即吃到改动
#   * openclaw plugin (TS) —— bind-mount fork 源码到容器 /app/extensions/openviking,
#                              openclaw 用 jiti 直接加载源 .ts(已实测,零编译)
#
# 用法:  ./build.sh <run-name> [额外 CLI 参数...]
#   ./build.sh topk20
#   ./build.sh smoke1 --from-conv 0 --to-conv 1     # 快速验证改动是否生效
# 长跑(~5h)脱离终端:
#   setsid nohup ./build.sh topk20 > .runlogs/run-topk20.log 2>&1 &
#
# 注意:会停掉 1933 上正在跑的 OV server 并清空 .ovdata —— 同一时间只能跑一轮。
# 注意:server 用当前 fork checkout 的代码(editable);跑前先 git checkout 到你要测的分支。
#
set -euo pipefail

RUN_NAME="${1:?用法: ./build.sh <run-name> [额外 CLI 参数...]}"
shift

WORKTREE="/Data3/shutong.shan/memory/refs/EverMemOS/.claude/worktrees/openviking-local-eval"
FORK="/Data3/shutong.shan/memory/refs/OpenViking-fork"
PLUGIN_DIR="$FORK/examples/openclaw-plugin"
OV_CONF="/Data/shutong.shan/.openviking/ov.local.conf"
TCMALLOC="/lib/x86_64-linux-gnu/libtcmalloc.so.4"
RUNNER="/Data3/shutong.shan/memory/refs/EverMemOS/.venv/bin/python"
SYSTEM="openclaw-docker-openviking-session-bundle-noop"
LOGDIR="$WORKTREE/.runlogs"
OVLOG="$LOGDIR/ov-server.log"
mkdir -p "$LOGDIR"

echo "[build] fork @ $(git -C "$FORK" rev-parse --short HEAD) ($(git -C "$FORK" branch --show-current 2>/dev/null || echo detached))"
echo "[build] run-name: $RUN_NAME"

# 一次性:plugin 运行时依赖(@sinclair/typebox, fflate),纯 JS、跨平台可用
if [ ! -d "$PLUGIN_DIR/node_modules" ]; then
  echo "[build] 首次:安装 plugin 运行时依赖..."
  ( cd "$PLUGIN_DIR" && npm install --omit=dev --omit=optional --no-audit --no-fund )
fi

# 阶段 1:从当前 fork checkout 重启源码 OV server(editable -> 吃到 Python 改动)
echo "[build] 重启源码 OV server (port 1933) ..."
OVPID=$( (ss -ltnp 2>/dev/null || true) | grep ':1933' | grep -oP 'pid=\K[0-9]+' | head -1 || true)
if [ -n "${OVPID:-}" ]; then kill "$OVPID" 2>/dev/null || true; sleep 2; fi
rm -rf "$FORK/.ovdata" && mkdir -p "$FORK/.ovdata"   # 干净 namespace
# 关键:从本会话 shell 起 server 会继承 HTTP(S)_PROXY=127.0.0.1:8899(Claude Code 会话出口代理),
# no_proxy 只含 localhost,于是 server 对 siliconflow(rerank)/sophnet(抽取) 的出站全过这个代理。
# 该代理 down 时 rerank 成批 "Connection refused"(召回退化成纯向量分),up 时也给 find 加延迟触发超时。
# image 容器是干净 env 不受影响。直连已实测可用 → 起 server 前清掉代理变量,直连 provider。
setsid nohup bash -c \
  "unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy; cd '$FORK' && LD_PRELOAD='$TCMALLOC' exec .venv/bin/openviking-server --host 0.0.0.0 --port 1933 --config '$OV_CONF'" \
  > "$OVLOG" 2>&1 < /dev/null &

ok=0
for _ in $(seq 1 40); do
  if curl -fsS -m 3 http://127.0.0.1:1933/health >/dev/null 2>&1; then
    ok=1; echo "[build] OV server healthy: $(curl -s http://127.0.0.1:1933/health)"; break
  fi
  sleep 2
done
[ "$ok" = 1 ] || { echo "[build] ERROR: OV server 未变健康,见 $OVLOG"; tail -30 "$OVLOG"; exit 1; }

# 阶段 2:把 fork plugin 源码挂进容器(openclaw 用 jiti 从源 .ts 加载)
export OPENCLAW_EXTRA_MOUNTS="$PLUGIN_DIR:/app/extensions/openviking:ro"
echo "[build] plugin 挂载: $OPENCLAW_EXTRA_MOUNTS"

# 阶段 3:跑评测(复用现有 noop 配置,不新建配置)
cd "$WORKTREE"
echo "[build] 启动评测: system=$SYSTEM run-name=$RUN_NAME"
exec "$RUNNER" -m evaluation.cli \
  --dataset locomo --system "$SYSTEM" \
  --run-name "$RUN_NAME" "$@"
