"""
配置与日志模块 —— 零第三方依赖，只用 Python 标准库。
配置与日志均保存到本地 data/ 与 logs/ 目录，不上传任何数据。
"""
import os
import json
import threading
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 程序版本号（与 VERSION 文件、界面徽标、/api/health 保持一致；改版本时一并更新）
APP_VERSION = "1.0.6"

# 功能开关：守候模式（快照差异检测新活动、命中即切高频）目前还不成熟，先整体隐藏。
# 置为 False 时：界面上不再出现「守候 / 定时+守候」选项，引擎即使读到旧配置里的
# mode=watch/both 也按「定时」跑（代码保留，改回 True 即可恢复，不需要动配置）。
WATCH_MODE_ENABLED = False
# 测试/多环境可用 SANDAU_DATA_DIR 指向临时目录，避免污染真实配置与日志
_ENV_DATA_DIR = os.environ.get("SANDAU_DATA_DIR")
DATA_DIR = _ENV_DATA_DIR or os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(_ENV_DATA_DIR, "logs") if _ENV_DATA_DIR else os.path.join(BASE_DIR, "logs")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LOCK_PATH = os.path.join(DATA_DIR, "instance.lock")
CACHE_PATH = os.path.join(DATA_DIR, "entry_cache.json")
RESULTS_PATH = os.path.join(DATA_DIR, "results.json")

DEFAULT_CONFIG = {
    # 登录信息（仅保存在本机，界面做脱敏显示）
    "login": {
        "username": "",
        "password": "",
        # 稳定锚点：校园统一门户根地址（唯一需用户给出的稳定信息）
        "anchor_url": "https://newehall.sandau.edu.cn/ywtb-portal/official/index.html",
        # 保留旧字段仅为向后兼容；发现器优先使用 anchor_url
        "portal_url": "",
        "target_url": "",
        # 统一身份认证地址；留空则由 anchor_url 自动推导（authserver.<校园根域名>）
        "login_url": ""
    },
    # 运行模式：scheduled(定时) / watch(守候) / both(两者)
    "mode": "scheduled",
    # 目标课程筛选（对齐本校第二课堂字段）
    "target": {
        "keywords": [],              # 名称关键词
        "only_when_available": True,
        "campus": "",                # 校区："金海"/"嘉善"/"金桥"/"长寿"，空=全部
        "category": "",              # 五育：德育/智育/体育/美育/劳育，空=全部
        "level": "",                 # 校级/院级，空=全部
        # 用户点选的目标课程（课程唯一键 title|time）；为空表示按筛选条件自动挑
        "selected": [],
        # 点到课程后是否按其「报名开始时间」到点再抢
        "wait_reg_start": True
    },
    # 放课时间窗口（定时模式）
    "schedule": {
        "release_time": "",          # ISO "2026-11-20T12:00:00"
        "lead_minutes": 10,          # 提前启动分钟数（覆盖打开+登录+就绪）
        "lead_seconds": 30,          # 进入高频的提前秒数
        "window_seconds": 120        # 窗口持续秒数
    },
    # 抢课策略（合规护栏内置下限，见 STRATEGY_LIMITS）
    "strategy": {
        "poll_interval_normal": 10.0,   # 平时/守候轮诲间隔（秒）
        "poll_interval_burst": 1.0,     # 窗口内轮询间隔（秒，下限 1.0）
        "max_retries": 3,               # 单课程最大重试次数
        "retry_backoff": 2.0,           # 重试退避基数（秒）
        "stop_on_captcha": True,        # 遇验证码暂停等待人工
        "max_courses": 5,               # 单次最多提交课程数，防止失控
        "miss_before_rediscover": 3     # 连续抓不到课程的次数，超过则重新发现入口
    },
    # 入口发现关键词（可用中文或英文，按可见文本匹配）
    "discovery": {
        "entry_keywords": ["二课-活动中心", "活动中心", "第二课堂", "二课", "secondclass", "activityCenter"],
        "transit_keywords": ["学生综合服务系统", "办事大厅", "应用列表", "学工"],
        "min_score": 5                  # 页面特征确认阈值
    },
    # 通知（本次方案未选，保留默认关闭的结构以便未来启用）
    "notify": {"enabled": False, "on": ["hit", "success", "failed", "need_human", "error"]}
}

# 合规护栏：用户可配置，但程序强制夹在安全区间内
STRATEGY_LIMITS = {
    "poll_interval_normal": (3.0, 3600.0),   # 平时间隔下限 3 秒
    "poll_interval_burst": (1.0, 60.0),      # 窗口内下限 1 秒，禁止毫秒级
    "max_retries": (0, 10),
    "retry_backoff": (0.5, 60.0),
    "max_courses": (1, 20),
    "miss_before_rediscover": (1, 20)
}


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def clamp_strategy(cfg):
    """把策略参数夹到合规区间，返回被修正的字段列表。"""
    fixed = []
    s = cfg.get("strategy", {})
    for key, (lo, hi) in STRATEGY_LIMITS.items():
        if key not in s:
            continue
        try:
            val = float(s[key])
        except (TypeError, ValueError):
            val = lo
        clamped = max(lo, min(hi, val))
        if clamped != s[key]:
            fixed.append({"key": key, "from": s[key], "to": clamped})
            s[key] = clamped
    # 提前启动分钟数：非负且不超过 24 小时
    sch = cfg.get("schedule", {})
    if "lead_minutes" in sch:
        try:
            v = float(sch["lead_minutes"])
        except (TypeError, ValueError):
            v = 10.0
        clamped = max(0.0, min(1440.0, v))
        if clamped != sch["lead_minutes"]:
            fixed.append({"key": "lead_minutes", "from": sch["lead_minutes"], "to": clamped})
            sch["lead_minutes"] = clamped
    return fixed


def load_config():
    ensure_dirs()
    cfg = _deep_merge(DEFAULT_CONFIG, {})
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = _deep_merge(cfg, json.load(f))
        except (OSError, json.JSONDecodeError):
            pass
    # 旧配置迁移：若只有 portal_url/target_url 而没有 anchor_url，用旧门户地址作锚点
    login = cfg.get("login", {})
    if not login.get("anchor_url"):
        login["anchor_url"] = login.get("portal_url") or DEFAULT_CONFIG["login"]["anchor_url"]
    return cfg


def _read_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return fallback


def _write_json(path, data):
    ensure_dirs()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_entry_cache():
    return _read_json(CACHE_PATH, {})


def save_entry_cache(cache):
    _write_json(CACHE_PATH, cache)


def load_results():
    return _read_json(RESULTS_PATH, {"rounds": []})


def append_results(round_data):
    data = load_results()
    data.setdefault("rounds", []).append(round_data)
    # 只保留最近 50 轮，避免无限增长
    data["rounds"] = data["rounds"][-50:]
    _write_json(RESULTS_PATH, data)
    return data


def _normalize_target(cfg):
    """规整目标课程相关的用户输入（点选列表 / 布尔开关）。"""
    t = cfg.get("target")
    if not isinstance(t, dict):
        cfg["target"] = t = {}
    sel = t.get("selected")
    if not isinstance(sel, list):
        sel = []
    out, seen = [], set()
    for s in sel:
        s = str(s or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s[:120])
    t["selected"] = out[:20]
    t["wait_reg_start"] = bool(t.get("wait_reg_start", True))
    return cfg


def save_config(cfg):
    ensure_dirs()
    merged = _deep_merge(load_config(), cfg)
    merged = _normalize_target(merged)
    fixed = clamp_strategy(merged)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
    return {"ok": True, "clamped": fixed, "config": public_config(merged)}


def public_config(cfg):
    """脱敏后的配置，用于界面展示。"""
    c = json.loads(json.dumps(cfg))
    u = c.get("login", {}).get("username", "")
    p = c.get("login", {}).get("password", "")
    c["login"]["username_masked"] = mask(u)
    c["login"]["password_set"] = bool(p)
    c["login"].pop("username", None)
    c["login"].pop("password", None)
    return c


def mask(value):
    if not value:
        return ""
    s = str(value)
    if len(s) <= 2:
        return "*" * len(s)
    return s[0] + "*" * (len(s) - 2) + s[-1]


class EventLog:
    """线程安全的内存日志 + 落盘，供 UI 实时读取。"""
    def __init__(self, max_memory=500):
        ensure_dirs()
        self.max_memory = max_memory
        self._events = []
        self._lock = threading.Lock()
        self._seq = 0

    def add(self, level, message, **extra):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._seq += 1
            ev = {
                "id": self._seq,
                "time": now,
                "level": level,
                "message": message,
                **extra
            }
            self._events.append(ev)
            if len(self._events) > self.max_memory:
                self._events = self._events[-self.max_memory:]
        # 落盘（追加），失败不影响主流程
        try:
            with open(os.path.join(LOG_DIR, "operation.log"), "a", encoding="utf-8") as f:
                f.write("[%s] %-6s %s\n" % (now, level.upper(), message))
        except OSError:
            pass
        return ev

    def info(self, msg, **kw): return self.add("info", msg, **kw)
    def warn(self, msg, **kw): return self.add("warn", msg, **kw)
    def error(self, msg, **kw): return self.add("error", msg, **kw)
    def success(self, msg, **kw): return self.add("success", msg, **kw)

    def since(self, event_id=0, limit=200):
        with self._lock:
            items = [e for e in self._events if e["id"] > event_id]
            return items[-limit:]


class InstanceLock:
    """单实例文件锁，防止多开导致重复提交或并发刷接口。"""
    def __init__(self):
        self._locked = False
        self._fh = None

    def acquire(self):
        ensure_dirs()
        self._fh = open(LOCK_PATH, "w")
        try:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError):
            # Windows 下用文件存在性 + 内容标记做简单互斥
            self._fh.seek(0)
            self._fh.write(str(os.getpid()))
            self._fh.flush()
        self._locked = True
        return True

    def release(self):
        if self._fh:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        if os.path.exists(LOCK_PATH):
            try:
                os.remove(LOCK_PATH)
            except OSError:
                pass
        self._locked = False


