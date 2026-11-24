"""
课程观察模块（方案 3）—— 活动快照差异检测 + 分类解析 + 筛选。

职责：解析活动列表 DOM，维护上一轮快照，识别"新活动 / 由满变可报 / 由未开始变可报"，
      并按用户筛选条件（校区/五育/层级/关键词）判断是否命中。
纯逻辑模块：不驱动浏览器、不发请求、不提交表单；输入为 DOM 解析出的课程列表。
"""
import datetime
import json
import re


# 从活动中心页面解析课程列表（含本校真实字段）
SCAN_JS = r"""(function(){
  var cards=[].slice.call(document.querySelectorAll('.act-item, [class*=act-item]'));
  var out=cards.map(function(c,i){
    var t=(c.innerText||'').replace(/\s+/g,' ');
    var title=(c.querySelector('.title')||{}).innerText||'';
    var quota='';
    // 余量文本在真实页面可能是「4/无限」也可能是「4 /无限」（数字与斜杠间有空格）
    var m=t.match(/已报名\s*\/\s*报名上限\s*[：:]?\s*([^\s\/]+)\s*\/\s*([^\s]+)/);
    if(m) quota=m[1]+'/'+m[2];
    else {
      var m2=t.match(/已报名\s*\/\s*报名上限\s*[：:]?\s*([^\s]+)/);
      if(m2) quota=m2[1];
    }
    var times='';
    var tm=t.match(/活动开始时间\s*[：:]?\s*([0-9\-: ]+)/);
    if(tm) times=tm[1].trim();
    // 报名开始时间：到点才可抢的关键字段（"-" 表示无独立报名开始时间）
    var regStart='';
    var rs=t.match(/报名开始时间\s*[：:]?\s*([0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:\s+[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)?)/);
    if(rs) regStart=rs[1].trim();
    var regEnd='';
    var re2=t.match(/报名结束时间\s*[：:]?\s*([0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:\s+[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)?)/);
    if(re2) regEnd=re2[1].trim();
    var campus='', level='', category='';
    var tags=[].slice.call(c.querySelectorAll('[class*=acttype-tag], .acttype-tag'))
      .map(function(e){return (e.innerText||'').trim();});
    for(var k=0;k<tags.length;k++){
      if(/德育|智育|体育|美育|劳育/.test(tags[k])) category=tags[k];
    }
    if(/校级/.test(t)) level='校级'; else if(/院级/.test(t)) level='院级';
    // 本校校区：金海 / 嘉善 / 金桥 / 长寿路（优先全称匹配）
    var cm=t.match(/(金海校区|嘉善校区|金桥校区|长寿路校区|长寿校区)/);
    if(cm) campus=cm[1];
    else{ var cm2=t.match(/(金海|嘉善|金桥|长寿路?)/); if(cm2) campus=cm2[1]; }
    return {index:i, title:title.trim(), quota:quota, time:times,
            reg_start:regStart, reg_end:regEnd,
            campus:campus, level:level, category:category, tags:tags,
            raw:t.slice(0,200)};
  });
  return JSON.stringify(out);
})()"""


# 切到官网抢课页上的「能报的二课」分类（该分类只列当前可报名的活动）
AVAILABLE_TAB_JS = r"""(function(){
  function vis(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
  var nodes=document.querySelectorAll('div,span,li,a,button,label,p,td');
  var best=null, near=[];
  for(var i=0;i<nodes.length;i++){
    var e=nodes[i];
    if(e.children.length>2) continue;
    var t=(e.innerText||'').replace(/\s+/g,' ').trim();
    if(!t||t.length>20) continue;
    if(!vis(e)) continue;
    if(/二课/.test(t) && near.length<6 && near.indexOf(t)<0) near.push(t);
    if(t.length>16) continue;
    if(!/能报的二课|能报|可报名|可报/.test(t)) continue;
    // 文本越短越精确（避免点到包裹整块列表的容器）
    if(!best||t.length<best.t.length) best={el:e,t:t};
  }
  if(!best) return JSON.stringify({found:false,near:near});
  var cls='';
  try{ cls=(best.el.className||'').toString(); }catch(e){}
  var active=/active|selected|current|checked/i.test(cls);
  var res={found:true,txt:best.t,cls:cls.slice(0,80),active:active,clicked:false};
  if(!active){
    try{ best.el.scrollIntoView({block:'center'}); }catch(e){}
    try{ best.el.click(); res.clicked=true; }catch(e){ res.err=String(e); }
  }
  return JSON.stringify(res);
})()"""


def parse_courses(raw_json):
    raw = raw_json
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    # 打上「当前可报名」标记：页面分类没切成功时，程序仍能只显示能报的二课
    for c in raw:
        if isinstance(c, dict):
            c["available"] = is_available(c.get("quota"))
    return raw


def is_available(quota):
    """余量是否可抢：'0/无限' 视为可报；'n/N' 且 n<N 可报。"""
    if not quota:
        return False
    q = str(quota).replace(" ", "")
    if "/" not in q:
        return False
    a, b = q.split("/", 1)
    if b in ("无限", "∞", "-1", "不限"):
        return True
    try:
        return int(a) < int(b)
    except (ValueError, TypeError):
        return False


def course_key(course):
    """活动唯一标识：优先名称+时间（页面无稳定 ID 时的近似）。"""
    return "%s|%s" % (course.get("title", ""), course.get("time", ""))


def parse_dt(text):
    """把页面上的 '2026-11-25 12:00' / '2026-11-25 12:00:00' 解析为 datetime。"""
    s = str(text or "").strip().replace("T", " ")
    if not s or s == "-":
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$", s)
    if not m:
        return None
    y, mo, d, hh, mm, ss = m.groups()
    try:
        return datetime.datetime(int(y), int(mo), int(d),
                                 int(hh or 0), int(mm or 0), int(ss or 0))
    except ValueError:
        return None


def match_filters(course, target_cfg):
    """按用户筛选条件判断是否命中。全部条件为 AND；空条件表示不限制。"""
    t = target_cfg or {}
    kw = [k.strip() for k in t.get("keywords", []) if k and k.strip()]
    if kw and not any(k in course.get("title", "") for k in kw):
        return False, "名称关键词不匹配"
    campus = (t.get("campus") or "").strip()
    if campus:
        known = (course.get("campus") or "").strip()
        if known:
            # 卡片里明确写明了校区：不在其中才算不匹配
            if campus not in known:
                return False, "校区不匹配"
        # 真实活动列表卡片只写教室（如“3109新型教室”），没有校区字段：
        # 这种情况下不因为“校区未知”而丢弃课程，交由地址/名称关键词进一步判断
    category = (t.get("category") or "").strip()
    if category and category not in (course.get("category", "") + " " + course.get("raw", "")):
        return False, "五育类别不匹配"
    level = (t.get("level") or "").strip()
    if level and level not in (course.get("level", "") + " " + course.get("raw", "")):
        return False, "层级不匹配"
    if t.get("only_when_available", True) and not is_available(course.get("quota")):
        return False, "当前不可报"
    return True, "命中筛选"


class CourseWatcher:
    def __init__(self, log):
        self.log = log
        self._snapshot = {}     # key -> {quota, available}
        self._seen = set()      # 已见过的活动 key

    def reset(self):
        self._snapshot = {}
        self._seen = set()

    def diff(self, courses):
        """比对快照，返回诊断信息。

        返回 dict:
          new: 新出现的活动列表
          became_available: 由满/不可报变为可报的活动列表
          unchanged: 无变化数量
        """
        new, became = [], []
        current = {}
        for c in courses:
            key = course_key(c)
            avail = is_available(c.get("quota"))
            current[key] = {"quota": c.get("quota"), "available": avail}
            if key not in self._seen:
                new.append(c)
                self._seen.add(key)
            else:
                prev = self._snapshot.get(key, {})
                if avail and not prev.get("available", False):
                    became.append(c)
        unchanged = max(0, len(courses) - len(new) - len(became))
        self._snapshot = current
        return {"new": new, "became_available": became, "unchanged": unchanged}

    def pick_candidates(self, courses, target_cfg, keyset,
                        selected=None, ignore_availability=False):
        """筛出本次可抢且未处理过的课程，返回 [(course, reason)]。

        selected: 用户点选的课程键列表；非空时只抢这些课程。
        ignore_availability: 到点抢课时，页面余量字段可能还没刷新（如"未开始"），
                             此时按用户点选直接尝试。
        """
        sel = [s for s in (selected or []) if s]
        out = []
        for c in courses:
            key = course_key(c)
            if key in keyset:
                continue
            if sel and key not in sel:
                continue
            if not ignore_availability and not is_available(c.get("quota")):
                continue
            if sel:
                # 用户点选的课程优先于关键词/类别筛选，只保留“可报”与“未处理”两个约束
                out.append((c, "选中的目标课程"))
                continue
            ok, reason = match_filters(
                c, dict(target_cfg or {}, only_when_available=not ignore_availability))
            if ok:
                out.append((c, reason))
        return out


def selected_courses(courses, selected):
    """按用户点选的键，取出当前页面上的课程对象（保持点击顺序）。"""
    index = {course_key(c): c for c in courses}
    return [index[k] for k in (selected or []) if k in index]


def next_reg_start(courses, selected):
    """选中课程里最近的、仍未到点的「报名开始时间」；没有则 None。"""
    now = datetime.datetime.now()
    best = None
    for c in selected_courses(courses, selected):
        dt = parse_dt(c.get("reg_start"))
        if not dt or dt <= now:
            continue
        if best is None or dt < best[1]:
            best = (c, dt)
    return best
