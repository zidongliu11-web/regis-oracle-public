(function () {
  'use strict';
  if (!document.body || document.body.dataset.decisionMode !== 'research') return;
  // Old bookmarks used root fragments; the local research landing page must
  // continue to send those deliberate visits to the existing canonical pages.
  var legacy = {signals:'/signals',plan:'/next-session-plan',review:'/review',
    fundflow:'/fund-flow',scanner:'/scanner',backtest:'/strategy-lab',
    stock:'/stock','factor-system':'/factor-system',positions:'/positions','data-center':'/data-center',
    'strategy-lab':'/strategy-lab',risk:'/risk',globalradar:'/global-radar',panorama:'/market-panorama'};
  if (window.location && window.location.pathname === '/' &&
      Object.prototype.hasOwnProperty.call(legacy, window.location.hash.slice(1))) {
    window.location.replace(legacy[window.location.hash.slice(1)]);return;
  }
  var get=function(id){return document.getElementById(id);};
  var service=get('researchService'),stamp=get('lastRefresh'),host=get('decisionMarketHistory');
  var nativeEnabled=!!get('decisionNativeSources'),sequence=0,controller=null,leaving=false,clockTimer=null;
  function clocks(){
    var now=new Date();
    get('beijingClock').textContent='北京 '+now.toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
    get('localClock').textContent='本机 '+now.toLocaleTimeString('zh-CN',{hour12:false});
  }
  function startClock(){clocks();if(clockTimer===null)clockTimer=setInterval(clocks,1000);}
  async function health(){
    if(leaving)return;var token=++sequence;if(controller)controller.abort();controller=new AbortController();var local=controller;
    service.textContent='正在检查页面服务…';stamp.textContent='尚未完成本次检查';
    var timer=setTimeout(function(){local.abort();},10000);
    try{
      var r=await fetch('/api/health/live',{cache:'no-store',credentials:'same-origin',signal:local.signal});var d=await r.json();
      if(leaving||token!==sequence)return;
      if(!r.ok||!d||d.status!=='alive'||d.service!=='Regis Oracle'||typeof d.time!=='string'||!Number.isFinite(Date.parse(d.time)))throw new Error('服务检查未通过，可点击刷新重试。');
      service.textContent='页面服务可响应 · 数据状态见下方各项';
      stamp.textContent='服务响应时间（北京） '+new Date(d.time).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
    }catch(e){if(!leaving&&token===sequence){service.textContent=e.name==='AbortError'?'服务检查超时，可点击刷新重试。':'服务检查失败，可点击刷新重试。';stamp.textContent='本次服务状态未确认';}}
    finally{clearTimeout(timer);}
  }
  function market(){
    if(!nativeEnabled||!host)return;
    if(window.LocalMarketHistory){window.LocalMarketHistory.unmount();host.replaceChildren();window.LocalMarketHistory.mount(host);}
    else host.textContent='历史统计组件未加载，请重新加载页面。';
  }
  function connections(){
    if(!nativeEnabled)return;
    if(window.LocalDataConnections)window.LocalDataConnections.refresh();
    else get('localConnectionsStatus').textContent='数据接线组件未加载，请重新加载页面。';
  }
  function refresh(){if(leaving)return;health();connections();market();}
  get('refreshButton').addEventListener('click',refresh);
  window.addEventListener('pagehide',function(){leaving=true;sequence++;if(controller)controller.abort();if(clockTimer!==null){clearInterval(clockTimer);clockTimer=null;}if(window.LocalMarketHistory)window.LocalMarketHistory.unmount();});
  window.addEventListener('pageshow',function(event){if(event.persisted&&leaving){leaving=false;startClock();health();market();}});
  startClock();health();market();
  // The connections component performs its own first load and BFCache recovery.
  if(nativeEnabled&&!window.LocalDataConnections)get('localConnectionsStatus').textContent='数据接线组件未加载，请重新加载页面。';
})();
