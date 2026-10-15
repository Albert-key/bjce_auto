# -*- coding: utf-8 -*-
"""冒烟测试: 浏览器启动 / 站点加载 / 登录态检测 / 退出"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "libs"))

# 沙箱环境的系统 Temp 可能拒绝驱动进程写入, 统一指向工作区
TMP = HERE / "tmp"
TMP.mkdir(exist_ok=True)
for k in ("TMP", "TEMP", "TMPDIR"):
    import os
    os.environ[k] = str(TMP)

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "https://bjce.bjdj.gov.cn"
CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

channel = None
for p in CANDIDATES:
    if Path(p).exists():
        channel = "chrome" if "Chrome" in p else "msedge"
        print(f"browser: {channel} ({p})")
        break
assert channel, "no chrome/edge found"

pw = sync_playwright().start()
ctx = pw.chromium.launch_persistent_context(
    user_data_dir=str(HERE / "profile"),
    channel=channel,
    headless=False,
    viewport=None,
    args=["--autoplay-policy=no-user-gesture-required",
          "--disable-background-timer-throttling",
          "--disable-backgrounding-occluded-windows",
          "--disable-renderer-backgrounding",
          "--start-maximized"],
)
page = ctx.pages[0] if ctx.pages else ctx.new_page()
ctx.add_init_script("""
(function () {
  if (window.__bjceIdleGuard) return;
  window.__bjceIdleGuard = setInterval(function () {
    try {
      document.dispatchEvent(new MouseEvent('mousemove', {bubbles: true}));
      document.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, key: 'Shift'}));
    } catch (e) {}
  }, 60 * 1000);
})();
""")
page.goto(BASE + "/#/index", timeout=60000)
print("title:", page.title())
token = page.evaluate(
    "() => sessionStorage.getItem('token') || localStorage.getItem('token')")
print("token:", repr(token))
guard = page.evaluate("() => !!window.__bjceIdleGuard")
print("idle-guard-injected:", guard)
page.wait_for_timeout(3000)
ctx.close()
pw.stop()
print("SMOKE OK")
