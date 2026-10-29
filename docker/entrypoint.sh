#!/bin/sh
# 容器入口：初始化可变文件 + 启动虚拟显示（有头模式需要）
set -e

# 首次启动（或挂载空卷）时，从镜像内置模板生成 config.json
if [ ! -f /app/config.json ]; then
    cp /app/config.default.json /app/config.json
    echo "[entrypoint] 已从默认模板初始化 /app/config.json"
fi
mkdir -p /app/data /app/Results

# 有头浏览器需要 X server；无头模式下 Xvfb 空转无害
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
export DISPLAY=:99

exec "$@"
