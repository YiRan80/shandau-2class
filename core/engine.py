"""
抢课引擎 —— 基于零依赖 CDP 客户端驱动本机浏览器，走正常页面交互。

整合三个改进方案：
  方案1 入口自动发现 + 会话长驻（core/discovery.py）
  方案2 分阶段就绪 S1-S5 + 登录态失效恢复（本文件状态机）
  方案3 定时/守候双模式 + 课程出现检测（core/watcher.py）

合规护栏（内置，不可通过配置绕过）：
1. 单账号单实例；2. 轮询间隔下限 1 秒；3. 遇验证码暂停等待人工；
4. 只做页面内点击/填表，不伪造签名、不直连私有接口；
5. 防重复提交；6. 全量日志；7. 可随时停止。
"""
import os
import sys
import time
import json
import threading
import datetime
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp_client import CDPClient, CDPError
from common import (EventLog, InstanceLock, load_config, public_config,
                    STATE_PATH, BASE_DIR, append_results, WATCH_MODE_ENABLED)
from discovery import EntryDiscovery, DiscoveryError
from watcher import (CourseWatcher, SCAN_JS, AVAILABLE_TAB_JS, parse_courses,
                     course_key, is_available, next_reg_start, selected_courses,
                     parse_dt)

# 静默拉起调试浏览器用到的 CDP 技能脚本目录（可选依赖）：
# 可用环境变量 CDP_SKILL_DIR 覆盖，默认取用户主目录下的 CDP 技能脚本。
SKILL_SCRIPTS = os.environ.get(
    "CDP_SKILL_DIR",
    os.path.join(os.path.expanduser("~"), ".workbuddy", "skills",
                 "browser-automation-cdp", "scripts"),
)
START_CHROME = os.path.join(SKILL_SCRIPTS, "start_chrome.py")


class EngineState:
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"      # 等待人工处理（如验证码 / 选角色）
    STOPPING = "stopping"


# 五阶段就绪
STAGE_LABELS = {
    "idle": "未启动", "s1": "发现入口中", "s2": "打开页面中", "s3": "待登录",
    "s4": "确认角色中", "s5": "确认课程态", "ready": "就绪", "human": "待人工处理"
}

# 提交失败的课程在该冷却期内不再重复点击（防刷屏），冷却后允许再试
FAIL_RETRY_COOLDOWN = 60.0

# 到点后仍忽略“已报满”显示的宽限期：活动刚开放时列表余量往往还没刷新
REG_GRACE_SECONDS = 600.0


def _js_string(s):
    return json.dumps(s, ensure_ascii=False)


class GrabbingEngine:
    def __init__(self, log=None):
        self.log = log or EventLog()
        self.lock = InstanceLock()
        self.state = EngineState.STOPPED
        self._thread = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self.courses = []
        self.submitted = {}          # key -> 结果，防重复
        self.stats = {
            "scans": 0, "found": 0, "submitted": 0,
            "failed": 0, "retries": 0, "started_at": None
        }
        self.client = None
        self.target_id = None
        self._state_lock = threading.Lock()
        self.login_status = {"code": "idle", "label": "未启动", "detail": "点击启动抢课后自动检测"}
        # 方案2：就绪阶段与暂停恢复点
        self.stage = "idle"
        self._resume_stage = None
        # 方案1/3：发现器、观察器、运行上下文
        self.discovery = None
        self.watcher = CourseWatcher(self.log)
        self.entry_source = "none"
        self.mode = "both" if WATCH_MODE_ENABLED else "scheduled"  # 守候隐藏时启动前也显示定时
        self._burst = False
        self._burst_until = 0.0
        self._miss_streak = 0
        # 目标课程「报名开始时间」到点抢课上下文
        self.next_due = None      # {"key","title","at","remain"} 供界面倒计时
        self._due_logged = set()  # 已播报过“开始守候”的课程键
        self._due_done = set()    # 已到点并尝试过的课程键
        # 界面「按筛选条件刷新」：置位后主循环立刻重读配置并重新抓取
        self._rescan_event = threading.Event()
        # 未确认任何目标课程时只提示一次，避免 10 秒一条刷屏
        self._no_sel_logged = False
        self._hit_logged = 0      # 上次播报“已确认 N 门 / 可抢 M 门”的数量，变化时才再播报

    # ---------- 状态管理 ----------
    def _set_state(self, s, detail=""):
        with self._state_lock:
            self.state = s
        self.log.info("状态变化 -> %s %s" % (s, ("(" + detail + ")") if detail else ""))
        self._persist_state()

    def _set_stage(self, stage):
        self.stage = stage
        self.login_status = {
            "code": stage,
            "label": STAGE_LABELS.get(stage, stage),
            "detail": self._stage_detail(stage)
        }
        self._persist_state()

    def _stage_detail(self, stage):
        return {
            "idle": "点击启动抢课后自动检测", "s1": "从校园锚点自动定位抢课入口",
            "s2": "等待页面加载完成", "s3": "请在浏览器完成登录（验证码需人工）",
            "s4": "检测到角色选择，正在确认学生身份", "s5": "正在读取活动列表",
            "ready": "已进入活动中心，开始监测", "human": "等待人工处理后继续"
        }.get(stage, "")

    def get_status(self):
        return {
            "state": self.state,
            "state_label": {
                EngineState.STOPPED: "未启动", EngineState.STARTING: "启动中",
                EngineState.RUNNING: "运行中", EngineState.PAUSED: "已暂停·待人工处理",
                EngineState.STOPPING: "停止中",
            }.get(self.state, self.state),
            "login": dict(self.login_status),
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, self.stage),
            "entry_source": self.entry_source,
            "mode": self.mode,
            "burst": self._burst,
            "burst_remain": max(0, int(self._burst_until - time.time())) if self._burst else 0,
            "stats": dict(self.stats),
            "courses": list(self.courses),
            "submitted": dict(self.submitted),
            "selected": list((load_config().get("target", {}) or {}).get("selected") or []),
            "due": dict(self.next_due) if self.next_due else None,
            "config": public_config(load_config()),
            "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    def _persist_state(self):
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.get_status(), f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ---------- 生命周期 ----------
    def start(self):
        if self.state in (EngineState.STARTING, EngineState.RUNNING, EngineState.PAUSED):
            return {"ok": False, "error": "引擎已在运行"}
        if not self.lock.acquire():
            return {"ok": False, "error": "另一个实例正在运行"}
        self._stop_event.clear()
        self._pause_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return {"ok": True}

    def stop(self):
        if self.state == EngineState.STOPPED:
            return {"ok": True}
        self._set_state(EngineState.STOPPING)
        self._stop_event.set()
        self._pause_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=8)
        self._set_state(EngineState.STOPPED)
        self._set_stage("idle")
        self.login_status = {"code": "idle", "label": "未启动", "detail": "点击启动抢课后自动检测"}
        self.lock.release()
        return {"ok": True}

    def resolve_captcha(self):
        """人工处理完验证码后调用，解除暂停并回到原阶段。"""
        if self.state == EngineState.PAUSED:
            self._pause_event.set()
            self.log.info("人工已处理，恢复运行")
            return {"ok": True}
        return {"ok": False, "error": "当前非暂停状态"}

    def rediscover(self):
        """手动触发重新发现入口（下次循环生效，不阻塞调用）。"""
        self._force_rediscover = True
        self.log.info("已标记重新发现入口")
        return {"ok": True}

    def rescan(self):
        """界面「按筛选条件刷新」：让主循环立刻重读配置并重新抓取课程与余量。"""
        if self.state in (EngineState.STOPPED, EngineState.STOPPING):
            return {"ok": False, "error": "引擎未启动，筛选条件已保存，启动后生效"}
        self._rescan_event.set()
        self.log.info("收到「按筛选条件刷新」请求，重新抓取课程与余量")
        return {"ok": True}

    def _wait_or_stop(self, seconds):
        return self._stop_event.wait(seconds)

    def _pause_for_human(self, detail):
        """暂停等待人工，返回是否因停止而中断。"""
        self._set_state(EngineState.PAUSED, detail)
        self._set_stage("human")
        self.login_status = {"code": "human", "label": "待人工处理", "detail": detail}
        while not self._stop_event.is_set() and not self._pause_event.is_set():
            time.sleep(0.4)
        interrupted = self._stop_event.is_set()
        self._pause_event.clear()
        if not interrupted:
            self._set_state(EngineState.RUNNING)
        return interrupted

    # ---------- 浏览器控制 ----------
    def _spawn_browser_hidden(self):
        if not os.path.exists(START_CHROME):
            raise RuntimeError(
                "没有可用的调试浏览器，也找不到 start_chrome.py（%s）。\n"
                "请任选一种方式：①手动用 Edge/Chrome 以 --remote-debugging-port=9222 启动；"
                "②把 CDP 技能脚本放到默认目录，或用环境变量 CDP_SKILL_DIR 指向它。" % START_CHROME)
        cmd = [sys.executable, START_CHROME]
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                  "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        subprocess.Popen(cmd, **kwargs)

    def _attach_browser(self):
        """确保浏览器可用并建立页面连接，复用现有标签（不新建、不关闭）。"""
        self.client = CDPClient(port=9222, timeout=15)
        if not self.client.is_alive():
            self.log.info("未发现调试浏览器，正在静默启动 Edge ...")
            self._spawn_browser_hidden()
            for _ in range(30):
                if self.client.is_alive():
                    break
                time.sleep(1)
            else:
                raise RuntimeError("浏览器启动失败，请确认 Edge 可用")
        pages = self.client.list_pages()
        if not pages:
            self.client.connect_browser()
            created = self.client.send("Target.createTarget", {"url": "about:blank"})
            self.client.close()
            time.sleep(0.4)
            pages = [{"id": created.get("targetId"), "url": "about:blank"}]
        page = pages[0]
        for p in pages:
            if (p.get("url") or "").strip() in ("about:blank", ""):
                page = p
                break
        self.target_id = page["id"]
        self.client.connect_page(self.target_id)   # 先建连再操作
        self.log.info("已建立 CDP 页面连接：%s" % self.target_id)

    def _close_blank_tabs(self):
        try:
            helper = CDPClient(port=9222, timeout=10)
            helper.connect_browser()
            for p in helper.list_pages():
                if p.get("id") != self.target_id and (p.get("url") or "").strip() == "about:blank":
                    helper.send("Target.closeTarget", {"targetId": p["id"]})
            helper.close()
        except Exception as e:
            self.log.warn("清理空白标签跳过：%s" % e)

    @staticmethod
    def _is_nav_race(exc):
        msg = str(exc)
        return ("-32000" in msg and ("navigated" in msg or "closed" in msg)) \
            or "Inspected target navigated" in msg \
            or "Execution context was destroyed" in msg

    def _eval(self, expression, timeout=15):
        for attempt in range(2):
            try:
                return self.client.evaluate(expression, timeout=timeout)
            except Exception as e:
                if self._is_nav_race(e):
                    time.sleep(0.6)
                    continue
                raise
        return {}

    def _eval_value(self, expression, timeout=15):
        try:
            return self._eval(expression, timeout).get("value")
        except AttributeError:
            return None

    # ---------- 方案2：五阶段就绪 ----------
    def _prepare(self, cfg, skip_discover=False):
        """S1-S5 就绪流程；返回 True 表示就绪，False 表示被停止。

        skip_discover=True 表示调用方已完成发现并定位到目标页，只做 S2-S5。
        """
        if not skip_discover:
            # S1 发现入口
            self._set_stage("s1")
            self.discovery = EntryDiscovery(self.client, self.log, self._eval_value, self._eval)
            force = getattr(self, "_force_rediscover", False)
            self._force_rediscover = False
            self.watcher.reset()
            try:
                result = self.discovery.discover(cfg, self.target_id, force=force)
            except DiscoveryError as e:
                self.log.error("入口发现失败：%s" % e)
                return False
            # 发现过程可能切换到新标签页/跨源 iframe，同步引擎持有的页面 id
            if getattr(self.client, "target_id", None):
                self.target_id = self.client.target_id
            self.entry_source = result.get("source", "unknown")
            if result.get("source") == "need_human":
                if self._pause_for_human("发现阶段需人工登录/验证码，处理后点继续"):
                    return False
                return self._prepare_after_human(cfg)

        # S2 打开完成
        self._set_stage("s2")
        for _ in range(10):
            ready = self._eval_value("document.readyState")
            if ready == "complete":
                break
            time.sleep(0.5)
        self._close_blank_tabs()

        # S3 登录（自动填账号密码并提交；只有验证码/缺配置才转人工）
        self._set_stage("s3")
        for _attempt in range(2):
            f = self.discovery.features()
            if not (f.get("pwd") or f.get("captcha")):
                break
            res = self._auto_login(cfg)
            if res.get("status") != "ok":
                if self._pause_for_human("自动登录需人工：%s，处理后点继续"
                                         % (res.get("reason") or "未完成")):
                    return False
                return self._prepare_after_human(cfg)
            # 登录成功后页面通常回到门户首页（不是抢课页）：重新走一遍入口发现
            self.log.info("登录成功，重新定位抢课入口")
            self._set_stage("s1")
            try:
                result = self.discovery.discover(cfg, self.target_id, force=False)
            except DiscoveryError as e:
                self.log.error("登录后入口发现失败：%s" % e)
                return False
            if getattr(self.client, "target_id", None):
                self.target_id = self.client.target_id
            self.entry_source = result.get("source", self.entry_source)
            if result.get("source") == "need_human":
                if self._pause_for_human("登录后仍需人工处理登录/验证码，处理后点继续"):
                    return False
                return self._prepare_after_human(cfg)
            self._set_stage("s3")

        # S4 角色
        self._set_stage("s4")
        f = self.discovery.features()
        if f.get("roleDlg"):
            self._select_student_role()
            time.sleep(1.5)

        # S5 课程态（并切到官网「能报的二课」分类，只留当前可报名的活动）
        self._set_stage("s5")
        self._apply_available_tab()
        courses = parse_courses(self._eval_value(SCAN_JS))
        courses = [c for c in courses if c.get("available")]
        if not courses:
            self.log.warn("就绪阶段未读到可报课程，将在监测中继续尝试")
        self._set_stage("ready")
        self.log.success("就绪完成（入口来源：%s）" % self.entry_source)
        return True

    def _prepare_after_human(self, cfg, manual=True):
        """人工处理后重新确认 S2-S5，并回到就绪（不再重复一次入口发现）。"""
        if manual:
            self.log.info("人工处理完成，重新确认就绪状态")
        # 人工处理期间用户可能刚补填账号密码：重新读盘，否则还用启动时的旧配置
        try:
            fresh = load_config()
            if isinstance(cfg, dict):
                cfg.clear()
                cfg.update(fresh)
            else:
                cfg = fresh
        except Exception as e:
            self.log.warn("重新读取配置失败（继续用内存里的）：%s" % e)
        self._set_stage("s2")
        try:
            result = self.discovery.discover(cfg, self.target_id, force=False)
        except DiscoveryError as e:
            self.log.error("人工处理后重新发现失败：%s" % e)
            return False
        if getattr(self.client, "target_id", None):
            self.target_id = self.client.target_id
        if result.get("source") == "need_human":
            self.log.warn("仍停留在登录/验证码页面")
            if self._pause_for_human("仍检测到登录/验证码页面，处理后点继续"):
                return False
            return self._prepare_after_human(cfg)
        self.entry_source = result.get("source", self.entry_source)
        return self._prepare(cfg, skip_discover=True)

    def _auto_login(self, cfg):
        """自动登录：填入界面保存的账号密码并提交登录；验证码不识别、转人工。"""
        login = cfg.get("login", {}) or {}
        if not (login.get("username") and login.get("password")):
            self.log.warn("门户需要登录，但「登录信息」里还没填学号/密码："
                          "请在页面左侧填好后点保存，再点「我已处理，继续」")
            return {"status": "need_human", "reason": "未配置账号密码"}
        return self.discovery.auto_login(cfg)

    def _select_student_role(self):
        self.log.info("检测到角色弹窗，尝试选择『学生』")
        self._eval("""(function(){
          var li=[].slice.call(document.querySelectorAll('.el-select-dropdown__item'))
            .filter(function(e){return (e.innerText||'').trim()==='学生';})[0];
          if(li)li.click();
          var btn=[].slice.call(document.querySelectorAll('button'))
            .filter(function(e){return /确\\s*定/.test(e.innerText||'');})[0];
          if(btn)setTimeout(function(){btn.click();},300);
          return true;
        })()""")

    def _check_session(self, cfg):
        """运行中检测登录态失效；返回 True 表示仍有效，False 表示已处理/需跳过本轮。"""
        f = self.discovery.features() if self.discovery else {}
        if not f:
            return True
        self._update_login_status(f)
        if f.get("captcha") or f.get("pwd"):
            # 先尝试自动登录（已保存账号密码且无验证码时可直接恢复，不必人工）
            res = self._auto_login(cfg)
            if res.get("status") == "ok":
                self.log.success("登录态失效后自动登录成功，重新确认就绪状态")
                ok = self._prepare_after_human(cfg, manual=False)
                self._resume_stage = None
                return ok
            self._resume_stage = self.stage
            self.log.warn("登录态失效，暂停等待人工（%s）" % (res.get("reason") or ""))
            if self._pause_for_human("登录态失效，请重新登录/输入验证码后点继续"):
                return False
            # 恢复：重新确认 S3-S5，再回到失效前阶段
            ok = self._prepare_after_human(cfg)
            if ok and self._resume_stage:
                self._set_stage(self._resume_stage)
                self.log.info("会话已恢复，回到阶段 %s 继续" % self._resume_stage)
            self._resume_stage = None
            return ok
        return True

    def _update_login_status(self, f):
        if f.get("captcha"):
            self.login_status = {"code": "captcha", "label": "需人工处理",
                "detail": "检测到验证码，请处理后点继续"}
        elif f.get("pwd"):
            self.login_status = {"code": "login", "label": "未登录",
                "detail": "停留在登录页，请在浏览器登录"}
        elif f.get("roleDlg"):
            self.login_status = {"code": "role", "label": "选择角色中",
                "detail": "正在确认学生身份"}
        elif f.get("score", 0) >= 5:
            self.login_status = {"code": "ok", "label": "已登录",
                "detail": "已进入活动中心，正常监测中"}
        else:
            self.login_status = {"code": "unknown", "label": "页面未识别",
                "detail": "当前页面结构未识别"}

    def _apply_available_tab(self):
        """切到官网抢课页的「能报的二课」分类：该分类只列当前可报名的活动。

        找不到该分类时不报错——`parse_courses` 会给每门课打 available 标记，
        主循环仍只显示可报活动，属于同样的效果。
        """
        try:
            raw = self._eval_value(AVAILABLE_TAB_JS)
            info = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception as e:
            self.log.warn("切换「能报的二课」分类失败：%s" % e)
            return False
        if not info.get("found"):
            near = info.get("near") or []
            if near:
                self.log.warn("页面上没有「能报的二课」分类，改为程序内只保留可报活动（页面二课相关文本：%s）"
                              % " / ".join(near[:5]))
            else:
                self.log.warn("页面上没有「能报的二课」分类，改为程序内只保留可报活动")
            return False
        if info.get("clicked"):
            self.log.info("已切换到官网分类「%s」（只显示能报的二课）" % info.get("txt"))
            time.sleep(1.2)
        else:
            self.log.info("官网当前已在「%s」分类" % info.get("txt"))
        return True

    # ---------- 方案3：双模式调度 ----------
    def _in_release_window(self, cfg):
        rt = cfg["schedule"].get("release_time", "")
        if not rt:
            return False
        try:
            lead = float(cfg["schedule"].get("lead_seconds", 30))
            win = float(cfg["schedule"].get("window_seconds", 120))
            t = datetime.datetime.fromisoformat(rt)
            now = datetime.datetime.now()
            return (t - datetime.timedelta(seconds=lead)) <= now <= (t + datetime.timedelta(seconds=win))
        except (ValueError, TypeError):
            return False

    def _should_wait_for_release(self, cfg):
        """定时模式：若尚未到提前启动时刻，返回需等待的秒数。"""
        rt = cfg["schedule"].get("release_time", "")
        if not rt:
            return 0
        try:
            lead = float(cfg["schedule"].get("lead_minutes", 10)) * 60
            t = datetime.datetime.fromisoformat(rt)
            remain = (t - datetime.timedelta(seconds=lead) - datetime.datetime.now()).total_seconds()
            return max(0, remain)
        except (ValueError, TypeError):
            return 0

    def _pick_interval(self, cfg, normal, burst):
        if self._burst:
            return burst
        if self._in_release_window(cfg):
            return burst
        return normal

    def _set_burst(self, cfg, on, reason=""):
        if on == self._burst:
            return
        self._burst = on
        if on:
            win = float(cfg["schedule"].get("window_seconds", 120))
            self._burst_until = time.time() + win
            self.log.info("切换高频模式（%s），持续 %ss" % (reason, int(win)))
        else:
            self._burst_until = 0.0
            self.log.info("回落低频模式（%s）" % reason)

    # ---------- 兼容包装（旧测试/外部调用保留接口）----------
    @staticmethod
    def _is_available(quota):
        return is_available(quota)

    def _scan_courses(self):
        return parse_courses(self._eval_value(SCAN_JS))

    @staticmethod
    def _match_target(course, cfg):
        """旧接口兼容：仅按名称关键词匹配（完整筛选见 watcher.pick_courses）。"""
        kws = [k.strip() for k in cfg.get("target", {}).get("keywords", []) if k and k.strip()]
        if not kws:
            return True
        return any(k in course.get("title", "") for k in kws)

    # ---------- 抓取与提交 ----------
    def _selected_candidates(self, courses, selected, wait_reg=True):
        """点选（且已确认）课程的到点抢课候选。

        规则：wait_reg=True 且课程有「报名开始时间」且未到点 → 不进入候选（避免提前点击无效提交）；
        到点后 REG_GRACE_SECONDS 内即便页面还显示已报满也尝试（余量可能尚未刷新）；
        超过宽限期或未标注时间 → 仅当当前可报。
        wait_reg=False 时忽略报名开始时间，只要可报就抢。
        """
        now = datetime.datetime.now()
        blocked = self._blocked_keys()
        out = []
        for c in courses:
            key = course_key(c)
            if key not in selected or key in blocked:
                continue
            dt = parse_dt(c.get("reg_start"))
            if wait_reg and dt and now < dt:
                continue
            fresh = bool(dt) and (now - dt).total_seconds() <= REG_GRACE_SECONDS
            if not (fresh or is_available(c.get("quota"))):
                continue
            if fresh and key not in self._due_done:
                self._due_done.add(key)
                self.log.success("报名开始时间已到，开始抢「%s」" % c.get("title"))
            out.append((c, "到达报名开始时间" if dt else "选中的目标课程"))
        return out

    def _enter_and_submit(self, course, cfg):
        # 按标题定位卡片：列表扫描用的是 .act-item,[class*=act-item] 宽选择器，
        # 与 .act-item 索引可能错位；标题匹配失败时再回退到 index。
        title = (course.get("title") or "").strip()
        idx = int(course.get("index") or 0)
        clicked = self._eval_value("""(function(){
          var want=%s, idx=%d;
          var cards=[].slice.call(document.querySelectorAll('.act-item, [class*=act-item]'));
          var pick=null;
          function tx(c){var el=c.querySelector('.title');return ((el&&el.innerText)||'').replace(/\\s+/g,' ').trim();}
          if(want){
            for(var i=0;i<cards.length;i++){ if(tx(cards[i])===want){pick=cards[i];break;} }
            if(!pick){
              for(var j=0;j<cards.length;j++){
                var s=tx(cards[j]);
                if(s&&s.indexOf(want)>=0){pick=cards[j];break;}
              }
            }
          }
          if(!pick && cards.length>idx) pick=cards[idx];
          if(!pick) return false;
          var t=pick.querySelector('.title')||pick;
          t.click();return true;
        })()""" % (_js_string(title), idx))
        if not clicked:
            return False, "找不到课程卡片"
        time.sleep(1.5)
        d = self._detail_state()
        if not d:
            return False, "解析详情失败"
        if d.get("signedUp"):
            return True, "页面已显示「取消报名」，视为已报名，跳过（防重复）"
        if not d.get("hasBtn"):
            return False, "详情页无报名按钮（可能已过报名时间或名额已满）"
        self._eval("""(function(){var b=document.querySelector('.regisiter-button');if(b)b.click();return true;})()""")
        time.sleep(1.5)
        self._confirm_dialog()
        time.sleep(0.6)
        d2 = self._detail_state() or {}
        if d2.get("signedUp") or not d2.get("hasBtn"):
            return True, "报名已生效（页面已显示已报名 / 取消报名）"
        # 页面仍是「立即报名」：不能算成功，避免把点击当报名
        return False, "未确认报名：页面仍显示「立即报名」（平台可能拦截或需验证码）"

    def _detail_state(self):
        """读取详情页状态：是否有报名按钮、是否已报名、余量、报名开始时间。"""
        raw = self._eval_value("""(function(){
          var t=document.body.innerText.replace(/\\s+/g,' ');
          var btn=document.querySelector('.regisiter-button');
          var q='';
          var mm=t.match(/已报名\\s*\\/\\s*报名上限[：:]?\\s*([^ \\/]+)\\s*\\/\\s*([^ ]+)/);
          if(mm) q=mm[1]+'/'+mm[2];
          var rs='';
          var mr=t.match(/报名开始时间[：:]?\\s*([0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?: [0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)?)/);
          if(mr) rs=mr[1];
          return JSON.stringify({
            hasBtn:!!btn, signedUp:/取消报名/.test(t), quota:q,
            regStart:rs, url:location.href
          });
        })()""")
        try:
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _confirm_dialog(self):
        """报名后若弹出确认框，点掉可见且可用的「确定/确认」。"""
        hit = self._eval_value("""(function(){
          var all=[].slice.call(document.querySelectorAll('button,.el-button'));
          for(var i=0;i<all.length;i++){
            var e=all[i], tx=(e.innerText||'').replace(/\\s+/g,'');
            if(!/^(确定|确认|是|继续)$/.test(tx)) continue;
            var r=e.getBoundingClientRect();
            if(!(r.width>0&&r.height>0)) continue;
            if(e.disabled) continue;
            if(e.className&&e.className.indexOf('is-disabled')>=0) continue;
            e.click();return tx;
          }
          return '';
        })()""")
        if hit:
            self.log.info("点击确认框「%s」" % hit)
            return True
        return False

    def _blocked_keys(self):
        """已成功报名的课程永久跳过；失败的课程只在冷却期内跳过，过期后可再抢。"""
        now = time.time()
        blocked = set()
        for key, info in self.submitted.items():
            if info.get("result") == "success":
                blocked.add(key)
                continue
            ts = info.get("ts")
            if isinstance(ts, (int, float)) and now - ts < FAIL_RETRY_COOLDOWN:
                blocked.add(key)
        return blocked

    def _submit_candidates(self, candidates, cfg):
        max_retries = int(cfg["strategy"]["max_retries"])
        backoff = float(cfg["strategy"]["retry_backoff"])
        max_courses = int(cfg["strategy"]["max_courses"])
        for c, reason in candidates:
            if self._stop_event.is_set():
                break
            if self.stats["submitted"] >= max_courses:
                break
            key = course_key(c)
            if key in self.submitted:
                continue
            ok = False
            for attempt in range(max_retries + 1):
                if self._stop_event.is_set():
                    break
                try:
                    ok, msg = self._enter_and_submit(c, cfg)
                    self.log.info("提交 %s：%s" % (c.get("title"), msg))
                    if ok:
                        break
                    self.stats["retries"] += 1
                except Exception as e:
                    self.log.error("提交异常：%s" % e)
                    ok = False
                if attempt < max_retries:
                    delay = backoff * (attempt + 1)
                    self.log.warn("第 %d 次重试，退避 %.1fs" % (attempt + 1, delay))
                    if self._wait_or_stop(delay):
                        break
            self.submitted[key] = {
                "title": c.get("title"), "time": c.get("time"),
                "category": c.get("category"), "level": c.get("level"),
                "campus": c.get("campus"), "quota": c.get("quota"),
                "result": "success" if ok else "failed",
                "at": datetime.datetime.now().strftime("%H:%M:%S"),
                "ts": time.time()
            }
            if ok:
                self.stats["submitted"] += 1
                self.log.success("成功提交：%s" % c.get("title"))
            else:
                self.stats["failed"] += 1
                self.log.error("提交失败：%s" % c.get("title"))
            try:
                self._eval_value("history.back()")
            except Exception:
                pass
            time.sleep(1.5)

    # ---------- 主循环 ----------
    def _run(self):
        cfg = load_config()
        self.mode = cfg.get("mode", "both")
        if not WATCH_MODE_ENABLED and self.mode != "scheduled":
            # 守候模式暂未开放：旧配置里的 watch/both 统一按「定时」跑
            self.log.info("守候模式已隐藏（功能暂不可用），本次按「定时」模式运行")
            self.mode = "scheduled"
        self.stats = {"scans": 0, "found": 0, "submitted": 0, "failed": 0,
                      "retries": 0, "started_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        self.submitted = {}
        self.courses = []
        self.next_due = None
        self._due_logged = set()
        self._due_done = set()
        self._no_sel_logged = False
        self._hit_logged = 0
        self._force_rediscover = False
        self._set_state(EngineState.STARTING)

        # 定时模式：等待到提前启动时刻；已点选课程时直接启动，由主循环按课程时间守候
        selected0 = [s for s in (cfg.get("target", {}) or {}).get("selected") or [] if s]
        wait = 0
        if self.mode in ("scheduled", "both") and not selected0:
            wait = self._should_wait_for_release(cfg)
        if wait > 0:
            self._set_stage("idle")
            self.log.info("定时模式：距离提前启动还有 %.0f 秒" % wait)
            if self._wait_or_stop(wait):
                self.lock.release()
                return

        try:
            self._attach_browser()
            if not self._prepare(cfg):
                self._set_state(EngineState.STOPPED)
                self.lock.release()
                return
            self._set_state(EngineState.RUNNING)
        except Exception as e:
            self.log.error("启动失败：%s" % e)
            self._set_state(EngineState.STOPPED)
            self.lock.release()
            return

        while not self._stop_event.is_set():
            try:
                # 每轮重读配置：界面点选/取消课程、改时间后无需重启即可生效
                cfg = load_config()
                self.mode = cfg.get("mode", self.mode)
                if not WATCH_MODE_ENABLED and self.mode != "scheduled":
                    self.mode = "scheduled"
                normal = float(cfg["strategy"]["poll_interval_normal"])
                burst = float(cfg["strategy"]["poll_interval_burst"])
                miss_limit = int(cfg["strategy"]["miss_before_rediscover"])

                if self.state == EngineState.PAUSED:
                    while not self._stop_event.is_set() and not self._pause_event.is_set():
                        time.sleep(0.4)
                    self._pause_event.clear()
                    self._set_state(EngineState.RUNNING)

                # 会话有效性检查（失效则暂停+恢复到原阶段）
                if not self._check_session(cfg):
                    continue

                # 高频窗口过期回落
                if self._burst and time.time() > self._burst_until:
                    self._set_burst(cfg, False, "窗口超时")

                interval = self._pick_interval(cfg, normal, burst)

                # 界面点了「按筛选条件刷新」：立刻重新切分类并抓取
                if self._rescan_event.is_set():
                    self._rescan_event.clear()
                    self._miss_streak = 0
                    self._apply_available_tab()
                    interval = min(interval, 1.0)

                all_courses = parse_courses(self._eval_value(SCAN_JS))
                self.stats["scans"] += 1
                # 「课程与余量」只显示能报的二课（官网分类没切成功时的兜底）
                courses = [c for c in all_courses if c.get("available")]
                if not courses and all_courses:
                    self._miss_streak = 0
                    self.courses = []
                    self.next_due = None
                    self.log.info("当前没有能报的二课（页面共 %d 个，均已满或不可报）"
                                  % len(all_courses))
                    if self._wait_or_stop(interval):
                        break
                    continue

                if not courses:
                    self._miss_streak += 1
                    self.log.warn("未抓取到课程（连续 %d 次）" % self._miss_streak)
                    if self._miss_streak >= miss_limit:
                        self.log.warn("连续未抓到课程，尝试重新发现入口")
                        self._miss_streak = 0
                        self._force_rediscover = True
                        if not self._prepare(cfg):
                            break
                    if self._wait_or_stop(interval):
                        break
                    continue
                self._miss_streak = 0
                self.courses = courses

                # 目标课程「报名开始时间」：到点自动抢（用户点选课程后生效）
                tgt = cfg.get("target", {}) or {}
                selected = [s for s in (tgt.get("selected") or []) if s]
                wait_reg = bool(tgt.get("wait_reg_start", True))
                lead = float(cfg["schedule"].get("lead_seconds", 30))
                due_pair = next_reg_start(courses, selected) if (selected and wait_reg) else None
                if due_pair:
                    due_course, due_at = due_pair
                    remain = (due_at - datetime.datetime.now()).total_seconds()
                    self.next_due = {
                        "key": course_key(due_course),
                        "title": due_course.get("title"),
                        "at": due_at.strftime("%Y-%m-%d %H:%M:%S"),
                        "remain": max(0, int(remain)),
                    }
                    dkey = self.next_due["key"]
                    if dkey not in self._due_logged:
                        self._due_logged.add(dkey)
                        self.log.info("目标「%s」报名开始时间 %s，到点自动抢课"
                                      % (due_course.get("title"), self.next_due["at"]))
                    if remain > lead:
                        # 离报名开始还早：低频守候（3~10 秒重读配置，改选可即时生效）
                        nap = 10.0 if (remain - lead) > 300 else 3.0
                        if self._wait_or_stop(max(1.0, min(nap, remain - lead))):
                            break
                        continue
                    self._set_burst(cfg, True, "目标课程进入抢课窗口")
                else:
                    self.next_due = None

                # 方案3：快照差异检测
                diff = self.watcher.diff(courses)
                if diff["new"] or diff["became_available"]:
                    self.log.info("检测到新课程 %d / 由满变可报 %d" %
                                  (len(diff["new"]), len(diff["became_available"])))
                    if self.mode in ("watch", "both") and (diff["new"] or diff["became_available"]):
                        self._set_burst(cfg, True, "守候模式命中新活动")
                if self._in_release_window(cfg):
                    self._set_burst(cfg, True, "进入放课窗口")

                # 只抢你在界面上「点选并确认」的课程：
                # 一个都没确认时，程序只刷新课程与余量，绝不自动报名（不再按筛选条件自动挑课）。
                if not selected:
                    if not self._no_sel_logged:
                        self._no_sel_logged = True
                        self.log.info("未确认目标课程：只刷新课程与余量，不会自动报名（在右侧课程卡点选后点「确认选课」）")
                    self.next_due = None
                    self.stats["found"] = len(courses)
                    if self._wait_or_stop(interval):
                        break
                    continue
                self._no_sel_logged = False

                candidates = self._selected_candidates(courses, selected, wait_reg)
                if candidates:
                    self.stats["found"] = len(candidates)
                    if not self._hit_logged or self._hit_logged != len(candidates):
                        self._hit_logged = len(candidates)
                        self.log.info("已确认 %d 门目标课程，其中 %d 门现在可抢"
                                      % (len(selected), len(candidates)))
                    self._submit_candidates(candidates, cfg)

                if self._wait_or_stop(interval):
                    break

            except Exception as e:
                if self._stop_event.is_set():
                    break
                if self._is_nav_race(e):
                    time.sleep(0.8)
                    continue
                # 浏览器被外部关闭（如测试清理）导致连接中止，静默退出
                msg = str(e)
                if "10053" in msg or "10054" in msg or "Connection reset" in msg:
                    self.log.warn("浏览器连接已断开，结束监测")
                    break
                self.log.error("主循环异常：%s" % e)
                if self._wait_or_stop(3):
                    break

        # 落盘本轮结果（方案5结构预留，本次仅存数据不通知）
        try:
            append_results({
                "started_at": self.stats.get("started_at"),
                "ended_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "mode": self.mode, "entry_source": self.entry_source,
                "stats": dict(self.stats), "submitted": dict(self.submitted)
            })
        except Exception:
            pass
        if self.client:
            self.client.close()
        self.log.info("引擎已停止")
