'use strict';
const $ = (id) => document.getElementById(id);
let lastLogId = 0;
let currentConfig = null;
// 用户点选的目标课程（键：title|time）。
// selected = 界面上“正在挑选”的集合；confirmed = 已点「确认选课」写回服务端的集合。
// 只有 confirmed 里的课程才会被引擎到点抢；点卡片本身不会立刻提交。
let selected = new Set();
let confirmed = new Set();
let courseEls = new Map();   // key -> DOM 节点，做增量渲染，避免每秒重绘
let lastDueKey = null;
let selBusy = 0;
let prevStats = {};

function val(id){return $(id)?$(id).value.trim():''}

/* ---------- 放课时间：datetime-local <-> ISO ---------- */
function toISO(rt){
  // datetime-local 值形如 2026-11-20T12:00 或 2026-11-20T12:00:00
  if(!rt) return '';
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(rt) ? rt+':00' : rt;
}
function toLocalInput(iso){
  if(!iso) return '';
  return String(iso).slice(0,16); // yyyy-MM-ddTHH:mm
}

function fillForm(cfg){
  const L=cfg.login||{}, T=cfg.target||{}, S=cfg.schedule||{}, G=cfg.strategy||{};
  $('username').value=''; // 脱敏，不回填
  $('password').value='';
  setSelHint(T);
  selected=new Set((T.selected||[]).map(String));
  confirmed=new Set(selected);
  updateConfirmUI();
  const waitEl=$('wait_reg_start');
  if(waitEl) waitEl.checked=T.wait_reg_start!==false;
  $('anchor_url').value=L.anchor_url||'';
  // 守候模式已隐藏：界面上只有「定时」一个选项，旧配置里的 both/watch 一律按定时显示
  document.querySelectorAll('input[name=mode]').forEach(r=>{r.checked=(r.value==='scheduled');});
  $('campus').value=T.campus||'';
  $('category').value=T.category||'';
  $('level').value=T.level||'';
  $('keywords').value=(T.keywords||[]).join('\n');
  $('release_time').value=toLocalInput(S.release_time);
  $('lead_minutes').value=S.lead_minutes??10;
  $('lead_seconds').value=S.lead_seconds??30;
  $('window_seconds').value=S.window_seconds??120;
  $('poll_interval_normal').value=G.poll_interval_normal??10;
  $('poll_interval_burst').value=G.poll_interval_burst??1;
  $('max_retries').value=G.max_retries??3;
  $('retry_backoff').value=G.retry_backoff??2;
  $('max_courses').value=G.max_courses??5;
  updateReleaseUI();
}

function buildConfig(){
  const modeEl=document.querySelector('input[name=mode]:checked');
  // 账号密码留空表示“沿用已保存的值”，避免脱敏回填把已存凭据冲掉
  const login={anchor_url:val('anchor_url')};
  if(val('username')) login.username=val('username');
  if(val('password')) login.password=val('password');
  return {config:{
    login:login,
    mode:(modeEl && modeEl.value) || 'scheduled',
    target:{
      keywords:val('keywords').split('\n').map(s=>s.trim()).filter(Boolean),
      only_when_available:true, campus:val('campus'),
      category:val('category'), level:val('level'),
      selected:[...selected],
      wait_reg_start:$('wait_reg_start')?$('wait_reg_start').checked:true
    },
    schedule:{
      release_time:toISO(val('release_time')),
      lead_minutes:+val('lead_minutes'), lead_seconds:+val('lead_seconds'),
      window_seconds:+val('window_seconds')
    },
    strategy:{
      poll_interval_normal:+val('poll_interval_normal'), poll_interval_burst:+val('poll_interval_burst'),
      max_retries:+val('max_retries'), retry_backoff:+val('retry_backoff'), max_courses:+val('max_courses'),
      stop_on_captcha:true
    }
  }};
}

/* ---------- 放课时间：倒计时 + 时间线 ---------- */
function fmtClock(d){
  const p=n=>String(n).padStart(2,'0');
  return p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
}
function fmtDur(ms){
  if(ms<0) ms=0;
  const s=Math.floor(ms/1000);
  const d=Math.floor(s/86400), h=Math.floor(s%86400/3600), m=Math.floor(s%3600/60), sec=s%60;
  if(d>0) return d+' 天 '+h+' 小时 '+m+' 分';
  if(h>0) return h+' 小时 '+m+' 分 '+sec+' 秒';
  if(m>0) return m+' 分 '+sec+' 秒';
  return sec+' 秒';
}

function updateReleaseUI(){
  const hint=$('rtHint'), tl=$('rtTimeline');
  if(!hint||!tl) return;
  const rt=val('release_time');
  if(!rt){
    hint.innerHTML='未设置放课时刻：启动后立即开始刷新课程与余量（选课确认后到点照常抢）。';
    tl.innerHTML='';
    return;
  }
  const t=new Date(toISO(rt));
  if(isNaN(t)){
    hint.innerHTML='<span class="warn-text">放课时间格式无法识别，请重新选择。</span>';
    tl.innerHTML='';
    return;
  }
  const now=new Date();
  const leadMs=(+val('lead_minutes')||0)*60*1000;
  const hsMs=(+val('lead_seconds')||0)*1000;
  const winMs=(+val('window_seconds')||0)*1000;
  const start=new Date(t.getTime()-leadMs);   // 启动准备时刻
  const burst=new Date(t.getTime()-hsMs);     // 进入高频时刻
  const end=new Date(t.getTime()+winMs);      // 窗口结束时刻

  const parts=[];
  parts.push('距放课 <b>'+fmtDur(t-now)+'</b>'+(t<now?'（已过）':''));
  parts.push(fmtClock(start)+' 启动准备');
  parts.push(fmtClock(burst)+' 起进入高频');
  parts.push(fmtClock(end)+' 窗口结束');
  let warn='';
  if(t.getTime()<now.getTime()-winMs) warn='<span class="warn-text">放课时间已过，请更新时间（守候模式已隐藏，不会再自动盯新活动）。</span>';
  else if(leadMs<5*60*1000) warn='<span class="warn-text">提前量偏小（建议 ≥5 分钟），可能来不及完成登录与就绪。</span>';
  hint.innerHTML=parts.join(' &nbsp;·&nbsp; ')+(warn?'<br>'+warn:'');

  // 时间线：[启动准备 → 高频起点 → 放课 → 窗口结束]
  const span=Math.max(end-start, 1);
  const pos=(x)=>Math.min(100,Math.max(0,(x-start)/span*100));
  tl.innerHTML=
    '<div class="tl-bar">'+
      '<div class="tl-seg prep" style="width:'+pos(burst)+'%"></div>'+
      '<div class="tl-seg burst" style="width:'+(pos(end)-pos(burst))+'%"></div>'+
    '</div>'+
    '<div class="tl-labels">'+
      '<span>'+fmtClock(start)+'</span>'+
      '<span class="tl-mid">放课 '+fmtClock(t)+'</span>'+
      '<span>'+fmtClock(end)+'</span>'+
    '</div>'+
    '<div class="tl-legend"><span class="k prep"></span>启动准备<span class="k burst"></span>高频窗口</div>';
}

function bindReleaseUI(){
  ['release_time','lead_minutes','lead_seconds','window_seconds'].forEach(id=>{
    const el=$(id);
    if(el){el.addEventListener('input',updateReleaseUI);el.addEventListener('change',updateReleaseUI);}
  });
  document.querySelectorAll('.chip[data-rt]').forEach(btn=>{
    btn.addEventListener('click',()=>{
      const v=btn.dataset.rt;
      if(v==='clear'){$('release_time').value='';updateReleaseUI();return;}
      const d=new Date();
      if(v.indexOf('tmr')===0) d.setDate(d.getDate()+1);
      const h=(v.indexOf('12')>=0)?12:20;
      d.setHours(h,0,0,0);
      const p=n=>String(n).padStart(2,'0');
      $('release_time').value=d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+'T'+p(d.getHours())+':'+p(d.getMinutes());
      updateReleaseUI();
    });
  });
}

async function api(path, method='GET', body=null){
  const opt={method, headers:{'Content-Type':'application/json'}};
  if(body) opt.body=JSON.stringify(body);
  const r=await fetch(path,opt);
  return r.json();
}

async function saveConfig(){
  const rt=val('release_time');
  if(rt){
    const t=new Date(toISO(rt));
    if(!isNaN(t)&&t.getTime()<Date.now()-2*3600*1000){
      if(!confirm('放课时间早于当前时间 2 小时以上，仍要保存吗？')) return;
    }
  }
  const res=await api('/api/config','POST',buildConfig());
  if(res.ok){
    currentConfig=res.config;
    alert('配置已保存'+(res.clamped&&res.clamped.length?'\n\n以下参数被合规下限/上限调整：\n'+res.clamped.map(c=>`${c.key}: ${c.from} → ${c.to}`).join('\n'):''));
  } else alert('保存失败');
}

async function start(){const r=await api('/api/start','POST');if(!r.ok)toast(r.error||'启动失败','err');else toast('已启动，正在准备就绪','ok');}
async function stop(){await api('/api/stop','POST');}
async function resolve(){await api('/api/resolve','POST');}

// 目标课程筛选「确认」：保存筛选条件并让引擎立刻按新条件重抓课程与余量
function setFilterHint(text, kind){
  const el=$('filterHint');
  if(!el) return;
  el.textContent=text||'改完筛选后点这里，右侧「课程与余量」会按新条件重新抓取';
  el.classList.toggle('err', kind==='err');
  el.classList.toggle('ok', kind==='ok');
}

async function applyFilter(){
  const btn=$('applyFilterBtn');
  if(btn) btn.disabled=true;
  try{
    setFilterHint('正在保存筛选条件…');
    const saved=await api('/api/config','POST',buildConfig());
    if(saved && saved.ok) currentConfig=saved.config;
    const r=await api('/api/rescan','POST');
    if(r && r.ok){
      setFilterHint('已刷新：右侧课程与余量按当前筛选条件重新抓取','ok');
      toast('已按筛选条件刷新','ok');
    }else{
      setFilterHint((r && r.error) || '已保存筛选条件，启动后生效');
      toast('已保存筛选条件，启动后生效','ok');
    }
  }catch(e){
    setFilterHint('刷新失败：'+e.message,'err');
    toast('刷新失败：'+e.message,'err');
  }finally{
    if(btn) btn.disabled=false;
  }
}

async function exportConfig(){
  try{
    const r=await api('/api/config/export');
    if(!r.ok)throw new Error('导出失败');
    const data=JSON.stringify(r.config,null,2);
    const blob=new Blob([data],{type:'application/json'});
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a');
    a.href=url;
    const d=new Date();
    const stamp=d.getFullYear()+String(d.getMonth()+1).padStart(2,'0')+String(d.getDate()).padStart(2,'0');
    a.download='sandau-config-'+stamp+'.json';
    document.body.appendChild(a);a.click();a.remove();
    URL.revokeObjectURL(url);
    alert('配置已导出为 JSON 文件。\n注意：文件含账号密码，请妥善保管，不要外传。');
  }catch(e){alert('导出失败：'+e.message);}
}

async function importConfig(file){
  try{
    const text=await file.text();
    const data=JSON.parse(text);
    const cfg=data.config||data;
    const r=await api('/api/config/import','POST',{config:cfg});
    if(!r.ok)throw new Error('导入失败');
    currentConfig=r.config;fillForm(r.config);
    let msg='配置已导入。';
    if(r.clamped&&r.clamped.length)msg+='\n\n以下参数被合规下限/上限调整：\n'+r.clamped.map(c=>`${c.key}: ${c.from} → ${c.to}`).join('\n');
    alert(msg);
  }catch(e){alert('导入失败：文件格式不正确或内容有误（'+e.message+'）');}
}

function renderStatus(s){
  const st=s.state;
  $('stateText').textContent=s.state_label||st;
  const dot=$('stateDot');
  dot.className='dot '+(st==='running'?'running':st==='paused'?'paused':st==='stopping'?'stopping':'');
  const running=['starting','running','paused','stopping'].includes(st);
  $('startBtn').disabled=running;
  $('stopBtn').disabled=!running;
  $('resolveBtn').disabled=st!=='paused';
  // 登录状态可视化
  const lg=s.login||{code:'idle',label:'未启动',detail:''};
  const box=$('loginBox');
  box.className='login-box '+(lg.code||'idle');
  $('loginCode').textContent=lg.label||'';
  $('loginDetail').textContent=lg.detail||'';
  // 因为“没填账号密码”暂停时，直接把登录输入框标红，避免用户不知道要填什么
  const needCreds=st==='paused'&&/未配置账号密码|账号密码/.test(lg.detail||'');
  ['username','password'].forEach(id=>{
    const el=$(id);
    if(el) el.classList.toggle('need-input',needCreds);
  });
  // 运行元信息：阶段 / 模式 / 入口来源 / 高频
  const modeName={both:'定时+守候（已隐藏）',scheduled:'定时',watch:'守候（已隐藏）'}[s.mode]||s.mode||'-';
  const srcName={cache:'缓存',discover:'自动发现',anchor:'锚点',login:'统一认证',need_human:'需人工',none:'无'}[s.entry_source]||s.entry_source||'-';
  const burstTag=s.burst?`<span class="meta-tag burst">高频 ${s.burst_remain||0}s</span>`:'';
  $('runMeta').innerHTML=
    `<span class="meta-tag">阶段：${esc(s.stage_label||'-')}</span>`+
    `<span class="meta-tag">模式：${esc(modeName)}</span>`+
    `<span class="meta-tag">入口：${esc(srcName)}</span>`+burstTag;
  const k=s.stats||{};
  renderStats(k);
  // 目标课程倒计时 + 服务端「已确认」状态同步
  renderDue(s.due||null);
  if(Array.isArray(s.selected)){
    // 服务端只认「已确认」的列表：先记录它，再决定要不要覆盖界面上的挑选态
    const srv=s.selected.map(String);
    const dirty=sameSet(selected,confirmed);      // 界面上是否有未确认的改动
    confirmed=new Set(srv);
    if(dirty && selBusy===0) selected=new Set(srv); // 没有改动才跟随服务端，避免冲掉用户正在选的内容
    syncCardClasses();
    updateConfirmUI();
  }
  renderCourses(s.courses||[]);
}

function sameSet(a,b){
  if(a.size!==b.size) return false;
  for(const k of a) if(!b.has(k)) return false;
  return true;
}

function isSelDirty(){ return !sameSet(selected,confirmed); }

// 卡片样式：已选中=实线高亮；选了但还没确认=虚线高亮（待确认）
function syncCardClasses(){
  courseEls.forEach((el,key)=>{
    const on=selected.has(key);
    el.classList.toggle('sel', on);
    el.classList.toggle('sel-pending', on && !confirmed.has(key));
    el.setAttribute('aria-checked', on?'true':'false');
  });
}

// 「确认选课」按钮：只在有未确认改动时可点
function updateConfirmUI(){
  const btn=$('confirmSelBtn');
  if(btn){
    const dirty=isSelDirty();
    btn.disabled=!dirty || selBusy>0;
    btn.classList.toggle('dirty', dirty);
    btn.textContent=dirty?`确认选课（${selected.size} 门）`:'已确认';
  }
  const tag=$('selPendingTag');
  if(tag){
    const n=[...selected].filter(k=>!confirmed.has(k)).length;
    tag.textContent = n?`待确认 ${n} 门`:'';
    tag.classList.toggle('hidden', n===0);
  }
}

function courseKey(c){return String(c&&c.title||'')+'|'+String(c&&c.time||'');}

function tzDate(s){
  // "2026-11-25 12:00:00" -> Date（本地时区）
  const m=String(s||'').match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/);
  if(!m) return null;
  return new Date(+m[1],+m[2]-1,+m[3],+m[4],+m[5],+(m[6]||0));
}

function countdownText(s){
  const d=tzDate(s); if(!d) return '';
  let ms=d.getTime()-Date.now();
  const past=ms<=0; if(past) ms=-ms;
  const sec=Math.floor(ms/1000);
  const h=Math.floor(sec/3600), m=Math.floor(sec%3600/60), ss=sec%60;
  const p=n=>String(n).padStart(2,'0');
  const txt=(h>0?h+':'+p(m):m)+':'+p(ss);
  return past?txt:txt;
}

function fmtRegAt(s){
  const d=tzDate(s); if(!d) return String(s||'');
  const p=n=>String(n).padStart(2,'0');
  return `${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function renderDue(due){
  const box=$('dueBanner'); if(!box) return;
  if(!due){ box.className='due-banner hidden'; box.innerHTML=''; lastDueKey=null; return; }
  const key=due.key+'@'+due.at;
  const clock=countdownText(due.at);
  if(key===lastDueKey && box.dataset.at===due.at){
    const c=box.querySelector('.due-clock'); if(c) c.textContent=clock;
    return;
  }
  lastDueKey=key;
  box.dataset.at=due.at;
  const c=countdownShort(due.at)||{past:false,sec:0,txt:clock};
  box.className='due-banner pop'+(c.past?' live':'')+(c.sec<=60&&!c.past?' urgent':'');
  box.innerHTML=`<div class="due-dot"></div>
    <div class="due-main"><b>${esc(due.title||'目标课程')}</b>
      <span class="due-sub">报名开始 ${esc(fmtRegAt(due.at)||due.at)}</span></div>
    <div class="due-clock">${esc((c.past?'已开始 ':'距开抢 ')+c.txt)}</div>`;
}

function toggleSelect(key, el){
  if(!key||key==='|') return;
  const on=!selected.has(key);
  if(on) selected.add(key); else selected.delete(key);
  if(el){
    el.classList.toggle('sel', on);
    el.classList.toggle('sel-pending', on && !confirmed.has(key));
    el.setAttribute('aria-checked', on?'true':'false');
    if(on){ el.classList.remove('pick-anim'); void el.offsetWidth; el.classList.add('pick-anim'); }
  }
  setSelHint();
  updateConfirmUI();
  if(isSelDirty()) toast('已挑选 '+selected.size+' 门，点「确认选课」后才生效','info');
}

// 「确认选课」：把当前挑选的整批写回服务端（引擎下一轮就读到，无需重启）
async function confirmSelection(){
  const btn=$('confirmSelBtn');
  if(btn){ btn.disabled=true; }
  selBusy++;
  try{
    const r=await api('/api/course/select','POST',{selected:[...selected]});
    if(!r||!r.ok) throw new Error((r&&r.error)||'确认失败');
    const srv=(r.selected||[]).map(String);
    confirmed=new Set(srv);
    selected=new Set(srv);
    syncCardClasses();
    setSelHint();
    toast(srv.length?`已确认 ${srv.length} 门目标课程，到点自动抢`:'已取消全部目标课程，不会自动报名','ok');
  }catch(e){
    toast(e.message||'确认失败','err');
  }finally{
    selBusy=Math.max(0,selBusy-1);
    updateConfirmUI();
  }
}

async function clearSelection(){
  selected.clear();
  confirmed.clear();
  syncCardClasses();
  setSelHint();
  updateConfirmUI();
  try{
    const r=await api('/api/course/clear','POST');
    if(r&&r.ok) toast('已清空抢课计划','ok');
  }catch(e){ toast('清空失败：'+e.message,'err'); }
}

/* ---------- 实时倒计时 / 统计动效 / 轻提示 ---------- */
function countdownShort(s){
  const d=tzDate(s); if(!d) return null;
  let ms=d.getTime()-Date.now();
  const past=ms<=0; if(past) ms=-ms;
  const sec=Math.floor(ms/1000);
  const h=Math.floor(sec/3600), m=Math.floor(sec%3600/60), ss=sec%60;
  const p=n=>String(n).padStart(2,'0');
  const txt=h>0?h+':'+p(m)+':'+p(ss):p(m)+':'+p(ss);
  return {past, sec, txt};
}

// 每 500ms 刷新所有倒计时：目标横幅 + 课程卡上的“距报名”
function tickClocks(){
  const box=$('dueBanner');
  if(box&&box.dataset.at){
    const c=countdownShort(box.dataset.at);
    const el=box.querySelector('.due-clock');
    if(c&&el){
      el.textContent=(c.past?'已开始 ':'距开抢 ')+c.txt;
      box.classList.toggle('urgent', !c.past&&c.sec<=60);
      box.classList.toggle('live', c.past);
    }
  }
  document.querySelectorAll('.creg[data-reg]').forEach(node=>{
    const c=countdownShort(node.dataset.reg);
    if(!c) return;
    node.textContent=c.past?'报名已开始':'距报名 '+c.txt;
    node.classList.toggle('urgent', !c.past&&c.sec<=60);
    node.classList.toggle('soon', !c.past&&c.sec>60);
    node.classList.toggle('live', c.past);
  });
}

function renderStats(k){
  const host=$('stats'); if(!host) return;
  const items=[['扫描次数',k.scans||0],['可报课程',k.found||0],['已提交',k.submitted||0],
               ['失败',k.failed||0],['重试',k.retries||0]];
  items.forEach(([name,v],i)=>{
    while(host.children.length<=i){
      const cell=document.createElement('div');
      cell.className='stat';
      cell.innerHTML='<b>0</b><span></span>';
      host.appendChild(cell);
    }
    const cell=host.children[i], b=cell.querySelector('b');
    cell.querySelector('span').textContent=name;
    const txt=String(v);
    if(b.textContent!==txt){
      const first=prevStats[name]===undefined;
      b.textContent=txt;
      prevStats[name]=v;
      if(!first){
        cell.classList.remove('pop'); void cell.offsetWidth; cell.classList.add('pop');
      }
    }
  });
}

function toast(msg, kind='info'){
  const wrap=$('toastWrap'); if(!wrap) return;
  const d=document.createElement('div');
  d.className='toast '+(kind||'info');
  d.textContent=msg;
  wrap.appendChild(d);
  setTimeout(()=>{ d.classList.add('out'); setTimeout(()=>d.remove(), 280); }, 2600);
}

function setSelHint(){
  const hint=$('selHint'); if(!hint) return;
  const n=selected.size;
  const dirty=!sameSet(selected,confirmed);
  if(!n){
    hint.textContent='未选择目标课程：点击课程卡片挑选，再点「确认选课」；未确认时程序只刷新、不会自动报名';
    hint.classList.remove('has-pick');
    return;
  }
  // 列表为空（尚未启动/尚未抓取）时，把已选课程的标题显示出来，避免“选了但看不到”
  const onCards=new Set();
  courseEls.forEach((el,k)=>{ if(selected.has(k)) onCards.add(el.dataset.key); });
  const orphanTitles=[...selected].filter(k=>!onCards.has(k)).map(k=>String(k).split('|')[0]).filter(Boolean);
  const extra=orphanTitles.length?`（${orphanTitles.slice(0,2).map(t=>t.length>16?t.slice(0,16)+'…':t).join('、')}${orphanTitles.length>2?' 等':''}）`:'';
  hint.textContent = dirty
    ? `已挑选 ${n} 门${extra}，还差一步：点右侧「确认选课」才会生效`
    : `已确认 ${n} 门${extra}：到「报名开始时间」自动抢；再点卡片可增删，需重新确认`;
  hint.classList.add('has-pick');
}

function buildCourseEl(c, key){
  const el=document.createElement('div');
  el.className='course';
  el.dataset.key=key;
  el.tabIndex=0;
  el.setAttribute('role','checkbox');
  el.setAttribute('aria-checked','false');
  el.innerHTML=`<div class="pick" title="点击挑选该课程（再点「确认选课」才生效）"></div>
    <div class="name"><span class="ttl"></span><div class="meta"></div></div>
    <div class="quota">-</div>`;
  el.addEventListener('click', ()=>toggleSelect(key, el));
  el.addEventListener('keydown', e=>{
    if(e.key==='Enter'||e.key===' '){ e.preventDefault(); toggleSelect(key, el); }
  });
  return el;
}

function updateCourseEl(el, c){
  const q=(c.quota||'').replace(/\s/g,'');
  let ok=false;
  if(q&&q.indexOf('/')>=0){const [a,b]=q.split('/');ok=(b==='无限'||b==='∞'||b==='-1'||(+a<+b));}
  const picked=selected.has(el.dataset.key);
  el.classList.toggle('sel', picked);
  el.classList.toggle('sel-pending', picked && !confirmed.has(el.dataset.key));
  el.setAttribute('aria-checked', picked?'true':'false');
  el.classList.toggle('avail', ok);
  el.querySelector('.ttl').textContent=c.title||'未命名';
  const qEl=el.querySelector('.quota');
  qEl.className='quota '+(ok?'ok':'no');
  qEl.textContent=c.quota||'-';
  const tags=[c.category,c.level,c.campus].filter(Boolean).map(x=>`<span class="ctag">${esc(x)}</span>`).join('');
  const regDate=c.reg_start?tzDate(c.reg_start):null;
  const reg=regDate
    ? `<span class="creg${regDate>new Date()?' soon':''}" data-reg="${esc(c.reg_start)}"></span>`
    : `<span class="creg unk" title="该课程卡片未标注报名开始时间，可报时立即尝试">报名时间未标注</span>`;
  el.querySelector('.meta').innerHTML=`${esc(c.time||'')}${tags?' '+tags:''}${reg}`;
}

function renderCourses(courses){
  const list=$('courseList'); if(!list) return;
  // 只展示能报的二课（后端已过滤，这里再兜一层，避免旧状态残留满员课程）
  courses=(courses||[]).filter(c=>c && c.available!==false);
  if(!courses.length){
    if(!list.dataset.empty){
      list.innerHTML='<p class="empty-state">当前没有能报的二课（已满或未开放）</p>';
      list.dataset.empty='1';
    }
    courseEls.clear();
    setSelHint();
    return;
  }
  if(list.dataset.empty){list.innerHTML='';delete list.dataset.empty;}
  const staleEmpty=list.querySelector('.empty-state');
  if(staleEmpty) staleEmpty.remove();
  const seen=new Set();
  courses.forEach(c=>{
    const key=courseKey(c);
    if(seen.has(key)) return;
    seen.add(key);
    let el=courseEls.get(key);
    if(!el){
      el=buildCourseEl(c,key);
      list.appendChild(el);
      courseEls.set(key,el);
      el.classList.add('enter');
    }
    updateCourseEl(el,c);
  });
  [...courseEls.keys()].forEach(k=>{
    if(!seen.has(k)){const el=courseEls.get(k);if(el)el.remove();courseEls.delete(k);}
  });
  setSelHint();
}

function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

async function pollLogs(){
  const r=await api('/api/logs?since='+lastLogId);
  if(!r.ok||!r.events.length)return;
  for(const e of r.events){
    lastLogId=Math.max(lastLogId,e.id);
    const d=document.createElement('div');
    d.className='log-item';
    d.innerHTML=`<span class="t">${esc(e.time)}</span><span class="lv-${e.level}">${esc(e.message)}</span>`;
    $('logList').appendChild(d);
  }
  $('logList').scrollTop=$('logList').scrollHeight;
}

async function pollStatus(){
  try{const r=await api('/api/status');if(r.ok)renderStatus(r.status);}catch(e){}
}

async function init(){
  const r=await api('/api/config');
  if(r.ok){currentConfig=r.config;fillForm(r.config);}
  try{
    const h=await api('/api/health');
    if(h && h.version){const el=$('appVer'); if(el) el.textContent='v'+h.version;}
  }catch(e){}
  bindReleaseUI();
  updateReleaseUI();
  pollStatus();pollLogs();
  setInterval(pollStatus,1000);
  setInterval(pollLogs,1200);
  setInterval(updateReleaseUI,1000);
  setInterval(tickClocks,500);
  tickClocks();
  $('saveBtn').onclick=saveConfig;
  $('startBtn').onclick=start;
  $('stopBtn').onclick=stop;
  $('resolveBtn').onclick=resolve;
  $('rediscoverBtn').onclick=async()=>{await api('/api/rediscover','POST');alert('已标记重新发现，下次循环生效（或在停止后重新启动）');};
  $('exportBtn').onclick=exportConfig;
  $('applyFilterBtn') && ($('applyFilterBtn').onclick=applyFilter);
  $('clearSelBtn') && ($('clearSelBtn').onclick=clearSelection);
  $('confirmSelBtn') && ($('confirmSelBtn').onclick=confirmSelection);
  $('wait_reg_start') && ($('wait_reg_start').onchange=async()=>{
    await api('/api/config','POST',{config:{target:{wait_reg_start:$('wait_reg_start').checked}}});
  });
  $('importBtn').onclick=()=>$('importFile').click();
  $('importFile').onchange=(e)=>{
    const f=e.target.files[0];
    if(f){importConfig(f);}
    e.target.value='';
  };
}
init();
