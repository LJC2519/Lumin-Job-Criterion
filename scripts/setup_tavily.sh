#!/usr/bin/env bash
# LJC · Tavily 全网语义搜索一键启用
# 用法：bash setup_tavily.sh tvly-xxxxxxxxxxxxxxxx
# 作用：写入 ~/.zshrc → 校验通道 → 实跑一次真实查询（证明真的通了）
set -euo pipefail

LJC="${LJC_DIR:-$HOME/.workbuddy/skills/lumin-job-criterion}"
KEY="${1:-}"

# --clipboard（或不传参）：从剪贴板读取——key 不经过对话、不进命令行历史
if [ "$KEY" = "--clipboard" ] || [ -z "$KEY" ]; then
  KEY="$(pbpaste 2>/dev/null || true)"
  if [ -n "$KEY" ]; then
    echo "ℹ️ 已从剪贴板读取 key（长度 ${#KEY}，不回显内容）"
  fi
fi

if [ -z "$KEY" ]; then
  echo "用法："
  echo "  bash setup_tavily.sh --clipboard      # 从剪贴板读（推荐，key 不落对话）"
  echo "  bash setup_tavily.sh tvly-xxxx       # 直接传参"
  echo "key 获取：https://app.tavily.com → 注册 → Dashboard → API Keys → 点复制图标"
  exit 1
fi

case "$KEY" in
  tvly-*) : ;;
  *) echo "⚠️  key 看起来不像 Tavily 格式（应形如 tvly-...），仍继续写入" ;;
esac

# 幂等写入：先清掉旧行再写（避免重复）
touch "$HOME/.zshrc"
if grep -q "TAVILY_API_KEY" "$HOME/.zshrc"; then
  sed -i '' 's#^export TAVILY_API_KEY=.*#export TAVILY_API_KEY="'"$KEY"'"#' "$HOME/.zshrc"
  echo "✅ 已更新 ~/.zshrc 中的既有 TAVILY_API_KEY"
else
  {
    echo ""
    echo "# LJC 全网语义搜索（Tavily）· $(date +%Y-%m-%d)"
    echo "export TAVILY_API_KEY=\"$KEY\""
  } >> "$HOME/.zshrc"
  echo "✅ 已写入 ~/.zshrc"
fi

# 同一进程立即生效
export TAVILY_API_KEY="$KEY"

# 本地密钥文件通道（关键）：WorkBuddy 是 macOS GUI 应用，不读 ~/.zshrc，
# GUI 内的调用只能靠这个文件拿到 key → 权限 600，只本人可读
KEYFILE="$HOME/.workbuddy/ljc/tavily.key"
mkdir -p "$(dirname "$KEYFILE")"
printf '%s' "$KEY" > "$KEYFILE"
chmod 600 "$KEYFILE"
echo "✅ 已写入本地密钥文件 ${KEYFILE}（权限 600，GUI 应用通道）"

echo
echo "== 通道自检 =="
python3 "$LJC/scripts/web_search.py" --check

echo
echo "== 实跑一次全网语义搜索（证明真的通了）=="
python3 "$LJC/scripts/web_search.py" "edge ai agent skill for legal research" | head -30

echo
echo "后续新开终端/会话会自动带上该 key；当前 shell 已即时生效。"
