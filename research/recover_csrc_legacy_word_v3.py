import argparse,hashlib,json,subprocess,time,xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime,timezone
from csrc_industry_members import flat_word_tables,parse_cell_tables
from csrc_word_process import cleanup_owned_word
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/CSRC_LEGACY_WORD_V3_PROTOCOL_20261003.json'
RESULT=ROOT/'research/CSRC_LEGACY_WORD_V3_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def utc():return datetime.now(timezone.utc).isoformat()
def check(s):
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('frozen Word input changed')
 for p,h in s['native_input_hashes'].items():
  if sha(p)!=h:raise ValueError('original or first attempt changed')
def run(s):
 check(s)
 if read(RESULT)['state']!='not_run':raise ValueError('preserve first v3 attempt')
 local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir(parents=True)
 start=time.monotonic();started=utc();copy=local/'2012Q4.byte_identical_input.doc';copy.write_bytes(Path(s['original']).read_bytes());assert sha(copy)==sha(s['original'])
 xml=local/'2012Q4.readonly_WordOpenXML.xml';receipt=local/'2012Q4.word_v3.receipt.json'
 cmd=['powershell.exe','-NoProfile','-NonInteractive','-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File',str(ROOT/'research/read_legacy_csrc_word_v3.ps1'),'-InputFile',str(copy),'-OutputFile',str(xml),'-ReceiptFile',str(receipt)]
 timeout=False
 try:p=subprocess.run(cmd,capture_output=True,timeout=40,creationflags=subprocess.CREATE_NO_WINDOW)
 except subprocess.TimeoutExpired:timeout=True;p=None
 meta=read(receipt) if receipt.exists() else {'state':'missing_helper_receipt'}
 meta.update(parent_timed_out=timeout,helper_returncode=p.returncode if p is not None else None,cleanup=cleanup_owned_word(meta),input_copy_sha256=sha(copy),original_unchanged=sha(copy)==sha(s['original']))
 if meta.get('owned_pid'):
  probe=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command','[bool](Get-Process -Id '+str(int(meta['owned_pid']))+' -ErrorAction SilentlyContinue)'],capture_output=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
  meta['actual_owned_PID_absent_after']=probe.returncode==0 and probe.stdout.strip()==b'False'
 write(receipt,meta)
 summary={'state':'legacy_Word_v3_read_failed_retained','diagnostic':meta}
 if not timeout and p is not None and p.returncode==0 and meta['state']=='derived_readonly_XML' and xml.exists():
  try:
   text=xml.read_text(encoding='utf-8');root=ET.fromstring(text);alltext=''.join(n.text or '' for n in root.iter() if n.tag.endswith('}t'))
   if '2012年4季度' not in alltext.replace(' ',''):raise ValueError('original quarter title mismatch')
   tables=flat_word_tables(text);members,counts=parse_cell_tables(tables);target=local/'2012Q4.members.json'
   write(target,{'quarter':'2012Q4','members':members,'original_source_sha256':sha(s['original']),'derived_XML_sha256':sha(xml),'historic_byte_vintage_certified':False})
   summary.update(state='legacy_Word_v3_members_parsed_readonly',source_rows=len(members),source_major_groups=len({r['source_industry_major_numeric'] for r in members.values()}),source_B_share_rows=sum(k.startswith(('2','9')) for k in members),reconciliation=counts,native_path=str(target),native_sha256=sha(target))
  except Exception as e:summary.update(state='legacy_Word_v3_XML_semantics_failed_retained',error_type=type(e).__name__,error=str(e)[:180])
 summary.update(s['source_availability']);check(s);manifest=local/'legacy_v3_manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.iterdir() if p.is_file() and not p.name.startswith('~$')}})
 r={'schema':'regis.csrc.legacy_word_result.v3','state':'specific_legacy_Word_v3_attempted','started_utc':started,'ended_utc':utc(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'helper_sha256':sha(ROOT/'research/read_legacy_csrc_word_v3.ps1'),'specific_result':summary,'local_details':{'path':str(manifest),'sha256':sha(manifest)},'first_E29_E30_E31v2_originals_and_failures_unchanged':True,'new_GETs':0,'price_arrays_read':False,'PIT_full_member_semantics_certified':False,'sector_factor_computed':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False,'limits':s['limits']}
 write(RESULT,r);print(json.dumps({'state':r['state'],'seconds':r['elapsed_seconds'],'specific_result':summary},ensure_ascii=False))
def verify(s):
 check(s);r=read(RESULT)
 if r['state']!='specific_legacy_Word_v3_attempted' or r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or r['helper_sha256']!=sha(ROOT/'research/read_legacy_csrc_word_v3.ps1') or sha(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('Word v3 evidence changed')
 for p,h in read(r['local_details']['path'])['files'].items():
  if sha(p)!=h:raise ValueError('Word v2 native derivative changed')
 if any(r[k] for k in ['price_arrays_read','PIT_full_member_semantics_certified','sector_factor_computed','economic_runs','model_admitted','full_goal_complete']):raise ValueError('Word scope escalated')
 print(json.dumps({'Word_v3_evidence_verified':True,'first_failures_unchanged':True,'economic_runs':0}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--run',action='store_true');ap.add_argument('--verify',action='store_true');a=ap.parse_args();s=read(PROTOCOL)
 if a.run==a.verify:raise SystemExit('Select one mode')
 run(s) if a.run else verify(s)
