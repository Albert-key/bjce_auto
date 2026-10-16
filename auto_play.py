# -*- coding: utf-8 -*-
"""
北京干部网络学院 (bjce.bjdj.gov.cn) 自动播放刷学时脚本
====================================================
功能:
  1. 启动持久化浏览器(登录状态保留), 首次运行手动登录一次即可
  2. 自动查询当前年度已获学时 (必修 / 选修)
  3. 自动构建课程队列: 先"我的课程-学习中", 不够再从选课中心按 必修/选修 补充
  4. 自动播放: 打开课程 → 自动点播放 → 每 60 秒派发鼠标/键盘事件防止
     "30 分钟未操作自动暂停" → 一旦暂停立即续播 → 播完一章自动切下一章
     → 播完一课自动切下一课
  5. 直到 必修 >= 10 学时 且 选修 >= 40 学时 (共 50 学时) 才停止, 全程写日志

用法:
  python auto_play.py

依赖:
  python -m pip install --target libs playwright
  本机需装有 Chrome 或 Edge 浏览器
"""

import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# 控制台可能是 GBK 编码, 强制 UTF-8 避免 ✗⚠ 等符号导致崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
LIBS = HERE / "libs"
if LIBS.exists():
    sys.path.insert(0, str(LIBS))

# 沙箱环境的系统 Temp 可能拒绝驱动进程写入, 统一指向工作区
import os
TMP_DIR = HERE / "tmp"
TMP_DIR.mkdir(exist_ok=True)
for _k in ("TMP", "TEMP", "TMPDIR"):
    os.environ[_k] = str(TMP_DIR)

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("[错误] 未找到 playwright, 请先在本目录执行:")
    print("    python -m pip install --target libs playwright")
    sys.exit(1)

# ----------------------------- 配置 -----------------------------
BASE = "https://bjce.bjdj.gov.cn"
TARGET_MUST = 10.0        # 必修目标学时
TARGET_SELECT = 40.0      # 选修目标学时
PROGRESS_PASS = 80.0      # 课程进度达到该百分比即视为完成, 无需重播
MUTE_VIDEO = True         # 静音播放(建议开启, 不影响学时记录)
SCAN_INTERVAL = 5         # 播放状态轮询间隔(秒)
MAX_COURSE_MIN = 240      # 跳过总时长超过该分钟数的课程
CHUNK_STALL_MIN = 15      # 单章节无进展超过该分钟数则跳过该课
PROFILE_DIR = str(HERE / "profile")
LOG_FILE = HERE / "progress.log"
STATE_FILE = HERE / "state.json"

BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# 注入到每个页面的防挂机脚本: 每 60 秒派发 mousemove/keydown,
# 重置播放页 Playcontrol 组件的 30 分钟无操作计时器
IDLE_GUARD_JS = """
(function () {
  if (window.__bjceIdleGuard) return;
  window.__bjceIdleGuard = setInterval(function () {
    try {
      document.dispatchEvent(new MouseEvent('mousemove', {
        bubbles: true, cancelable: true, view: window, clientX: 300, clientY: 300
      }));
      document.dispatchEvent(new KeyboardEvent('keydown', {
        bubbles: true, cancelable: true, key: 'Shift', code: 'ShiftLeft'
      }));
    } catch (e) {}
  }, 60 * 1000);
})();
"""

# 读取播放页状态
STATE_JS = """
() => {
  const v = document.querySelector('video');
  // 只取章节/集菜单 ul —— 排除"相关课程"列表(点击会跳去其他课程)
  const lis = Array.from(document.querySelectorAll(
      '.iv-course-play-detail-menu-item:not(.iv-course-play-detail-menu-cour) li'));
  let activeIdx = -1;
  lis.forEach((li, i) => {
    if ((li.className || '').indexOf('iv-c-p-active') >= 0) activeIdx = i;
  });
  let alertShown = !!document.querySelector('#alert');
  let busy = false, modalText = '';
  document.querySelectorAll('.ivu-modal').forEach(m => {
    if (m.offsetParent !== null) {
      const t = m.textContent || '';
      modalText = t.slice(0, 120);
      if (t.indexOf('正在学习') >= 0) busy = true;
    }
  });
  return {
    hasVideo: !!v,
    paused: v ? v.paused : true,
    ended: v ? (v.ended || (v.duration > 0 && v.currentTime >= v.duration - 1)) : false,
    t: v ? Math.floor(v.currentTime) : 0,
    d: v ? Math.floor(v.duration || 0) : 0,
    chapters: lis.length,
    activeIdx: activeIdx,
    alert: alertShown,
    busy: busy,
    modalText: modalText
  };
}
"""

PLAY_JS = """
async (mute) => {
  const v = document.querySelector('video');
  if (!v) return 'no-video';
  if (mute) { try { v.muted = true; } catch (e) {} }
  try { await v.play(); return 'ok'; }
  catch (e) { return 'reject:' + ((e && e.name) || e); }
}
"""

PAUSE_JS = """
() => {
  const v = document.querySelector('video');
  if (v && !v.paused) { v.pause(); return true; }
  return false;
}
"""

CLOSE_ALERT_JS = """
() => {
  const b = document.querySelector('#alert .uit-alert-close');
  if (b) { b.click(); return true; }
  return false;
}
"""

HIDE_BUSY_MODAL_JS = """
() => {
  let n = 0;
  document.querySelectorAll('.ivu-modal-wrap').forEach(w => {
    if ((w.textContent || '').indexOf('正在学习') >= 0) {
      w.style.display = 'none'; n++;
    }
  });
  return n;
}
"""


# ----------------------------- 日志 -----------------------------
class Log:
    def __init__(self, path: Path):
        self.path = path

    def write(self, msg: str):
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            pass
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


LOG = Log(LOG_FILE)


def save_state(state: dict):
    try:
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def fmt_sec(s):
    s = int(s or 0)
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def norm_progress(p):
    """学时进度统一转为 0-100: 平台不同接口有的返回 0-1 小数, 有的返回 0-100"""
    try:
        p = float(p)
    except (TypeError, ValueError):
        return 0.0
    if p <= 0:
        return 0.0
    if p < 1:
        return p * 100.0
    return min(p, 100.0)


# ----------------------------- 站点封装 -----------------------------
class Site:
    def __init__(self):
        self.pw = None
        self.context = None
        self.page = None
        self.sniffed = {}
        self.recent = []
        self._channel = None
        self._play_counts = {}

    # ---- 浏览器 ----
    def start(self):
        self.pw = sync_playwright().start()
        # 依次尝试 Chrome / Edge (Playwright 按注册表与安装路径自动定位)
        last_err = None
        for channel in ("chrome", "msedge"):
            try:
                self.context = self.pw.chromium.launch_persistent_context(
                    user_data_dir=PROFILE_DIR,
                    channel=channel,
                    headless=False,
                    viewport=None,
                    args=[
                        "--autoplay-policy=no-user-gesture-required",
                        "--mute-audio" if MUTE_VIDEO else "--no-first-run",
                        "--disable-background-timer-throttling",
                        "--disable-backgrounding-occluded-windows",
                        "--disable-renderer-backgrounding",
                        "--start-maximized",
                    ],
                )
                self._channel = channel
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                LOG.write(f"浏览器 {channel} 启动失败: {str(e)[:200]}")
        if not self._channel:
            raise RuntimeError(
                "未找到可用的 Chrome/Edge 浏览器, 请先安装其中之一。"
                f"最后错误: {last_err}")
        self.context.add_init_script(IDLE_GUARD_JS)
        self.page = self.context.pages[0] if self.context.pages \
            else self.context.new_page()
        self.page.on("response", self._on_response)
        LOG.write(f"浏览器已启动 ({self._channel}), 正在打开 {BASE} ...")
        self.page.goto(BASE + "/#/index", timeout=60000)

    def close(self):
        for f in (self.context, self.pw):
            try:
                if f:
                    f.close()
            except Exception:
                pass

    # ---- 网络嗅探 ----
    def _on_response(self, resp):
        url = resp.url
        try:
            self.recent.append((resp.status, url))
            if len(self.recent) > 120:
                del self.recent[:60]
        except Exception:
            pass
        try:
            key = None
            if "getStudyHourDetail" in url or "listStudyHourByUserYear" in url:
                key = "hours"
            elif "listCourseChapter" in url:
                key = "chapters"
            elif "getPlayInfo" in url:
                key = "playinfo"
            elif "listMyCourse" in url:
                key = "mycourse"
            if key:
                self.sniffed[key] = resp.json()
        except Exception:
            pass

    def dump_page_state(self, tag):
        """打开课程失败时的页面诊断"""
        try:
            info = self.page.evaluate("""
() => ({
  url: location.href,
  title: document.title,
  hasVideo: !!document.querySelector('video'),
  chapters: document.querySelectorAll(
      '.iv-course-play-detail-menu-item:not(.iv-course-play-detail-menu-cour) li').length,
  modals: Array.from(document.querySelectorAll('.ivu-modal'))
      .filter(m => m.offsetParent !== null)
      .map(m => (m.textContent || '').replace(/\\s+/g, ' ').slice(0, 80)),
  bodyText: (document.body.innerText || '')
      .replace(/\\s+/g, ' ').slice(0, 260)
})""")
            LOG.write(f"  [诊断:{tag}] {json.dumps(info, ensure_ascii=False)}")
            LOG.write("  [诊断] 最近网络请求:")
            for s, u in self.recent[-15:]:
                LOG.write(f"    [{s}] {u[:130]}")
        except Exception as e:
            LOG.write(f"  [诊断:{tag}] 导出失败: {e}")
        try:
            err_dir = HERE / "errors"
            err_dir.mkdir(exist_ok=True)
            self.page.screenshot(
                path=str(err_dir / f"err_{tag}_{int(time.time())}.png"))
        except Exception:
            pass

    def wait_sniff(self, key, timeout_s=40):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if key in self.sniffed:
                return self.sniffed.pop(key)
            self.page.wait_for_timeout(500)
        return None

    # ---- API ----
    def api_get(self, path, timeout_s=30):
        """在页面上下文中发起同源 fetch, 返回 JSON body (可能为 None)"""
        url = f"{BASE}/{path}"
        js = """
        async (u) => {
          try {
            const r = await fetch(u, {credentials: 'include'});
            let j = null;
            try { j = await r.json(); } catch (e) {}
            return {status: r.status, body: j};
          } catch (e) { return {status: 0, body: null}; }
        }
        """
        try:
            res = self.page.evaluate(js, url)
        except Exception as e:
            LOG.write(f"    [api] evaluate 失败 {path}: {e}")
            return None
        if not res or res.get("status") != 200 or res.get("body") is None:
            LOG.write(f"    [api] 请求失败 {path} -> {res and res.get('status')}")
            return None
        return res["body"]

    @staticmethod
    def payload(body):
        """平台响应有两种形态: {code,data,...} 或 {count,data}; 统一取 data"""
        if body is None:
            return None
        if isinstance(body, dict):
            return body.get("data", body)
        return body

    def token(self):
        try:
            return self.page.evaluate(
                "() => sessionStorage.getItem('token') || "
                "localStorage.getItem('token')")
        except Exception:
            return None

    def wait_login(self, prompt_every=30):
        if self.token():
            LOG.write("检测到已登录 (会话保留), 继续")
            return
        LOG.write("=" * 60)
        LOG.write("请在刚刚打开的浏览器窗口中登录北京干部网络学院")
        LOG.write("(登录一次即可, 之后会话会自动保留)")
        LOG.write("=" * 60)
        last = 0
        while not self.token():
            self.page.wait_for_timeout(2000)
            if time.time() - last > prompt_every:
                LOG.write("等待登录中 ...")
                last = time.time()

    # ---- 学时 ----
    def get_scores(self):
        year = datetime.now().year
        must = select = None
        # 途径1: 年度学时汇总接口
        body = self.api_get(
            f"api-course/portal/open/personal/getStudyHourDetail?year={year}")
        d = self.payload(body)
        if isinstance(d, dict):
            must = d.get("mustScore")
            select = d.get("selectScore")
        # 途径2: 按已通过课程明细累加 (必修=1 选修=0)
        if must is None or select is None:
            must2 = select2 = 0.0
            for ctype, flag in ((1, "must"), (0, "select")):
                body = self.api_get(
                    "api-course/portal/open/personal/listCourseDetail"
                    f"?searchCourseType={ctype}&searchPassYear={year}"
                    "&currentPage=1&pageSize=1000")
                rows = self.payload(body) or []
                if isinstance(rows, dict):
                    rows = rows.get("data") or []
                s = 0.0
                for r in rows:
                    if isinstance(r, dict):
                        try:
                            s += float(r.get("learningHour") or 0)
                        except (TypeError, ValueError):
                            pass
                if flag == "must":
                    must2 = s
                else:
                    select2 = s
            if must is None:
                must = must2
            if select is None:
                select = select2
        try:
            must = float(must or 0)
        except (TypeError, ValueError):
            must = 0.0
        try:
            select = float(select or 0)
        except (TypeError, ValueError):
            select = 0.0
        return must, select

    # ---- 课程列表 ----
    def _norm_row(self, r):
        if not isinstance(r, dict):
            return None
        course = r.get("course") if isinstance(r.get("course"), dict) else {}
        cid = r.get("courseID") or r.get("courseId") or course.get("courseID")
        if not cid:
            return None
        dur = r.get("courseDuration") or course.get("courseDuration") or 0
        try:
            dur = float(dur)
        except (TypeError, ValueError):
            dur = 0.0
        return {
            "id": str(cid),
            "name": r.get("courseName") or course.get("courseName") or "",
            "type": r.get("courseType") or course.get("courseType") or 0,
            "progress": norm_progress(
                r.get("learningProgress") if r.get("learningProgress") is not None
                else course.get("learningProgress")),
            "duration": dur,
            "hours": r.get("courseLearningHour")
                     or course.get("courseLearningHour") or 0,
            "userCourseID": r.get("userCourseID") or r.get("userCourseId") or "",
            "passState": r.get("passState"),
        }

    def fetch_my_courses(self, learn_state):
        """learn_state: 2=学习中, 1=已完成"""
        rows = []
        page_no = 1
        while page_no <= 10:
            body = self.api_get(
                "api-course/portal/usercourse/listMyCourse"
                f"?searchLearnState={learn_state}&searchCourseYear="
                f"&pageSize=100&currentPage={page_no}")
            data = self.payload(body)
            if isinstance(data, dict):
                total = data.get("count", 0)
                data = data.get("data") or []
            else:
                total = len(data)
            if not data:
                break
            rows.extend(data)
            if len(rows) >= (total or 0):
                break
            page_no += 1
        out = []
        for r in rows:
            n = self._norm_row(r)
            if n:
                out.append(n)
        return out

    def is_marked_done(self, cid):
        """平台是否已将该课标记为"已完成"(listMyCourse searchLearnState=1)"""
        cid = str(cid)
        page_no = 1
        while page_no <= 10:
            try:
                body = self.api_get(
                    "api-course/portal/usercourse/listMyCourse"
                    "?searchLearnState=1&searchCourseYear="
                    f"&pageSize=100&currentPage={page_no}")
            except Exception:
                return False
            data = self.payload(body)
            if isinstance(data, dict):
                data = data.get("data") or []
            if not isinstance(data, list) or not data:
                return False
            for r in data:
                n = self._norm_row(r)
                if n and n["id"] == cid:
                    return True
            page_no += 1
        return False

    def fetch_library(self, course_type):
        """选课中心课程库: course_type 2=必修 1=选修"""
        rows = []
        page_no = 1
        while page_no <= 5:
            body = self.api_get(
                "api-course/portal/open/usercourse/listCourse"
                f"?searchCourseType={course_type}&searchHasChild=1"
                f"&pageSize=100&currentPage={page_no}")
            data = self.payload(body)
            total = 0
            if isinstance(data, dict):
                total = data.get("count", 0)
                data = data.get("data") or []
            if not data:
                break
            rows.extend(data)
            if len(rows) >= (total or 0):
                break
            page_no += 1
        out = []
        for r in rows:
            n = self._norm_row(r)
            if n and n["progress"] < PROGRESS_PASS:
                out.append(n)
        return out

    def fetch_course_detail(self, cid):
        body = self.api_get(f"api-course/portal/open/course/{cid}")
        return self.payload(body)

    # ---- 播放控制 ----
    def read_state(self):
        try:
            return self.page.evaluate(STATE_JS)
        except Exception:
            return {"hasVideo": False, "paused": True, "ended": False,
                    "t": 0, "d": 0, "chapters": 0, "activeIdx": -1,
                    "alert": False, "busy": False, "modalText": ""}

    def try_play(self):
        try:
            r = self.page.evaluate(PLAY_JS, MUTE_VIDEO)
        except Exception as e:
            r = f"err:{e}"
        if r == "ok":
            return True
        # 退化: 尝试点击 xgplayer 播放按钮
        for sel in (".xgplayer-start", ".xgplayer-play"):
            try:
                btn = self.page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click(timeout=3000)
                    return True
            except Exception:
                pass
        return False

    def _refresh_and_nudge(self):
        """播放结束后使用: 刷新播放页并重新播放 3 秒,
        触发新一轮学习上报(userLearningFlow), 促使服务端结算课程最新进度,
        之后再校验进度"""
        try:
            LOG.write("  ↻ 刷新播放页 ...")
            self.page.reload(timeout=60000)
        except Exception as e:
            LOG.write(f"  ⚠ 刷新页面失败(忽略): {str(e)[:100]}")
            return
        self.page.wait_for_timeout(4000)
        # 防止"正在学习"冲突弹窗挡住播放器
        try:
            self.page.evaluate(HIDE_BUSY_MODAL_JS)
        except Exception:
            pass
        try:
            self.page.wait_for_selector("video", timeout=30000)
        except PWTimeout:
            LOG.write("  ⚠ 刷新后未见视频元素(忽略)")
            return
        # 重新播放 3 秒
        LOG.write("  ▶ 重新播放 3 秒, 触发服务端进度结算 ...")
        if self.try_play():
            self.page.wait_for_timeout(3000)
            try:
                self.page.evaluate(PAUSE_JS)  # 暂停以落地本轮上报(PUT)
            except Exception:
                pass
            self.page.wait_for_timeout(3000)
        else:
            LOG.write("  ⚠ 重新播放未成功(忽略)")
            self.page.wait_for_timeout(2000)

    def open_course(self, cid):
        """打开播放页, 返回 playinfo 数据 (失败返回 None)"""
        self.sniffed.pop("playinfo", None)
        self.sniffed.pop("chapters", None)
        self.recent.clear()
        landed = False
        for _ in range(3):
            self.page.goto("about:blank")
            self.page.goto(f"{BASE}/#/course/coursePlay?courseID={cid}",
                           timeout=60000)
            self.page.wait_for_timeout(3000)
            if "coursePlay" in self.page.url:
                landed = True
                break
            # 被重定向回首页: 登录态失效, 等待重新登录
            LOG.write("  ⚠ 未落在播放页, 登录态可能失效, 请在浏览器中重新登录 ...")
            self.page.goto(BASE + "/#/index", timeout=60000)
            self.wait_login()
        if not landed:
            self.dump_page_state(f"redirect_{cid[:8]}")
            return None
        info = self.wait_sniff("playinfo", 60)
        if not info:
            self.dump_page_state(f"noPlayinfo_{cid[:8]}")
            return None
        return self.payload(info)

    def click_chapter(self, idx):
        """点击目录中的第 idx 章(从0开始)
        等待元素可见(限时) → 普通点击; 元素隐藏/被遮挡时用 JS 点击兜底"""
        try:
            # 限时等待目标章节可见(菜单可能异步渲染/折叠)
            deadline = time.time() + 10
            lis = []
            while time.time() < deadline:
                lis = self.page.query_selector_all(
                    ".iv-course-play-detail-menu-item"
                    ":not(.iv-course-play-detail-menu-cour) li")
                if 0 <= idx < len(lis):
                    try:
                        if lis[idx].is_visible():
                            break
                    except Exception:
                        pass
                self.page.wait_for_timeout(500)
            if not (0 <= idx < len(lis)):
                LOG.write(f"    [章节] 未找到第 {idx + 1} 章")
                return False
            try:
                lis[idx].scroll_into_view_if_needed(timeout=5000)
                lis[idx].click(timeout=5000)
                return True
            except Exception:
                # actionability 检查失败(元素隐藏/被弹窗遮挡):
                # 直接派发 DOM click 事件, Vue 处理器仍会执行切章
                lis[idx].evaluate("e => e.click()")
                LOG.write(f"    [章节] 第 {idx + 1} 章不可见, 已用JS点击切换")
                return True
        except Exception as e:
            LOG.write(f"    [章节] 点击失败: {e}")
        return False

    def play_course(self, cid, name=""):
        """完整播放一门课程, 返回 True 表示已尽力播完"""
        LOG.write(f"  打开课程: {name} ({cid})")
        info = self.open_course(cid)
        ctype = None
        if info:
            ctype = info.get("coursewareType")
        else:
            # 兜底: 未嗅探到 playinfo, 但若视频元素已出现仍尝试播放
            try:
                self.page.wait_for_selector("video", timeout=15000)
            except PWTimeout:
                LOG.write("  ✗ 未获取到播放信息且无视频元素, 跳过")
                return False
            LOG.write("  ⚠ 未嗅探到 playinfo, 但视频元素存在, 尝试直接播放")

        if ctype is not None and str(ctype) != "4":
            # 非视频课件(AICC/文档): 单章节课程直接跳过;
            # 多章节/多集课程继续尝试(监控循环会自动切换到有视频的章节)
            st0 = self.read_state()
            if st0.get("chapters", 0) <= 1:
                LOG.write(f"  ✗ 课件类型 coursewareType={ctype}, 非视频课件"
                          "(AICC/文档), 暂不支持自动播放, 跳过")
                return False
            LOG.write(f"  ⚠ 首课件类型 coursewareType={ctype} 非视频, "
                      f"共 {st0['chapters']} 章/集, 尝试切换后续章节")

        try:
            self.page.wait_for_selector("video", timeout=30000)
        except PWTimeout:
            LOG.write("  ✗ 页面未出现视频元素, 跳过")
            return False

        LOG.write("  ▶ 开始播放")
        stall_start = None
        stall_t = 0
        busy_count = 0
        play_fail = 0
        no_video_since = None
        finished = False
        last_log = 0
        # 完成条件轮询: 每 60 秒检查"已学完/进度>=80%", 满足立即切下一课
        # (首次检查在开播后立即执行一次, 防止课程开局即已完成仍全程重看)
        last_check = time.time() - 60

        while not finished:
            st = self.read_state()

            # 每 60 秒检查完成条件: 已学完 或 进度>=80% → 立即切下一课
            if time.time() - last_check >= 60:
                last_check = time.time()
                prog, done = self._completion_status(cid)
                if done or prog >= PROGRESS_PASS:
                    why = "平台已标记已学完" if done \
                        else f"进度已达 {prog:.0f}%"
                    LOG.write(f"  ✓ 播放中检测到{why}, 切换下一课")
                    try:
                        self.page.evaluate(PAUSE_JS)  # 暂停落地当前上报
                    except Exception:
                        pass
                    self.page.wait_for_timeout(8000)
                    return True

            # "正在学习"弹窗: 有其他会话在播放, 隐藏弹窗并等待重试
            if st.get("busy"):
                busy_count += 1
                LOG.write(f"  ⚠ 检测到\"正在学习\"弹窗(可能有其他窗口/会话在播放), "
                          f"第 {busy_count} 次, 等待 120 秒后重试")
                self.page.evaluate(HIDE_BUSY_MODAL_JS)
                if busy_count >= 6:
                    LOG.write("  ✗ 连续多次\"正在学习\", 放弃本课")
                    return False
                self.page.wait_for_timeout(120000)
                continue
            busy_count = 0

            # 关闭"自动停止播放"提示框
            if st.get("alert"):
                try:
                    self.page.evaluate(CLOSE_ALERT_JS)
                    LOG.write("  ⚠ 出现\"自动停止播放\"提示, 已关闭")
                except Exception:
                    pass

            if not st.get("hasVideo"):
                # 无视频元素: 课程包中可能混有文档/AICC 类型的集
                if no_video_since is None:
                    no_video_since = time.time()
                elif time.time() - no_video_since > 30:
                    nxt = st["activeIdx"] + 1
                    if (st["activeIdx"] >= 0 and st["chapters"]
                            and nxt < st["chapters"]):
                        LOG.write(f"  ⚠ 无视频元素(30秒), "
                                  f"切换第 {nxt + 1} 章/集")
                        self.click_chapter(nxt)
                        self.page.wait_for_timeout(6000)
                        self.sniffed.pop("playinfo", None)
                        no_video_since = None
                        continue
                    if time.time() - no_video_since > 180:
                        LOG.write("  ✗ 长时间无视频元素(可能未登录或页面异常), "
                                  "跳过本课")
                        return False
                self.page.wait_for_timeout(SCAN_INTERVAL * 1000)
                continue
            no_video_since = None

            # 播完当前章节 → 切下一章
            if st.get("ended"):
                nxt = st["activeIdx"] + 1
                if st["chapters"] and nxt < st["chapters"]:
                    LOG.write(f"  ▸ 第 {st['activeIdx'] + 1} 章播完, "
                              f"切换第 {nxt + 1} 章 / 共 {st['chapters']} 章")
                    self.click_chapter(nxt)
                    self.page.wait_for_timeout(6000)
                    self.sniffed.pop("playinfo", None)
                    self.try_play()
                    stall_start = None
                else:
                    LOG.write("  ✓ 全部章节播放完毕")
                    finished = True
                continue

            # 暂停 → 续播
            if st.get("paused"):
                if self.try_play():
                    LOG.write("  ↻ 检测到暂停, 已自动续播")
                    stall_start = None
                    play_fail = 0
                else:
                    play_fail += 1
                    if play_fail > 60:  # 约 5 分钟仍无法续播
                        LOG.write("  ✗ 多次尝试无法续播, 跳过本课")
                        return False

            # 卡住检测: 有视频、未结束, 但时间长时间不前进
            cur = st.get("t", 0)
            if cur > stall_t + 5:
                stall_t = cur
                stall_start = None
            elif st.get("paused") is False and cur <= stall_t + 5:
                if stall_start is None:
                    stall_start = time.time()
                elif time.time() - stall_start > CHUNK_STALL_MIN * 60:
                    LOG.write(f"  ✗ 超过 {CHUNK_STALL_MIN} 分钟无进展, 跳过本课")
                    return False

            # 进度日志
            now = time.time()
            if now - last_log > 60:
                LOG.write(f"  … 播放中 {fmt_sec(st.get('t'))} / "
                          f"{fmt_sec(st.get('d'))}  第 {st['activeIdx'] + 1} 章/"
                          f"{st['chapters']} 章")
                last_log = now

            self.page.wait_for_timeout(SCAN_INTERVAL * 1000)

        # 退出前暂停一次, 触发站点的进度上报(PUT userLearningFlow)
        try:
            self.page.evaluate(PAUSE_JS)
        except Exception:
            pass
        self.page.wait_for_timeout(8000)

        # 刷新播放页并重新播放3秒(触发服务端结算), 再校验进度
        self._refresh_and_nudge()

        # 校验: 进度>=80% 或 平台标记已学完, 满足其一即视为完成
        detail = self.fetch_course_detail(cid)
        if not isinstance(detail, dict):
            LOG.write("  ⚠ 未能获取课程详情, 视为本课完成(避免误重播)")
            return True
        prog = norm_progress(detail.get("learningProgress"))
        LOG.write(f"  ◉ 课程学习进度: {prog:.0f}%")
        if prog >= PROGRESS_PASS:
            LOG.write(f"  ✓ 进度已达 {PROGRESS_PASS:g}%, 本课完成")
            return True
        if self.is_marked_done(cid):
            LOG.write("  ✓ 平台已标记该课为已学完, 本课完成")
            return True
        # 两者均不满足, 补播一轮
        LOG.write(f"  ↻ 进度未达 {PROGRESS_PASS:g}% 且未标记已学完, 再播一轮")
        return self._replay_incomplete(cid, name)

    def _completion_status(self, cid):
        """轮询用: 返回 (进度百分比, 是否已学完)。查询失败按未完成处理"""
        prog = 0.0
        try:
            detail = self.fetch_course_detail(cid)
            if isinstance(detail, dict):
                prog = norm_progress(detail.get("learningProgress"))
        except Exception:
            pass
        done = False
        if prog < PROGRESS_PASS:
            done = self.is_marked_done(cid)
        return prog, done

    def _replay_incomplete(self, cid, name):
        info = self.open_course(cid)
        if not info:
            return False
        try:
            self.page.wait_for_selector("video", timeout=30000)
        except PWTimeout:
            return False
        # 从第一章开始再走一遍; 每分钟检查完成条件, 满足即提前结束
        LOG.write("  ▶ 重播开始 (每分钟检查: 已学完 或 进度≥"
                  f"{PROGRESS_PASS:g}%, 满足即切下一课)")
        deadline = time.time() + 3 * 3600
        last_chapter = -2
        last_check = time.time()
        no_video_since = None
        while time.time() < deadline:
            # 每 60 秒检查一次完成条件
            if time.time() - last_check >= 60:
                last_check = time.time()
                prog, done = self._completion_status(cid)
                if done or prog >= PROGRESS_PASS:
                    why = "平台已标记已学完" if done \
                        else f"进度已达 {prog:.0f}%"
                    LOG.write(f"  ✓ 补播中检测到{why}, 提前结束, 播放下一课")
                    try:
                        self.page.evaluate(PAUSE_JS)  # 暂停落地当前上报
                    except Exception:
                        pass
                    self.page.wait_for_timeout(8000)
                    return True
            st = self.read_state()
            if st.get("busy"):
                self.page.evaluate(HIDE_BUSY_MODAL_JS)
                self.page.wait_for_timeout(60000)
                continue
            if st.get("alert"):
                try:
                    self.page.evaluate(CLOSE_ALERT_JS)
                except Exception:
                    pass
            if not st.get("hasVideo"):
                # 无视频(文档/AICC 集 或切换暂态): 30 秒后切换下一章/集
                if no_video_since is None:
                    no_video_since = time.time()
                elif time.time() - no_video_since > 30:
                    nxt = st["activeIdx"] + 1
                    if (st["activeIdx"] >= 0 and st["chapters"]
                            and nxt < st["chapters"]):
                        LOG.write(f"  ⚠ 补播遇无视频章节, 切换第 {nxt + 1} 章/集")
                        self.click_chapter(nxt)
                        self.page.wait_for_timeout(6000)
                        self.sniffed.pop("playinfo", None)
                        no_video_since = None
                self.page.wait_for_timeout(SCAN_INTERVAL * 1000)
                continue
            no_video_since = None
            if st.get("ended"):
                nxt = st["activeIdx"] + 1
                if st["chapters"] and nxt < st["chapters"]:
                    self.click_chapter(nxt)
                    self.page.wait_for_timeout(6000)
                    self.try_play()
                else:
                    break
            elif st.get("paused"):
                self.try_play()
            if st["activeIdx"] != last_chapter:
                last_chapter = st["activeIdx"]
                LOG.write(f"  … 重播中, 当前第 {last_chapter + 1} 章")
            self.page.wait_for_timeout(SCAN_INTERVAL * 1000)
        try:
            self.page.evaluate(PAUSE_JS)
        except Exception:
            pass
        self.page.wait_for_timeout(8000)
        # 补播结束后: 刷新播放页并重新播放3秒, 再以服务端进度为准
        self._refresh_and_nudge()
        detail = self.fetch_course_detail(cid)
        prog = norm_progress(
            detail.get("learningProgress")) if isinstance(detail, dict) else 0
        LOG.write(f"  ◉ 重播后课程进度: {prog:.0f}%")
        if prog >= PROGRESS_PASS:
            return True
        if self.is_marked_done(cid):
            LOG.write("  ✓ 平台已标记该课为已学完, 本课完成")
            return True
        return False


# ----------------------------- 主流程 -----------------------------
def pick_browser_channel_ok(site: Site) -> bool:
    try:
        site.start()
        return True
    except Exception as e:
        LOG.write(f"浏览器启动失败: {e}")
        traceback.print_exc()
        return False


def build_queue(site: Site, need_must: bool, need_select: bool):
    must_q, elec_q = [], []
    seen = set()

    def add(rows, queue):
        for r in rows:
            if r["id"] in seen:
                continue
            if r["duration"] > MAX_COURSE_MIN * 60:
                continue
            if r["progress"] >= PROGRESS_PASS:
                continue  # 进度已达80%视为完成, 不再重复播放
            seen.add(r["id"])
            queue.append(r)

    learning = site.fetch_my_courses(learn_state=2)
    add([r for r in learning if r["type"] == 2], must_q)
    add([r for r in learning if r["type"] == 1], elec_q)
    LOG.write(f"我的课程(学习中): 必修 {len(must_q)} 门, 选修 {len(elec_q)} 门")

    if need_must and len(must_q) < 5:
        lib = site.fetch_library(course_type=2)
        add(lib, must_q)
        LOG.write(f"选课中心补充必修: 现有 {len(must_q)} 门候选")
    if need_select and len(elec_q) < 15:
        lib = site.fetch_library(course_type=1)
        add(lib, elec_q)
        LOG.write(f"选课中心补充选修: 现有 {len(elec_q)} 门候选")

    # 过滤不适合自动播放的课程(考试课/外链课)
    def playable(queue):
        out = []
        for r in queue:
            detail = site.fetch_course_detail(r["id"])
            if not isinstance(detail, dict):
                out.append(r)  # 查不到详情不拦截, 播放页再判断
                continue
            if detail.get("isContainExam") == 1:
                LOG.write(f"    跳过含考试课程: {r['name']}")
                continue
            if detail.get("courseResourceUrl"):
                LOG.write(f"    跳过外链课程: {r['name']}")
                continue
            # setType=3 课程包(多集课程)不再跳过: 已实测可通过菜单切集自动播放
            out.append(r)
        return out

    return playable(must_q), playable(elec_q)


def run(site: Site):
    site.wait_login()
    round_no = 0
    while True:
        round_no += 1
        # 登录态中途失效则等待重新登录
        if not site.token():
            LOG.write("⚠ 登录态已失效, 请在浏览器中重新登录 ...")
            site.wait_login()
        must, select = site.get_scores()
        LOG.write("=" * 60)
        LOG.write(f"第 {round_no} 轮 | 当前年度学时: 必修 {must:g} / {TARGET_MUST:g}, "
                  f"选修 {select:g} / {TARGET_SELECT:g}")
        save_state({"time": datetime.now().isoformat(),
                    "mustScore": must, "selectScore": select,
                    "target": {"must": TARGET_MUST, "select": TARGET_SELECT}})

        if must >= TARGET_MUST and select >= TARGET_SELECT:
            LOG.write("🎉 已达成目标: 必修 + 选修 >= 50 学时, 任务完成!")
            return

        need_must = must < TARGET_MUST
        need_select = select < TARGET_SELECT
        must_q, elec_q = build_queue(site, need_must, need_select)

        played_any = False
        queues = []
        if need_must:
            queues.append(("必修", must_q))
        if need_select:
            queues.append(("选修", elec_q))

        for label, queue in queues:
            for r in queue:
                # 播放前再查一次学时, 达标即停
                m, s = site.get_scores()
                if m >= TARGET_MUST and s >= TARGET_SELECT:
                    LOG.write("🎉 播放中途检测到达成目标!")
                    return
                if label == "必修" and m >= TARGET_MUST:
                    break
                if label == "选修" and s >= TARGET_SELECT:
                    break
                LOG.write(f"[{label}] 剩余目标: "
                          f"{max(TARGET_MUST - m, 0):g} 必修 / "
                          f"{max(TARGET_SELECT - s, 0):g} 选修 学时")
                try:
                    site.play_course(r["id"], r["name"])
                    played_any = True
                except Exception as e:
                    LOG.write(f"  ✗ 播放异常, 跳过该课: {e}")
                    traceback.print_exc()
                    try:
                        site.page.goto(BASE + "/#/index", timeout=60000)
                    except Exception:
                        pass

        if not played_any:
            LOG.write("⚠ 本轮没有可播放的课程, 5 分钟后重试"
                      "(可在网站中手动选课后再让脚本继续)")
            time.sleep(300)
        else:
            time.sleep(10)


def main():
    LOG.write("北京干部网络学院自动播放脚本启动")
    LOG.write(f"目标: 必修 {TARGET_MUST:g} + 选修 {TARGET_SELECT:g} = "
              f"{TARGET_MUST + TARGET_SELECT:g} 学时")
    site = Site()
    if not pick_browser_channel_ok(site):
        sys.exit(1)
    try:
        run(site)
    except KeyboardInterrupt:
        LOG.write("收到中断, 退出")
    except Exception as e:
        LOG.write(f"发生未预期错误: {e}")
        traceback.print_exc()
    finally:
        try:
            site.page.screenshot(path=str(HERE / "final_screenshot.png"))
        except Exception:
            pass
        site.close()
        LOG.write("浏览器已关闭, 脚本退出")


if __name__ == "__main__":
    main()
