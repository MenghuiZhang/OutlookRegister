import json
import os
import sys
import time
import random
from playwright.sync_api import sync_playwright
from camoufox import NewBrowser
from utils import log
from .base_controller import BaseBrowserController


class CamoufoxController(BaseBrowserController):
    """
    基于 camoufox (反指纹 Firefox) 的控制器

    - 兼容 Playwright API, 直接复用 base_controller 的通用注册流程
    - geoip=True: 时区/语言/经纬度/屏幕分辨率自动跟随代理 IP
    - humanize=True: 鼠标移动自带拟人轨迹 (配合基类 smooth_click 双保险)
    - block_images/block_webrtc: 省流量开关 (默认关闭)
    - 支持 macOS (含 Apple Silicon)
    """

    def __init__(self):
        super().__init__()
        with open('config.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        cfg = data.get("camoufox", {})
        self.headless = cfg.get("headless", False)
        self.geoip = cfg.get("geoip", True)
        self.humanize = cfg.get("humanize", True)
        self.locale = cfg.get("locale", "zh-CN")
        self.browser_version = cfg.get("version", "")
        # 注册结束后保留浏览器窗口, 便于人工检查结果
        self.keep_browser = cfg.get("keep_browser", False)
        # 指纹 OS: macOS 宿主机默认锁 macos — Windows 身份在 Mac 上会回退到
        # 宿主机隐藏系统字体, 有头模式中文字形错乱 (camoufox #706/#708)
        self.os = cfg.get("os") or ("macos" if sys.platform == "darwin" else None)
        # 默认关闭: camoufox 官方提示 block_images 在主流 WAF 上会造成检测特征;
        # WebRTC 由 geoip 负责把 IP 伪装成代理出口, 无需硬屏蔽
        self.block_images = cfg.get("block_images", False)
        self.block_webrtc = cfg.get("block_webrtc", False)

    def _resolve_executable(self, version):
        """按版本号定位已安装的 camoufox 运行时 (绕过 pip 包的版本锁定)"""
        import glob
        if sys.platform == "darwin":
            base = os.path.expanduser("~/Library/Caches/camoufox/browsers/official")
            hits = sorted(glob.glob(os.path.join(base, f"{version}*/Camoufox.app/Contents/MacOS/camoufox")))
        else:
            # Linux 布局: ~/.cache/camoufox/browsers/official/<ver>-<build>-<sha8>/camoufox-bin
            base = os.path.expanduser("~/.cache/camoufox/browsers/official")
            hits = sorted(glob.glob(os.path.join(base, f"{version}*/camoufox-bin")))
        return hits[-1] if hits else None

    def launch_browser(self):
        p = None
        try:
            p = sync_playwright().start()

            # 代理优先级: 代理池任务租约 (线程级) > config.json proxy > 直连
            proxy_settings = self._task_proxy_option()

            # locale 显式锁定页面语言 (注册流程依赖中文选择器);
            # geoip 仍负责时区/经纬度/IP 一致性, 显式 locale 优先生效
            launch_kwargs = dict(
                headless=self.headless,
                geoip=self.geoip,
                humanize=self.humanize,
                proxy=proxy_settings,
            )
            if self.locale:
                launch_kwargs["locale"] = self.locale
            if self.os:
                launch_kwargs["os"] = self.os
            if self.browser_version:
                exe = self._resolve_executable(self.browser_version)
                if exe:
                    launch_kwargs["executable_path"] = exe
                else:
                    log(f"未找到 camoufox 运行时 {self.browser_version}, 使用默认版本", "WARN")
            if self.block_images:
                launch_kwargs["block_images"] = True
            if self.block_webrtc:
                launch_kwargs["block_webrtc"] = True

            b = NewBrowser(p, **launch_kwargs)
            log(f"浏览器已启动 (headless={self.headless} geoip={self.geoip} locale={self.locale} "
                f"os={self.os or '默认'} 代理={'有' if proxy_settings else '无'})")
            return p, b

        except Exception as e:
            log(f"浏览器启动失败: {type(e).__name__}: {e}", "ERROR")
            if "Failed to get IP address" in str(e) or "geoip" in str(e).lower():
                log("GeoIP 查询失败 → 代理节点不通或过慢, 建议换节点/直连", "ERROR")
            if p:
                try:
                    p.stop()
                except Exception:
                    pass
            return False, False

    def _find_in_frames(self, page, selector):
        """遍历所有 frame 查找元素, 不依赖 iframe title 等脆弱选择器"""
        for f in page.frames:
            try:
                loc = f.locator(selector)
                if loc.count() > 0:
                    return loc
            except Exception:
                continue
        return None

    def handle_captcha(self, page):
        """
        PerimeterX 无障碍按压挑战 (兼容 Firefox):
        遍历所有 frame 找 [aria-label="可访问性挑战"] → 点击 → 找 [aria-label="再次按下"] → 点击
        """
        page.wait_for_timeout(random.randint(400, 700))
        captcha_start = time.time()

        for round_no in range(self.max_captcha_retries + 1):
            log(f"按压验证码第 {round_no + 1}/{self.max_captcha_retries + 1} 轮")
            self._shot(page, f"captcha-round-{round_no + 1}")

            # 等挑战按钮出现 (期间若已直接通过则提前返回)
            loc = None
            deadline = time.time() + 15
            while time.time() < deadline:
                loc = self._find_in_frames(page, '[aria-label="可访问性挑战"]')
                if loc:
                    break
                if page.get_by_text('保护你的帐户').count() > 0 or page.locator('[data-testid="primaryButton"]').count() > 0:
                    log(f"验证码直接通过, 无需按压 (第 {round_no + 1} 轮, 耗时 {time.time() - captcha_start:.0f}s)", "SUCCESS")
                    return True
                page.wait_for_timeout(300)
            if not loc:
                log(f"挑战按钮 15s 未出现且无通过迹象 (已等 {time.time() - captcha_start:.0f}s)", "WARN")
                return False

            self.smooth_click(page, loc)
            page.wait_for_timeout(random.randint(300, 600))

            loc2 = None
            deadline = time.time() + 10
            while time.time() < deadline:
                loc2 = self._find_in_frames(page, '[aria-label="再次按下"]')
                if loc2:
                    break
                page.wait_for_timeout(300)
            if loc2:
                self.smooth_click(page, loc2)
            else:
                log("'再次按下' 按钮 10s 未出现", "WARN")

            try:
                page.locator('.draw').wait_for(state="detached", timeout=14000)
                try:
                    # 简单的认为加载8秒后成功，暂不考虑请求.
                    page.locator('[role="status"][aria-label="正在加载..."]').wait_for(timeout=5000)

                    captcha_passed = False
                    for _ in range(20):
                        if page.get_by_text('一些异常活动').count() or page.get_by_text('此站点正在维护，暂时无法使用，请稍后重试。').count() > 0:
                            log("通过验证码但 IP 注册频率过快被拒 → 换 IP/节点", "ERROR")
                            self.thread_local.fail_code = "RATE_LIMIT"
                            return False
                        elif self._find_in_frames(page, '[aria-label="可访问性挑战"]'):
                            captcha_passed = False
                            page.wait_for_timeout(random.randint(500, 1000))
                            break
                        elif page.get_by_text('保护你的帐户').count() > 0 or page.locator('[data-testid="primaryButton"]').count() > 0:
                            captcha_passed = True
                            break
                        page.wait_for_timeout(random.randint(375, 425))
                    else:
                        # 部分会一直加载，不确定是否注册成功
                        if not self._find_in_frames(page, '[aria-label="可访问性挑战"]'):
                            captcha_passed = True

                    if captcha_passed:
                        log(f"按压验证码通过 (第 {round_no + 1} 轮, 耗时 {time.time() - captcha_start:.0f}s)", "SUCCESS")
                        break

                except Exception:
                    if page.get_by_text('保护你的帐户').count() > 0 or page.locator('[data-testid="primaryButton"]').count() > 0:
                        break
                    log("加载状态异常消失, 查找重试提示中…", "WARN")
                    # 等待重试提示 ("请再试一次" 等, 可能出现在任意 frame 中)
                    retry_hint = None
                    deadline = time.time() + 15
                    while time.time() < deadline:
                        for f in page.frames:
                            try:
                                hint = f.locator(':has-text("请再试一次"), :has-text("Keep going"), :has-text("a few more tries")')
                                if hint.count() > 0:
                                    retry_hint = hint
                                    break
                            except Exception:
                                continue
                        if retry_hint:
                            break
                        page.wait_for_timeout(400)
                    if retry_hint:
                        log("出现 '请再试一次', 进入下一轮按压", "WARN")
                    else:
                        log("未见重试提示也未见通过, 进入下一轮", "WARN")
                    continue

            except Exception:
                if page.get_by_text('保护你的帐户').count() > 0 or page.locator('[data-testid="primaryButton"]').count() > 0:
                     break
                log(".draw 元素等待超时", "WARN")
                return False
        else:
            log(f"按压 {self.max_captcha_retries + 1} 轮仍未通过 → 多为指纹/IP 特征被标记", "ERROR")
            return False

        return True

    def get_thread_page(self):
        browser = self.get_thread_browser()
        context = browser.new_context()
        return context.new_page()

    def clean_up(self, page=None, type="all_browser"):
        if self.keep_browser:
            log(f"keep_browser=True, 保留浏览器不关闭 ({type})")
            return

        if type == "done_browser" and page:
            context = page.context
            context.close()

        elif type == "all_browser":
            for p, b in self.active_resources:
                try:
                    b.close()
                except Exception: pass
                try:
                    p.stop()
                except Exception: pass
