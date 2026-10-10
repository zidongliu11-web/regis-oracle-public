(function () {
  'use strict';
  var root=document.getElementById('factorResearchCatalogue');if(!root)return;
  var get=function(id){return document.getElementById(id);};
  var rows=get('factorCatalogueRows'),status=get('factorCatalogueStatus'),count=get('factorCatalogueCount'),sources=get('factorCatalogueSources');
  var search=get('factorCatalogueSearch'),filter=get('factorCatalogueFilter');
  // Names and input conventions reused from the existing strategy catalogue.
  var names={m24_negative_daily_return_rank:'日频反转基线',range_contraction_5_20:'波幅收缩',breakout_distance_20:'突破距离',close_acceptance:'收盘承接',pullback_volume_3_20:'回调量比',industry_excess_20:'行业相对强弱',sector_breadth_excess_v1:'板块上涨扩散',sector_effective_amount_participation_v1:'板块成交参与',attention_price_diffusion_gap_v1:'关注与价格扩散差',market_up_ratio_pct:'全市场上涨比例',market_top5pct_amount_share_pct:'成交集中度'};
  var decisions={evidence_insufficient:'证据不足',excluded_pending_lineage:'已排除 · 来源谱系待补',control_only:'仅作对照'};
  var inputs={stock_daily:'股票日线',etf_daily:'ETF日线',PIT_universe_L_D_P_BSE:'历史股票范围',PIT_ST:'历史ST状态',PIT_suspension:'历史停牌',historical_limits:'历史涨跌停价',adjustment_and_actions:'复权与公司行动',ETF_complete_actions:'ETF分红与拆合',exchange_calendar:'交易日历',rights_and_retention:'许可与留存权',historical_publication_evidence:'历史公布时点',minute_execution_quotes:'分钟执行行情',PIT_sector_membership:'历史板块成员',attention_event_truth:'关注事件原始证据'};
  var data=null,sequence=0,controller=null,leaving=false;
  var inputStatus=get('factorInputStatus'),inputRetry=get('factorInputRetry'),inputData=null,inputSequence=0,inputController=null;
  var localPreview=!!(document.body&&document.body.getAttribute('data-local-research-preview')==='true');
  var inputStates={files_matched:'原件身份已匹配',partial_or_unavailable:'原件缺失或已变化',not_connected:'尚未接入',scope_limited:'仅限既有用途'};
  function node(tag,text){var e=document.createElement(tag);e.textContent=text;return e;}
  function printable(value){return typeof value==='string'?value:JSON.stringify(value);}
  function refValid(ref){return ref&&typeof ref.path==='string'&&ref.path.length>0&&/^[a-f0-9]{64}$/.test(ref.sha256);}
  function valid(d){return d&&d.schema==='regis.website.research_catalogue.v1'&&d.status==='available'&&d.catalogue_only===true&&d.capital_yuan===150000&&d.current_catalogue_economic_runs===0&&d.prior_search_count==='unknown_not_reset'&&['economic_admitted','production_eligible','generates_alert','auto_orders','account_access','real_replay_enabled'].every(function(k){return d[k]===false;})&&Array.isArray(d.factors)&&d.factors.length>0&&d.factors.length<=16&&d.factor_count===d.factors.length&&new Set(d.factors.map(function(r){return r&&r.id;})).size===d.factor_count&&Array.isArray(d.source_bindings)&&d.source_bindings.length===2&&d.source_bindings.every(refValid)&&d.factors.every(function(r){return r&&typeof r.id==='string'&&r.id.length>0&&Object.prototype.hasOwnProperty.call(decisions,r.decision)&&r.prior_search_count==='unknown_not_reset'&&['family','formula','unit','input_clock','failure_condition'].every(function(k){return r[k]!==null&&r[k]!==undefined&&printable(r[k]).length>0;})&&Array.isArray(r.required_inputs)&&r.required_inputs.length>0&&r.required_inputs.every(function(v){return typeof v==='string';})&&refValid(r.source_binding);});}
  function clear(){data=null;rows.replaceChildren();sources.replaceChildren();count.textContent='';}
  function validInputs(d){return d&&d.schema==='regis.local_factor_inputs.v1'&&d.status==='available'&&refValid(d.catalogue_binding)&&Array.isArray(d.source_bindings)&&d.source_bindings.length>0&&d.source_bindings.every(refValid)&&d.price_rows_read_by_this_request===0&&d.new_source_requests===0&&d.new_economic_runs===0&&d.research_only===true&&['account_access','model_admitted','production_eligible','generates_alert','automatic_orders','historical_feature_PIT_certified'].every(function(k){return d[k]===false;})&&['checked_at','file_verified_at','audit_recorded_at','note'].every(function(k){return typeof d[k]==='string'&&d[k].length>0;})&&Array.isArray(d.inputs)&&d.inputs.length>0&&d.input_count===d.inputs.length&&new Set(d.inputs.map(function(r){return r&&r.id;})).size===d.input_count&&d.inputs.every(function(r){return r&&typeof r.id==='string'&&Object.prototype.hasOwnProperty.call(inputStates,r.current_state)&&r.historical_PIT_certified===false&&r.research_admitted===false&&typeof r.audit==='string'&&typeof r.limitation==='string'&&Array.isArray(r.files)&&r.files.every(function(f){return f&&typeof f.name==='string'&&['matched','unavailable_or_changed'].includes(f.state)&&/^[a-f0-9]{64}$/.test(f.expected_sha256);});});}
  function inputDetails(card,factor){
    if(!inputStatus)return;
    var matches=inputData&&data.source_bindings.some(function(b){return b.path===inputData.catalogue_binding.path&&b.sha256===inputData.catalogue_binding.sha256;});
    if(!matches){card.appendChild(node('p',inputData?'输入状态与当前登记版本不一致；请重新读取两项。':'输入连接尚未核验；可使用上方独立重读。'));return;}
    factor.required_inputs.forEach(function(id){
      var row=inputData.inputs.find(function(r){return r.id===id;}),box=node('div','');box.className='factor-input-row';
      box.appendChild(node('strong',(inputs[id]||id)+' · '+(row?inputStates[row.current_state]:'未知输入，尚未核验')));
      if(row){box.appendChild(node('p',row.files.length?'原件：'+row.files.map(function(f){return f.name+'（'+(f.state==='matched'?'匹配':'缺失或变化')+'）';}).join('、'):'没有可据此认定已接入的数据文件。'));
        box.appendChild(node('p',row.audit));box.appendChild(node('p',row.limitation));}
      box.appendChild(node('p','研究资格：历史时点与回测准入未通过。'));card.appendChild(box);
    });
  }
  async function loadInputs(){
    if(!inputStatus||leaving)return;
    if(!localPreview){inputStatus.textContent='当前入口未启用本机输入核验；登记可独立查看。';if(inputRetry)inputRetry.disabled=true;return;}
    var token=++inputSequence;if(inputController)inputController.abort();inputController=new AbortController();var local=inputController;
    inputData=null;paint();inputStatus.textContent='正在核对已保存输入…';var timer=setTimeout(function(){local.abort();},12000);
    try{var response=await fetch('/api/local-research/factor-inputs',{cache:'no-store',credentials:'same-origin',signal:local.signal});var d=await response.json();
      if(leaving||token!==inputSequence)return;
      if(!response.ok||!validInputs(d))throw new Error('输入状态读取或来源校验失败；登记仍可单独查看。');
      inputData=d;inputStatus.textContent='已核对 '+d.input_count+' 类输入。原件校验：'+d.file_verified_at+'；旧审计：'+d.audit_recorded_at+'。'+d.note;paint();
    }catch(e){if(!leaving&&token===inputSequence){inputData=null;inputStatus.textContent=e.name==='AbortError'?'输入核验超时或取消，请重读；当前不沿用旧连接状态。':e.message;paint();}}
    finally{clearTimeout(timer);}
  }
  function paint(){
    rows.replaceChildren();if(!data)return;
    var query=search.value.trim().toLocaleLowerCase(),selected=filter.value;
    var found=data.factors.filter(function(r){return (selected==='all'||r.decision===selected)&&(!query||[names[r.id]||'',r.id,printable(r.formula)].join(' ').toLocaleLowerCase().includes(query));});
    count.textContent='显示 '+found.length+' / '+data.factor_count+' 项登记 · 处置与原记录一致';
    if(!found.length){rows.appendChild(node('p','没有符合当前筛选的登记；可清除关键词或选择全部。'));return;}
    found.forEach(function(r){var card=node('details',''),title=node('summary',names[r.id]||r.id);title.appendChild(node('span',decisions[r.decision]));card.appendChild(title);
      [['登记标识',r.id],['因子家族',r.family],['公式',r.formula],['单位',r.unit],['可用时点',r.input_clock],['所需输入（尚待核验）',r.required_inputs.map(function(v){return inputs[v]||v;}).join('、')],['失败条件',r.failure_condition],['来源记录',r.source_binding.path],['来源校验',r.source_binding.sha256]].forEach(function(pair){card.appendChild(node('p',pair[0]+'：'+printable(pair[1])));});
      inputDetails(card,r);card.appendChild(node('p','历史尝试总数未知，未重置。没有授予经济试验或交易资格。'));rows.appendChild(card);});
  }
  async function load(){
    if(leaving)return;var token=++sequence;if(controller)controller.abort();controller=new AbortController();var local=controller;
    clear();status.textContent='正在独立读取登记…';root.setAttribute('aria-busy','true');
    var timer=setTimeout(function(){local.abort();},12000);
    try{var response=await fetch('/api/research-workbench/catalogue',{cache:'no-store',credentials:'same-origin',signal:local.signal});var d=await response.json();
      if(leaving||token!==sequence)return;
      if(!response.ok||!valid(d))throw new Error('登记读取或来源校验未通过；不能据此判定为空库。');
      data=d;status.textContent=d.factor_count+' 项可行性登记 · 尚未获得经济验证';
      sources.appendChild(node('p','登记时间：'+(d.registered_utc||'原记录未提供')+'。历史尝试总数未知；本目录没有新增经济试验。'));
      d.source_bindings.forEach(function(r){sources.appendChild(node('p',r.path+' · SHA256 '+r.sha256));});paint();
    }catch(e){if(!leaving&&token===sequence){clear();status.textContent=e.name==='AbortError'?'本次读取超时或取消，可重新读取。':e.message;}}
    finally{clearTimeout(timer);if(!leaving&&token===sequence)root.setAttribute('aria-busy','false');}
  }
  search.addEventListener('input',paint);filter.addEventListener('change',paint);get('factorCatalogueRetry').addEventListener('click',load);
  if(inputRetry)inputRetry.addEventListener('click',loadInputs);
  window.addEventListener('pagehide',function(){leaving=true;sequence++;inputSequence++;if(controller)controller.abort();if(inputController)inputController.abort();inputData=null;paint();if(inputStatus)inputStatus.textContent='页面已暂停；返回后重新核对连接状态。';});
  window.addEventListener('pageshow',function(e){if(e.persisted&&leaving){leaving=false;load();loadInputs();}});
  load();loadInputs();
})();
