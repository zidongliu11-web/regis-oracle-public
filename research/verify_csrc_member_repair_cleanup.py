"""Verify E30 capture unchanged, and the separately observed owned-process cleanup."""
import hashlib,json,subprocess,sys
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def stamp(v):return datetime.fromisoformat(v.replace('Z','+00:00'))
def main():
 p=subprocess.run([sys.executable,'-X','utf8',str(ROOT/'research/repair_csrc_source_members.py'),'--verify'],cwd=ROOT,capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
 if p.returncode:raise ValueError('original capture verification failed')
 r=read(ROOT/'research/CSRC_MEMBER_REPAIR_RESULT_20261003.json');probe=r['post_run_cleanup_probe'];path=Path(probe['native_path'])
 if hashlib.sha256(path.read_bytes()).hexdigest()!=probe['native_sha256']:raise ValueError('post-run cleanup original changed')
 e=read(path);b=e['basis']
 if e['cleanup']!='exact_owned_instance_terminated' or probe['cleanup']!=e['cleanup'] or not probe['actual_PID_absent_after'] or not e['ownership_reconstructed'] or not probe['reconstructed_ownership']:raise ValueError('cleanup conclusion mismatch')
 if b['helper_before_word_count']!=0 or not b['cim_exact_installed_word_executable'] or not b['cim_com_automation_command_line'] or not b['source_document_open_never_reached']:raise ValueError('process reconstruction insufficient')
 if not stamp(b['helper_start_utc'])<=stamp(e['owned_start_utc'])<=stamp(b['helper_end_utc']):raise ValueError('creation outside own helper')
 if not e['window_visibility_only'] or any(x['visible'] for x in e['window_visibility_only']) or not e['no_titles_or_documents_read']:raise ValueError('hidden process check not preserved')
 original=next(x for x in r['specific_quarters'] if x['quarter']=='2012Q4')['diagnostic']
 if original['cleanup']!='no_owned_process' or original['state']!='failed' or original['before_word_count']!=0:raise ValueError('first failed receipt rewritten')
 print(json.dumps({'original_capture_verified':True,'separate_cleanup_native_SHA_verified':True,'first_failed_receipt_preserved':True,'no_process_action_in_verifier':True,'economic_runs':0}))
if __name__=='__main__':main()
