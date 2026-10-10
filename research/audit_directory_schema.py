"""Offline structural QA of two exact retained directory payloads; no dataset adoption."""
from pathlib import Path
from io import BytesIO
import datetime,hashlib,json,re,socket,subprocess,sys,zipfile
from xml.etree import ElementTree as ET
ROOT=Path(__file__).resolve().parents[1]
P=ROOT/'research/DIRECTORY_SCHEMA_AUDIT_PROTOCOL_20261009.json'
O=ROOT/'research/DIRECTORY_SCHEMA_AUDIT_RESULT_20261009.json'
NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
SH_FIELDS=('A_STOCK_CODE','SEC_NAME_CN','SEC_NAME_FULL','COMPANY_ABBR','FULL_NAME','LIST_DATE')
SZ_FIELDS=('板块','A股代码','A股简称','A股上市日期','A股总股本','A股流通股本','所属行业')
def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def utc():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def put(p,j):p.write_text(json.dumps(j,ensure_ascii=False,separators=(',',':'))+'\n',encoding='utf-8')
def denied(*a,**k):raise RuntimeError('offline_source_audit_network_process_denied')
READ_CAP=2097152
def verified(ref):
    assert ref['bytes']<=READ_CAP
    with Path(ref['path']).open('rb') as stream:b=stream.read(READ_CAP+1)
    assert len(b)<=READ_CAP and len(b)==ref['bytes'] and hashlib.sha256(b).hexdigest()==ref['sha256']
    return b
def counts(rows,code_field,date_field,expected):
    seen=set();out={'records':len(rows),'known_field_presence':{k:0 for k in expected},'non_mapping_rows':0,'missing_code':0,'code_not_exact_six_ascii_digits':0,'duplicate_code_occurrences':0,'missing_listing_cell':0,'listing_cell_lexical_date_shape':0}
    for row in rows:
        if not isinstance(row,dict):out['non_mapping_rows']+=1;continue
        for k in expected:
            if k in row:out['known_field_presence'][k]+=1
        v=row.get(code_field)
        if v is None or v=='':out['missing_code']+=1
        elif not isinstance(v,str) or not re.fullmatch(r'[0-9]{6}',v):out['code_not_exact_six_ascii_digits']+=1
        else:
            if v in seen:out['duplicate_code_occurrences']+=1
            seen.add(v)
        d=row.get(date_field)
        if d is None or d=='':out['missing_listing_cell']+=1
        elif isinstance(d,str) and re.fullmatch(r'(?:19|20)[0-9]{2}[-/.][0-9]{1,2}[-/.][0-9]{1,2}',d):out['listing_cell_lexical_date_shape']+=1
    out['unique_six_digit_code_strings']=len(seen)
    return out
def audit_sh(b):
    obj=json.loads(b.decode('utf-8-sig',errors='strict'))
    assert isinstance(obj,dict) and isinstance(obj.get('result'),list),'expected_result_list_missing'
    rows=obj['result'];assert len(rows)<=10000
    q=counts(rows,'A_STOCK_CODE','LIST_DATE',SH_FIELDS)
    return {'format':'JSON','row_statistics':q,'root_field_count':len(obj),'pagehelp_present':isinstance(obj.get('pageHelp'),dict),'root_result_is_list':True,'current_main_A_only':True,'asof_and_pagination_completeness_not_certified':True}
def audit_sz(b):
    with zipfile.ZipFile(BytesIO(b)) as z:
        entries=z.infolist();assert len(entries)<=64 and sum(i.file_size for i in entries)<=16777216,'ZIP_decoded_size_or_entry_bound'
        names=[i.filename for i in entries]
        sheets=[n for n in names if re.fullmatch(r'xl/worksheets/sheet[0-9]+\.xml',n)]
        assert len(sheets)==1 and len(names)==len(set(names)),'unexpected_or_duplicate_sheet_layout'
        def xml(name):
            info=z.getinfo(name);assert info.file_size<=8388608
            raw=z.read(name);assert len(raw)==info.file_size and b'<!DOCTYPE' not in raw.upper() and b'<!ENTITY' not in raw.upper(),'XML_DTD_or_size_rejected'
            return ET.fromstring(raw)
        strings=[]
        if 'xl/sharedStrings.xml' in names:
            strings=[''.join(t.text or '' for t in si.findall('.//s:t',NS)) for si in xml('xl/sharedStrings.xml').findall('s:si',NS)]
            assert len(strings)<=100000
        sheet=xml(sheets[0]);xmlrows=sheet.findall('s:sheetData/s:row',NS);assert len(xmlrows)<=10000
        allrows=[];formula_cells=0;cell_count=0
        for xr in xmlrows:
            cells={}
            for c in xr.findall('s:c',NS):
                cell_count+=1;assert cell_count<=200000
                if c.find('s:f',NS) is not None:formula_cells+=1
                ref=c.attrib.get('r','');m=re.fullmatch(r'([A-Z]+)[0-9]+',ref);assert m,'unexpected_cell_reference'
                key=m.group(1);assert key not in cells,'duplicate_cell_reference'
                value=c.find('s:v',NS);v=value.text if value is not None else None
                typ=c.attrib.get('t')
                if typ=='s':
                    assert v is not None and v.isdigit() and 0<=int(v)<len(strings),'shared_string_reference_invalid'
                    v=strings[int(v)]
                elif typ=='inlineStr':
                    inline=c.find('s:is',NS);v=''.join(t.text or '' for t in inline.findall('.//s:t',NS)) if inline is not None else None
                cells[key]=v
            allrows.append(cells)
        headers=[]
        for i,row in enumerate(allrows[:20]):
            if any(v=='A股代码' for v in row.values()):headers.append((i,row))
        assert len(headers)==1,'unique_A_code_header_not_found'
        headeridx,header=headers[0]
        for label in SZ_FIELDS:assert sum(v==label for v in header.values())<=1,'duplicate_known_header'
        mapping={v:k for k,v in header.items() if v in SZ_FIELDS}
        assert all(k in mapping for k in ['A股代码','A股上市日期']),'required_known_header_missing'
        rows=[{label:row.get(col) for label,col in mapping.items()} for row in allrows[headeridx+1:]]
        return {'format':'OOXML_XLSX','archive_entries':len(entries),'archive_uncompressed_bytes':sum(i.file_size for i in entries),'worksheet_count':len(sheets),'XML_row_count':len(xmlrows),'header_row_index_zero_based':headeridx,'expected_header_presence':{k:k in mapping for k in SZ_FIELDS},'row_statistics':counts(rows,'A股代码','A股上市日期',SZ_FIELDS),'formula_cells_not_evaluated':formula_cells,'shared_string_count':len(strings),'numeric_or_date_style_cells_not_coerced':True,'source_HTTP_charset_not_used_to_decode_binary_XLSX':True}
def plan():
    p=read(P);assert p['new_requests']==0 and p['data_rows_to_AI_or_Git']==0 and p['unit_tests']==2
    for f,h in p['input_hashes'].items():assert sha(ROOT/f)==h
    previous=read(ROOT/p['source_result']);byrole={v['role']:v for v in previous['diagnostics']['records']}
    for role,ref in p['body_refs'].items():
        q=byrole[role];assert q['HTTP_status']==200 and q['complete_bounded_HTTP_200_body'] and not ref['truncated_for_capture'] and q['body_ref']==ref
        verified(ref)
    return p
def main():
    p=plan()
    if '--validate-plan' in sys.argv:
        print(json.dumps({'plan_passed':True,'two_exact_body_refs_bound':True,'new_requests':0}));return
    socket.socket=denied;socket.create_connection=denied;subprocess.Popen=denied;subprocess.run=denied
    n=Path(p['native_directory']);started=utc()
    with(n/p['audit_dispatch_filename']).open('x',encoding='utf-8') as f:f.write(started+'\n')
    records=[];ok=True
    for role,ref in p['body_refs'].items():
        try:
            q=(audit_sh if role=='SSE_main_A_directory_payload' else audit_sz)(verified(ref));q.update(role=role,body_ref=ref,audit_completed=True,failure_type=None)
        except Exception as exc:
            q={'role':role,'body_ref':ref,'audit_completed':False,'failure_type':type(exc).__name__};ok=False
        records.append(q)
    r={'schema':'regis.directory_schema_audit_result.v1','state':'structural_counts_preserved_not_dataset_admission' if ok else 'structural_failure_preserved','started_utc':started,'ended_utc':utc(),'records':records,
       'new_requests':0,'SDK_calls':0,'data_rows_to_AI_or_Git':0,'derived_market_values':0,'new_formulas':0,'economic_runs':0,'unit_tests':2,'raw_source_rows_persisted_by_audit':0,
       'rights_verified':False,'source_publication_asof_verified':False,'current_full_A_verified':False,'historical_PIT_verified':False,'permanent_identity_verified':False,'market_gate_ready':False,'model_admitted':False,'full_goal_complete':False,
       'protocol_sha256':sha(P),'implementation_sha256':sha(Path(__file__)),'contract_sha256':read(ROOT/'WORKFLOW_TASK.json')['contract_sha256'],
       'limits':['Counts and lexical code/date shapes only; no source values exported, IDs/dates normalised, missing cells filled, formulas evaluated or records selected.',
       'Current partial directories cannot prove historical universe, valid eligible securities, asof/publication clocks, stock identity intervals or data use rights.',
       'XLSX numeric/date styles remain raw lexical values; no zero-padding codes or Excel serial-date conversion inferred from HTTP charset.',
       'SSE result list and SZSE one worksheet only; pagination, membership semantics, board/status/action roles independently unverified.']}
    put(n/'structural_counts.json',r);put(O,r);print(json.dumps(r,ensure_ascii=False))
    raise SystemExit(0 if ok else 1)
if __name__=='__main__':main()
