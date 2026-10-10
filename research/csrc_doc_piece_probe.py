"""Bounded MS-DOC main-text probe; never starts Office or assigns industry classes."""
import argparse,hashlib,json,re,struct,subprocess,sys,time,urllib.request,zipfile
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/CSRC_DOC_PIECE_PROTOCOL_20261003.json'
RESULT=ROOT/'research/CSRC_DOC_PIECE_RESULT_20261003.json'
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def check(s):
 for p,h in s['input_hashes'].items():
  if sha(ROOT/p)!=h:raise ValueError('frozen piece probe input changed')
 for p,h in s['native_input_hashes'].items():
  if sha(p)!=h:raise ValueError('native first source changed')
def fetch_one(url,path,limit):
 req=urllib.request.Request(url,headers={'User-Agent':'RegisOracle-source-probe/1.0'})
 with urllib.request.urlopen(req,timeout=12) as response:
  if response.status!=200:raise ValueError('dependency HTTP status')
  b=response.read(limit+1)
  if len(b)>limit:raise ValueError('bounded dependency size')
  Path(path).write_bytes(b)
 return len(b)
def fetch(url,path,limit):
 p=subprocess.run([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--fetch-one',url,str(path),str(limit)],capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
 if p.returncode:raise ValueError('bounded dependency child fetch failed')
 return Path(path).stat().st_size
def u16(b,p):return struct.unpack_from('<H',b,p)[0]
def u32(b,p):return struct.unpack_from('<I',b,p)[0]
def extract(word,table):
 if len(word)>16*1024*1024 or len(table)>16*1024*1024:raise ValueError('stream ceiling')
 if len(word)<426 or u16(word,0)!=0xA5EC:raise ValueError('not supported MS-DOC FIB')
 base_version=u16(word,2);flags=u16(word,10);sizes={0xC1:0x5D,0xD9:0x6C,0x101:0x88,0x10C:0xA4,0x112:0xB7};pairs=u16(word,152)
 if pairs not in sizes.values():raise ValueError('unsupported FC/LCB pair count')
 new_at=154+pairs*8
 if new_at+2>len(word):raise ValueError('truncated FIB new-version count')
 new_count=u16(word,new_at)
 if new_count not in (0,2,5) or new_at+2+2*new_count>len(word):raise ValueError('FIB new-version bounds')
 n=u16(word,new_at+2) if new_count else base_version
 expected_new={0xC1:0,0xD9:2,0x101:2,0x10C:2,0x112:5}
 if flags&0x100:raise ValueError('encrypted or obfuscated source excluded')
 if n not in sizes or u16(word,32)!=14 or u16(word,62)!=22 or pairs!=sizes[n] or new_count!=expected_new[n]:raise ValueError('unsupported FIB counts/version')
 cbmac=u32(word,64);main=u32(word,76)
 if not 1<=main<=1_000_000 or not 426<=cbmac<=len(word):raise ValueError('main document/meaningful stream bounds')
 fc,lcb=u32(word,418),u32(word,422)
 if lcb<=0 or fc+lcb>len(table):raise ValueError('CLX exceeds table stream')
 clx=table[fc:fc+lcb];pos=0;prcs=0
 while pos<len(clx) and clx[pos]==1:
  if pos+3>len(clx):raise ValueError('truncated Prc')
  length=struct.unpack_from('<h',clx,pos+1)[0]
  if not 0<=length<=0x3FA2:raise ValueError('signed PrcData length bounds')
  pos+=3+length;prcs+=1
  if pos>len(clx) or prcs>4096:raise ValueError('Prc bounds')
 if pos+5>len(clx) or clx[pos]!=2:raise ValueError('Pcdt marker absent')
 size=u32(clx,pos+1);start=pos+5
 if size<4 or (size-4)%12 or start+size!=len(clx):raise ValueError('PlcPcd count/end bounds')
 count=(size-4)//12
 if not 1<=count<=4096:raise ValueError('piece count ceiling')
 plc=clx[start:];cps=[u32(plc,4*i) for i in range(count+1)];pcd_start=4*(count+1)
 parts=[u32(word,64+4*i) for i in (4,5,7,8,9,10)]
 expected=main+sum(parts)+(1 if any(parts) else 0)
 if cps[0]!=0 or cps[-1]!=expected or any(b<=a for a,b in zip(cps,cps[1:])):raise ValueError('ordered/complete CP census failed')
 # The specification maps these compressed 8-bit Unicode values explicitly.
 special={0x82:0x201A,0x83:0x0192,0x84:0x201E,0x85:0x2026,0x86:0x2020,0x87:0x2021,0x88:0x02C6,0x89:0x2030,0x8A:0x0160,0x8B:0x2039,0x8C:0x0152,0x91:0x2018,0x92:0x2019,0x93:0x201C,0x94:0x201D,0x95:0x2022,0x96:0x2013,0x97:0x2014,0x98:0x02DC,0x99:0x2122,0x9A:0x0161,0x9B:0x203A,0x9C:0x0153,0x9F:0x0178}
 main_parts=[];pieces=[]
 for i in range(count):
  raw=u32(plc,pcd_start+i*8+2);compressed=bool(raw&0x40000000);offset=raw&0x3FFFFFFF
  if raw&0x80000000:raise ValueError('reserved FC bit nonzero')
  if compressed:
   if offset%2:raise ValueError('compressed FC not exactly divisible')
   offset//=2
  width=1 if compressed else 2;length=cps[i+1]-cps[i]
  if offset+width*length>cbmac:raise ValueError('piece extends outside meaningful WordDocument')
  main_length=max(0,min(cps[i+1],main)-cps[i])
  payload=word[offset:offset+width*main_length]
  # Decode UTF-16 only after joining pieces so an astral pair may cross a piece.
  decoded=b''.join(chr(special.get(x,x)).encode('utf-16-le') for x in payload) if compressed else payload
  main_parts.append(decoded);pieces.append({'cp_start':cps[i],'cp_end':cps[i+1],'stream_offset':offset,'compressed':compressed,'main_code_units':main_length})
 units=b''.join(main_parts)
 if len(units)!=main*2:raise ValueError('main code unit reconstruction failed')
 text=units.decode('utf-16-le',errors='strict')
 if not text.endswith('\r'):raise ValueError('main document does not end with paragraph mark')
 return text,{'nFib':n,'FibBase_nFib':base_version,'cswNew':new_count,'table_stream':'1Table' if flags&0x200 else '0Table','cbMac':cbmac,'main_UTF16_code_units':main,'piece_count':count,'compressed_pieces':sum(x['compressed'] for x in pieces),'Prc_count':prcs,'subdocument_code_units':parts,'pieces':pieces,'FIB_fcClx_pair_index_zero_based':33}
def run(s):
 check(s)
 if read(RESULT)['state']!='not_run':raise ValueError('preserve first nonCOM piece probe')
 local=Path(s['native_directory']);assert not local.exists() and 'onedrive' not in str(local).lower();local.mkdir(parents=True);start=time.monotonic();attempts=[];state='nonCOM_piece_probe_failed';facts={}
 try:
  metadata=local/'olefile-0.47-pypi.json';attempts.append({'url':s['pypi_metadata_url'],'kind':'dependency_metadata','state':'attempted'});attempts[-1]['bytes']=fetch(s['pypi_metadata_url'],metadata,256*1024);attempts[-1]['state']='captured'
  package=read(metadata);wheels=[x for x in package['urls'] if x['filename']==s['wheel_filename']]
  if len(wheels)!=1 or wheels[0]['digests']['sha256']!=s['expected_wheel_sha256'] or not wheels[0]['url'].startswith('https://files.pythonhosted.org/'):raise ValueError('pinned official wheel metadata changed')
  wheel=local/s['wheel_filename'];attempts.append({'url':wheels[0]['url'],'kind':'dependency_wheel','state':'attempted'});attempts[-1]['bytes']=fetch(wheels[0]['url'],wheel,128*1024);attempts[-1]['state']='captured'
  if sha(wheel)!=s['expected_wheel_sha256']:raise ValueError('pinned wheel hash differs')
  with zipfile.ZipFile(wheel) as z:
   if z.testzip() is not None or len(z.namelist())>64:raise ValueError('wheel archive bounds/CRC')
   licences=[n for n in z.namelist() if n.endswith('/LICENSE.txt') or n.endswith('/LICENSE')]
   if len(licences)!=1:raise ValueError('single preserved dependency licence required')
   licence=z.read(licences[0]);(local/'olefile-LICENSE.txt').write_bytes(licence)
  sys.path.insert(0,str(wheel));import olefile
  if olefile.__version__!='0.47':raise ValueError('pinned dependency version')
  with olefile.OleFileIO(s['native_source'],raise_defects=olefile.DEFECT_INCORRECT,write_mode=False) as ole:
   word=ole.openstream('WordDocument').read();flags=u16(word,10);table_name='1Table' if flags&0x200 else '0Table';table=ole.openstream(table_name).read();issues=[{'exception_class':x[0].__name__,'message':str(x[1])[:120]} for x in ole.parsing_issues]
  text,structural=extract(word,table);(local/'2012Q4.main-text.txt').write_text(text,encoding='utf-8',newline='')
  write(local/'2012Q4.piece-locators.json',structural)
  tokens=re.findall(r'(?<!\d)\d{6}(?!\d)',text);controls=Counter(ord(c) for c in text if ord(c)<32)
  facts={'main_text_recovered':True,'six_digit_token_occurrences':len(tokens),'unique_six_digit_tokens':len(set(tokens)),'duplicate_token_occurrences':len(tokens)-len(set(tokens)),'control_character_counts':{str(k):v for k,v in sorted(controls.items())},'main_document_year_title_present':bool(re.search('2012.{0,20}(?:四季度|4季度|第4季度|第四季度)',text,re.S)),'FIB_structure':{k:v for k,v in structural.items() if k!='pieces'},'ole_parse_issues':issues,'olefile_version':olefile.__version__}
  if time.monotonic()-start>s['max_seconds']:raise TimeoutError('nonCOM probe elapsed ceiling')
  state='actual_nonCOM_main_text_recovered_no_class_members_activated'
 except Exception as error:facts.update(error_type=type(error).__name__,error=str(error)[:180])
 check(s);manifest=local/'piece_probe_manifest.json';write(manifest,{'files':{str(p):sha(p) for p in local.iterdir() if p.is_file()}})
 r={'schema':'regis.csrc.doc_piece_probe.v1','state':state,'observed_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-start,3),'protocol_sha256':sha(PROTOCOL),'implementation_sha256':sha(__file__),'attempts':attempts,'source_sha256':sha(s['native_source']),'facts':facts,'local_details':{'path':str(manifest),'sha256':sha(manifest)},'Office_COM_started':False,'source_snapshot_replaced':False,'industry_member_classes_activated':0,'price_arrays_read':False,'economic_runs':0,'model_admitted':False,'full_goal_complete':False,'limits':s['limits']};write(RESULT,r)
 print(json.dumps({'state':state,'elapsed_seconds':r['elapsed_seconds'],'six_digit_token_occurrences':facts.get('six_digit_token_occurrences'),'unique_six_digit_tokens':facts.get('unique_six_digit_tokens'),'error_type':facts.get('error_type'),'class_members_activated':0}))
def verify(s):
 check(s);r=read(RESULT)
 if r['state']=='not_run' or r['protocol_sha256']!=sha(PROTOCOL) or r['implementation_sha256']!=sha(__file__) or sha(r['local_details']['path'])!=r['local_details']['sha256']:raise ValueError('piece probe evidence changed')
 for p,h in read(r['local_details']['path'])['files'].items():
  if sha(p)!=h:raise ValueError('first nonCOM native evidence changed')
 if r['state']=='actual_nonCOM_main_text_recovered_no_class_members_activated':
  local=Path(r['local_details']['path']).parent;wheel=local/s['wheel_filename']
  if sha(wheel)!=s['expected_wheel_sha256']:raise ValueError('native dependency identity')
  sys.path.insert(0,str(wheel));import olefile
  with olefile.OleFileIO(s['native_source'],raise_defects=olefile.DEFECT_INCORRECT,write_mode=False) as ole:
   word=ole.openstream('WordDocument').read();table=ole.openstream('1Table' if u16(word,10)&0x200 else '0Table').read()
  text,structure=extract(word,table)
  with (local/'2012Q4.main-text.txt').open(encoding='utf-8',newline='') as f:retained=f.read()
  if text!=retained or structure!=read(local/'2012Q4.piece-locators.json'):raise ValueError('source byte piece reconstruction differs')
  tokens=re.findall(r'(?<!\d)\d{6}(?!\d)',text)
  if len(tokens)!=r['facts']['six_digit_token_occurrences'] or len(set(tokens))!=r['facts']['unique_six_digit_tokens']:raise ValueError('native token census replay failed')
 if any(r[k] for k in ['Office_COM_started','source_snapshot_replaced','industry_member_classes_activated','price_arrays_read','economic_runs','model_admitted','full_goal_complete']):raise ValueError('piece probe scope escalated')
 print(json.dumps({'first_nonCOM_probe_evidence_verified':True,'source_snapshot_still_unknown':True,'economic_runs':0}))
if __name__=='__main__':
 if len(sys.argv)==5 and sys.argv[1]=='--fetch-one':
  fetch_one(sys.argv[2],Path(sys.argv[3]),int(sys.argv[4]));raise SystemExit(0)
 ap=argparse.ArgumentParser();ap.add_argument('--run',action='store_true');ap.add_argument('--verify',action='store_true');a=ap.parse_args();s=read(PROTOCOL)
 if a.run==a.verify:raise SystemExit('Select one mode')
 run(s) if a.run else verify(s)
