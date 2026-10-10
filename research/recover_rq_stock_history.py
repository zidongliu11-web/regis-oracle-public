"""Recover missing stock/identity/state originals for local research; no SDK execution."""
import argparse,json,os,shutil,time
from pathlib import Path
import inspect_short_rq_full as base

ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=ROOT/'research/RQ_STOCK_HISTORY_RECOVERY_PROTOCOL_20261002.json'
RECEIPT=ROOT/'research/RQ_STOCK_HISTORY_RECOVERY_RECEIPT_20261002.json'

def finish(report,local,budget):
    archive=local/'bundle.tar.bz2'
    try:
        if archive.exists() or archive.is_symlink():
            if archive.is_symlink() or archive.resolve().parent!=local:raise ValueError('unsafe archive cleanup')
            archive.unlink()
        report['temporary_archive_removed']=not archive.exists() and not archive.is_symlink()
    except Exception as e:
        report['state']='failed';report['temporary_archive_removed']=False
        report['cleanup_failure']={'type':type(e).__name__,'reason':'temporary archive retained; cleanup failed'}
    finally:
        report['ended_utc']=base.utc();report['resources']['elapsed_seconds']=round(time.monotonic()-budget.started,3)
        RECEIPT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')

def capture(spec):
    if json.loads(RECEIPT.read_text(encoding='utf-8'))['state']!='not_run':raise ValueError('already attempted; verify retained inputs instead')
    local=Path(spec['retained_directory']).resolve()
    allowed=(Path(os.environ['LOCALAPPDATA'])/'RegisOracle-local-research').resolve()
    if not local.is_relative_to(allowed) or 'onedrive' in str(local).lower() or local.exists():raise ValueError('invalid/existing acquisition directory')
    for p,h in spec['source_hashes'].items():
        if base.digest(ROOT/p)!=h:raise ValueError('frozen source changed')
    if shutil.disk_usage(allowed).free<spec['limits']['minimum_free_disk_bytes']:raise ValueError('disk headroom')
    local.mkdir(parents=True,exist_ok=False)
    report={'schema':'regis.rq.stock_recovery.v1','state':'running','started_utc':base.utc(),
      'protocol_sha256':base.digest(PROTOCOL),'implementation_sha256':base.digest(__file__),
      'base_reader_sha256':base.digest(base.__file__),'GET_invocations':0,'downloaded_bytes':0,
      'raw_directory':str(local),'economic_run':False,'model_admitted':False,
      'capture_contract_sha256':json.loads((ROOT/'WORKFLOW_TASK.json').read_text(encoding='utf-8'))['contract_sha256'],
      'resources':{'elapsed_seconds':0,'peak_rss_bytes':0,'decompressed_bytes':0}}
    RECEIPT.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    budget=base.Budget(spec,report)
    try:
        archive=base.download(spec,report,local,budget)
        if report['compressed_sha256']!=spec['expected_compressed_sha256']:raise ValueError('archive identity changed')
        base.extract(spec,report,archive,local,budget)
        for item in report['members']:
            if item['selected'] and item['name'] in spec['expected_member_sha256'] and item['sha256']!=spec['expected_member_sha256'][item['name']]:raise ValueError('previous observed member changed')
        report['retained']=[{'name':p.name,'bytes':p.stat().st_size,'sha256':base.digest(p)} for p in sorted(local.iterdir()) if p.name in spec['selected_members']]
        report['state']='captured';report['identity_or_data_admitted']=False
    except Exception as e:
        report['state']='failed';report['failure']={'type':type(e).__name__,'reason':str(e)}
    finally:finish(report,local,budget)
    print(json.dumps({'state':report['state'],'members_retained':len(report.get('retained',[])),
      'downloaded_bytes':report['downloaded_bytes'],'failure':report.get('failure'),'economic_run':False,'raw_quotes_printed':False}))
    if report['state']!='captured':raise SystemExit(1)

def verify(spec):
    r=json.loads(RECEIPT.read_text(encoding='utf-8'))
    if r['state']!='captured' or r['protocol_sha256']!=base.digest(PROTOCOL) or r['implementation_sha256']!=base.digest(__file__) or r['base_reader_sha256']!=base.digest(base.__file__):raise ValueError('source/capture identity changed')
    if r['economic_run'] or r['model_admitted'] or not r['temporary_archive_removed'] or r['GET_invocations']!=1 or not r['archive_complete']:raise ValueError('scope or completeness')
    if {i['name'] for i in r['retained']}!=set(spec['selected_members']):raise ValueError('retained members differ')
    for i in r['retained']:
        p=Path(r['raw_directory'])/i['name']
        if p.is_symlink() or p.stat().st_size!=i['bytes'] or base.digest(p)!=i['sha256']:raise ValueError('retained identity changed')
    print(json.dumps({'verified_retained_members':len(r['retained']),'downloaded_bytes':r['downloaded_bytes'],'raw_quotes_printed':False,'economic_run':False}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--capture',action='store_true');mode.add_argument('--verify',action='store_true');args=parser.parse_args()
    spec=json.loads(PROTOCOL.read_text(encoding='utf-8'));capture(spec) if args.capture else verify(spec)
