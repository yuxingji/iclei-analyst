import argparse
from pathlib import Path
from . import core
from .core import connect,inventory
from .stages import text_stage,rules_stage
from .llm import llm_stage
from .output import validate_stage,eval_stage,export_stage

def main():
    p=argparse.ArgumentParser(prog='python -m cap_extract'); p.add_argument('stage',choices=['inventory','text','rules','llm','validate','eval','export','estimate']); p.add_argument('--limit',type=int); p.add_argument('--only',nargs='+'); p.add_argument('--force',action='store_true'); p.add_argument('--provider',choices=['ollama','deepseek'],default='ollama'); p.add_argument('--model'); p.add_argument('--top-k',type=int,default=25); p.add_argument('--max-calls-per-plan',type=int,default=15); p.add_argument('--num-ctx',type=int,default=12288); p.add_argument('--output-reserve',type=int,default=2048); p.add_argument('--max-tokens',type=int,default=8192); p.add_argument('--effort',choices=['none','low','high','max'],default='none'); p.add_argument('--tasks',nargs='+',choices=['plan','risk','action']); p.add_argument('--temperature',type=float,default=0); p.add_argument('--seed',type=int,default=42); p.add_argument('--concurrency',type=int,default=8); p.add_argument('--offpeak-discount',type=float,default=.5); p.add_argument('--budget-usd',type=float,default=10); p.add_argument('--pdf-dir',type=Path); p.add_argument('--atlas',type=Path); p.add_argument('--template',type=Path); p.add_argument('--output-dir',type=Path); a=p.parse_args()
    if a.pdf_dir: core.PDF_DIR=a.pdf_dir
    if a.atlas: core.ATLAS=a.atlas
    if a.template: core.TEMPLATE=a.template
    if a.output_dir: core.OUT=a.output_dir; core.DB=core.OUT/'cap.sqlite'
    c=connect()
    model=a.model or ('deepseek-flash' if a.provider=='deepseek' else 'qwen2.5:7b-instruct')
    funcs={'inventory':lambda:inventory(c,a.force,a.limit,a.only),'text':lambda:text_stage(c,a.force,a.limit,a.only),'rules':lambda:rules_stage(c,a.force,a.limit,a.only),'llm':lambda:llm_stage(c,model,a.top_k,a.force,a.limit,a.only,a.num_ctx,a.temperature,a.seed,a.max_calls_per_plan,a.provider,a.max_tokens,a.budget_usd,a.offpeak_discount,a.concurrency,a.output_reserve,a.effort,a.tasks),'validate':lambda:validate_stage(c,a.force,a.limit,a.only),'eval':lambda:eval_stage(c),'export':lambda:export_stage(c,model,a.only),'estimate':lambda:__import__('cap_extract.estimate',fromlist=['estimate']).estimate(c,a.provider,a.model or 'deepseek-flash',a.top_k,a.max_calls_per_plan)}
    result=funcs[a.stage](); print(f'{a.stage} complete: {result}')

if __name__=='__main__': main()
