from __future__ import annotations
import hashlib,json,re,time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from pydantic import BaseModel,constr,create_model,model_validator
from typing import Literal
from .core import *
from .stages import selected
from .providers import get_provider
from .providers import ProviderCallError, RequestBudget
from .text_utils import truncate_at_word_boundary,parse_truncated_json

MODEL_DEFAULT='qwen2.5:7b-instruct'

class CappedTextModel(BaseModel):
    @model_validator(mode='before')
    @classmethod
    def clip_capped_fields(cls, data):
        if isinstance(data,dict):
            data=dict(data)
            for field,limit in (('evidence_text',200),('action_description',500)):
                if isinstance(data.get(field),str): data[field]=truncate_at_word_boundary(data[field],limit)
        return data

def model_for(task,cols):
    config=schema_config()
    identity={'plan_id','risk_id','action_id','evidence_id','state','jurisdiction','gov_type','plan_year','plan_title','source_file'}
    required={'source_page','evidence_text'}
    if task=='plan': required.update({'plan_orientation_controlled','document_type_controlled','plan_scope','plan_formal_status','plan_implementation_status'})
    if task in {'risk','action'}: required.add('M_or_A')
    if task=='risk': required.update({'hazard_normalized','fiscal_consequence_disclosed'})
    if task=='action': required.update({'action_title','action_description'})
    fields={}
    for x in cols:
        if x in identity: continue
        allowed=config.get('fields',{}).get(x,{}).get('allowed')
        if task=='risk' and x=='hazard_normalized':
            import yaml
            allowed=list(yaml.safe_load((Path(__file__).parent/'dictionaries.yaml').read_text(encoding='utf-8')).get('hazards',{}))
        if isinstance(allowed,list): field_type=Literal.__getitem__(tuple(allowed))
        elif x=='evidence_text': field_type=constr(strip_whitespace=True,min_length=1,max_length=200)
        elif x=='action_description': field_type=constr(strip_whitespace=True,min_length=1,max_length=500)
        elif x=='action_title': field_type=constr(strip_whitespace=True,min_length=1)
        else: field_type=str
        fields[x]=(field_type,...) if x in required else (field_type|None,None)
    # These records are validated later against source pages and controlled values.
    item=create_model(task.title()+'Item',__base__=CappedTextModel,**fields)
    return create_model(task.title()+'Response',records=(list[item],[]))
def make_prompt(task,cols,dict_rows,controlled_rows,protocol_rows,schema,include_schema=True):
    config=schema_config()
    base=(Path(__file__).parent/'prompts'/f"{task}_{config['prompt_version']}.txt").read_text(encoding='utf-8')
    fields=[]
    for row in dict_rows[1:]:
        if len(row)>6 and row[2] in cols: fields.append(f'- {row[2]}: {row[4]}; allowed/format: {row[5]}; rule: {row[6]}')
    controls=[f'- {r[0]}: {r[1]} ({r[2] or ""})' for r in controlled_rows[1:] if len(r)>1 and str(r[0]) in cols]
    protocol=[f'- {r[1]} Rule: {r[2]}' for r in protocol_rows[1:] if len(r)>2 and not str(r[0]).startswith('Schema v2.2 ')]
    prompt=base+'\n'+config['prompts'].get(task,'')+'\nField guide:\n'+'\n'.join(fields)+'\nControlled values:\n'+'\n'.join(controls)+'\nProtocol:\n'+'\n'.join(protocol)+'\nReturn a valid json object with a records array. Include all required keys. Omit optional keys when the supplied text gives no value; do not use empty strings, nulls, or guesses for optional fields. Do not score plan quality. Cite only page numbers in the supplied [PAGE n] markers. Every evidence_text must be a verbatim quote no longer than 200 characters. A named funding source does not establish secured funding. municipal_fiscal_link is Direct only for an explicit municipal revenue, expenditure, asset, service, or capital link.'
    if task=='plan':
        prompt+=' Search every supplied page for plan-level governance and monitoring details, not only the cover. Extract named committees, task forces, departments, offices, boards, or other responsible bodies into governance_body_raw. Extract review cadence, annual progress reports, monitoring indicators, and recurring GHG inventories into monitoring_raw. Combine related details concisely and cite their supplied page markers.'
    if task=='risk':
        try:
            import yaml
            hazards=yaml.safe_load((Path(__file__).parent/'dictionaries.yaml').read_text(encoding='utf-8')).get('hazards',{})
            labels=', '.join(hazards)
        except Exception:
            labels='the controlled hazard labels in dictionaries.yaml'
        prompt+=" hazard_normalized must be exactly one of these controlled labels: "+labels+"."
    if task=='action':
        prompt+=' Do not create rows for background statements, inventories, accomplishments, or analysis unless the text states a substantive intervention, project, programme, or policy. Every action record MUST provide a concise, non-empty action_title and a non-empty action_description of at most 500 characters summarizing the action. Do not omit either required field; use the evidence quote to write the description.'
    return prompt+'\n\nJSON Schema:\n'+json.dumps(schema,ensure_ascii=False,sort_keys=True) if include_schema else prompt

def token_estimate(text):
    return (len(text)+2)//3

def task_configs(cols,dicts,max_tokens,ollama_num_ctx=12288,ollama_output_reserve=2048):
    configs={}
    for task,sheet in (('plan','Plan Level'),('risk','Risk Level'),('action','Action Level')):
        response_model=model_for(task,cols[sheet]); schema=response_model.model_json_schema()
        system=make_prompt(task,cols[sheet],dicts['Variable Dictionary'],dicts['Controlled Values'],dicts['Extraction Protocol'],schema,include_schema=False)
        system_tokens=token_estimate(system); schema_tokens=token_estimate(json.dumps(schema,ensure_ascii=False,sort_keys=True))
        available=ollama_num_ctx-system_tokens-schema_tokens-ollama_output_reserve-300
        deepseek_input=12000; deepseek_output=max_tokens
        deepseek_context=system_tokens+schema_tokens+deepseek_output+300+deepseek_input
        configs[task]={'response_model':response_model,'schema':schema,'system':system,'system_tokens':system_tokens,'schema_tokens':schema_tokens,'ollama_available':available,'deepseek_available':deepseek_input,'deepseek_context':deepseek_context,'prompt_hash':hashlib.sha256((system+'\n\nJSON Schema:\n'+json.dumps(schema,ensure_ascii=False,sort_keys=True)).encode('utf-8')).hexdigest()}
    return configs

def validate_ollama_budgets(configs,min_input=1500):
    too_small=[f"{task}={cfg['ollama_available']}" for task,cfg in configs.items() if cfg['ollama_available']<min_input]
    if too_small:
        raise ValueError(f"Ollama context configuration leaves fewer than {min_input} input tokens for task(s): {', '.join(too_small)}. Increase --num-ctx or reduce prompt/schema/output reserve.")

def _page_value(page,key,default=None):
    try:
        value=page[key]
    except (KeyError,IndexError):
        return default
    return default if value is None else value

def _table_bonus(text):
    lines=[line for line in str(text or '').splitlines() if line.strip()]
    structured=sum(bool(re.search(r'\||\t|\s{2,}',line) and re.search(r'\d|\||\t',line)) for line in lines)
    return min(12, structured*2) if structured>=2 else 0

def plan_page_score(page):
    text=str(_page_value(page,'text','') or '').casefold()
    gov_hits=sum(len(re.findall(pattern,text)) for pattern in (r'governance body',r'responsible (?:department|office|committee|agency)',r'climate (?:action )?committee',r'implementation',r'annual report',r'progress report',r'ghg inventory',r'\binventory\b',r'monitoring',r'every five years',r'every \d+ years'))
    return float(_page_value(page,'relevance',0) or 0)+min(30,gov_hits*5)+_table_bonus(text)

def governance_page_score(page):
    text=str(_page_value(page,'text','') or '').casefold()
    hits=sum(len(re.findall(pattern,text)) for pattern in (r'governance body',r'responsible (?:department|office|committee|agency)',r'climate (?:action )?committee',r'implementation',r'annual report',r'progress report',r'ghg inventory',r'\binventory\b',r'monitoring',r'every five years',r'every \d+ years'))
    return hits*5+_table_bonus(text)

def select_plan_pages(pages):
    first=list(pages[:3])
    best=max(pages,key=lambda p:(p['relevance'], -p['page_no']))
    governance=max(pages,key=lambda p:(governance_page_score(p),plan_page_score(p),-p['page_no']))
    ranked=max(pages,key=lambda p:(plan_page_score(p),-p['page_no']))
    return list({p['page_no']:p for p in first+[best,governance,ranked]}.values())

def plan_task_chunks(pages, char_budget):
    """Return a single bounded plan-task input with opening pages and best page."""
    first=list(pages[:3]); best=max(pages,key=lambda p:(p['relevance'], -p['page_no'])); governance=max(pages,key=lambda p:(governance_page_score(p),plan_page_score(p),-p['page_no'])); ranked=max(pages,key=lambda p:(plan_page_score(p),-p['page_no']))
    groups=[first]
    extras=[p for p in (best,governance,ranked) if p['page_no'] not in {x['page_no'] for x in first}]
    if extras: groups.append(extras)
    parts=[]; group_budget=max(1,char_budget//len(groups))
    for group in groups:
        quota=max(80,group_budget//max(1,len(group)))
        for page in group:
            marker=f"[PAGE {page['page_no']}]\n"; parts.append(marker+str(page['text'])[:max(0,quota-len(marker))])
    return ['\n\n'.join(parts)]

def score_risk_action_pages(pages):
    risk_rank=[]; action_rank=[]
    for page in pages:
        hit=json.loads(page['hits']); hazards=hit.get('hazards') or {}
        hazard_count=sum(v for v in hazards.values() if isinstance(v,(int,float))) if isinstance(hazards,dict) else len(hazards)
        implementation=hit.get('implementation') or []; finance=float(hit.get('finance_count') or 0); money=len(hit.get('money') or [])
        risk_score=hazard_count*5+len(hazards)*2 if isinstance(hazards,dict) else hazard_count*5
        text=str(_page_value(page,'text','') or '').casefold()
        action_score=int(page['action_table'] or 0)*8+len(implementation)*3+finance*2+money*2
        for pattern,weight in ((r'\$\s?[\d,]+(?:\.\d+)?',12),(r'\bcost\b',6),(r'\bpayback\b',12),(r'\bfunding\b',7),(r'\btime[- ]frame\b',6),(r'\bresponsible\b',6),(r'annual report',8),(r'\binventory\b',7),(r'implementation',7),(r'\bmonitoring\b',6)):
            action_score+=min(5,len(re.findall(pattern,text)))*weight
        action_score+=_table_bonus(text)
        risk_rank.append((float(risk_score),page['page_no'],page))
        action_rank.append((float(action_score),page['page_no'],page))
    return risk_rank,action_rank

def rank_risk_action_pages(pages,top_k):
    risk_rank,action_rank=score_risk_action_pages(pages)
    ranked=lambda values:[x[2] for x in sorted((x for x in values if x[0]>0),key=lambda x:(-x[0],x[1]))[:top_k]]
    return ranked(risk_rank),ranked(action_rank)

def chunk_page_numbers(chunks):
    return {int(n) for n in re.findall(r'\[PAGE (\d+)\]', '\n'.join(chunks))}

def page_ranges(pages):
    values=sorted(set(int(p) for p in pages)); ranges=[]
    for page in values:
        if ranges and page==ranges[-1][1]+1: ranges[-1][1]=page
        else: ranges.append([page,page])
    return [str(a) if a==b else f'{a}-{b}' for a,b in ranges]

def _persist_results(c,pid,new_results,replace_tasks=None):
    for task,payload in new_results.items():
        new_records=payload.get('records',[])
        old=c.execute('SELECT payload FROM extracted WHERE plan_id=? AND task=?',(pid,task)).fetchone()
        if not new_records and old: continue
        old_records=json.loads(old[0]).get('records',[]) if old and task not in (replace_tasks or set()) else []
        seen=set(); merged=[]
        for record in old_records+new_records:
            key=json.dumps(record,ensure_ascii=False,sort_keys=True)
            if key not in seen: seen.add(key); merged.append(record)
        c.execute('INSERT OR REPLACE INTO extracted VALUES(?,?,?)',(pid,task,json.dumps({'records':merged},ensure_ascii=False)))

def split_capped_chunk(ch):
    if len(ch)>1:
        middle=len(ch)//2
        splits=[ch[:middle],ch[middle:]]
    else:
        marker=re.match(r'(\[PAGE \d+\]\n)',ch[0]); prefix=marker.group(1) if marker else ''; content=ch[0][len(prefix):]
        if len(content)<256: return [],f'Output exceeds token limit for {len(content)}-character fragment'
        middle=len(content)//2; splits=[[prefix+content[:middle]],[prefix+content[middle:]]]
    before=chunk_page_numbers(ch); after=set().union(*(chunk_page_numbers(part) for part in splits))
    if not before.issubset(after): raise RuntimeError(f'Adaptive split dropped page markers: {sorted(before-after)}')
    return splits,None

def llm_stage(c,model=MODEL_DEFAULT,top_k=25,force=False,limit=None,only=None,num_ctx=12288,temperature=0,seed=42,max_calls_per_plan=15,provider_name='ollama',max_tokens=8192,budget_usd=10,offpeak_discount=1.0,concurrency=8,output_reserve=2048,effort='none',tasks_filter=None):
    import requests
    if top_k<1 or max_calls_per_plan<1 or concurrency<1 or num_ctx<1 or output_reserve<1 or max_tokens<1: raise ValueError('--top-k, --max-calls-per-plan, --concurrency, --num-ctx, --output-reserve and --max-tokens must be positive integers')
    if tasks_filter is not None and (not tasks_filter or not set(tasks_filter).issubset({'plan','risk','action'})): raise ValueError('--tasks must contain one or more of plan, risk, action')
    if effort not in {'none','low','high','max'}: raise ValueError('--effort must be none, low, high, or max')
    if budget_usd<=0 or not 0<=offpeak_discount<=1: raise ValueError('--budget-usd must be positive and --offpeak-discount must be between 0 and 1')
    from .estimate import cost_usd
    cols,dicts=template_data(); n=0; start=time.time(); todo=selected(c,only,limit)
    print(f'LLM CONCURRENCY provider_workers={concurrency}; sqlite_writer=main_thread',flush=True)
    task_config=task_configs(cols,dicts,max_tokens,num_ctx,output_reserve)
    for task,cfg in task_config.items():
        print(f"TOKEN BUDGET ollama task={task}: system={cfg['system_tokens']}, schema={cfg['schema_tokens']}, output_reserve={output_reserve}, available_input={cfg['ollama_available']}",flush=True)
        print(f"TOKEN BUDGET deepseek task={task}: system={cfg['system_tokens']}, schema={cfg['schema_tokens']}, output_reserve={max_tokens}, available_input={cfg['deepseek_available']} (configured chunk input cap; effective context estimate={cfg['deepseek_context']})",flush=True)
    if provider_name=='ollama':
        validate_ollama_budgets(task_config)
    if provider_name=='ollama':
        try:
            check=requests.get('http://localhost:11434/api/tags',timeout=(5,10)); check.raise_for_status()
        except requests.RequestException as e: raise RuntimeError(f'Ollama is unavailable at http://localhost:11434: {e}') from e
        installed={x.get('name') for x in check.json().get('models',[])}
        if model not in installed: raise RuntimeError(f'Ollama model {model!r} is not installed; available: {sorted(installed)}')
    provider_options={'num_ctx':num_ctx,'temperature':temperature,'seed':seed,'max_tokens':(output_reserve if provider_name=='ollama' else max_tokens)}
    if provider_name=='deepseek': provider_options['reasoning_effort']=effort
    api=get_provider(provider_name,model,**provider_options)
    options={'num_ctx':num_ctx,'temperature':temperature,'seed':seed,'num_predict':output_reserve,'max_tokens':max_tokens,'reasoning_effort':effort if provider_name=='deepseek' else None}
    previous=c.execute("SELECT * FROM llm_runs WHERE provider=? AND requested_model=? AND status IN ('running','budget','interrupted') ORDER BY started_at DESC LIMIT 1",(provider_name,model)).fetchone()
    if previous:
        run_id=previous['run_id']; run_cost=float(previous['cost_usd'] or 0); actual_model=previous['actual_model']
        c.execute("UPDATE llm_runs SET ended_at=NULL,status='running' WHERE run_id=?",(run_id,))
        run_cached,run_uncached,run_output,run_reasoning,run_reasoning_cost,run_retries,run_calls=(previous['input_cached'] or 0,previous['input_uncached'] or 0,previous['output_tokens'] or 0,previous['reasoning_tokens'] or 0,previous['reasoning_cost_usd'] or 0,previous['retries'] or 0,previous['api_calls'] or 0)
    else:
        run_id=uuid.uuid4().hex; run_cost=0.0; run_cached=run_uncached=run_output=run_reasoning=run_reasoning_cost=run_calls=run_retries=0; actual_model=None
        c.execute('INSERT INTO llm_runs(run_id,provider,requested_model,started_at,status) VALUES(?,?,?,datetime(\'now\'),?)',(run_id,provider_name,model,'running'))
    c.commit(); budget_stop=run_cost>=budget_usd
    if budget_stop:
        c.execute("UPDATE llm_runs SET ended_at=datetime('now'),status='budget' WHERE run_id=?",(run_id,)); c.commit()
        print(f'LLM BUDGET STOP before work: cumulative=${run_cost:.6f}, limit=${budget_usd:.2f}',flush=True); return 0
    for ix,plan in enumerate(todo,1):
        pid=plan['plan_id']; plan_start=time.time(); calls_made=0; retries=0; split_calls=0; pages_total=0; selected_counts={'plan':0,'risk':0,'action':0}; total_input=total_cached=total_output=total_reasoning=total_reasoning_cost=total_cost=plan_retries=0; discarded_pages=set(); successful_response_pages=set(); failed_chunks=[]; reused_raw_chunks=0; account_stop_reason=None
        status=c.execute('SELECT status_llm FROM plans WHERE plan_id=?',(pid,)).fetchone()
        if not force and (done(c,pid,'llm') or (status and status[0]=='capped')):
            print(f'LLM SKIP {pid}: {status[0] if status and status[0]=="capped" else "already done"}',flush=True); continue
        mark(c,pid,'llm','running'); c.execute("UPDATE plans SET status_llm='running',failed_pages=NULL WHERE plan_id=?",(pid,)); c.commit()
        try:
            pages=c.execute('SELECT p.page_no,p.text,COALESCE(h.relevance,0) relevance,COALESCE(h.action_table,0) action_table,COALESCE(h.hits_json,\'{}\') hits FROM pages p LEFT JOIN page_hits h USING(plan_id,page_no) WHERE p.plan_id=? ORDER BY p.page_no',(pid,)).fetchall()
            if not pages: raise ValueError('No extracted pages; run text and rules first')
            pages_total=len(pages)
            plan_pages=select_plan_pages(pages); best_page=max(pages,key=lambda p:(p['relevance'], -p['page_no']))
            risk_pages,action_pages=rank_risk_action_pages(pages,top_k)
            selected_counts={'plan':len(plan_pages),'risk':len(risk_pages),'action':len(action_pages)}
            risk_scores,action_scores=score_risk_action_pages(pages)
            score_text=lambda rows:', '.join(f"{pno}:{score:g}" for score,pno,_ in sorted(rows,key=lambda x:(-x[0],x[1])))
            print(f'LLM SELECT {pid}: pages_total={pages_total}, plan={len(plan_pages)} pages={[p["page_no"] for p in plan_pages]}, risk={len(risk_pages)} pages={[p["page_no"] for p in risk_pages]}, action={len(action_pages)} pages={[p["page_no"] for p in action_pages]}, top_k={top_k}',flush=True)
            print(f'LLM RANK {pid}: plan={score_text([(plan_page_score(p),p["page_no"],p) for p in pages])}; risk={score_text(risk_scores)}; action={score_text(action_scores)}',flush=True)
            new_results={}; capped=False; call_gate=RequestBudget(max_calls_per_plan,run_cost,budget_usd,0.0)
            task_pages_all=(('plan',plan_pages),('risk',risk_pages),('action',action_pages))
            if tasks_filter is not None: task_pages_all=tuple((t,p) for t,p in task_pages_all if t in set(tasks_filter))
            for task,task_pages in task_pages_all:
                if not task_pages: continue
                cols_task=cols['Plan Level'] if task=='plan' else cols['Risk Level'] if task=='risk' else cols['Action Level']
                cfg=task_config[task]; response_model=cfg['response_model']; schema=cfg['schema']
                prompt_base=cfg['system']+'\n\nJSON Schema:\n'+json.dumps(schema,ensure_ascii=False,sort_keys=True)
                prompt_hash=cfg['prompt_hash']
                c.execute('INSERT OR REPLACE INTO llm_prompt_versions VALUES(?,?,?)',(run_id,task,prompt_hash)); c.commit()
                available=cfg['ollama_available'] if provider_name=='ollama' else cfg['deepseek_available']
                content_budget=available if provider_name=='ollama' else min(12000,available); char_budget=content_budget*3
                if task=='plan':
                    all_chunks=[plan_task_chunks(pages,char_budget)]
                    chunk_queue=list(all_chunks)
                    plan_chunk=chunk_queue[0][0]
                    print(f'LLM PLAN CHUNK {pid}: pages={sorted(chunk_page_numbers([plan_chunk]))}, chars={len(plan_chunk)}, page23_chars={next((len(str(p["text"])) for p in plan_pages if p["page_no"]==23),0)}',flush=True)
                else:
                    chunks=[]; chunk=[]; size=0
                    for page in task_pages:
                        marker=f"[PAGE {page['page_no']}]\n"; page_text=str(page['text']); fragment_size=max(1,char_budget-len(marker)-30)
                        parts=[marker+page_text] if len(marker)+len(page_text)<=char_budget else [marker+page_text[i:i+fragment_size] for i in range(0,len(page_text),fragment_size)]
                        for part in parts:
                            part_tokens=(len(part)+2)//3
                            if chunk and size+part_tokens>content_budget: chunks.append(chunk); chunk=[]; size=0
                            chunk.append(part); size+=part_tokens
                    if chunk: chunks.append(chunk)
                    all_chunks=list(chunks); chunk_queue=list(all_chunks)
                raw_rows=c.execute('SELECT id,run_id,response,options FROM llm_raw WHERE plan_id=? AND task=? AND provider=? AND model=? AND prompt_hash=? ORDER BY id',(pid,task,provider_name,model,cfg['prompt_hash'])).fetchall()
                remaining=set(range(len(all_chunks)))
                metadata_rows=[]; legacy_rows=[]
                for raw_row in raw_rows:
                    opts=json.loads(raw_row['options'] or '{}')
                    if 'chunk_pages' in opts:
                        if opts.get('chunk_pages'): metadata_rows.append(raw_row)
                    else: legacy_rows.append(raw_row)
                metadata_rows.sort(key=lambda row:row['id'],reverse=True)
                legacy_by_run={}
                for raw_row in legacy_rows: legacy_by_run.setdefault(raw_row['run_id'],[]).append(raw_row)
                ordered_legacy=[]
                for run_rows in sorted(legacy_by_run.values(),key=lambda group:max(row['id'] for row in group),reverse=True): ordered_legacy.append(run_rows)
                candidates=[(raw_row,None) for raw_row in metadata_rows]
                for group in ordered_legacy: candidates.extend((raw_row,index) for index,raw_row in enumerate(group))
                for raw_row,legacy_index in candidates:
                    opts=json.loads(raw_row['options'] or '{}'); raw_pages=set(int(x) for x in opts.get('chunk_pages',[]) if str(x).isdigit())
                    match_index=next((i for i in sorted(remaining) if raw_pages and chunk_page_numbers(all_chunks[i])==raw_pages),None)
                    if match_index is None and not raw_pages and legacy_index in remaining: match_index=legacy_index
                    if match_index is None: continue
                    ch=all_chunks[match_index]; allowed_pages=chunk_page_numbers(ch)
                    chunk_hash=hashlib.sha256('\n'.join(ch).encode('utf-8')).hexdigest()
                    if opts.get('chunk_sha256') and opts['chunk_sha256']!=chunk_hash: continue
                    try:
                        parsed=parse_truncated_json(raw_row['response'] or '',response_model); records=parsed.get('records',[]); valid_refs=True
                        for record in records:
                            cited={int(v) for v in re.findall(r'\d+',str(record.get('source_page') or ''))}
                            if not cited or not cited.issubset(allowed_pages): valid_refs=False; break
                        if not valid_refs: continue
                        new_results.setdefault(task,{'records':[]})['records'].extend(records)
                        successful_response_pages.update(allowed_pages); remaining.discard(match_index); reused_raw_chunks+=1
                    except Exception:
                        continue
                chunk_queue=[all_chunks[i] for i in sorted(remaining)]
                def request_chunk(ch):
                    user=f'Plan ID: {pid}; task: {task}. Cite page markers exactly as supplied.\n\n'+'\n\n'.join(ch)
                    projected=cost_usd((len(prompt_base)+len(user)+2)//3,0,max_tokens) if provider_name=='deepseek' else 0
                    started=time.time()
                    try:
                        parsed,usage,raw=api.extract(task,[{'role':'system','content':prompt_base},{'role':'user','content':user}],response_model,call_gate,projected)
                        return ch,parsed,usage,raw,'',time.time()-started,None
                    except Exception as exc:
                        return ch,None,getattr(exc,'usage',{}),getattr(exc,'raw_text',''),str(exc),time.time()-started,getattr(exc,'stop_reason',None)
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    while chunk_queue and not (budget_stop or capped):
                        batch=[]
                        while chunk_queue and len(batch)<concurrency and call_gate.used+len(batch)<max_calls_per_plan:
                            batch.append(chunk_queue.pop(0))
                        if not batch: capped=True; break
                        responses=list(pool.map(request_chunk,batch))
                        splits=[]
                        for ch,result,usage,raw,err,duration,stop_reason in responses:
                            usage=usage or {}; used=usage.get('api_calls',1 if not err else 0); calls_made+=used; run_calls+=used
                            retries_used=usage.get('retries',0); plan_retries+=retries_used; run_retries+=retries_used
                            total_input+=usage.get('input_tokens',0); total_cached+=usage.get('cached_input_tokens',0); total_output+=usage.get('output_tokens',0); total_reasoning+=usage.get('reasoning_tokens',0); total_reasoning_cost+=usage.get('reasoning_cost_usd',0); run_reasoning+=usage.get('reasoning_tokens',0); run_reasoning_cost+=usage.get('reasoning_cost_usd',0)
                            uncached=usage.get('input_tokens',0)-usage.get('cached_input_tokens',0); request_cost=usage.get('cost_usd',0); total_cost+=request_cost; run_cost+=request_cost
                            actual_model=usage.get('model_id') or actual_model
                            stored_error=err or None
                            call_options=options|usage|{'chunk_pages':sorted(chunk_page_numbers(ch)),'chunk_sha256':hashlib.sha256('\n'.join(ch).encode('utf-8')).hexdigest()}
                            c.execute('INSERT INTO llm_raw(plan_id,task,model,prompt_hash,created_at,duration,response,error,options,run_id,provider,actual_model,input_cached,input_uncached,output_tokens,reasoning_tokens,reasoning_cost_usd,cost_usd,retry_count,api_calls) VALUES(?,?,?,?,datetime(\'now\'),?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(pid,task,model,prompt_hash,duration,raw,stored_error,json.dumps(call_options),run_id,provider_name,usage.get('model_id'),usage.get('cached_input_tokens',0),uncached,usage.get('output_tokens',0),usage.get('reasoning_tokens',0),usage.get('reasoning_cost_usd',0),request_cost,retries_used,used))
                            c.execute('UPDATE llm_runs SET actual_model=COALESCE(?,actual_model),input_cached=input_cached+?,input_uncached=input_uncached+?,output_tokens=output_tokens+?,reasoning_tokens=reasoning_tokens+?,reasoning_cost_usd=reasoning_cost_usd+?,cost_usd=cost_usd+?,cost_discounted_usd=cost_discounted_usd+?,api_calls=api_calls+?,retries=retries+? WHERE run_id=?',(actual_model,usage.get('cached_input_tokens',0),uncached,usage.get('output_tokens',0),usage.get('reasoning_tokens',0),usage.get('reasoning_cost_usd',0),request_cost,request_cost*offpeak_discount,used,retries_used,run_id)); c.commit()
                            if usage.get('budget_reached'): budget_stop=True
                            if usage.get('cap_reached'): capped=True
                            if stop_reason:
                                account_stop_reason=stop_reason; budget_stop=True
                                failed_chunks.append({'task':task,'pages':sorted(chunk_page_numbers(ch)),'error':stop_reason})
                            if err:
                                failed_chunks.append({'task':task,'pages':sorted(chunk_page_numbers(ch)),'error':err})
                                continue
                            if usage.get('finish_reason')=='length':
                                discarded_pages.update(chunk_page_numbers(ch))
                                if task=='plan': failed_chunks.append({'task':task,'pages':sorted(chunk_page_numbers(ch)),'error':'Plan task output reached max_tokens; plan task is never retried or split'})
                                else:
                                    split_calls+=1; retries+=1
                                    children,split_error=split_capped_chunk(ch); splits.extend(children)
                                    if split_error: failed_chunks.append({'task':task,'pages':sorted(chunk_page_numbers(ch)),'error':split_error})
                                continue
                            if result is None:
                                if usage.get('cap_reached'): capped=True
                                elif usage.get('budget_reached'): budget_stop=True
                                else: failed_chunks.append({'task':task,'pages':sorted(chunk_page_numbers(ch)),'error':'Provider returned no parsed result'})
                                continue
                            allowed_pages=chunk_page_numbers(ch); invalid_source=None
                            for record in result['records']:
                                source=str(record.get('source_page') or ''); cited={int(v) for v in re.findall(r'\d+',source)}
                                if not cited or not cited.issubset(allowed_pages): invalid_source=f'Invalid source_page {source!r}; allowed pages: {sorted(allowed_pages)}'; break
                            if invalid_source:
                                failed_chunks.append({'task':task,'pages':sorted(allowed_pages),'error':invalid_source}); continue
                            successful_response_pages.update(allowed_pages)
                            new_results.setdefault(task,{'records':[]})['records'].extend(result['records'])
                        chunk_queue[0:0]=splits
                    if chunk_queue and (budget_stop or capped):
                        for pending in chunk_queue: failed_chunks.append({'task':task,'pages':sorted(chunk_page_numbers(pending)),'error':'budget limit reached' if budget_stop else 'per-plan call cap reached'})
                if budget_stop or capped: break
            elapsed=time.time()-plan_start; coverage_gaps=sorted(discarded_pages-successful_response_pages)
            failure_payload=[]
            for task_name in dict.fromkeys(item['task'] for item in failed_chunks):
                matching=[item for item in failed_chunks if item['task']==task_name]
                pages=sorted({page for item in matching for page in item['pages']})
                failure_payload.append({'task':task_name,'pages':pages,'page_ranges':page_ranges(pages),'errors':list(dict.fromkeys(item['error'] for item in matching))})
            failed_json=json.dumps(failure_payload,ensure_ascii=False) if failure_payload else None
            selected_tasks=set(tasks_filter) if tasks_filter is not None else ({'plan','risk','action'} if force else set())
            if force:
                for selected_task in selected_tasks: new_results.setdefault(selected_task,{'records':[]})
            if budget_stop:
                _persist_results(c,pid,new_results,selected_tasks)
                c.commit()
                c.execute("UPDATE plans SET status_llm='budget',failed_pages=? WHERE plan_id=?",(failed_json,pid)); mark(c,pid,'llm','budget')
                if account_stop_reason:
                    print(f'LLM BALANCE STOP: {account_stop_reason}; plan={pid} remains not-done, completed chunk results are retained, no further provider calls will be made, and the run can resume after account funding.',flush=True)
                else:
                    print(f'LLM BUDGET STOP {pid}: calls={calls_made}, retries={plan_retries}, input_tokens={total_input}, cached_input_tokens={total_cached}, output_tokens={total_output}, reasoning_tokens={total_reasoning}, reasoning_cost_usd={total_reasoning_cost:.6f}, cost_usd={total_cost:.6f}, coverage_gap_pages={coverage_gaps}, seconds={elapsed:.1f}',flush=True)
                c.execute("UPDATE llm_runs SET ended_at=datetime('now'),status='budget' WHERE run_id=?",(run_id,)); c.commit(); break
            if capped:
                _persist_results(c,pid,new_results,selected_tasks)
                c.commit()
                c.execute("UPDATE plans SET status_llm='capped',failed_pages=? WHERE plan_id=?",(failed_json,pid)); mark(c,pid,'llm','capped')
                print(f'LLM CAPPED {pid}: pages_total={pages_total}, selected={selected_counts}, calls={calls_made}, retries={plan_retries}, input_tokens={total_input}, cached_input_tokens={total_cached}, uncached_input_tokens={total_input-total_cached}, output_tokens={total_output}, reasoning_tokens={total_reasoning}, reasoning_cost_usd={total_reasoning_cost:.6f}, cost_usd={total_cost:.6f}, discounted_cost_usd={total_cost*offpeak_discount:.6f}, coverage_gap_pages={coverage_gaps}, seconds={elapsed:.1f}',flush=True); continue
            _persist_results(c,pid,new_results,selected_tasks)
            plan_status='partial' if failure_payload else 'done'
            c.execute("UPDATE plans SET status_llm=?,failed_pages=? WHERE plan_id=?",(plan_status,failed_json,pid)); c.commit(); mark(c,pid,'llm',plan_status); n+=1
            if failure_payload: print(f'LLM PARTIAL {pid}: failed_page_ranges={failure_payload}, reused_raw_chunks={reused_raw_chunks}',flush=True)
            else: print(f'LLM DONE {pid}: reused_raw_chunks={reused_raw_chunks}',flush=True)
            print(f'LLM TELEMETRY {pid}: pages_total={pages_total}, selected={selected_counts}, calls={calls_made}, retries={plan_retries}, split_calls={split_calls}, input_tokens={total_input}, cached_input_tokens={total_cached}, uncached_input_tokens={total_input-total_cached}, output_tokens={total_output}, reasoning_tokens={total_reasoning}, reasoning_cost_usd={total_reasoning_cost:.6f}, cost_usd={total_cost:.6f}, discounted_cost_usd={total_cost*offpeak_discount:.6f}, coverage_gap_pages={coverage_gaps}, seconds={elapsed:.1f}',flush=True)
        except KeyboardInterrupt:
            c.commit(); c.execute("UPDATE llm_runs SET ended_at=datetime('now'),status='interrupted' WHERE run_id=?",(run_id,)); c.commit(); print('Interrupted; completed plans are checkpointed and can be resumed.',flush=True); break
        except Exception as e:
            c.execute("UPDATE plans SET status_llm='error' WHERE plan_id=?",(pid,)); c.commit(); mark(c,pid,'llm','error'); error(c,pid,'llm','',e)
            elapsed=time.time()-plan_start; coverage_gaps=sorted(discarded_pages-successful_response_pages); print(f'LLM ERROR {pid}: pages_total={pages_total}, selected={selected_counts}, calls={calls_made}, retries={plan_retries}, input_tokens={total_input}, cached_input_tokens={total_cached}, uncached_input_tokens={total_input-total_cached}, output_tokens={total_output}, reasoning_tokens={total_reasoning}, reasoning_cost_usd={total_reasoning_cost:.6f}, cost_usd={total_cost:.6f}, coverage_gap_pages={coverage_gaps}, seconds={elapsed:.1f}: {e}',flush=True)
    c.execute("UPDATE llm_runs SET ended_at=COALESCE(ended_at,datetime('now')),status=COALESCE(NULLIF(status,'running'),'done') WHERE run_id=?",(run_id,)); c.commit()
    summary=c.execute('SELECT actual_model,input_cached,input_uncached,output_tokens,reasoning_tokens,reasoning_cost_usd,cost_usd,cost_discounted_usd FROM llm_runs WHERE run_id=?',(run_id,)).fetchone()
    print(f"LLM RUN SUMMARY run_id={run_id}, provider={provider_name}, model={summary['actual_model'] or model}, calls={run_calls}, retries={run_retries}, cached_input_tokens={summary['input_cached']}, uncached_input_tokens={summary['input_uncached']}, output_tokens={summary['output_tokens']}, reasoning_tokens={summary['reasoning_tokens']}, reasoning_cost_usd={summary['reasoning_cost_usd']:.6f}, cost_usd={summary['cost_usd']:.6f}, discounted_cost_usd={summary['cost_discounted_usd']:.6f}",flush=True)
    return n
