(function () {
  'use strict';
  var root=document.getElementById('localDataConnections');if(!root)return;
  var cards=document.getElementById('localConnectionsCards'),status=document.getElementById('localConnectionsStatus');
  var remaining=document.getElementById('localConnectionsRemaining'),retry=document.getElementById('localConnectionsRetry');
  var allowed={stock:'/stock?code=000001#localResearchHistory',etf:'/stock?code=000001&history_code=510300#localResearchHistory',market:'/market-panorama',baseline:'/strategy-lab#localBaselineReference',catalogue:'/strategy-lab#slabResearchCatalogue'};
  var sequence=0,controller=null,leaving=false;
  function valid(d){return d && d.schema==='regis.local_data_connections.v1' && d.status==='available' && d.account_access===false && d.research_only===true && d.model_admitted===false && d.generates_alert===false && d.production_eligible===false && d.live_market_data===false && d.price_rows_read_by_this_request===0 && d.new_source_requests===0 && d.new_economic_runs===0 && typeof d.checked_at==='string' && typeof d.remaining==='string' && Array.isArray(d.connections) && d.connections.length===5 && new Set(d.connections.map(function(r){return r&&r.id;})).size===5 && d.connections.every(function(r){return r && Object.prototype.hasOwnProperty.call(allowed,r.id) && r.href===allowed[r.id] && ['available','unavailable'].includes(r.state) && ['label','coverage','detail'].every(function(k){return typeof r[k]==='string';}) && (r.recorded_at===null||typeof r.recorded_at==='string') && (r.state==='unavailable' || /^[a-f0-9]{64}$/.test(r.source_sha256));});}
  function node(tag,text){var e=document.createElement(tag);e.textContent=text;return e;}
  async function load(){
    if(leaving)return;var token=++sequence;if(controller)controller.abort();controller=new AbortController();var local=controller;
    cards.replaceChildren();remaining.textContent='';status.textContent='正在逐项核对本机已接数据…';root.setAttribute('aria-busy','true');
    var timer=setTimeout(function(){local.abort();},15000);
    try {
      var response=await fetch('/api/local-research/connections',{cache:'no-store',credentials:'same-origin',signal:local.signal});var d=await response.json();
      if(leaving||token!==sequence)return;
      if(!response.ok)throw new Error(d.message||'接线状态读取失败，请重试。');if(!valid(d))throw new Error('接线状态格式未通过校验，请重试。');
      d.connections.forEach(function(r){var card=node('article','');card.className='data-feature';card.dataset.state=r.state;
        card.appendChild(node('h3',r.label));var state=node('span',r.state==='available'?'已连接 · 限所列范围':'本次不可用');state.className='data-tag';card.appendChild(state);
        card.appendChild(node('p',r.coverage));card.appendChild(node('p',r.detail));
        if(r.state==='available')card.appendChild(node('p','原记录时间：'+(r.recorded_at||'未提供')+' · 校验 '+r.source_sha256.slice(0,12)));
        var link=node('a',r.state==='available'?'打开已接功能 →':'查看对应页面 →');link.href=allowed[r.id];card.appendChild(link);cards.appendChild(card);
      });
      var count=d.connections.filter(function(r){return r.state==='available';}).length;
      status.textContent='本次可读取 '+count+' / 5 项接线 · 检查于 '+d.checked_at+'；未读取账户或重新采集行情。';remaining.textContent=d.remaining;
    }catch(e){if(!leaving&&token===sequence){cards.replaceChildren();remaining.textContent='';status.textContent=e.name==='AbortError'?'本次检查超时或取消；可重新核对。':e.message;}}
    finally{clearTimeout(timer);if(!leaving&&token===sequence)root.setAttribute('aria-busy','false');}
  }
  window.LocalDataConnections={refresh:load};
  retry.addEventListener('click',load);
  window.addEventListener('pagehide',function(){leaving=true;sequence++;if(controller)controller.abort();});
  window.addEventListener('pageshow',function(event){if(event.persisted&&leaving){leaving=false;load();}});
  load();
})();
