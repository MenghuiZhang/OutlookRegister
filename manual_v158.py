"""
手动注册测试: 启动 v158 camoufox 窗口并停在 Outlook 注册页, 由人工操作
用法: .venv/bin/python manual_v158.py [代理地址]
"""
import sys
import glob
import os
import time

from camoufox import NewBrowser
from playwright.sync_api import sync_playwright

# 定位 v158
candidates = sorted(glob.glob(os.path.expanduser(
    "~/Library/Caches/camoufox/browsers/official/158.0*/Camoufox.app/Contents/MacOS/camoufox")))
if not candidates:
    print("未找到 v158 运行时")
    sys.exit(1)
exe = candidates[-1]
print("使用运行时:", exe)

proxy = {"server": sys.argv[1]} if len(sys.argv) > 1 else None
if proxy:
    print("代理:", proxy["server"])
else:
    print("直连 (无代理)")

p = sync_playwright().start()
try:
    b = NewBrowser(
        p,
        headless=False,
        geoip=True,
        humanize=True,
        locale="zh-CN",
        os="macos",
        executable_path=exe,
        proxy=proxy,
    )
    page = b.new_page()
    page.goto("https://outlook.live.com/mail/0/?prompt=create_account",
              timeout=60000, wait_until="domcontentloaded")
    print("已打开注册页, 窗口保持打开, 请手动操作 (Ctrl+C 结束)")
    while True:
        time.sleep(60)
except KeyboardInterrupt:
    print("退出")
finally:
    try:
        b.close()
    except Exception:
        pass
    p.stop()
