# =============================================================================
# 阶段 1：依赖 + Camoufox 浏览器缓存（体积大头，只有必要部分进最终镜像）
# =============================================================================
FROM python:3.13-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python -m venv "$VIRTUAL_ENV" \
    && python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

# 给 Playwright Firefox 驱动打 null-guard 补丁：Turnstile 跨域 iframe 导航提交
# 与 frame 销毁竞态会杀死 Node 驱动进程（上游 microsoft/playwright#13444，
# 1.62.0 仍未修复）。补丁失败（版本变化导致目标文本不匹配）会让构建失败。
# 运行时另有代理池任务级超时兜底，驱动再死也不会卡死 worker。
COPY docker/patch_playwright_driver.py /tmp/patch_playwright_driver.py
RUN python /tmp/patch_playwright_driver.py

# Camoufox 安装方式（build-arg），优先级：指定 ZIP URL > 在线 fetch
#   CAMOUFOX_ZIP_URL    — 直接下载浏览器 zip（官方 release / 自建镜像 / ghproxy
#                         等反代），路径最可控、不查版本；双架构构建时需要
#                         分别传各自架构的 URL
#   CAMOUFOX_MODEL_URL  — 可选，配套的 fpgen 模型 zip；不填则在线拉取
#   CAMOUFOX_VERSION    — 在线模式的仓库/版本，须与 config.json 的
#                         camoufox.version 前缀一致（如 158.0-beta.40），
#                         controller 按版本目录 glob 定位可执行文件
ARG CAMOUFOX_VERSION=official/158.0-beta.40
ARG CAMOUFOX_ZIP_URL=""
ARG CAMOUFOX_MODEL_URL=""
# 直连 URL 且文件名不是 camoufox-<version>-<build>-... 时，用它显式声明版本
ARG CAMOUFOX_BROWSER_SPEC=""
ENV CAMOUFOX_BROWSER_SPEC=${CAMOUFOX_BROWSER_SPEC}

COPY docker/install_camoufox_zip.py /tmp/install_camoufox_zip.py

RUN set -eux; \
    if [ -n "$CAMOUFOX_ZIP_URL" ]; then \
        zip_name="$(basename "${CAMOUFOX_ZIP_URL%%\?*}")"; \
        case "$zip_name" in \
            *.zip) : ;; \
            *) zip_name="camoufox-${CAMOUFOX_BROWSER_SPEC:-browser}.zip" ;; \
        esac; \
        curl -fL --retry 3 --retry-delay 2 --connect-timeout 20 -o "/tmp/$zip_name" "$CAMOUFOX_ZIP_URL"; \
        model_arg=""; \
        if [ -n "$CAMOUFOX_MODEL_URL" ]; then \
            curl -fL --retry 3 --retry-delay 2 --connect-timeout 20 -o /tmp/model-release.zip "$CAMOUFOX_MODEL_URL"; \
            model_arg="/tmp/model-release.zip"; \
        fi; \
        python /tmp/install_camoufox_zip.py "/tmp/$zip_name" $model_arg; \
        python -c "from camoufox.geolocation import download_mmdb; download_mmdb()"; \
        python -c "import camoufox.fpgen_model as fm; fm.ensure_fpgen_model()" || true; \
    else \
        # prerelease 版本 fetch 时会交互确认，用 yes 喂掉（CI 非交互环境）
        yes | python -m camoufox fetch "$CAMOUFOX_VERSION"; \
    fi; \
    rm -rf /tmp/model-release.zip /tmp/camoufox_extract /tmp/camoufox-*.zip

# 校验浏览器缓存可用（Linux 布局: <ver>-<build>-<sha8>/camoufox-bin）
RUN test -n "$(find /root/.cache/camoufox/browsers/official -mindepth 1 -maxdepth 1 -type d | head -1)" \
    && echo BROWSER_CACHE_OK

# =============================================================================
# 阶段 2：运行镜像（只带 venv + 浏览器缓存 + 运行代码）
# =============================================================================
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    RUNNING_IN_DOCKER=1

# 浏览器运行所需的系统库（不含编译工具链）
# xvfb: 有头模式虚拟显示；fonts-noto-cjk: 注册流程锁定 zh-CN 需要中文字形
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-liberation \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        libasound2 \
        libdbus-glib-1-2 \
        libegl1 \
        libgbm1 \
        libgl1-mesa-dri \
        libgtk-3-0 \
        libnss3 \
        libx11-xcb1 \
        libxcomposite1 \
        libxcursor1 \
        libxdamage1 \
        libxfixes3 \
        libxi6 \
        libxkbcommon0 \
        libxrandr2 \
        libxrender1 \
        libxss1 \
        libxt6 \
        libxtst6 \
        lsof \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 直接复用 builder 的 venv 与浏览器缓存，避免在最终镜像里再装一遍/再解压一遍
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /root/.cache/camoufox /root/.cache/camoufox

# 只复制运行所需代码：Results/、data/ 运行时生成（建议挂卷持久化）
COPY main.py web_console.py proxy_pool.py proxy_probe.py utils.py get_token.py ./
COPY controllers/ ./controllers/
COPY web/ ./web/
# 默认配置模板：entrypoint 首次启动复制为 /app/config.json（可被同名卷覆盖）
COPY config.json /app/config.default.json

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8788

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8788/login', timeout=3)" || exit 1

ENTRYPOINT ["entrypoint.sh"]
CMD ["python", "web_console.py", "--host", "0.0.0.0"]
