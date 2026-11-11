#!/bin/sh
# install.sh — 把 cua skill 装进 prime-agent 的用户 skill 目录。
# 只写 ~/.prime/agent/skills/cua/，不触碰官方 payload（~/.local/share/prime-agent），
# 因此 prime-agent update 不会冲掉本 skill。
set -eu

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.prime/agent/skills/cua"

if ! command -v prime-agent >/dev/null 2>&1; then
    echo "未找到 prime-agent，请先安装官方版："
    echo "  curl -Ls https://pub-728493de92a943e2a9b2d17b4719f318.r2.dev/install.sh | sh"
    exit 1
fi

mkdir -p "$DEST"
cp -R "$SRC/src" "$SRC/SKILL.md" "$SRC/pyproject.toml" "$DEST/"
rm -rf "$DEST/src/cua/__pycache__" 2>/dev/null || true

# 已有配置就不覆盖，绝不冲掉用户填好的端点
if [ ! -f "$DEST/cua.config.json" ]; then
    cp "$SRC/config.example.json" "$DEST/cua.config.json"
    echo "已复制配置模板到 $DEST/cua.config.json —— 请填写后再使用 (见 README)。"
fi

echo "安装完成: $DEST"
echo "下一步: 编辑 $DEST/cua.config.json 填入你的视觉模型端点。"
