#!/usr/bin/env bash
# LJC · Tavily 全网语义搜索一键启用
# 用法：bash setup_tavily.sh tvly-xxxxxxxxxxxxxxxx
# 作用：写入 ~/.zshrc → 校验通道 → 实跑一次真实查询（证明真的通了）
set -euo pipefail

LJC="${LJC_DIR:-$HOME/.workbuddy/skills/lumin-job-criterion}"
KEY="${1:-}"

if [ -z "$KEY" ]; then
  echo "用法: bash setup_tavily.sh <你的 tvly-... key>"
  echo "key 获取：https://app.tavily.com → 注册 → Dashboard → API Keys"
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

# 仅本进程生效（当前 shell 立即可用）
export TAVILY_API_KEY="$KEY"

echo
echo "== 通道自检 =="
python3 "$LJC/scripts/web_search.py" --check

echo
echo "== 实跑一次全网语义搜索（证明真的通了）=="
python3 "$LJC/scripts/web_search.py" "edge ai agent skill for legal research" | head -30

echo
echo "后续新开终端/会话会自动带上该 key；当前 shell 已即时生效。"
