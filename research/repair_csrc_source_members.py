import argparse,json,hashlib,time,subprocess,sys,xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime,timezone
from csrc_word_process import cleanup_owned_word
from csrc_industry_members import parse_cell_tables,flat_word_tables,parse_pdf_texts,STOCK
ROOT=Path(__file__).resolve().parents[1];PROTOCOL=ROOT/'research/CSRC_MEMBER_REPAIR_PROTOCOL_20261003.json';RESULT=ROOT/'research/CSRC_MEMBER_REPAIR_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def utc():return datetime.now(timezone.utc).isoformat()
def check(s):
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('frozen repair input changed')
 for p,h in s['native_input_hashes'].items():
  if sha(p)!=h:raise ValueError('original source member attempt or page evidence changed')
def run(s):
 check(s)
 if read(RESULT)['state']!='not_run':raise ValueError('preserve source repair attempt')
 local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir(parents=True);start=time.monotonic();started=utc();out=[]
 # Exactly identified publisher duplicate is quarantined, never auto corrected.
 texts=Path(s['2017_transcript']).read_text(encoding='utf-8').split('\n\f\n');filtered=[];quarantine=[];line_maps=[]
 for page,text in enumerate(texts,1):
  keep=[];original_lines=[]
  for line_no,line in enumerate(text.splitlines(),1):
   if s['quarantined_code'] in STOCK.findall(line):quarantine.append({'page':page,'line':line_no,'source_line':line})
   else:keep.append(line);original_lines.append(line_no)
  filtered.append('\n'.join(keep));line_maps.append(original_lines)
 if [(x['page'],x['line']) for x in quarantine]!=[(14,43),(14,44)]:raise ValueError('duplicate source positions changed')
 members,counts=parse_pdf_texts(filtered);counts['quarantined_original_rows']=len(quarantine)
 for row in members.values():
  locator=row['source_locator'];locator['line']=line_maps[locator['page']-1][locator['line']-1]
 if len(members)+len(quarantine)!=len(STOCK.findall('\n'.join(texts))):raise ValueError('quarantine token count mismatch')
 p=local/'2017Q1.quarantine_members.json';write(p,{'quarter':'2017Q1','members':members,'quarantined_original_rows':quarantine,'quarantined_source_code':s['quarantined_code'],'unresolved_company_code_from_wrong_name':True,'actual_corrected_stock_code':None,'original_source_unchanged':True,'not_complete_classification':True});out.append({'quarter':'2017Q1','state':'source_members_parsed_with_specific_conflict_quarantined','known_source_rows':len(members),'quarantined_rows':2,'classification_of603026_unknown':True,'no_guess603626':True,'source_major_groups':len({r['source_industry_major_numeric'] for r in members.values()}),'reconciliation':counts,'native_path':str(p),'native_sha256':sha(p)})
 # The first attempt failed before any Office process; use standard native APIs.
 receipt=local/'2012Q4.word.receipt.json';copy=local/'2012Q4.byte_identical_input.doc';xml=local/'2012Q4.readonly_WordOpenXML.xml';copy.write_bytes(Path(s['2012_original']).read_bytes());assert sha(copy)==sha(s['2012_original'])
 command=['powershell.exe','-NoProfile','-NonInteractive','-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File',str(ROOT/'research/read_legacy_csrc_word.ps1'),'-InputFile',str(copy),'-OutputFile',str(xml),'-ReceiptFile',str(receipt)]
 timeout=False
 try:p=subprocess.run(command,capture_output=True,timeout=40,creationflags=subprocess.CREATE_NO_WINDOW)
 except subprocess.TimeoutExpired:timeout=True;p=None
 meta=read(receipt) if receipt.exists() else {'state':'no_receipt'};meta.update(parent_timed_out=timeout,helper_returncode=None if p is None else p.returncode,cleanup=cleanup_owned_word(meta),original_source_unchanged=sha(copy)==sha(s['2012_original']));write(receipt,meta)
 if timeout or p.returncode or meta['state']!='derived_readonly_XML' or not xml.exists():out.append({'quarter':'2012Q4','state':'legacy_readonly_original_still_failed','diagnostic':meta})
 else:
  try:
   root=ET.fromstring(xml.read_text(encoding='utf-8'));alltext=''.join(n.text or '' for n in root.iter() if n.tag.endswith('}t'))
   if '2012年4季度' not in alltext.replace(' ',''):raise ValueError('Word original quarter title mismatch')
   members,counts=parse_cell_tables(flat_word_tables(xml.read_text(encoding='utf-8')));target=local/'2012Q4.members.json';write(target,{'quarter':'2012Q4','members':members,'original_source_sha256':sha(s['2012_original']),'derived_XML_sha256':sha(xml),'historic_byte_vintage_certified':False});out.append({'quarter':'2012Q4','state':'legacy_Word_source_members_parsed_readonly','source_rows':len(members),'source_major_groups':len({r['source_industry_major_numeric'] for r in members.values()}),'reconciliation':counts,'native_path':str(target),'native_sha256':sha(target),'Word_readonly_diagnostic':meta})
  except Exception as error:out.append({'quarter':'2012Q4','state':'legacy_readonly_XML_semantics_failed','error_type':type(error).__name__,'error':str(error)[:180],'Word_readonly_diagnostic':meta})
 for quarter in out:quarter.update(s['source_availability'][quarter['quarter']])
 check(s);manifest=local/'repair_native_manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.iterdir() if p.is_file() and not p.name.startswith('~$')}})
 result={'schema':'regis.csrc.member_repair.v1','state':'specific_source_member_repair_attempted','started_utc':started,'ended_utc':utc(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'native_cleanup_sha256':sha(ROOT/'research/csrc_word_process.py'),'specific_quarters':out,'local_details':{'path':str(manifest),'sha256':sha(manifest),'contains_quote_arrays':False},'original_sources_and_first_member_attempt_unchanged':True,'new_GETs':0,'price_arrays_read':False,'PIT_full_member_semantics_certified':False,'sector_factor_computed':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False,'limits':s['limits']};write(RESULT,result);print(json.dumps({'state':result['state'],'seconds':result['elapsed_seconds'],'specific_results':out,'sector_factor_computed':False},ensure_ascii=False))
def verify(s):
 check(s);r=read(RESULT)
 if r['state']!='specific_source_member_repair_attempted' or r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or r['native_cleanup_sha256']!=sha(ROOT/'research/csrc_word_process.py') or sha(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('repair evidence changed')
 for p,h in read(r['local_details']['path'])['files'].items():
  if sha(p)!=h:raise ValueError('native repair output changed')
 if any(r[k] for k in ['price_arrays_read','PIT_full_member_semantics_certified','sector_factor_computed','economic_runs','model_admitted','full_goal_complete']):raise ValueError('repair scope escalated')
 print(json.dumps({'specific_source_repair_verified':True,'original_first_attempt_unchanged':True,'economic_runs':0}))
if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--run',action='store_true');ap.add_argument('--verify',action='store_true');a=ap.parse_args();s=read(PROTOCOL)
 if a.run==a.verify:raise SystemExit('Select one mode')
 run(s) if a.run else verify(s)
