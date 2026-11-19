"""Execute the approved corpus, checkpoint it, then export full or partial results."""
from __future__ import annotations
import argparse,json,os,subprocess
import msvcrt
from pathlib import Path
from .cdp_consolidated_prepare import OUT
from .cdp_full import load_or_create_manifest
from .cdp_consolidated_run import run
from .cdp_consolidated_export import prepare_export

def run_locked():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--budget-usd',type=float,required=True)
    parser.add_argument('--max-calls-per-plan',type=int,default=150)
    parser.add_argument('--concurrency',type=int,default=8)
    parser.add_argument('--reuse-run',default='7e7d720c2f4284018eaf5884')
    args=parser.parse_args()
    manifest=load_or_create_manifest()
    cards=json.loads((OUT/'consolidated_question_cards_prepared.json').read_text(encoding='utf8'))['cards']
    keywords=json.loads((OUT/'keywords_draft.json').read_text(encoding='utf8'))
    print(f'Full corpus: {len(manifest)} indexed PDFs; approved pilot is reused; budget USD {args.budget_usd}',flush=True)
    result=run(manifest,cards,keywords,args.budget_usd,args.max_calls_per_plan,'deepseek-flash','none',
               args.concurrency,True,args.reuse_run)
    prefix='CDP_consolidated_full' if result['status']=='finished' else 'CDP_consolidated_full_PARTIAL'
    json_name=prefix+'_export.json'
    print('Preparing output: '+prefix,flush=True)
    prepare_export(result['run_id'],prefix,json_name,full=True)
    home=Path(os.environ['USERPROFILE'])
    node=home/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
    marker=home/'.codex/plugins/cache/openai-primary-runtime/spreadsheets/26.1026.10807/skills/spreadsheets/container_tools/mark_artifact_operation_started.mjs'
    subprocess.run([str(node),str(marker),'--operation-kind','create','--expected-output-count','1','--output-format','xlsx'],check=True)
    env=dict(os.environ,CDP_PILOT_JSON=str(OUT/json_name),CDP_PILOT_XLSX=str(OUT/(prefix+'.xlsx')))
    subprocess.run([str(node),str(Path(__file__).resolve().parent.parent/'cdp_tools/build_consolidated_pilot.mjs')],env=env,check=True)
    (OUT/'consolidated_full_summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print('Run and export complete: '+json.dumps(result,ensure_ascii=False),flush=True)

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    with (OUT/'consolidated_full.lock').open('a+b') as lock:
        lock.seek(0,os.SEEK_END)
        if lock.tell()==0:lock.write(b'0');lock.flush()
        lock.seek(0)
        try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:raise RuntimeError('Another full CDP job already holds the lock')
        try:run_locked()
        finally:
            lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)

if __name__=='__main__':main()
