"""
本地 Web 服务 —— 纯 Python 标准库，提供 UI 静态文件与 REST API。
入口：双击「启动程序.bat」，或运行 python server.py。
启动后会自动在默认浏览器打开下方 PAGE_URL。
"""
import os
import sys
import json
import time
import webbrowser
import threading
import urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ==================== 配置区（如需修改，只改这里）====================
HOST = "127.0.0.1"                       # 监听地址，保持本机访问
PORT = 8765                              # 服务端口
PAGE_URL = "http://%s:%d/" % (HOST, PORT)  # 启动后自动打开的页面地址
AUTO_OPEN_BROWSER = True                 # 是否自动打开默认浏览器
# ====================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, "core"))

from common import EventLog, load_config, save_config, APP_VERSION
from engine import GrabbingEngine
from watcher import course_key

log = EventLog()
engine = GrabbingEngine(log)


class APIHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=os.path.join(BASE_DIR, "web"), **kwargs)

    def log_message(self, fmt, *args):
        pass  # 静默访问日志，操作日志走 EventLog

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # 静态页面/脚本/样式也禁止缓存：改了 index.html、app.js、style.css 后
        # 用户按一次刷新就能看到新版本，不会出现“改了界面但浏览器还在跑旧页面”。
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self._api_get(path)
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self._api_post(path, self._read_body())
        return self._send(404, {"error": "not found"})

    def _api_get(self, path):
        if path == "/api/status":
            return self._send(200, {"ok": True, "status": engine.get_status()})
        if path == "/api/config":
            from common import public_config
            return self._send(200, {"ok": True, "config": public_config(load_config())})
        if path == "/api/config/export":
            # 导出完整配置（含密码），供本机备份；前端提示用户妥善保管
            return self._send(200, {"ok": True, "config": load_config()})
        if path == "/api/logs":
            qs = parse_qs(urlparse(self.path).query)
            since = int(qs.get("since", [0])[0])
            return self._send(200, {"ok": True, "events": log.since(since)})
        if path == "/api/results":
            from common import load_results
            return self._send(200, {"ok": True, "results": load_results()})
        if path == "/api/health":
            return self._send(200, {"ok": True, "version": APP_VERSION})
        return self._send(404, {"error": "unknown api"})

    def _api_post(self, path, body):
        if path == "/api/config":
            res = save_config(body.get("config", {}))
            log.info("配置已保存")
            return self._send(200, res)
        if path == "/api/config/import":
            res = save_config(body.get("config", {}))
            log.info("配置已导入")
            return self._send(200, res)
        if path == "/api/course/select":
            return self._course_select(body)
        if path == "/api/course/clear":
            res = save_config({"target": {"selected": []}})
            log.info("已清空抢课计划")
            return self._send(200, {"ok": True, "selected": [],
                                    "config": res.get("config")})
        if path == "/api/start":
            res = engine.start()
            return self._send(200, res)
        if path == "/api/stop":
            res = engine.stop()
            return self._send(200, res)
        if path == "/api/resolve":
            res = engine.resolve_captcha()
            return self._send(200, res)
        if path == "/api/rediscover":
            res = engine.rediscover()
            return self._send(200, res)
        if path == "/api/rescan":
            res = engine.rescan()
            return self._send(200, res)
        return self._send(404, {"error": "unknown api"})

    def _course_select(self, body):
        """确认抢课计划：整批覆盖，或单门点选/取消（兼容旧接口）。

        - body={"selected": [...]}：把抢课计划整批设成这份列表（界面「确认选课」用，原子生效）
        - body={"key": .., "action": "add"|"remove"|"toggle"}：单门增删
        """
        # 整批确认
        if "selected" in body:
            raw = body.get("selected")
            if not isinstance(raw, list):
                return self._send(400, {"ok": False, "error": "selected 必须是数组"})
            res = save_config({"target": {"selected": raw}})
            sel = ((res.get("config", {}) or {}).get("target", {}) or {}).get("selected", [])
            if sel:
                log.info("已确认抢课计划：%d 门课程（%s）" % (
                    len(sel), "、".join([str(s).split("|")[0] for s in sel[:3]])))
            else:
                log.info("已取消全部目标课程：程序只刷新课程与余量，不会自动报名")
            return self._send(200, {"ok": True, "selected": sel,
                                    "config": res.get("config")})

        key = str(body.get("key") or "").strip()
        action = str(body.get("action") or "toggle").lower()
        if not key:
            return self._send(400, {"ok": False, "error": "缺少课程标识"})
        cfg = load_config()
        sel = [str(s) for s in (cfg.get("target", {}) or {}).get("selected") or [] if s]
        exists = key in sel
        if action == "remove" or (action == "toggle" and exists):
            sel = [s for s in sel if s != key]
            log.info("移出抢课计划：%s" % (key.split("|")[0] or key))
        else:
            course = None
            for c in engine.courses:
                if course_key(c) == key:
                    course = c
                    break
            if course is None:
                return self._send(404, {"ok": False,
                                        "error": "列表中暂无该课程，请等待页面刷新后重试"})
            if not exists:
                sel.append(key)
            reg = (course.get("reg_start") or "").strip()
            if reg and reg != "-":
                log.info("加入抢课计划：%s（报名开始 %s），到点自动抢"
                         % (course.get("title"), reg))
            else:
                log.info("加入抢课计划：%s（未标注报名开始时间，按可报即抢）"
                         % course.get("title"))
        res = save_config({"target": {"selected": sel}})
        return self._send(200, {"ok": True,
                                "selected": (res.get("config", {}).get("target", {}) or {}).get("selected", []),
                                "config": res.get("config")})


class LocalHTTPServer(ThreadingHTTPServer):
    # Windows 下 allow_reuse_address=True 会让「第二个实例」也能绑成功（连接仍然进第一个），
    # 于是双击启动器看着“启动了”其实什么都没变。这里关掉它，宁可让第二个实例明确失败。
    allow_reuse_address = False
    daemon_threads = True


def open_page(cache_bust=True):
    """打开界面地址；带时间戳参数，保证浏览器每次都是新请求（不读缓存）。"""
    url = PAGE_URL if not cache_bust else "%s?t=%d" % (PAGE_URL, int(time.time()))
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return url


def existing_instance():
    """检查端口上是不是已经跑着本程序（返回 (ok, version_or_error)）。"""
    try:
        with urllib.request.urlopen("http://%s:%d/api/health" % (HOST, PORT), timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
        return True, data.get("version", "")
    except Exception as e:
        return False, str(e)


def main():
    # 先看端口上是不是已经有本程序在跑：有就直接开页面退出，避免“双击了没反应”
    ok, info = existing_instance()
    if ok:
        url = open_page()
        msg = ("检测到程序已在运行（版本 %s），已直接打开界面：%s"
               % (info or "?", url))
        print(msg)
        log.info("重复启动：%s" % msg)
        time.sleep(1.5)
        return 0

    srv = None
    for attempt in range(3):
        try:
            srv = LocalHTTPServer((HOST, PORT), APIHandler)
            break
        except OSError as e:
            if attempt == 2:
                print("启动失败：端口 %d 被占用（%s）" % (PORT, e))
                print("请先用 stop.py 停止旧实例，或改 server.py 顶部的 PORT。")
                time.sleep(4)
                return 1
            time.sleep(0.5)

    log.success("服务已启动：" + PAGE_URL)
    print("第二课堂抢课辅助程序已启动：%s（版本 %s）" % (PAGE_URL, APP_VERSION))
    print("按 Ctrl+C 停止服务")

    # 服务就绪后自动打开默认浏览器（延迟 0.6s，避免抢在服务监听前访问）
    if AUTO_OPEN_BROWSER:
        threading.Timer(0.6, open_page).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        engine.stop()
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
