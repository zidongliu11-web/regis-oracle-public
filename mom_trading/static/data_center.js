(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const states = {missing:'缺失',empty:'空库',empty_table:'空表',nonempty_unvalidated:'已有记录 · 待核验',bytes_present_unvalidated:'已有字节 · 待审计',path_unavailable:'路径不可读取',consistent_snapshot_required:'需一致备份',schema_incompatible:'库结构不兼容',inspection_size_limit:'超过网页检查上限',unreadable:'文件不可读取',invalid:'文件无效'};
  let statusController = null, inspectedFile = null, inspectedSha = null, localRecovery = false, uploadBusy = false;

  async function jsonRequest(url, options = {}, seconds = 15) {
    const controller = options.controller || new AbortController();
    const timer = setTimeout(() => controller.abort('timeout'), seconds * 1000);
    try {
      const response = await fetch(url, {...options, credentials:'same-origin',cache:'no-store',signal:controller.signal});
      const data = await response.json();
      if (!response.ok || data.ok === false) throw new Error(data.error || ('HTTP ' + response.status));
      return data;
    } catch (error) {
      if (controller.signal.aborted) throw new Error('请求超时或已取消；请重新检查，勿重复恢复。');
      throw error;
    } finally { clearTimeout(timer); }
  }

  function updateRestoreButton() {
    $('registryRestore').disabled = uploadBusy || !localRecovery || !inspectedFile
      || $('registrySnapshot').files[0] !== inspectedFile || $('registryExpectedSha').value !== inspectedSha;
  }

  function renderStatus(data) {
    const inputs = data.inputs || {}, registry = inputs.registry || {}, store = inputs.market_store || {};
    localRecovery = data.runtime?.local_recovery_allowed === true;
    $('dataService').textContent = data.service?.reachable === true ? '服务在线' : '服务待核验';
    $('dataRegistry').textContent = states[registry.state] || '待核验';
    $('dataRecoveryMode').textContent = localRecovery ? '本机可用' : '远程只读';
    $('dataRuntime').textContent = data.runtime?.background_enabled === true ? '后台模式' : '交互模式';
    $('dataCheckedAt').textContent = data.checked_at ? new Date(data.checked_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}) + ' 上海' : '检查时间待核';
    $('dataSummary').dataset.error = 'false';
    $('dataSummary').textContent = '网站与旧研究存储分别核对；本页不读取账户。已接行情及研究记录见接线清单，旧库缺失不表示所有行情都缺失。当前检查不会启动采集或研究。';
    $('dataFeatures').innerHTML = (data.features || []).map(row => '<article class="data-feature" data-state="' + esc(row.state) + '"><h3>' + esc(row.label) + '</h3><span class="data-tag">' + ({available:'基础操作可用',read_only:'只读',needs_input:'需要原始输入',needs_audit:'需要来源与时点核验'}[row.state] || '待核验') + '</span><p>' + esc(row.detail) + '</p><a href="' + esc(row.href) + '">打开功能 →</a></article>').join('');
    const rows = [{name:'原因子研究库',state:registry.state,count:registry.rows,next:'在下方检查并恢复合法原快照。'}];
    Object.entries(store.table_counts || {}).forEach(([name,count]) => {
      const readable = ['nonempty_unvalidated','empty'].includes(store.state);
      const knownCount = Number.isInteger(count) && count >= 0;
      const state = readable ? (knownCount ? (count === 0 ? 'empty_table' : 'nonempty_unvalidated') : 'unknown') : store.state;
      rows.push({name:'市场存储 · ' + name,state,count:knownCount ? count : null,next:'核对原数据源与存储；空表不代表已接入。'});
    });
    if (!Object.keys(store.table_counts || {}).length) rows.push({name:'市场数据存储',state:store.state,count:null,next:'需要原合法数据与当前配置核查。'});
    (inputs.panels || []).forEach(row => rows.push({name:row.path || row.name || '历史面板',state:row.state,count:null,next:'恢复原面板及审计记录，再核验共享面板原子发布。'}));
    $('dataInputs').innerHTML = rows.map(row => '<tr><td>' + esc(row.name) + '</td><td>' + esc(states[row.state] || '待核验') + '</td><td>' + (Number.isInteger(row.count) ? row.count : '未知') + '</td><td>' + esc(row.next) + '</td></tr>').join('');
    $('registryInspect').disabled = !localRecovery || uploadBusy;
    updateRestoreButton();
  }

  async function refresh() {
    if (statusController) statusController.abort('superseded');
    const controller = new AbortController(); statusController = controller;
    $('dataRefresh').disabled = true;
    $('dataSummary').textContent = '正在重新核对当前服务与输入…';
    try {
      const data = await jsonRequest('/api/site/status',{controller,headers:{'X-Regis-Account-Access':'false'}});
      if (statusController === controller) renderStatus(data);
    } catch (error) {
      if (statusController !== controller) return;
      localRecovery = false;
      $('dataSummary').dataset.error = 'true';
      $('dataSummary').textContent = '本次检查失败：' + error.message + '。下方内容保留上次观察，不作为当前状态；可重新检查。';
      ['dataService','dataRegistry','dataRecoveryMode','dataRuntime'].forEach(id => { $(id).textContent = '本次未核验'; });
      $('registryInspect').disabled = true;
      updateRestoreButton();
    } finally {
      if (statusController === controller) { statusController = null; $('dataRefresh').disabled = false; }
    }
  }

  async function upload(restore) {
    const file = $('registrySnapshot').files[0];
    if (uploadBusy) return;
    if (!localRecovery || !file || file.size > 32 * 1024 * 1024 || file.size === 0) {
      $('registryResult').textContent = '请选择不超过32MiB的非空本地 SQLite 快照，并先检查网站状态。'; return;
    }
    if (restore && (file !== inspectedFile || $('registryExpectedSha').value !== inspectedSha)) return;
    uploadBusy = true; $('registryInspect').disabled = true; updateRestoreButton();
    const form = new FormData(); form.append('snapshot',file);
    if (restore) form.append('expected_sha256',$('registryExpectedSha').value);
    $('registryResult').className = '';
    $('registryResult').textContent = restore ? '正在核验并恢复；请等待结果。' : '正在检查快照结构与字节身份…';
    try {
      const data = await jsonRequest('/api/site/registry/' + (restore ? 'restore' : 'inspect'),{
        method:'POST',headers:{'X-Regis-Request':'data-center'},body:form,
      },60);
      if (restore) {
        inspectedFile = null; inspectedSha = null;
        $('registryResult').textContent = data.message;
        $('registryResult').className = 'data-result-ok';
        await refresh();
      } else {
        inspectedFile = data.restorable ? file : null; inspectedSha = data.restorable ? data.sha256 : null;
        $('registryInspection').textContent = (data.restorable ? '结构与非空记录检查通过' : '检查未满足恢复条件')
          + ' · ' + (states[data.inspection?.state] || '待核验') + ' · 记录数 ' + (data.inspection?.rows ?? '未知')
          + ' · SHA256 ' + data.sha256 + '。原件来源与研究结论尚未认证。';
        $('registryResult').textContent = data.restorable ? '核对期望摘要后，可以恢复到缺失的研究库。' : '请提供完整、兼容、非空的一致备份；当前文件不会恢复。';
      }
    } catch (error) {
      inspectedFile = null; inspectedSha = null;
      $('registryResult').textContent = error.message + (restore ? ' 请重新检查目标状态后再决定下一步。' : '');
      $('registryResult').className = 'data-result-error';
    } finally { uploadBusy = false; $('registryInspect').disabled = !localRecovery; updateRestoreButton(); }
  }

  $('dataRefresh').addEventListener('click',refresh);
  $('registrySnapshot').addEventListener('change',() => { inspectedFile = null; inspectedSha = null; $('registryExpectedSha').value = ''; $('registryInspection').textContent = '文件已变化，请重新检查。'; updateRestoreButton(); });
  $('registryExpectedSha').addEventListener('input',updateRestoreButton);
  $('registryInspect').addEventListener('click',() => upload(false));
  $('registryRecoveryForm').addEventListener('submit',event => { event.preventDefault(); if (!$('registryRestore').disabled) upload(true); });
  window.addEventListener('pagehide',() => { if (statusController) statusController.abort('navigation'); });
  void refresh();
})();
