import argparse,json,hashlib,time,subprocess,sys,os,logging,zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime,timezone
from collections import Counter
from pypdf import PdfReader
from csrc_industry_members import parse_pdf_texts,parse_cell_tables,flat_word_tables,following_session,select_snapshot
import inspect_short_rq_full as base
from read_instruments_static import parse,date_int
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/CSRC_INDUSTRY_MEMBER_PROTOCOL_20261003.json'
RESULT=ROOT/'research/CSRC_INDUSTRY_MEMBER_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def utc():return datetime.now(timezone.utc).isoformat()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def check(s):
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('frozen membership source changed')
 r=read(ROOT/s['source_result']);d=read(r['local_details']['path'])
 if sha(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('native source packet changed')
 for q in d['quarter_sources']:
  if sha(q['attachment']['native_path'])!=q['attachment']['sha256']:raise ValueError('original source bytes changed')
 return d
def dependency(s,local):
 spec=s['xlrd'];wheel=local/spec['filename'];receipt=local/'xlrd-fetch.receipt.json'
 p=subprocess.run([sys.executable,'-X','utf8',str(Path(__file__).parent/'csrc_xlrd_fetch_one.py'),spec['url'],str(wheel),str(receipt),'150000'],capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
 if p.returncode or not receipt.exists() or read(receipt)['state']!='captured' or sha(wheel)!=spec['sha256']:raise ValueError('pinned xlrd dependency capture failed')
 with zipfile.ZipFile(wheel) as z:
  if z.testzip() is not None:raise ValueError('xlrd wheel CRC mismatch')
 sys.path.insert(0,str(wheel));import xlrd
 if xlrd.__version__!=spec['version']:raise ValueError('unexpected xlrd version')
 return xlrd,{'path':str(wheel),'sha256':sha(wheel),'version':xlrd.__version__,'global_runtime_install':False,'dependency_GETs':1,'receipt':str(receipt),'receipt_sha256':sha(receipt)}
def word_xml(s,original,local):
 import psutil
 copy=local/'2012Q4.byte_identical_input.doc';xml=local/'2012Q4.readonly_WordOpenXML.xml';receipt=local/'2012Q4.Word_readonly.receipt.json'
 copy.write_bytes(Path(original).read_bytes());assert sha(copy)==sha(original)
 command=['powershell.exe','-NoProfile','-NonInteractive','-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File',str(ROOT/'research/read_legacy_csrc_word.ps1'),'-InputFile',str(copy),'-OutputFile',str(xml),'-ReceiptFile',str(receipt)]
 timed_out=False
 try:p=subprocess.run(command,capture_output=True,timeout=40,creationflags=subprocess.CREATE_NO_WINDOW)
 except subprocess.TimeoutExpired:timed_out=True;p=None
 meta=read(receipt) if receipt.exists() else {'state':'no_owned_receipt'}
 pid=meta.get('owned_pid');cleanup='no_owned_process'
 if pid and psutil.pid_exists(pid):
  proc=psutil.Process(pid);expected=datetime.fromisoformat(meta['owned_start_utc'].replace('Z','+00:00')).timestamp()
  if proc.name().lower()=='winword.exe' and abs(proc.create_time()-expected)<1:
   # Only the newly owned instance proved by its HWND/start-time may be cleaned up.
   try:proc.wait(timeout=3);cleanup='own_instance_quit'
   except psutil.TimeoutExpired:proc.terminate();proc.wait(timeout=5);cleanup='own_instance_terminated'
  else:cleanup='ownership_mismatch_not_terminated'
 meta.update(parent_timed_out=timed_out,helper_returncode=None if p is None else p.returncode,exact_owned_process_cleanup=cleanup,input_copy_sha256=sha(copy),original_sha256=sha(original),original_unchanged=True)
 write(receipt,meta)
 if timed_out or p.returncode or meta['state']!='derived_readonly_XML' or not xml.exists():raise ValueError('legacy Word read-only conversion unavailable')
 return xml,meta
def run(s):
 if read(RESULT)['state']!='not_run':raise ValueError('retain source membership attempt; no rerun')
 d=check(s);local=Path(s['native_directory']);assert 'onedrive' not in str(local).lower() and not local.exists();local.mkdir(parents=True)
 start=time.monotonic();started=utc();stock=read(ROOT/s['stock_receipt']);raw=Path(stock['raw_directory']);expected={x['name']:x['sha256'] for x in stock['members'] if x['selected']}
 for name in ['trading_dates.npy','instruments.pk']:
  if sha(raw/name)!=expected[name]:raise ValueError('source calendar or identity bytes changed')
 _,np=base.load_dependencies();calendar=list(map(int,base.read_calendar(raw/'trading_dates.npy',np)));identities,_=parse((raw/'instruments.pk').read_bytes())
 census={x['order_book_id']:x for x in identities if x.get('type')=='CS'};code_census={k.split('.')[0]:k for k in census};assert len(code_census)==len(census)
 try:xlrd,dep=dependency(s,local)
 except Exception as e:xlrd=None;dep={'state':'failed','error_type':type(e).__name__,'error':str(e)[:100],'global_runtime_install':False}
 results=[];snapshots=[];native_outputs=[]
 ordered=sorted(d['quarter_sources'],key=lambda x:x['declared_publication_date'])
 for x in ordered:
  q=x['quarter'];src=Path(x['attachment']['native_path']);result={'quarter':q,'source_sha256':x['attachment']['sha256'],'publication_date':x['declared_publication_date'],'effective_session':following_session(calendar,x['declared_publication_date']),'historic_byte_vintage_certified':False,'financial_admission':False}
  try:
   if time.monotonic()-start>s['max_parse_seconds']:raise TimeoutError('frozen parse budget')
   if src.suffix=='.pdf':
    warnings=[]
    class Warn(logging.Handler):
     def emit(self,record):warnings.append(record.getMessage()[:150])
    handler=Warn();logger=logging.getLogger('pypdf');logger.addHandler(handler)
    try:
     pdf=PdfReader(str(src),strict=True);texts=[p.extract_text() or '' for p in pdf.pages]
     if not any(q[:4]+'年'+q[-1]+'季度' in text.replace(' ','') for text in texts[:2]):raise ValueError('original PDF quarter title mismatch')
     transcript=local/(q+'.extracted.txt');transcript.write_text('\n\f\n'.join(texts),encoding='utf-8');native_outputs.append(transcript)
     members,counts=parse_pdf_texts(texts);result.update(reader_warnings=warnings,format='PDF text column order, source major numeric code')
    finally:logger.removeHandler(handler)
   elif src.suffix=='.xls':
    if xlrd is None:raise ValueError('xlrd missing for original XLS')
    with (local/(q+'.xlrd.log')).open('w',encoding='utf-8') as log:book=xlrd.open_workbook(str(src),formatting_info=True,logfile=log,ignore_workbook_corruption=False)
    tables=[]
    for sh in book.sheets():
     merged={}
     for rlo,rhi,clo,chi in sh.merged_cells:
      for r in range(rlo,rhi):
       for c in range(clo,chi):merged[r,c]=sh.cell_value(rlo,clo)
     tables.append([[merged.get((r,c),sh.cell_value(r,c)) for c in range(sh.ncols)] for r in range(sh.nrows)])
    if not any(q[:4]+'年'+q[-1]+'季度' in ''.join(str(v) for row in table[:10] for v in row).replace(' ','') for table in tables):raise ValueError('original XLS quarter title mismatch')
    members,counts=parse_cell_tables(tables);result.update(format='Original XLS via pinned xlrd, merged-cell anchors; cached values read, macros not run',sheet_names=book.sheet_names())
   elif q=='2012Q4' and src.read_bytes()[:8]==bytes.fromhex('d0cf11e0a1b11ae1'):
    xml,word=word_xml(s,src,local);native_outputs.append(xml);tables=flat_word_tables(xml.read_text(encoding='utf-8'))
    title_text=''.join(n.text or '' for n in ET.fromstring(xml.read_text(encoding='utf-8')).iter() if n.tag.endswith('}t'))
    if '2012年4季度' not in title_text.replace(' ',''):raise ValueError('derived Word quarter title mismatch')
    members,counts=parse_cell_tables(tables);result.update(format='docx-named OLE read-only Word conversion derived XML; original byte-identical',Word_readonly=word)
   else:raise ValueError('original source format unsupported')
   bshare=[k for k in members if k.startswith(('2','9'))];mapped={code_census[k]:v for k,v in members.items() if k in code_census and k not in bshare};unmapped=[k for k in members if k not in code_census and k not in bshare]
   target=local/(q+'.members.json');write(target,{'quarter':q,'effective_session':result['effective_session'],'publication_date':result['publication_date'],'original_source_sha256':x['attachment']['sha256'],'members':members,'source_CS_identity_join':mapped,'unmapped_non_B_share_codes':unmapped,'historic_byte_vintage_certified':False});native_outputs.append(target)
   result.update(state='source_members_parsed_not_PIT_admitted',rows=len(members),source_B_share_rows=len(bshare),source_CS_identity_matched=len(mapped),unmapped_non_B_share_rows=len(unmapped),source_major_groups=len({v['source_industry_major_numeric'] for v in members.values()}),reconciliation=counts,local_member_path=str(target),local_member_sha256=sha(target))
   snapshots.append({'quarter':q,'effective_session':result['effective_session'],'members':mapped})
  except Exception as e:result.update(state='source_member_parse_failed_retained',error_type=type(e).__name__,error=str(e)[:180])
  results.append(result);print(json.dumps({'quarter':q,'state':result['state'],'rows':result.get('rows'),'error':result.get('error')},ensure_ascii=False),flush=True)
 check(s)
 for name in ['trading_dates.npy','instruments.pk']:
  if sha(raw/name)!=expected[name]:raise ValueError('source metadata changed after parse')
 manifest=local/'membership_packet.json'
 # Include failed dependencies, read-only copies, helper receipts and logs too.
 all_outputs=[p for p in local.iterdir() if p.is_file()]
 write(manifest,{'quarter_results':results,'native_outputs':{str(p):sha(p) for p in all_outputs},'xlrd_dependency':dep})
 summary={'schema':'regis.csrc.industry_member_result.v1','state':'finite_original_membership_parse_attempted','started_utc':started,'ended_utc':utc(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'core_sha256':sha(ROOT/'research/csrc_industry_members.py'),'quarter_results':results,'parsed_quarters':sum(x['state']=='source_members_parsed_not_PIT_admitted' for x in results),'failed_quarters':sum(x['state']!='source_members_parsed_not_PIT_admitted' for x in results),'rows_in_parsed_source_snapshots':sum(x.get('rows',0) for x in results),'local_details':{'path':str(manifest),'sha256':sha(manifest),'native_directory':str(local),'contains_quote_arrays':False},'source_calendar_and_identities_unchanged':True,'input_metadata_vintage_certified':False,'source_snapshots_publication_lag_respected':True,'PIT_historical_byte_vintage_certified':False,'sector_factor_computed':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False,'limits':s['limits']}
 write(RESULT,summary);print(json.dumps({k:summary[k] for k in ['state','elapsed_seconds','parsed_quarters','failed_quarters','rows_in_parsed_source_snapshots','sector_factor_computed','model_admitted']},ensure_ascii=False))
def verify(s):
 check(s);r=read(RESULT)
 if r['state']!='finite_original_membership_parse_attempted' or r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or r['core_sha256']!=sha(ROOT/'research/csrc_industry_members.py') or sha(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('member evidence changed')
 d=read(r['local_details']['path'])
 for p,h in d['native_outputs'].items():
  if sha(p)!=h:raise ValueError('member native derivative changed')
 dep=d['xlrd_dependency']
 if dep.get('path') and (sha(dep['path'])!=dep['sha256'] or sha(dep['receipt'])!=dep['receipt_sha256']):raise ValueError('pinned dependency changed')
 if any(r[k] for k in ['PIT_historical_byte_vintage_certified','sector_factor_computed','economic_runs','model_admitted','full_goal_complete']):raise ValueError('member scope escalated')
 print(json.dumps({'original_member_evidence_verified':True,'parsed_quarters':r['parsed_quarters'],'failed_quarters':r['failed_quarters'],'economic_runs':0}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--run',action='store_true');ap.add_argument('--verify',action='store_true');a=ap.parse_args();s=read(PROTOCOL)
 if a.run==a.verify:raise SystemExit('Select one mode')
 run(s) if a.run else verify(s)
