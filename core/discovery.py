"""
入口自动发现模块（方案 1）—— 四级定位 + 页面特征确认。

职责：给定 CDP 客户端，从稳定锚点出发，找到并确认抢课页面。
不负责浏览器启动（由 engine 负责），不发起网络请求，不提交任何表单。
仅读取可见 DOM 并模拟正常点击导航。
"""
import os
import sys
import json
import re
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (load_entry_cache, save_entry_cache)


class DiscoveryError(Exception):
    """入口发现失败。"""
    pass


def _js(s):
    return json.dumps(s, ensure_ascii=False)


# 页面特征确认用的 JS：返回各特征命中情况与分数（与 URL 无关）
FEATURE_JS = r"""(function(cfg){
  function feat(doc, win){
    var t = doc.body ? doc.body.innerText.replace(/\s+/g,' ') : '';
    var title = doc.title || '';
    var actItems = doc.querySelectorAll('.act-item, [class*=act-item]').length;
    var regBtns = doc.querySelectorAll('[class*=regis], [class*=signup]').length;
    var quotaText = /已报名\s*\/\s*报名上限|报名上限|已报名/.test(t);
    var quotaNum = /\d+\s*\/\s*(\d+|无限)/.test(t);
    var hasCenter = /活动中心|第二课堂/.test(t);
    var titleHit = /学生综合|学工|第二课堂|活动中心/.test(title);
    function visEl(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
    var pwd = [].filter.call(doc.querySelectorAll('input[type=password]'), visEl).length;
    var captcha = [].filter.call(
      doc.querySelectorAll('[class*=captcha],[class*=verify],input[placeholder*=验证码]'), visEl).length;
    var roleDlg = /选择角色/.test(t);
    // 门户未登录时页头是「登录」按钮：识别出来才能主动进统一身份认证
    var loginBtn = 0;
    var cands = doc.querySelectorAll('a,button,div,span,li');
    var limit = Math.min(cands.length, 2000);
    for(var j=0;j<limit;j++){
      var e2=cands[j];
      if(e2.children.length>1) continue;
      var t2=(e2.innerText||'').replace(/\s+/g,' ').trim();
      if(t2 && t2.length<=8 && /^(登录|登入|立即登录|登录系统|统一身份认证)$/.test(t2) && visEl(e2)){
        loginBtn = 1; break;
      }
    }
    var score = 0;
    if(actItems>0) score+=3;
    if(quotaText && quotaNum) score+=2;
    if(regBtns>0) score+=2;
    if(hasCenter) score+=1;
    if(titleHit) score+=1;
    return {
      score: score, actItems: actItems, regBtns: regBtns,
      quotaText: quotaText, quotaNum: quotaNum, hasCenter: hasCenter,
      titleHit: titleHit, pwd: pwd, captcha: captcha, roleDlg: roleDlg,
      loginBtn: loginBtn,
      url: (win && win.location) ? win.location.href : location.href,
      title: title, textHead: t.slice(0,160)
    };
  }
  var best = feat(document, window);
  // 门户/办事大厅常把内容放在 iframe 里：同源 iframe 直接读，跨源由 CDP 层切换 target
  var frames = document.querySelectorAll('iframe,frame');
  for(var i=0;i<frames.length;i++){
    try {
      var d = frames[i].contentDocument;
      if(!d) continue;
      var f = feat(d, frames[i].contentWindow);
      if(f.score > best.score){ f.frameIndex = i; best = f; }
    } catch(e) {}
  }
  return JSON.stringify(best);
})(CFG)"""


# 从当前 DOM 提取可点击候选项的文本（语义发现用）
CANDIDATES_JS = r"""(function(){
  var out=[];
  function scan(doc, fi){
    var nodes=doc.querySelectorAll('a,button,[role=button],div,li,span,td');
    for(var i=0;i<nodes.length;i++){
      var e=nodes[i];
      if(e.children.length>2) continue;
      var txt=(e.innerText||'').replace(/\s+/g,' ').trim();
      if(!txt||txt.length>24) continue;
      var cls='';
      try { cls = (e.className||'').toString(); } catch(x) { cls=''; }
      var r = e.getBoundingClientRect();
      var vis = !!(r.width>0 && r.height>0);
      out.push({tag:e.tagName,txt:txt,cls:cls.slice(0,80),frame:fi,vis:vis});
    }
  }
  scan(document, -1);
  var frames=document.querySelectorAll('iframe,frame');
  for(var k=0;k<frames.length;k++){
    try { var d=frames[k].contentDocument; if(d) scan(d, k); } catch(e) {}
  }
  return JSON.stringify(out.slice(0,600));
})()"""

# 导航/页头类区域：命中关键词也尽量不点（点进去通常只是切路由）
_CHROME_CLASS = re.compile(r"menu|nav|header|breadcrumb|shortcut|statistics|tab-item|user-", re.I)
# 内容/服务卡片类区域：命中关键词优先点
_CONTENT_CLASS = re.compile(r"service|app|entry|card|application|content-item|list-item|tile", re.I)
# 纯页头/导航/面包屑文本：命中关键词也不该先点（点了常常只是原地刷新）
_NAV_TEXT = re.compile(
    r"我的大学|个人中心|账号管理|安全退出|一站式网上办事大厅|暂无搜索内容|"
    r"回到顶部|我的消息|我的办件|我的收藏")
# 单个词就是导航项（必须完全相等才算，避免误伤服务名称）
_NAV_EXACT = {"登录", "登入", "搜索", "首页", "门户首页", "全部服务"}
# 逐级导航最大跳数（办事大厅 → 学生综合服务系统 → 二课-活动中心，留余量）
L3_MAX_HOPS = 5


class EntryDiscovery:
    def __init__(self, client, log, eval_value, evaluate):
        """client: CDPClient；eval_value/evaluate: 引擎提供的求值包装（含导航竞态容错）。"""
        self.client = client
        self.log = log
        self.ev = eval_value
        self.eval = evaluate
        self._anchor = ""
        self._click_note = ""
        self._adopted = set()      # 已切换过的 target id，避免同一标签页反复采纳

    def features(self):
        """读取当前页面特征，返回 dict；失败返回空 dict。"""
        raw = self.ev(FEATURE_JS.replace("CFG", "{}"))
        try:
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def is_target(self, features, min_score=5):
        """是否到达抢课页：特征分达阈值且非登录页。"""
        return bool(features) and features.get("score", 0) >= min_score \
            and not features.get("pwd") and not features.get("captcha")

    def needs_human(self, features):
        return bool(features) and bool(
            features.get("pwd") or features.get("captcha") or features.get("roleDlg")
        )

    def _click_by_text(self, text):
        """按可见文本点击候选项（正常页面交互，含同源 iframe 内的元素）。

        优先点可见元素；隐藏副本（尺寸为 0 的 el-scrollbar 镜像、折叠菜单）也允许
        用脚本 click 触发——真实门户的入口卡片常同时存在可见/隐藏两份 DOM，
        而隐藏那份往往才是真正带跳转事件的节点。
        """
        js = r"""(function(txt){
          function scan(doc){
            var nodes=doc.querySelectorAll('a,button,[role=button],div,li,span,td');
            var fallback=null, hidden=null;
            for(var i=0;i<nodes.length;i++){
              var e=nodes[i];
              if(e.children.length>2) continue;
              var t=(e.innerText||'').replace(/\s+/g,' ').trim();
              if(t===txt) return e;
              if(t.indexOf(txt)>=0){
                var r=e.getBoundingClientRect();
                var visible=(r.width>0 && r.height>0);
                if(visible && !fallback) fallback=e;
                if(!visible && !hidden) hidden=e;
              }
            }
            return fallback || hidden;
          }
          var el=null, inFrame=false;
          el=scan(document);
          if(!el){
            var frames=document.querySelectorAll('iframe,frame');
            for(var k=0;k<frames.length && !el;k++){
              try { var d=frames[k].contentDocument; if(d){ el=scan(d); inFrame=!!el; } } catch(e) {}
            }
          }
          if(!el) return JSON.stringify({status:'notfound'});
          try { el.scrollIntoView({block:'center'}); } catch(e) {}
          var r = el.getBoundingClientRect();
          var vh = window.innerHeight || 800;
          var vw = window.innerWidth || 1200;
          var visible = !!(r.width>0 && r.height>0) && r.bottom >= 0 && r.top <= vh
                        && r.right >= 0 && r.left <= vw;
          var pt = {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)};
          return JSON.stringify({status: visible ? 'ok' : 'ok-hidden',
                                 x: pt.x, y: pt.y, inFrame: inFrame});
        })(%s)""" % _js(text)
        self._click_note = ""
        try:
            raw = self.ev(js) or ""
        except Exception as e:
            self._click_note = "求值失败：%s" % e
            return False
        try:
            info = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except Exception:
            info = {"status": str(raw)}
        status = info.get("status")
        if status == "ok":
            # iframe 内元素的坐标是子文档坐标系，不能换算成顶层坐标点击：
            # 那种情况直接用脚本 click（Vue/普通 a 链接都能跳）
            click_at = None if info.get("inFrame") else getattr(self.client, "click_at", None)
            if click_at:
                try:
                    click_at(info.get("x", 0), info.get("y", 0))
                    return True
                except Exception as e:
                    self._click_note = "鼠标点击失败：%s" % e
            if self._script_click(text):
                return True
            return False
        if status == "ok-hidden":
            self._click_note = "元素不可见（已用脚本点击）"
            if self._script_click(text):
                self.log.info("候选「%s」不可见，改用脚本点击（隐藏副本常见于门户镜像 DOM）" % text)
                return True
            return False
        self._click_note = {"notfound": "页面上找不到该文本",
                            "error": "点击出错"}.get(status, str(status))
        return False

    def _script_click(self, text):
        """兜底：直接在页面里对元素调用 click()。"""
        js = r"""(function(txt){
          function scan(doc){
            var nodes=doc.querySelectorAll('a,button,[role=button],div,li,span,td');
            var fallback=null, hidden=null;
            for(var i=0;i<nodes.length;i++){
              var e=nodes[i];
              if(e.children.length>2) continue;
              var t=(e.innerText||'').replace(/\s+/g,' ').trim();
              if(t===txt) return e;
              if(t.indexOf(txt)>=0){
                var r=e.getBoundingClientRect();
                var visible=(r.width>0 && r.height>0);
                if(visible && !fallback) fallback=e;
                if(!visible && !hidden) hidden=e;
              }
            }
            return fallback || hidden;
          }
          var el=scan(document);
          if(!el){
            var frames=document.querySelectorAll('iframe,frame');
            for(var k=0;k<frames.length && !el;k++){
              try { var d=frames[k].contentDocument; if(d) el=scan(d); } catch(e) {}
            }
          }
          if(!el) return 'notfound';
          try { el.click(); } catch(e) { return 'error'; }
          return 'ok';
        })(%s)""" % _js(text)
        try:
            return self.ev(js) == "ok"
        except Exception:
            return False

    def _looks_nav(self, cand):
        """候选是不是纯页头/导航/面包屑（这类点了几乎不会真的跳转）。"""
        txt = (cand.get("txt") or "").strip()
        cls = cand.get("cls") or ""
        if txt in _NAV_EXACT:
            return True
        if _NAV_TEXT.search(txt):
            return True
        if _CHROME_CLASS.search(cls) and not _CONTENT_CLASS.search(cls):
            return True
        return False

    def _rank_matches(self, cfg, candidates, which="entry"):
        """给候选打分（关键词长度/命中数 + 所在区域），按分数降序返回。"""
        kws = [k for k in cfg.get("discovery", {}).get(
            "entry_keywords" if which == "entry" else "transit_keywords", []) if k]
        scored = []
        for c in candidates or []:
            txt = (c.get("txt") or "").strip()
            if not txt:
                continue
            low = txt.lower()
            hits = [k for k in kws if k.lower() in low]
            if not hits:
                continue
            score = 0.0
            for k in hits:
                # 关键词越长越具体；完全等于关键词再加分
                score += 2.0 + min(len(k), 12) / 12.0
                if low == k.lower():
                    score += 1.0
            cls = c.get("cls") or ""
            if _CHROME_CLASS.search(cls):
                score -= 1.0        # 页头/菜单/面包屑：点进去多数只是切路由
            if _CONTENT_CLASS.search(cls):
                score += 1.5        # 服务卡片/应用条目：更接近真正入口
            if _NAV_TEXT.search(txt) or txt in _NAV_EXACT:
                score -= 3.0        # 纯导航文本（面包屑/用户菜单/搜索框）：几乎必然空点
            if "访问量" in txt:
                score += 1.0        # 门户服务卡片常带「访问量：xxx」，是真正可点的应用
            if c.get("vis"):
                score += 1.5        # 可见元素优先；隐藏副本降权但仍可点
            else:
                score -= 0.5
            scored.append({"txt": txt, "score": score, "cls": cls, "tag": c.get("tag")})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored

    def _match_keyword(self, cfg, candidates, which="entry"):
        """返回得分最高的候选文本，没有命中返回 None。"""
        ranked = self._rank_matches(cfg, candidates, which)
        return ranked[0]["txt"] if ranked else None

    def _candidates(self):
        """当前页可点击候选（含同源 iframe 内），带 cls/vis 供打分区分区域与可见性。"""
        for _ in range(3):
            raw = self.ev(CANDIDATES_JS)
            items, seen = [], set()
            try:
                for x in (json.loads(raw) if raw else []):
                    t = (x or {}).get("txt")
                    if t and t not in seen:
                        seen.add(t)
                        items.append({"txt": t, "cls": (x or {}).get("cls", ""),
                                      "tag": (x or {}).get("tag", ""),
                                      "vis": bool((x or {}).get("vis"))})
            except (json.JSONDecodeError, TypeError, KeyError, AttributeError):
                items = []
            if items:
                return items
            time.sleep(0.6)
        return []

    def _focus(self, target_id=None):
        """把当前标签页切到前台：后台标签页会被节流，SPA 可能迟迟不渲染。"""
        try:
            self.client.activate(target_id)
        except Exception:
            pass

    def _wait_for_candidate(self, cfg, timeout=8.0):
        """等待 SPA 渲染出入口/中转候选（路由切换后列表是异步加载的）。

        页头和导航节点一开始就在 DOM 里，只匹配到这类文本时继续等：
        真正的服务卡片（带「访问量」或内容类 class）通常晚 1-3 秒才渲染出来。
        """
        deadline = time.time() + max(0.5, timeout)
        texts, entry, transit = [], None, None
        focused = False
        while True:
            texts = self._candidates()
            entry = self._match_keyword(cfg, texts, "entry")
            transit = self._match_keyword(cfg, texts, "transit")
            if entry or transit:
                best = [c for c in texts
                        if c["txt"] in (entry, transit)]
                contentish = [c for c in best if not self._looks_nav(c)]
                if contentish or time.time() >= deadline:
                    return texts, entry, transit
            if not focused:
                focused = True
                self._focus()
                continue
            if time.time() >= deadline:
                return texts, entry, transit
            time.sleep(0.8)

    def _page_state(self):
        """页面状态快照，用于判断一次点击是否真的改变了页面。

        除了顶层 URL，还把同源 iframe 的地址一起算进去：门户常把内容放在 iframe 里，
        子框架内部跳转不会改变顶层 URL。
        """
        js = r"""(function(){
          var out=[location.href];
          var fs=document.querySelectorAll('iframe,frame');
          for(var i=0;i<fs.length;i++){
            try { out.push(fs[i].contentWindow.location.href); } catch(e) { out.push('#'); }
          }
          return out.join(' | ');
        })()"""
        url, score = "", 0
        try:
            url = self.ev(js) or ""
        except Exception:
            url = ""
        try:
            score = (self.features() or {}).get("score", 0)
        except Exception:
            score = 0
        return {"url": url, "score": score, "ids": frozenset(self._page_ids())}

    def _click_and_verify(self, text, before_ids=None):
        """点击候选并确认页面确实变化（URL/特征分/标签页集合任一变化）。

        门户的页头面包屑点下去往往什么都不发生，若不加校验会一直空点同一批导航文本。
        """
        before = self._page_state()
        if not self._click_by_text(text):
            return False
        time.sleep(0.9)
        after = self._page_state()
        changed = (
            after["url"] != before["url"]
            or after["score"] != before["score"]
            or bool(after["ids"] - before["ids"])
        )
        if changed:
            return True
        self._click_note = "点击后页面没有任何变化"
        return False

    def _page_ids(self):
        """当前所有页面 target 的 id 集合（用于识别点击后新开的标签页）。"""
        try:
            return {p.get("id") for p in self.client.list_pages() if p.get("id")}
        except Exception:
            return set()

    def _adopt_new_tab(self, before_ids, max_wait=6.0):
        """点击后若跳转发生在新建标签页（门户/SSO 常见 target=_blank），把会话切过去。"""
        try:
            page = self.client.find_new_page(before_ids, wait=max_wait)
        except Exception as e:
            self.log.warn("检查新标签页失败：%s" % e)
            return False
        if not page:
            return False
        pid = page.get("id")
        if pid in self._adopted:
            return False
        url = (page.get("url") or "").strip()
        self.log.info("检测到新标签页，切换监听：%s" % url[:120])
        try:
            self.client.retarget(pid)
        except Exception as e:
            self.log.warn("切换到新标签页失败：%s" % e)
            return False
        self._adopted.add(pid)
        time.sleep(1.2)
        self._focus(pid)
        return True

    def _adopt_frame_target(self, min_score=5):
        """跨源 iframe(OOPIF) 在 CDP 中是独立 target：页面特征命中才切过去。"""
        try:
            frames = self.client.iframe_targets()
        except Exception:
            return False
        for t in frames:
            url = (t.get("url") or "").lower()
            if not any(k in url for k in ("stuworkpc", "secondclass", "activitycenter", "xgfw")):
                continue
            self.log.info("发现跨源 iframe 目标，切换监听：%s" % url[:120])
            try:
                self.client.retarget(t["id"])
            except Exception as e:
                self.log.warn("切换 iframe 目标失败：%s" % e)
                continue
            time.sleep(1.2)
            f = self.features()
            if self.is_target(f, min_score):
                return True
        return False

    def select_student_role(self):
        """角色选择页（#/role 或弹窗）自动选「学生」并确定；纯页面交互，不涉及报名。"""
        js = r"""(function(){
          function vis(e){
            var r=e.getBoundingClientRect();
            return r.width>0 && r.height>0;
          }
          function pick(list){
            var hit=null, i;
            for(i=0;i<list.length;i++){
              var t=(list[i].innerText||'').replace(/\s+/g,'').trim();
              if(t==='学生'){ hit=list[i]; break; }
            }
            return hit;
          }
          var items=[].slice.call(document.querySelectorAll('.el-select-dropdown__item,li'));
          var li=pick(items.filter(vis)) || pick(items);
          // 下拉项在折叠面板里时先点开选择框
          if(!li || !vis(li)){
            var sel=[].slice.call(document.querySelectorAll('.el-select,.el-input,.el-input__inner'))
              .filter(vis)[0];
            if(sel){ try{ sel.click(); }catch(e){} }
          }
          var out={opened:!!li, selected:false, confirmed:false};
          if(li){ try{ li.click(); out.selected=true; }catch(e){} }
          var btns=[].slice.call(document.querySelectorAll('button'));
          var ok=btns.filter(function(b){
            return /确\s*定/.test(b.innerText||'') && vis(b);
          })[0] || btns.filter(function(b){ return /确\s*定/.test(b.innerText||''); })[0];
          if(ok){ try{ ok.click(); out.confirmed=true; }catch(e){} }
          return JSON.stringify(out);
        })()"""
        try:
            raw = self.ev(js)
            self.log.info("角色页：自动选择「学生」并确定（%s）" % (raw or ""))
            return True
        except Exception as e:
            self.log.warn("角色页自动选择失败：%s" % e)
            return False

    # ---------- 登录表单：自动填入账号密码 ----------
    LOGIN_PROBE_JS = r"""(function(){
      function vis(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
      var inputs=[].slice.call(document.querySelectorAll('input')).filter(function(e){
        if(!vis(e))return false;
        var t=(e.type||'text').toLowerCase();
        return t!=='hidden'&&t!=='checkbox'&&t!=='radio'&&t!=='submit'&&t!=='button';
      });
      var pwd=inputs.filter(function(e){return (e.type||'').toLowerCase()==='password';})[0];
      var user=inputs.filter(function(e){
        if((e.type||'').toLowerCase()==='password')return false;
        var s=[e.placeholder,e.name,e.id,e.autocomplete,(e.getAttribute('aria-label')||'')].join(' ');
        return /学号|工号|账号|用户名|用户|username|user|login|account|student|job/i.test(s);
      })[0];
      if(!user){
        user=inputs.filter(function(e){
          var t=(e.type||'text').toLowerCase();
          return t==='text'||t===('');
        })[0]||null;
      }
      var cap=inputs.filter(function(e){
        var s=[e.placeholder,e.name,e.id].join(' ');
        return /验证码|captcha|verify|checkcode/i.test(s);
      })[0]||null;
      var capImg=null;
      [].slice.call(document.querySelectorAll('img')).forEach(function(im){
        // 只认可见的验证码图片：真实页面常把验证码框放在隐藏分支里
        if(vis(im)&&/captcha|verify|checkcode/i.test(im.src||''))capImg=im;
      });
      var btns=[].slice.call(document.querySelectorAll('button,input[type=submit],a'));
      var submit=btns.filter(function(b){
        return /登\s*录|登\s*入|提\s*交|log\s*in|sign\s*in/i.test(b.innerText||b.value||'')&&vis(b);
      })[0]||null;
      return JSON.stringify({user:!!user, pwd:!!pwd, captcha:!!cap||!!capImg,
        submit:!!submit, url:location.href});
    })()"""

    LOGIN_FILL_JS = r"""(function(u,p){
      function vis(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
      function setv(el,val){
        if(!el||val===undefined||val===null)return false;
        try{
          var d=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value');
          if(d&&d.set){ d.set.call(el,val); } else { el.value=val; }
        }catch(e){ try{ el.value=val; }catch(e2){ return false; } }
        ['input','change','blur'].forEach(function(n){
          try{ el.dispatchEvent(new Event(n,{bubbles:true})); }catch(e){}
        });
        return true;
      }
      var inputs=[].slice.call(document.querySelectorAll('input')).filter(function(e){
        if(!vis(e))return false;
        var t=(e.type||'text').toLowerCase();
        return t!=='hidden'&&t!=='checkbox'&&t!=='radio'&&t!=='submit'&&t!=='button';
      });
      var pwd=inputs.filter(function(e){return (e.type||'').toLowerCase()==='password';})[0]||null;
      var user=inputs.filter(function(e){
        if((e.type||'').toLowerCase()==='password')return false;
        var s=[e.placeholder,e.name,e.id,e.autocomplete,(e.getAttribute('aria-label')||'')].join(' ');
        return /学号|工号|账号|用户名|用户|username|user|login|account|student|job/i.test(s);
      })[0]||null;
      if(!user){
        user=inputs.filter(function(e){
          var t=(e.type||'text').toLowerCase();
          return t==='text'||t==='';
        })[0]||null;
      }
      var okUser=setv(user,u), okPwd=setv(pwd,p);
      return JSON.stringify({user:!!user, pwd:!!pwd, filled_user:okUser, filled_pwd:okPwd});
    })"""

    LOGIN_SUBMIT_JS = r"""(function(){
      function vis(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
      var cand=[].slice.call(document.querySelectorAll('button,input[type=submit],a,.el-button'));
      var btn=cand.filter(function(b){
        return /登\s*录|登\s*入|提\s*交|log\s*in|sign\s*in/i.test(b.innerText||b.value||'')&&vis(b);
      })[0]||null;
      if(btn){ try{ btn.click(); return 'click'; }catch(e){} }
      // 兜底：在密码框上回车提交
      var pwd=document.querySelector('input[type=password]');
      if(pwd){
        ['keydown','keypress','keyup'].forEach(function(n){
          try{
            pwd.dispatchEvent(new KeyboardEvent(n,{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true}));
          }catch(e){}
        });
        var form=pwd.closest('form');
        if(form){ try{ form.submit(); return 'form'; }catch(e){} }
        return 'enter';
      }
      return 'none';
    })()"""

    def login_probe(self):
        """探测当前页面是否有登录表单 / 验证码 / 提交按钮。"""
        try:
            raw = self.ev(self.LOGIN_PROBE_JS)
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def fill_login(self, cfg):
        """自动填入界面里配置的账号密码（ElementUI/Vue 需原生 setter + input 事件）。"""
        login = (cfg or {}).get("login", {}) or {}
        user = login.get("username", "") or ""
        pwd = login.get("password", "") or ""
        if not (user and pwd):
            return {"ok": False, "reason": "未配置账号密码"}
        try:
            raw = self.ev("(%s)(%s,%s)" % (self.LOGIN_FILL_JS.lstrip(), _js(user), _js(pwd)))
            info = json.loads(raw) if raw else {}
        except Exception as e:
            return {"ok": False, "reason": "填入失败：%s" % e}
        if info.get("filled_user") and info.get("filled_pwd"):
            self.log.info("已自动填入账号密码（学号 %s）" % user)
            return {"ok": True, "info": info}
        return {"ok": False, "reason": "未找到账号或密码输入框", "info": info}

    def submit_login(self):
        """点击登录按钮（纯页面交互，不读取验证码）。"""
        try:
            how = self.ev(self.LOGIN_SUBMIT_JS)
            return how or "none"
        except Exception:
            return "error"

    def auto_login(self, cfg, wait_seconds=20.0, captcha_wait=240.0):
        """自动登录：填账号密码 →（有验证码时等人输入）→ 提交 → 等登录表单消失。

        返回 dict: status = ok / need_human（验证码或未配置）/ failed
        """
        probe = self.login_probe()
        if not probe:
            return {"status": "failed", "reason": "未检测到登录表单"}
        # 即使有验证码也先把账号密码填好：验证码不识别，但账号密码不用再手打
        filled = self.fill_login(cfg)
        if not filled.get("ok"):
            self.log.warn("自动填入账号密码失败：%s" % filled.get("reason"))
            if probe.get("captcha"):
                return {"status": "need_human", "reason": filled.get("reason"),
                        "probe": probe}
            return {"status": "need_human", "reason": filled.get("reason"), "probe": probe}
        if probe.get("captcha"):
            self.log.info("登录页需要验证码：账号密码已自动填好，请在弹出的浏览器里输入验证码"
                          "（输入后会自动提交，最长等 %d 秒）" % int(captcha_wait))
            if not self._wait_captcha(captcha_wait):
                self.log.warn("等待验证码超时，需要人工处理")
                return {"status": "need_human", "reason": "等待验证码超时", "probe": probe}
            self.log.info("已检测到验证码输入，自动提交登录")
        how = self.submit_login()
        self.log.info("已自动提交登录（%s），等待登录结果" % how)
        deadline = time.time() + wait_seconds
        retried_captcha = False
        while time.time() < deadline:
            time.sleep(1.0)
            f = self.features()
            if not f.get("pwd"):
                self.log.success("自动登录成功，已离开登录页")
                return {"status": "ok", "features": f}
            if f.get("captcha") and not retried_captcha:
                # 有些统一认证页第一次提交后才弹出验证码：填好后自动再提交一次
                retried_captcha = True
                self.log.info("登录页弹出验证码：账号密码已填好，请在浏览器里输入验证码"
                              "（输入后自动再提交一次，最长等 %d 秒）" % int(captcha_wait))
                if not self._wait_captcha(captcha_wait):
                    self.log.warn("等待验证码超时，需要人工处理")
                    return {"status": "need_human", "reason": "等待验证码超时", "probe": f}
                self.submit_login()
                self.log.info("验证码已输入，自动提交登录")
                continue
        return {"status": "failed", "reason": "自动登录超时（仍停留在登录页）"}

    def _wait_captcha(self, timeout=240.0):
        """等人在页面上把验证码填进去（只读输入框长度，不读图片内容）。"""
        js = r"""(function(){
          var ins=[].slice.call(document.querySelectorAll('input'));
          for(var i=0;i<ins.length;i++){
            var e=ins[i];
            var key=((e.id||'')+' '+(e.name||'')+' '+(e.placeholder||'')+' '+(e.className||'')).toLowerCase();
            if(/captcha|checkcode|verify|验证码/.test(key)){
              var v=(e.value||'').trim();
              if(v.length>=3) return 'filled';
            }
          }
          return 'empty';
        })()"""
        deadline = time.time() + max(10.0, timeout)
        while time.time() < deadline:
            try:
                if self.ev(js) == "filled":
                    return True
            except Exception:
                pass
            time.sleep(1.0)
        return False

    def login_url(self, cfg):
        """统一身份认证地址：优先用户配置，其次按校园域名推导（authserver.<根域名>）。"""
        login = (cfg or {}).get("login", {}) or {}
        url = (login.get("login_url") or "").strip()
        if url:
            return url
        anchor = (login.get("anchor_url") or "").strip()
        if not anchor:
            return ""
        try:
            from urllib.parse import urlparse, quote
            host = (urlparse(anchor).netloc or "").split(":")[0]
        except Exception:
            return ""
        if not host:
            return ""
        parts = host.split(".")
        base = ".".join(parts[-3:]) if len(parts) >= 3 else host
        return "https://authserver.%s/authserver/login?service=%s" % (
            base, quote(anchor, ""))

    def goto_login(self, cfg):
        """直接打开统一身份认证页（门户的「登录」按钮在后台标签页里点不动时的兜底）。

        这不是绕过登录：打开的就是门户「登录」按钮指向的标准身份认证页。
        """
        url = self.login_url(cfg)
        if not url:
            return None
        self.log.info("直接打开统一身份认证页（门户「登录」按钮在后台标签页无法触发）：%s"
                      % url[:110])
        try:
            self.client.navigate(url, timeout=25)
        except Exception as e:
            self.log.warn("打开统一身份认证页失败：%s" % e)
            return None
        time.sleep(1.5)
        self._focus()
        return self.features()

    def _settle(self, cfg, before_ids=None, timeout=10.0):
        """点击后等待跳转落定：跟新标签页/跨源 iframe，返回 (status, features)。

        status: target（已到抢课页）/ human（登录或验证码）/ timeout（还在路上）。
        """
        min_score = cfg.get("discovery", {}).get("min_score", 5)
        deadline = time.time() + max(0.5, timeout)
        f = {}
        role_tries = 0
        while time.time() < deadline:
            f = self.features() or f
            if self.is_target(f, min_score):
                return "target", f
            if f.get("pwd") or f.get("captcha"):
                return "human", f
            if f.get("roleDlg"):
                # 角色选择页：自动选“学生”（页面交互，不涉及提交）
                if role_tries < 3:
                    role_tries += 1
                    self.select_student_role()
                    time.sleep(1.5)
                    continue
            else:
                # 跳转可能落在新建标签页或跨源 iframe(OOPIF) 里
                if before_ids and self._adopt_new_tab(before_ids, max_wait=0.8):
                    f = self.features() or f
                    if self.is_target(f, min_score):
                        return "target", f
                    continue
                if self._adopt_frame_target(min_score):
                    f = self.features() or f
                    if self.is_target(f, min_score):
                        return "target", f
                    continue
            time.sleep(1.0)
        return "timeout", f

    def _try_click(self, cfg, texts, which="entry", before_ids=None):
        """按打分顺序尝试点击候选，跳过点不动的项，返回成功点击（页面已变化）的文本。"""
        for item in self._rank_matches(cfg, texts, which):
            if self._click_and_verify(item["txt"], before_ids):
                return item["txt"]
            self.log.info("候选「%s」跳过（%s），换下一个" % (item["txt"], self._click_note))
        return None

    def discover(self, cfg, current_page_id=None, force=False):
        """执行发现，返回当前标签页 id（已定位到抢课页）与特征。

        流程：缓存校验 → 锚点 → 语义发现 → 逐级导航 → 特征确认。
        抛 DiscoveryError 表示全部失败。
        """
        min_score = cfg.get("discovery", {}).get("min_score", 5)
        anchor = cfg.get("login", {}).get("anchor_url", "")
        if not anchor:
            raise DiscoveryError("未配置校园门户锚点(anchor_url)")
        self._anchor = anchor

        # ---- L1 缓存复用 ----
        if not force:
            cache = load_entry_cache()
            cached_url = cache.get("last_success_url")
            if cached_url and cache.get("anchor") == anchor:
                self.log.info("L1 尝试复用缓存入口")
                try:
                    self.client.navigate(cached_url, timeout=20)
                    time.sleep(1.5)
                    f = self.features()
                    if f.get("roleDlg"):
                        # 缓存页落在角色选择（登录态仍在）：自动选学生后再判定
                        self.select_student_role()
                        status, f = self._settle(cfg, None, timeout=15.0)
                        if status == "target":
                            self.log.info("L1 缓存入口有效（已自动选择角色），复用成功")
                            return self._result("cache", f, f.get("url", cached_url))
                    if self.is_target(f, min_score):
                        self.log.info("L1 缓存入口有效，复用成功")
                        return self._result("cache", f, cached_url)
                    self.log.info("L1 缓存入口已失效，清除后重新发现")
                except Exception as e:
                    self.log.info("L1 缓存入口不可达：%s" % e)
                self._clear_cache()

        # ---- L0 锚点 ----
        self.log.info("L0 打开校园门户锚点")
        try:
            self.client.navigate(anchor, timeout=30)
        except Exception as e:
            raise DiscoveryError("打开校园锚点失败：%s" % e)
        time.sleep(2)
        self._focus()
        f = self.features()
        if f.get("roleDlg"):
            # 角色页不算“需要人工”：账号多角色时自动选「学生」
            self.log.info("锚点落在角色选择页，自动选择「学生」")
            self.select_student_role()
            status, f = self._settle(cfg, None, timeout=15.0)
            if status == "target":
                return self._result("discover", f, f.get("url", ""))
        if self.needs_human(f):
            self.log.warn("发现阶段遇到登录/验证码，需人工处理")
            return self._result("need_human", f, anchor)
        if self.is_target(f, min_score):
            return self._result("anchor", f, anchor)

        # 门户未登录：页头是「登录」按钮，先点它进统一身份认证，登录由 S3 自动完成
        if f.get("loginBtn"):
            self.log.info("门户处于未登录状态（页头有「登录」按钮），尝试点击进入统一身份认证")
            before = self._page_ids()
            if self._click_by_text("登录") or self._click_by_text("立即登录") \
                    or self._click_by_text("登录系统"):
                status, f2 = self._settle(cfg, before, timeout=12.0)
                if status == "target":
                    return self._result("discover", f2, f2.get("url", ""))
                if status == "human":
                    # 登录页：交给引擎 S3 自动填入账号密码；缺配置时会转人工
                    self.log.info("已进入统一身份认证页，交由自动登录处理")
                    return self._result("login", f2 or f, anchor)
            else:
                self.log.warn("未能点击门户「登录」按钮，继续尝试页面内的入口发现")
            # 后台标签页里脚本/鼠标点击都可能被浏览器忽略：直接打开标准登录地址
            f2 = self.goto_login(cfg)
            if f2 and (f2.get("pwd") or f2.get("captcha")):
                self.log.info("已打开统一身份认证页，交由自动登录处理")
                return self._result("login", f2, f2.get("url", "") or anchor)

        # ---- L2 门户语义发现 ----
        self.log.info("L2 门户语义发现")
        before = self._page_ids()
        texts = self._wait_for_candidate(cfg, timeout=6.0)[0]
        hit = self._try_click(cfg, texts, "entry")
        if hit:
            self.log.info("L2 命中入口候选：%s" % hit)
            status, f = self._settle(cfg, before, timeout=12.0)
            if status == "target":
                return self._result("discover", f, f.get("url", ""))
            if status == "human":
                return self._result("need_human", f, f.get("url", ""))

        # ---- L3 逐级导航（办事大厅 → 学生综合服务系统 → 二课-活动中心）----
        clicked = []
        for hop in range(L3_MAX_HOPS):
            self.log.info("L3 逐级导航（第 %d 跳）" % (hop + 1))
            texts, entry, transit = self._wait_for_candidate(cfg)
            order = []
            for which in ("entry", "transit"):
                for item in self._rank_matches(cfg, texts, which):
                    if item["txt"] not in clicked:
                        order.append((which, item["txt"]))
            if not order:
                self.log.warn("第 %d 跳没有可点击的入口/中转候选（页面可能仍在加载）" % (hop + 1))
                break

            which = pick = None
            before = self._page_ids()
            for w, txt in order:
                if self._click_and_verify(txt, before):
                    which, pick = w, txt
                    break
                self.log.info("候选「%s」跳过（%s），换下一个" % (txt, self._click_note))
            if not pick:
                break
            clicked.append(pick)
            self.log.info("L3 点击%s：%s" % ("入口" if which == "entry" else "中转", pick))

            # 入口点击后可能经 SSO 跳到新标签页，等久一点；中转点击只等页面渲染
            status, f = self._settle(cfg, before, timeout=12.0 if which == "entry" else 4.0)
            if status == "target":
                return self._result("discover", f, f.get("url", ""))
            if status == "human":
                return self._result("need_human", f, f.get("url", ""))

        # ---- L4 最终判定 ----
        f = self.features()
        if self.is_target(f, min_score):
            return self._result("discover", f, f.get("url", ""))
        if f.get("roleDlg"):
            self.log.info("最终页为角色选择页，自动选择「学生」")
            self.select_student_role()
            status, f = self._settle(cfg, None, timeout=15.0)
            if status == "target":
                return self._result("discover", f, f.get("url", ""))
        if self.needs_human(f):
            return self._result("need_human", f, f.get("url", ""))
        # 未登录时页头会一直挂着「登录」按钮：与其报“找不到”，不如把登录页交给 S3
        if f.get("loginBtn"):
            self.log.info("最终页仍是未登录门户（页头有「登录」按钮），尝试进入统一身份认证")
            before = self._page_ids()
            if self._click_by_text("登录") or self._click_by_text("立即登录"):
                status, f2 = self._settle(cfg, before, timeout=12.0)
                if status == "target":
                    return self._result("discover", f2, f2.get("url", ""))
                if status == "human":
                    return self._result("login", f2 or f, f.get("url", ""))
            f2 = self.goto_login(cfg)
            if f2 and (f2.get("pwd") or f2.get("captcha")):
                return self._result("login", f2, f2.get("url", "") or f.get("url", ""))
        top = []
        try:
            top = [c["txt"] for c in self._rank_matches(cfg, self._candidates(), "entry")[:5]]
        except Exception:
            top = []
        hint = "（页面处于未登录状态，可先填左侧「登录信息」让程序自动登录）" \
            if f.get("loginBtn") else ""
        raise DiscoveryError("四级发现均未定位到抢课页（特征分 %s）%s；当前页面候选：%s"
                             % (f.get("score", 0), hint, top or "无"))

    def _clear_cache(self):
        save_entry_cache({})

    def _result(self, source, features, url):
        if url and source in ("cache", "discover", "anchor"):
            cache = load_entry_cache()
            cache.update({
                "anchor": self._anchor or self._anchor_of(url),
                "last_success_url": url,
                "discovered_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "feature_signature": {
                    "act_item": features.get("actItems", 0) > 0,
                    "reg_button": features.get("regBtns", 0) > 0,
                    "score": features.get("score", 0)
                }
            })
            save_entry_cache(cache)
            self.log.info("入口缓存已更新，来源=%s" % source)
        return {"source": source, "features": features, "url": url}

    @staticmethod
    def _anchor_of(url):
        """从 URL 取域名+协议作为锚点片段（仅用于缓存标记）。"""
        try:
            from urllib.parse import urlparse
            p = urlparse(url)
            return "%s://%s/" % (p.scheme, p.netloc)
        except Exception:
            return ""
