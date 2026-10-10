(function () {
  'use strict';
  var active = null;
  var keys = ['snapshot_members','no_current','invalid_current','zero_volume','no_previous','invalid_previous','up','down','unchanged'];
  function integer(v) { return Number.isInteger(v) && v >= 0; }
  function validCounts(c) { return c && keys.every(function (k) {return integer(c[k]);}) && c.snapshot_members === keys.slice(1).reduce(function (sum,k) {return sum+c[k];},0); }
  function valid(d) {
    return d && d.status === 'available' && d.schema === 'regis.local_market_history.v1' && d.research_only === true &&
      d.account_access === false && d.live_market_data === false && d.model_admitted === false && d.production_eligible === false &&
      d.generates_alert === false && d.automatic_orders === false && d.full_A_coverage_certified === false && d.historical_feature_PIT_certified === false && d.sector_membership_available === false && d.new_economic_runs === 0 &&
      integer(d.included_datasets) && d.included_datasets > 0 && d.included_datasets <= 7000 &&
      ['source_sha256','summary_sha256','receipt_sha256'].every(function (k) {return /^[a-f0-9]{64}$/.test(d[k]);}) &&
      typeof d.source_captured_at === 'string' && Array.isArray(d.conditions_and_limits) && d.conditions_and_limits.length > 0 && d.conditions_and_limits.every(function (s) {return typeof s === 'string';}) &&
      Array.isArray(d.days) && d.days.length > 0 && d.days.length <= 30 && d.days.every(function (day,i) {
        return integer(day.date) && integer(day.previous_date) && day.previous_date < day.date && day.date <= 20260930 && day.previous_date >= 20130101 &&
          (!i || day.previous_date === d.days[i-1].date) && validCounts(day.total) && day.total.snapshot_members === d.included_datasets &&
          day.boards && ['main','chinext','star'].every(function (b) {return validCounts(day.boards[b]);}) &&
          keys.every(function (k) {return day.total[k] === ['main','chinext','star'].reduce(function (sum,b) {return sum+day.boards[b][k];},0);});
      }) && d.start_date === d.days[0].date && d.end_date === d.days[d.days.length-1].date;
  }
  function iso(d) {var s=String(d);return s.slice(0,4)+'-'+s.slice(4,6)+'-'+s.slice(6,8);}
  function unmount() {if(active) {active.cancel('已离开页面，读取已停止。');active=null;}}
  function mount(host) {
    unmount();
    var template=document.getElementById('localMarketHistoryTemplate');
    if(!template || !host) return;
    host.appendChild(template.content.cloneNode(true));
    var root=host.querySelector('#localMarketHistory');
    var get=function(id){return root.querySelector('#'+id);};
    var state={sequence:0,controller:null,data:null,timer:null,paused:false};active=state;
    var status=get('localMarketStatus'),content=get('localMarketContent'),date=get('localMarketDate'),board=get('localMarketBoard');
    var rows=get('localMarketRows'),distribution=get('localMarketDistribution');
    function clear(){state.data=null;content.hidden=true;date.disabled=true;date.replaceChildren();rows.replaceChildren();distribution.replaceChildren();get('localMarketCounts').textContent='';get('localMarketExclusions').textContent='';get('localMarketSource').textContent='';get('localMarketLimits').replaceChildren();}
    state.cancel=function(message){state.sequence++;clearTimeout(state.timer);state.timer=null;if(state.controller) state.controller.abort();state.controller=null;clear();root.setAttribute('aria-busy','false');status.textContent=message;};
    function group(day){return board.value==='total'?day.total:day.boards[board.value];}
    function paint(){
      if(!state.data) return;
      var day=state.data.days.find(function(d){return String(d.date)===date.value;});if(!day) return;
      var c=group(day);if(!c) return;
      var pairs=c.up+c.down+c.unchanged;distribution.replaceChildren();
      [['up','#ed6876'],['down','#4fc1a0'],['unchanged','#68768a']].forEach(function(part){var span=document.createElement('span');span.style.width=(pairs?c[part[0]]/pairs*100:0)+'%';span.style.backgroundColor=part[1];distribution.appendChild(span);});
      var caption='上涨 '+c.up+' · 下跌 '+c.down+' · 持平 '+c.unchanged+' · 有效价格对 '+pairs+' / 快照代码 '+c.snapshot_members;
      distribution.setAttribute('aria-label',caption);get('localMarketCounts').textContent=caption;
      get('localMarketExclusions').textContent='排除：当日无记录 '+c.no_current+'（可含未上市、已退市或缺失） · 当日零成交 '+c.zero_volume+' · 前一交易日无记录 '+c.no_previous+' · 当日/前日记录无效 '+(c.invalid_current+c.invalid_previous)+'。无记录不计作持平。';
      rows.replaceChildren();state.data.days.forEach(function(d){var v=group(d),tr=document.createElement('tr');[iso(d.date),v.up+v.down+v.unchanged,v.up,v.down,v.unchanged,v.no_current,v.zero_volume,v.no_previous,v.invalid_current+v.invalid_previous].forEach(function(value){var td=document.createElement('td');td.textContent=String(value);tr.appendChild(td);});rows.appendChild(tr);});
      status.textContent='历史样本 '+iso(day.date)+'，对比 '+iso(day.previous_date)+' · 已保存 '+state.data.days.length+' 个交易日 · 未进行策略回测';
    }
    async function load(){
      if(active!==state || state.paused || document.hidden) return;
      var selected=date.value,token=++state.sequence;if(state.controller) state.controller.abort();state.controller=new AbortController();clear();status.textContent='正在读取本机已保存的历史统计…';root.setAttribute('aria-busy','true');
      clearTimeout(state.timer);
      var localController=state.controller,abort;
      var cancelled=new Promise(function(_,reject){abort=function(){var e=new Error('cancelled');e.name='AbortError';reject(e);};localController.signal.addEventListener('abort',abort,{once:true});});
      var timer=state.timer=setTimeout(function(){localController.abort();},15000);
      try {
        var result=await Promise.race([(async function(){var response=await fetch('/api/local-research/market-history',{cache:'no-store',credentials:'same-origin',signal:localController.signal});return {ok:response.ok,data:await response.json()};})(),cancelled]);
        var d=result.data;
        if(active!==state || token!==state.sequence) return;
        if(!result.ok) throw new Error(d && d.message||'历史统计暂不可用，请重试。');if(!valid(d)) throw new Error('历史统计校验未通过，请恢复原件后重试。');
        state.data=d;d.days.forEach(function(day){var option=document.createElement('option');option.value=String(day.date);option.textContent=iso(day.date);date.appendChild(option);});
        date.value=d.days.some(function(day){return String(day.date)===selected;})?selected:String(d.end_date);date.disabled=false;
        get('localMarketSource').textContent='来源：RQ历史回测快照 · 采集 '+d.source_captured_at+' · 原价/未复权方向 · 来源校验 '+d.source_sha256.slice(0,12)+' · 统计校验 '+d.summary_sha256.slice(0,12);
        d.conditions_and_limits.forEach(function(s){var li=document.createElement('li');li.textContent=s;get('localMarketLimits').appendChild(li);});content.hidden=false;paint();
      }catch(e){if(active===state && token===state.sequence){clear();status.textContent=e.name==='AbortError'?'请求超时或取消，可重试。':e.message;}}
      finally{clearTimeout(timer);localController.signal.removeEventListener('abort',abort);if(active===state && token===state.sequence){state.timer=null;state.controller=null;root.setAttribute('aria-busy','false');}}
    }
    state.load=load;
    date.addEventListener('change',paint);board.addEventListener('change',paint);get('localMarketRetry').addEventListener('click',load);load();
  }
  document.addEventListener('visibilitychange',function(){if(active){active.paused=!!document.hidden;if(active.paused) active.cancel('页面已隐藏，读取已停止。');else active.load();}});
  window.addEventListener('pagehide',function(){if(active){active.paused=true;active.cancel('已离开页面，读取已停止。');}});
  window.addEventListener('pageshow',function(event){if(active && event.persisted){active.paused=!!document.hidden;active.load();}});
  window.LocalMarketHistory={mount:mount,unmount:unmount};
})();
