"""Archive existing company research metadata and historical observations; no new market research."""
from pathlib import Path
import json,hashlib,csv,re,datetime,socket,subprocess,sys,os,zipfile,xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/COMPANY_RESEARCH_ARCHIVE_PROTOCOL_20261009.json'
RESULT=ROOT/'research/COMPANY_RESEARCH_ARCHIVE_RESULT_20261009.json'
def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def put(p,j):p.write_text(json.dumps(j,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def denied(*a,**k):raise RuntimeError('historical_archive_no_network_or_process')
def md(v):return str(v).replace('|','\\|').replace('\n',' ')
def scalar(v):
    if v is None:return '未记录'
    if isinstance(v,bool):return '是' if v else '否'
    if isinstance(v,(list,dict)):return json.dumps(v,ensure_ascii=False,separators=(',',':'))
    return str(v)
KINDS={'split_or_capital_transfer':'分红拆股转增公告','record_date_verification':'登记日专项核查','rights_or_merger':'配股或换股事项','alias_dividend_dates':'旧代码分红日期','issuer_original_notice':'发行人原公告','automatic_notice_candidates':'公告自动提取候选','alias_source_capture':'代码变更原资料获取','alias_interval_diagnostic':'标识区间核查','alias_series_diagnostic':'新旧序列对照','alias_action_gap':'代码变更公司行为缺项','alias_price_limit_gap':'价格限制数据缺项','industry_class_original_attempts':'行业分类原报告核查','prospectus_class':'招股资料行业分类核查','issuer_identity':'发行人身份报告核查','market_data_QA_only':'行情质量核查','fixed_sample_input_only':'固定样本因子输入','original_factor_input_only':'原因子输入检查','quarantined_source_field_audit':'隔离行情字段检查','current_identity_only':'当前身份检查','historical_replay_exposure':'历史模拟记录','company_source_cache':'公司画像缓存','other_existing_company_metadata':'其他历史结果资料'}
FIELDS={'record_date':'原登记日','ex_date':'原除权除息日','share_credit_date':'股份到账日','unrestricted_share_trading_start_date':'新增股可交易日','post_split_total_share_ratio':'拆股后总股数倍数','cash_CNY_per_pre_split_share':'每原股现金元','cash_pay_date':'现金派发日','subscription_start_date':'配股缴款开始','subscription_end_date':'配股缴款截止','subscription_price':'配股价元','actual_credit_date':'实际到账日','public_new_share_trading_date':'公告新股上市日','notice_id':'公告编号','title':'标题','ann_date':'原公告日期','state':'原记录状态','claim_identity_predicates_all':'所有身份谓词通过','classification_corrected':'分类已修正','intended_class':'待核行业代码','intended_class_name':'待核行业名称','report_year':'报告年度','source':'缓存维度','fetched_at':'原抓取时点','items':'缓存条数','summary':'历史内容摘要','old':'旧标识','new':'新标识','expected_date_from_protocol_not_validated_source_date':'协议预期日期非来源确认','source_classes_activated':'已启用分类','dates_ready':'自动日期字段就绪','visual_checked':'已视觉复核','automatically_extracted_only':'仅自动提取','distribution_type_uncertain':'分派类型不确定','distribution_type':'原分派类型'}
def checked():
    p=read(PROTOCOL);m=read(ROOT/p['source_manifest'])
    for rel,h in p['input_hashes'].items():assert sha(ROOT/rel)==h,rel
    for ref in m['sources']:
        path=ROOT/ref['path'];assert path.exists() and sha(path)==ref['sha256'],ref['path']
    for ref in m['native_refs']:
        path=Path(ref['path']);assert path.exists() and sha(path)==ref['sha256'],path.name
    q=read(Path(p['prepared_ref']['path']));assert sha(Path(p['prepared_ref']['path']))==p['prepared_ref']['sha256']
    assert len(q['companies'])==p['expected_security_codes'] and len(q['events'])==p['expected_events']
    assert all(not x['fully_researched'] and not x['trading_qualified'] for x in q['companies'])
    return p,m,q
def csvfile(p,fields,rows):
    with p.open('w',newline='',encoding='utf-8-sig') as f:
        wr=csv.DictWriter(f,fieldnames=fields);wr.writeheader();wr.writerows(rows)
def record_link(ref,base):
    if 'path' in ref:
        rel=os.path.relpath(ROOT/ref['path'],base).replace('\\','/')
        return '['+ref['path']+']('+rel+')'+ref.get('pointer','')
    cache_path=read(PROTOCOL)['cache_ref']['path']
    return '[本地公司缓存快照](<'+Path(cache_path).as_posix()+'>) '+ref.get('pointer','')
def build(p,m,q):
    out=ROOT/p['output_directory'];out.mkdir(exist_ok=True);(out/'companies').mkdir(exist_ok=True)
    existing={x.relative_to(ROOT).as_posix() for x in out.rglob('*') if x.is_file()}
    if any((ROOT/rel).stat().st_size for rel in existing):
        previous=read(Path(p['native_directory'])/'generated_manifest.json')
        owned={rel for rel in previous['paths'] if rel.startswith(p['output_directory']+'/')}
        assert existing==owned,'refuse overwrite with missing or unowned archive files'
        for rel in owned:assert sha(ROOT/rel)==previous['hashes'][rel],rel
    else:
        for rel in existing:assert (ROOT/rel).stat().st_size==0
    cache=read(Path(p['cache_ref']['path']))['entries']
    cachemap={(e['code'],e['source']):e['payload'] for e in cache}
    index=[];work_rows=[]
    for comp in q['companies']:
        code=comp['code'];name=comp['names'][0] if comp['names'] else '历史记录未绑定名称'
        namekind='已有结构化名称或原报告身份字段' if code in ['000001','600000','603026','603626'] or any('announcement metadata' in e.get('kind','') for e in comp['name_evidence']) else ('公告首面名称候选待复核' if comp['names'] else '名称未绑定')
        company=(out/'companies'/(code+'.md'));lines=['# '+name+' '+code,'',comp['completeness']+'。名称按历史资料保留，代码变更关系见对应原记录。','', '## 已有资料','', '| 资料类型 | 来源记录 |','| --- | --- |']
        unique=[]
        for rec in comp['records']:
            key=(rec['kind'],json.dumps(rec['source'],sort_keys=True))
            if key in unique:continue
            unique.append(key);lines.append('| '+KINDS.get(rec['kind'],rec['kind'])+' | '+record_link(rec['source'],company.parent)+' |')
        if code=='000001':
            financial_rows=cachemap[(code,'financials')]['data']
            assert all(isinstance(row.get('period'),str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',row['period']) and datetime.date.fromisoformat(row['period']) for row in financial_rows)
            finance=max(financial_rows,key=lambda row:row['period'])
            periods=[row['period'] for row in financial_rows]
            lines+=['','## 历史财务与公司画像','', '现存财务缓存共 '+str(len(financial_rows))+' 期，范围 '+min(periods)+' 至 '+max(periods)+'；下表选取缓存中最大报告期 '+finance['period']+'，不依据原列表顺序推断最新。原抓取时间 '+cachemap[(code,'financials')]['fetched_at']+'。金额单位按已有解析器的亿元口径保留；这是历史缓存，尚未在本次与原报告逐项核对。完整历史序列见 '+record_link({'pointer':'/entries/'+str(next(i for i,e in enumerate(cache) if e['code']==code and e['source']=='financials'))+'/payload/data'},company.parent)+'。','', '| 指标 | 缓存数值 | 单位 |','| --- | --- | --- |']
            for field,label,unit in [('revenue','营业收入','亿元'),('net_profit','净利润','亿元'),('deducted','扣非净利润','亿元'),('revenue_yoy','营业收入同比','%'),('np_yoy','净利润同比','%'),('eps','每股收益','元每股'),('roe','ROE','%'),('debt_ratio','资产负债率','%')]:
                lines.append('| '+label+' | '+scalar(finance.get(field))+' | '+unit+' |')
            audit=cachemap[(code,'audit_opinion')]['data'];bus=cachemap[(code,'business')]['data']
            lines+=['','既有审计缓存记录 '+str(audit.get('report_year'))+' 年度意见为 '+str(audit.get('label'))+'，审计机构 '+str(audit.get('audit_firm'))+'。[原审计报告]('+audit['source_url']+')。','', '主营构成缓存报告期 '+str(bus.get('period'))+'，记录 '+ '、'.join(e['name'] for e in bus.get('product',[]))+'。','', '公告缓存有 13 条；研报缓存有 100 条题录，另附汇总表。研报题录不表示全文已阅。旧画像中的红绿灯、分数和否决仅为当时程序结果，本次未采用为公司质量或交易结论。']
            anns=cachemap[(code,'announcements')]['data']
            lines+=['','### 公告题录','', '| 日期 | 标题 | 原链接 |','| --- | --- | --- |']
            for a in anns:lines.append('| '+md(a.get('date'))+' | '+md(a.get('title'))+' | [公告]('+a['url']+') |')
        lines+=['','## 原专项记录','']
        for i,rec in enumerate(comp['records']):
            detail=rec['detail'];visible={k:v for k,v in detail.items() if k in FIELDS and v is not None}
            if not visible:continue
            lines+=['### '+KINDS.get(rec['kind'],rec['kind'])+' '+str(i+1),'','| 字段 | 原记录内容 |','| --- | --- |']
            for k,v in visible.items():lines.append('| '+FIELDS[k]+' | '+md(scalar(v))+' |')
            url=detail.get('url',detail.get('URL'));native=detail.get('native_pdf')
            if url:lines+=['','[来源页面或公告]('+url+')']
            if native:
                path=Path(native)
                if not path.is_absolute():path=ROOT/path
                if path.exists():lines+=['','[本地原公告](<'+path.as_posix()+'>)']
                else:lines+=['','原记录本地公告路径已不可用，仍保留原 URL 与 SHA。']
            if detail.get('pdf_sha256',detail.get('sha256')):lines+=['','原文件 SHA256：'+str(detail.get('pdf_sha256',detail.get('sha256')))]
            lines+=['','来源记录：'+record_link(rec['source'],company.parent),'']
        if comp['name_evidence']:
            lines+=['','## 历史名称依据','', '| 名称 | 依据 |','| --- | --- |']
            for e in comp['name_evidence']:
                lines.append('| '+md(e['name'])+' | '+('公告首面文本候选，未重新逐页视觉认证' if 'candidate' in e.get('kind','') else '已保存结构化公告元数据')+' |')
        lines+=['','## 使用范围','', '本页保留历史观察、核查状态和失败记录。原公告自动提取、来源访问失败及未决字段分别保留；没有将它们提升为完整经营调研、历史时点完整数据或可交易策略。原缓存和 PDF 留在原存储，未把原始行情或完整缓存复制进 Git。','']
        company.write_text('\n'.join(lines),encoding='utf-8')
        idx={'证券代码':code,'历史名称':name,'资料范围':comp['completeness'],'名称依据':namekind,'资料引用数':len(unique),'公司档案':'companies/'+code+'.md'}
        index.append(idx);work_rows.append([code,name,comp['completeness'],namekind,len(unique),'companies/'+code+'.md'])
    csvfile(out/'company_index.csv',list(index[0]),index)
    event_fields=['证券代码','资料类型','原登记日','原除权除息日','现金派发日','公告编号','证据记录','原链接','来源文件']
    eventrows=[]
    for e in q['events']:
        quality='原记录含视觉页核查' if e.get('visually_checked_pages') else ('自动提取候选或日期专项绑定，未逐字段视觉认证')
        eventrows.append({'证券代码':e['code'],'资料类型':KINDS[e['kind']],'原登记日':e.get('record_date'),'原除权除息日':e.get('ex_date'),'现金派发日':e.get('cash_pay_date'),'公告编号':e.get('notice_id'),'证据记录':quality,'原链接':e.get('url'),'来源文件':e['reference']['path']+e['reference']['pointer']})
    csvfile(out/'events.csv',event_fields,eventrows)
    srcrows=[{'来源编号':str(i+1),'原资料文件':e['path'],'类别':e['category'],'SHA256':e['sha256']} for i,e in enumerate(q['sources'])]
    csvfile(out/'sources.csv',list(srcrows[0]),srcrows)
    reports=cachemap[('000001','research_reports')]['data']
    reportrows=[{'日期':e.get('date'),'机构':e.get('institution'),'历史评级':e.get('rating'),'研报标题':e.get('title'),'题录链接':e.get('pdf_url')} for e in reports]
    csvfile(out/'research_reports_000001.csv',list(reportrows[0]),reportrows)
    short={k:v for k,v in q.items() if k not in ['cache_snapshot']};put(out/'catalogue.json',short)
    lines=['# 以前调查的公司资料总目录','', '整理日期 2026年10月9日。现存资料按证券代码汇总，共 '+str(len(q['companies']))+' 份代码档案，其中 '+str(sum(bool(e['names']) for e in q['companies']))+' 份有历史名称或公告首面名称候选。旧、新代码可能属于同一发行人，不能按代码数计算独立公司数。','', '平安银行保留 8 类画像缓存；浦发银行和 600519 各保留 2 类局部缓存。另收录分红拆股转增、配股换股、代码变更、行业分类与行情核查。未把目录成员、模拟出现的证券或虚构测试样本称为完成公司调研。','', '## 查阅入口','', '- [Excel 汇总表](../../outputs/01a0e81f-c45e-7e22-8786-8c5118aa2f9c/company_research_20261009.xlsx)','- [可筛选公司索引 CSV](company_index.csv)','- [110 条公司事件资料记录](events.csv)','- [平安银行 100 条研报题录](research_reports_000001.csv)','- [原资料文件索引与 SHA](sources.csv)','- [机器可读资料目录](catalogue.json)','', '## 重点公司资料','', '- [平安银行 000001](companies/000001.md)：财务、主营、公告、审计、研报、质押与筹码估算；画像仍不完整。','- [浦发银行 600000](companies/600000.md)：原报告身份、公司行为、局部画像、行情与原因子输入。','- [600519](companies/600519.md)：只找到质押汇总与筹码估算；现存缓存没有名称绑定和完整公司画像。','- [石大胜华历史发行人 603026](companies/603026.md)、[科森科技 603626](companies/603626.md)：行业分类专项；原分类冲突和获取失败保留，未认证完整历史分类。','', '## 完整代码目录','', '| 代码及档案 | 历史名称或候选 | 已有资料范围 |','| --- | --- | --- |']
    for e in index:lines.append('| ['+e['证券代码']+']('+e['公司档案']+') | '+md(e['历史名称'])+' | '+e['资料范围']+' |')
    lines+=['','## 关联基金与数据平台资料','', '510300 为华泰柏瑞沪深300 ETF，既有基金报告身份、分红与基准研究记录见 [身份原记录](../../research/IDENTITY_OBSERVATIONS_20261002.json) 与 [基金公司行为记录](../../research/ETF_ACTION_CENSUS_RESULT_20261002.json)。基金管理人历史名称为华泰柏瑞基金管理有限公司，不计入上述 A 股代码档案数。','', '| 平台或项目 | 以前调查的结论 | 原记录 |','| --- | --- | --- |','| Tushare | 项目用途许可、留存、AI处理与实际接口权益待明确，不能据积分价格认定可用。 | [旧来源决策](../../QUANT_RESEARCH_M24_SOURCE_DECISION_20261001.md) |','| BaoStock | 已有匿名有限行情与字段检查；ETF连续历史、全A覆盖、时点身份及行情质量各有缺项。 | [原用途范围](../../research/GOAL_BAOSTOCK_SOURCE_SCOPE_20261001.json)、[字段记录](../../research/BAOSTOCK_FIELD_CONTRACT_RESULT_20261006.json) |','| AKShare | 复用了开源维护方定位器，原数据提供方权益须分别确认。 | [来源矩阵](../../QUANT_RESEARCH_M19_SOURCE_MATRIX_20260930.json) |','| 米筐 RQData 与 RQAlpha | 旧来源比较与离线参考中使用，未证明可准入项目完整数据。 | [来源矩阵](../../QUANT_RESEARCH_M19_SOURCE_MATRIX_20260930.json) |','| 聚宽 JQData | 原公开文档路线遇访问限制；产品权益与价格未核实，未读用户账户。 | [原调查结果](../../research/ALTERNATIVE_DATA_PROVIDER_RESULT_20261006.json) |','| 掘金 GM MyQuant | 旧公开文档获取403，目录索引不能证明实际权限或ETF执行数据。 | [原调查结果](../../research/NEW_PROVIDER_ETF_FEASIBILITY_RESULT_20261007.json) |','| 上证信息 SSEInfo | 已有历史与用途权益调查，按原结果保留访问和使用范围限制。 | [原调查结果](../../research/SSEINFO_HISTORY_RIGHTS_RESULT_20261007.json) |','', '## 资料时间与范围','', '公告日期、报告期、原抓取时间和本次整理日期分别保留。历史名称可能已变更；公告首面自动恢复的名称仅为候选。旧公司评分、银行通用负债率灯号、筹码估算和研报评级不提升为本次投资结论。部分原字段只做了自动提取或专项核查，具体状态见公司页。','', '本次覆盖当前工作分支已有结构化研究结果、本地公司缓存、选定的已保存公告元数据，以及所有本地可达 Git 引用的相关文件名审计。没有读取交易账户、凭证、原始交易所目录行或行情数组。未知外部备份、不可达 Git 引用与已丢失聊天资料不在可恢复范围内。原公司缓存可在本地 SQLite 或此次本地快照查阅，完整原缓存和 PDF 未复制入 Git。','']
    lines+=['','[完整本地公司缓存快照](<'+Path(p['cache_ref']['path']).as_posix()+'>)：12 项原缓存完整保留，包括财务历史序列、原公告与研报题录、主营、审计、质押与筹码估算；抓取时点和报告期沿用原记录。此链接供当前电脑查阅，GitHub 上保留索引、摘要与来源记录。','']
    (out/'README.md').write_text('\n'.join(lines),encoding='utf-8')
    cache_rows=[[e['code'],e['source'],e['payload'].get('fetched_at'),'历史缓存，未重查原源',len(e['payload']['data']) if isinstance(e['payload']['data'],list) else None] for e in cache]
    put(Path(p['native_directory'])/'workbook_inputs.json',{'company_index':work_rows,'events':[[e[f] for f in event_fields] for e in eventrows],'sources':[[e[f] for f in list(srcrows[0])] for e in srcrows],'reports':[[e[f] for f in list(reportrows[0])] for e in reportrows],'cache':cache_rows,'counts':{'security_codes':len(index),'events':len(eventrows),'source_files':len(srcrows),'reports':len(reports),'cache_entries':len(cache)}})
    paths=sorted(x.relative_to(ROOT).as_posix() for x in out.rglob('*') if x.is_file())
    put(Path(p['native_directory'])/'generated_manifest.json',{'paths':paths,'hashes':{rel:sha(ROOT/rel) for rel in paths}})
    print(json.dumps({'archive_written':True,'dossiers':len(index),'files':len(paths),'new_requests':0,'model_admitted':False}))
def verify(p,m,q):
    out=ROOT/p['output_directory'];n=Path(p['native_directory']);g=read(n/'generated_manifest.json')
    for rel,h in g['hashes'].items():assert sha(ROOT/rel)==h
    expected={v['code'] for v in q['companies']}
    assert {x.stem for x in (out/'companies').glob('*.md')}==expected
    index=list(csv.DictReader((out/'company_index.csv').open(encoding='utf-8-sig')))
    assert {e['证券代码'] for e in index}==expected and len(index)==len(expected)
    for e in index:assert (out/e['公司档案']).exists()
    for comp in q['companies']:
        for rec in comp['records']:
            if 'path' not in rec['source']:continue
            value=read(ROOT/rec['source']['path'])
            for key in rec['source'].get('pointer','').split('/')[1:]:value=value[int(key)] if isinstance(value,list) else value[key]
            assert value is not None
    broken=[]
    for path in out.rglob('*.md'):
        for href in re.findall(r'\]\(([^)]+)\)',path.read_text(encoding='utf-8')):
            href=href.strip('<>')
            if href.startswith(('https://','http://')):continue
            target=Path(href)
            if not target.is_absolute():target=path.parent/target
            if not target.exists():broken.append({'file':str(path.relative_to(ROOT)),'href':href})
    assert not broken,broken
    finance=next(e['payload']['data'] for e in read(Path(p['cache_ref']['path']))['entries'] if e['code']=='000001' and e['source']=='financials')
    latest=max(finance,key=lambda row:row['period'])
    dossier=(out/'companies/000001.md').read_text(encoding='utf-8')
    assert '缓存中最大报告期 '+latest['period'] in dossier
    for field,label in [('revenue','营业收入'),('net_profit','净利润'),('deducted','扣非净利润'),('revenue_yoy','营业收入同比'),('np_yoy','净利润同比'),('eps','每股收益'),('roe','ROE'),('debt_ratio','资产负债率')]:
        assert '| '+label+' | '+scalar(latest.get(field))+' |' in dossier
    wb=read(n/'workbook_verification.json');assert wb['values_preserved_on_reimport'] and wb['sheet_count']==5 and wb['source_fetch_clocks_exact']
    assert wb['visual_review_by_main']=='passed_all_five_previews'
    x=ROOT/p['workbook_path'];assert x.exists() and sha(x)==wb['xlsx_sha256']
    previews=read(n/'preview_manifest.json');assert previews['xlsx_sha256']==sha(x) and len(previews['previews'])==5
    for image_ref in previews['previews']:assert sha(Path(image_ref['path']))==image_ref['sha256']
    ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(x) as z:
        strings=ET.fromstring(z.read('xl/sharedStrings.xml')) if 'xl/sharedStrings.xml' in z.namelist() else None
        for sheet,rows in [('sheet1',read(n/'workbook_inputs.json')['company_index']),('sheet2',read(n/'workbook_inputs.json')['events']),('sheet4',read(n/'workbook_inputs.json')['cache'])]:
            xml=ET.fromstring(z.read('xl/worksheets/'+sheet+'.xml'))
            for i,row in enumerate(rows,6):
                cell=xml.find('.//m:c[@r="A'+str(i)+'"]',ns);assert cell is not None and cell.get('t') in ['str','s','inlineStr']
                value=cell.find('m:v',ns)
                literal=''.join(cell.itertext()) if value is None else value.text
                if cell.get('t')=='s':literal=''.join(strings[int(value.text)].itertext())
                assert literal==row[0] and re.fullmatch(r'[0-9]{6}',literal)
    if p['workbook_path'] not in g['paths']:g['paths'].append(p['workbook_path'])
    g['hashes'][p['workbook_path']]=sha(x);put(n/'generated_manifest.json',g)
    r={'schema':1,'state':'historical_company_archive_verified_not_financial_qualification','security_code_dossiers':len(expected),'not_independent_company_count':True,'names_or_heading_candidates':sum(bool(e['names']) for e in q['companies']),'company_cache_codes':3,'company_cache_entries':12,'event_records':len(q['events']),'source_files':len(q['sources']),'research_report_metadata_rows':100,'broken_local_links':0,'new_source_requests':0,'account_or_credential_reads':0,'new_tests':0,'new_financial_runs':0,'raw_cache_or_quote_data_to_Git':False,'model_admitted':False,'full_goal_complete':False,'archive_directory':p['output_directory'],'workbook_path':p['workbook_path'],'generated_paths':g['paths'],'generated_hashes':g['hashes'],'protocol_sha256':sha(PROTOCOL),'source_manifest_sha256':sha(ROOT/p['source_manifest']),'implementation_sha256':sha(Path(__file__)),'contract_sha256':read(ROOT/'WORKFLOW_TASK.json')['contract_sha256'],'native_coverage_limit':q['coverage'],'fixed_review_and_Git':'pending_at_metadata_write'}
    put(RESULT,r);print(json.dumps({k:r[k] for k in ['state','security_code_dossiers','event_records','source_files','broken_local_links','new_source_requests','full_goal_complete']}))
def main():
    p,m,q=checked();socket.socket=denied;socket.create_connection=denied;subprocess.run=denied;subprocess.Popen=denied
    if '--build' in sys.argv:build(p,m,q)
    elif '--verify' in sys.argv:verify(p,m,q)
    else:raise ValueError('explicit build or verify required')
if __name__=='__main__':main()

