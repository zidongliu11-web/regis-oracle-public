"""Inspect one retained official HTML body as metadata, never rows or price data."""
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urljoin,urlsplit
import datetime,hashlib,json,re,socket,subprocess,sys
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/RETAINED_CATALOGUE_STRUCTURE_PROTOCOL_20261009.json'
RESULT=ROOT/'research/RETAINED_CATALOGUE_STRUCTURE_RESULT_20261009.json'

def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def utc():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def put(p,j):p.write_text(json.dumps(j,ensure_ascii=False,separators=(',',':'))+'\n',encoding='utf-8')
def deny(*a,**k):raise RuntimeError('offline_audit_network_or_process_denied')

HEADER_LABELS=('新证券代码','原证券代码','证券代码','证券简称','公司名称','上市日期','证券新代码','证券原代码','原代码','新代码')
POLICY_LABELS=('法律声明','版权声明','免责声明','网站声明','使用条款','服务条款','著作权','版权')
class Structure(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables=[];self.table=None;self.row=None;self.cell=None
        self.script_depth=0;self.script_tags=0;self.nested_tables=0
        self.a=None;self.policy_links=[];self.note_matches=0
        self.header_counts={v:0 for v in HEADER_LABELS}
    def handle_starttag(self,tag,attrs):
        if tag=='script':self.script_depth+=1;self.script_tags+=1;return
        if self.script_depth:return
        if tag=='table':
            if self.table is not None:raise ValueError('nested_table_not_supported_for_static_audit')
            self.table={'index':len(self.tables),'rows':0,'rows_with_2_six_digit_cells':0,
                'rows_with_2_six_digit_cells_and_date_cell':0,'blank_cells_in_code_pair_rows':0,
                'header_occurrences':{v:0 for v in HEADER_LABELS}}
            self.tables.append(self.table)
        elif tag=='tr' and self.table is not None:
            if self.row is not None:raise ValueError('implicit_row_closure_not_supported')
            self.row=[]
        elif tag in ('td','th') and self.row is not None:
            if self.cell is not None:raise ValueError('implicit_cell_closure_not_supported')
            self.cell=[]
        elif tag=='a':self.a={'href':dict(attrs).get('href',''),'text':[]}
    def handle_data(self,data):
        if self.script_depth:return
        if self.cell is not None:self.cell.append(data)
        if self.a is not None:self.a['text'].append(data)
        if '精选层' in data and '上市日期' in data:self.note_matches+=1
    def handle_endtag(self,tag):
        if tag=='script':self.script_depth=max(0,self.script_depth-1);return
        if self.script_depth:return
        if tag in ('td','th') and self.cell is not None:
            text=''.join(self.cell).strip()
            self.row.append(text)
            for h in HEADER_LABELS:
                if text==h:self.header_counts[h]+=1;self.table['header_occurrences'][h]+=1
            self.cell=None
        elif tag=='tr' and self.row is not None:
            if self.cell is not None:raise ValueError('implicit_cell_closure_not_supported')
            self.table['rows']+=1
            codes=sum(bool(re.fullmatch(r'[0-9]{6}',s)) for s in self.row)
            dates=sum(bool(re.fullmatch(r'(?:19|20)[0-9]{2}[-/.][0-9]{1,2}[-/.][0-9]{1,2}',s)) for s in self.row)
            if codes>=2:
                self.table['rows_with_2_six_digit_cells']+=1
                self.table['blank_cells_in_code_pair_rows']+=sum(not s for s in self.row)
                if dates:self.table['rows_with_2_six_digit_cells_and_date_cell']+=1
            self.row=None
        elif tag=='table':
            if self.cell is not None or self.row is not None:raise ValueError('implicit_row_or_cell_closure_not_supported')
            self.table=None
        elif tag=='a' and self.a is not None:
            title=''.join(self.a['text']).strip()
            labels=[h for h in POLICY_LABELS if h in title]
            target=urlsplit(urljoin('https://www.bse.cn/service/code_mapping.html',self.a['href']))
            if labels and target.scheme=='https' and target.hostname=='www.bse.cn' and not target.query and not target.fragment:
                self.policy_links.append({'matched_policy_labels':labels,'url':target.geturl()})
            self.a=None

def load_plan():
    p=read(PROTOCOL)
    assert p['schema']=='regis.retained_catalogue_structure_protocol.v1'
    assert p['new_requests']==0 and p['scripts_executed']==0 and p['unit_tests']==2
    assert p['dataset_rows_persisted']==0 and p['source_or_financial_admission'] is False
    for f,h in p['input_hashes'].items():assert sha(ROOT/f)==h,f
    r=read(ROOT/p['source_result'])
    records=[v for v in r['diagnostics']['records'] if v['role']=='BSE_code_mapping']
    assert len(records)==1
    q=records[0]
    assert q['HTTP_status']==200 and q['complete_bounded_HTTP_200_body'] and not q['body_ref']['truncated_for_capture']
    assert q['body_ref']==p['body_ref']
    b=Path(q['body_ref']['path'])
    assert b.stat().st_size==q['body_ref']['bytes'] and b.stat().st_size<=2097152
    assert sha(b)==q['body_ref']['sha256']
    return p,b

def main():
    p,b=load_plan()
    if '--validate-plan' in sys.argv:
        print(json.dumps({'plan_passed':True,'source_body_bound':True,'new_requests':0,'raw_body_to_AI_or_Git':False}));return
    socket.socket=deny;socket.create_connection=deny;subprocess.Popen=deny;subprocess.run=deny
    n=Path(p['native_directory']);started=utc()
    with(n/p['audit_dispatch_filename']).open('x',encoding='utf-8') as out:out.write(started+'\n')
    buf=b.read_bytes()
    assert len(buf)==p['body_ref']['bytes'] and hashlib.sha256(buf).hexdigest()==p['body_ref']['sha256']
    doc=buf.decode('utf-8',errors='strict')
    parser=Structure();parser.feed(doc);parser.close()
    if parser.table is not None or parser.row is not None or parser.cell is not None:raise ValueError('unclosed_table_markup_not_supported')
    candidates=[v['index'] for v in parser.tables if v['rows_with_2_six_digit_cells']>0 and sum(v['header_occurrences'].values())>=2]
    r={'schema':'regis.retained_catalogue_structure_result.v1','state':'retained_official_body_structural_metadata_only',
        'started_utc':started,'ended_utc':utc(),'body_ref':p['body_ref'],'source_url':p['source_url'],
        'encoding_used':'UTF-8 strict','table_count':len(parser.tables),'table_metadata':parser.tables,
        'candidate_table_indices_lexical_only':candidates,'header_label_counts':parser.header_counts,
        'nested_table_count':parser.nested_tables,'script_tag_count_not_executed':parser.script_tags,
        'policy_link_records_not_fetched':parser.policy_links,'former_select_tier_and_listing_date_same_text_block_count':parser.note_matches,
        'source_publication_timestamp_verified':False,'asof_or_completeness_verified':False,
        'current_full_A_verified':False,'historical_PIT_verified':False,'permanent_identity_verified':False,
        'rights_verified':False,'source_or_financial_admission':False,'market_gate_ready':False,
        'rows_to_AI_or_Git':0,'dataset_rows_persisted':0,'numeric_feature_computations':0,
        'new_requests':0,'SDK_calls':0,'scripts_executed':0,'unit_tests':2,'new_formulas':0,'economic_runs':0,
        'model_admitted':False,'full_goal_complete':False,'protocol_sha256':sha(PROTOCOL),
        'implementation_sha256':sha(Path(__file__)),'contract_sha256':read(ROOT/'WORKFLOW_TASK.json')['contract_sha256'],
        'limits':['Lexical table/code/date counts cannot establish valid securities, current completeness, effective identity intervals, or original BSE listing dates.',
        'Presence or absence of a policy anchor is not a licence. No linked page fetched or underlying dataset adopted.',
        'HTMLParser ignores script text; this is static-body inspection, not JavaScript execution or browser rendering.',
        'No source publication or effective-asof timestamp verified; HTTP capture clock only proves acquisition.']}
    put(n/'structural_metadata.json',r);put(RESULT,r)
    print(json.dumps(r,ensure_ascii=False))
if __name__=='__main__':main()
