"""Run a bounded CDP consolidated pilot with isolated SQLite storage."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from .cdp_consolidated import (GROUPS,PROMPT_VERSION,SAFE_CARD_FIELDS,cards_by_group,
                               estimate,open_cap_readonly,selected_pages_for_plan,full_document_selection,page_chunks)
from .cdp_consolidated_models import GroupResponse
from .cdp_consolidated_prepare import OUT
from .providers import DeepSeekProvider,ProviderCallError,RequestBudget

SYSTEM_RULES="""Independently answer CDP questions from the supplied CAP document only.
The document may be a chunk; absence applies only to this chunk. Do not infer no or zero.
For question 9.1.1 describe this document first only if it is actually the jurisdiction's plan;
do not treat other plans referenced in it as this document.
Return every requested question ID in JSON with status Answered, Explicit No, Not Disclosed,
Not Applicable or Unclear. Return a concise natural answer containing only supported facts.
Do not repeat missing fields or write Not Disclosed inside answers. Record potentially missing
fields in missing_components. For multiple hazards, targets and actions, return separately
named items, without numbering, and keep each item's cost, funding, status, target and owner
attached to that item. Do not cap item counts. Put a verbatim quotation and source page on
each item and every other independently stated fact. Never invent dates, values or statuses.
For choice fields use an exact questionnaire option only when reliably supported; otherwise
mark Unclear or leave the field missing. Return every chosen option as choice_fields with
its exact column_id and value, so the option can be validated for the correct field.
Preserve the PDF's original numbers and units;
never perform unit arithmetic. For fields requiring conversion return quantities with original
value, original unit, target unit and field name; the program handles conversion. Avoid
semicolons within a single item. Do not infer policies, finance, status or targets from context.
"""


def system_prompt(group_cards):
    return base_system_prompt()+'\nQUESTION CARDS (2026):\n'+json.dumps(group_cards,ensure_ascii=False,sort_keys=True)

def base_system_prompt():
    return SYSTEM_RULES+'\nJSON SCHEMA:\n'+json.dumps(GroupResponse.model_json_schema(),ensure_ascii=False,sort_keys=True)


def open_store(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(path);c.row_factory=sqlite3.Row
    c.executescript('''
    CREATE TABLE IF NOT EXISTS cdp_runs(run_id TEXT PRIMARY KEY, started_at TEXT, ended_at TEXT, status TEXT, model TEXT, budget_usd REAL, prompt_version TEXT);
    CREATE TABLE IF NOT EXISTS cdp_group_jobs(run_id TEXT,pdf_id TEXT,plan_id TEXT,group_id TEXT,prompt_hash TEXT,status TEXT,payload TEXT,selected_pages TEXT,updated_at TEXT,PRIMARY KEY(run_id,pdf_id,group_id,prompt_hash));
    CREATE TABLE IF NOT EXISTS cdp_calls(call_id TEXT PRIMARY KEY,run_id TEXT,pdf_id TEXT,plan_id TEXT,group_id TEXT,prompt_hash TEXT,pages TEXT,raw_response TEXT,error TEXT,finish_reason TEXT,input_cached INTEGER,input_uncached INTEGER,output_tokens INTEGER,reasoning_tokens INTEGER,cost_usd REAL,api_calls INTEGER,retries INTEGER,actual_model TEXT,uncertain_reserved_usd REAL DEFAULT 0,created_at TEXT);
    CREATE TABLE IF NOT EXISTS cdp_answers(run_id TEXT,pdf_id TEXT,question_id TEXT,answer_part INTEGER,prompt_hash TEXT,call_ids TEXT,raw_response TEXT,parsed_answer TEXT,disclosure_status TEXT,updated_at TEXT,PRIMARY KEY(run_id,pdf_id,question_id,answer_part));
    CREATE TABLE IF NOT EXISTS cdp_evidence(run_id TEXT,export_id TEXT,pdf_id TEXT,question_id TEXT,item_label TEXT,evidence_page INTEGER,evidence_excerpt TEXT,confidence TEXT,evidence_verified TEXT,page_corrected TEXT);
    CREATE TABLE IF NOT EXISTS cdp_review_flags(run_id TEXT,export_id TEXT,pdf_id TEXT,question_id TEXT,flag_type TEXT,reason TEXT,consolidated_answer TEXT,disclosure_status TEXT);
    CREATE INDEX IF NOT EXISTS idx_cdp_calls_run_pdf_group ON cdp_calls(run_id,pdf_id,group_id);
    ''')
    if 'uncertain_reserved_usd' not in {x[1] for x in c.execute('PRAGMA table_info(cdp_calls)')}:
        c.execute('ALTER TABLE cdp_calls ADD COLUMN uncertain_reserved_usd REAL DEFAULT 0')
    c.commit();return c


class _Rows:
    def __init__(self,rows):self.rows=rows
    def fetchone(self):return self.rows[0] if self.rows else None
    def __iter__(self):return iter(self.rows)


class SingleSQLiteWriter:
    """Serialize all CDP database operations on one owning worker thread."""
    def __init__(self,path):
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='cdp-sqlite')
        self.connection=self.pool.submit(open_store,path).result()
    def execute(self,sql,params=()):
        def action():
            cursor=self.connection.execute(sql,params)
            return _Rows(cursor.fetchall() if cursor.description else [])
        return self.pool.submit(action).result()
    def commit(self):return self.pool.submit(self.connection.commit).result()
    def close(self):
        self.pool.submit(self.connection.close).result();self.pool.shutdown()


def _cost_projection(system,text,max_output=8192):
    # Byte count is a conservative bound for input tokens. Reserve additional
    # input for up to three provider validation retries with previous outputs.
    maximum_input=len(system.encode('utf8'))+len(text.encode('utf8'))+1024+3*(max_output+65536)
    return (maximum_input*.30+max_output*1.20)/1_000_000


def _split_chunk(chunk):
    if len(chunk)>1:
        middle=len(chunk)//2
        return [chunk[:middle],chunk[middle:]]
    if not chunk or len(chunk[0][1])<2000:return []
    number,text=chunk[0];middle=len(text)//2
    return [[(number,text[:middle])],[(number,text[middle:])]]


def _page_numbers(chunk):return sorted({number for number,_ in chunk})


def _call_and_store(c,provider,run_id,plan,group,prompt_hash,system,chunk,budget,group_cards=None,extra_task=''):
    text='\n\n'.join(f'=== Page {number} ===\n{value}' for number,value in chunk)
    estimate_cost=_cost_projection(system,text)
    err='';raw='';result=None;usage={};call_id=uuid.uuid4().hex
    # Persist a conservative crash reserve before entering the provider. Up to
    # two validation attempts may each encounter six network attempts. On a
    # hard interruption the exact usage is unknowable, so resume counts this
    # upper bound until the ledger is inspected; normal completion replaces it.
    c.execute('''INSERT INTO cdp_calls(call_id,run_id,pdf_id,plan_id,group_id,prompt_hash,pages,error,
                 api_calls,retries,cost_usd,uncertain_reserved_usd,created_at)
                 VALUES(?,?,?,?,?,?,?, ?,12,0,0,?,datetime('now'))''',
              (call_id,run_id,plan['pdf_id'],plan['plan_id'],group,prompt_hash,json.dumps(_page_numbers(chunk)),
               'IN_FLIGHT: upper-bound reserve until provider returns',12*estimate_cost))
    c.commit()
    try:
        messages=[{'role':'system','content':base_system_prompt()},{'role':'user','content':text},
                  {'role':'user','content':'QUESTION CARDS (2026):\n'+json.dumps(group_cards or [],ensure_ascii=False,sort_keys=True)+'\n'+extra_task}]
        result,usage,raw=provider.extract(group,messages,GroupResponse,call_budget=budget,projected_cost=estimate_cost)
    except ProviderCallError as exc:
        usage=exc.usage or {};raw=exc.raw_text or '';err=str(exc)
        if exc.stop_reason:budget.parent.halt(exc.stop_reason)
    cached=int(usage.get('cached_input_tokens') or 0);total_input=int(usage.get('input_tokens') or 0)
    unknown_attempts=max(0,int(usage.get('api_calls') or 0)-int(usage.get('responses_received') or 0))
    uncertain_reserve=unknown_attempts*estimate_cost
    c.execute('''UPDATE cdp_calls SET raw_response=?,error=?,finish_reason=?,input_cached=?,input_uncached=?,output_tokens=?,
                 reasoning_tokens=?,cost_usd=?,api_calls=?,retries=?,actual_model=?,uncertain_reserved_usd=? WHERE call_id=?''',
        (raw,err,usage.get('finish_reason'),cached,max(0,total_input-cached),int(usage.get('output_tokens') or 0),
         int(usage.get('reasoning_tokens') or 0),float(usage.get('cost_usd') or 0),int(usage.get('api_calls') or 0),
         int(usage.get('retries') or 0),usage.get('model_id'),uncertain_reserve,call_id))
    c.commit()
    return result,usage,err,call_id


def run_group(c,provider,run_id,plan,group,group_cards,pages,selected,budget,keyword_sets=None):
    system=system_prompt(group_cards);prompt_hash=hashlib.sha256(system.encode('utf8')).hexdigest()
    existing=c.execute('SELECT status,payload FROM cdp_group_jobs WHERE run_id=? AND pdf_id=? AND group_id=? AND prompt_hash=?',
                       (run_id,plan['pdf_id'],group,prompt_hash)).fetchone()
    if existing and existing['status']=='done':return json.loads(existing['payload'])
    page_text=dict(pages);queue=[(chunk,group_cards) for chunk in page_chunks([(n,page_text[n] or '') for n in selected])]
    prior=json.loads(existing['payload']) if existing and existing['payload'] else {}
    # A previous interrupted multi-page split may have persisted complete child
    # responses and an explicit list of still-unprocessed pages.
    if prior.get('resume_pages'):
        queue=[([(n,page_text[n] or '') for n in prior['resume_pages']],group_cards)]
    if prior.get('pending_work'):
        queue=[([(int(n),t) for n,t in work['chunk']],work['cards']) for work in prior['pending_work']]
    elif prior.get('phase')=='postprocess':queue=[]
    successful=list(prior.get('responses',[]));discarded=set();completed=set();failed=set();splits=0;errors=[];halted=False
    retry_work=[]
    absence_checks=list(prior.get('absence_checks',[]));evidence_retries=list(prior.get('evidence_retries',[]))
    for saved in successful:completed.update(saved.get('pages',[]))
    allowed={card['question_id'] for card in group_cards}
    def checkpoint(phase,work):
        payload={'question_ids':sorted(allowed),'responses':successful,'selected_pages':selected,'prompt_hash':prompt_hash,
                 'pending_work':[{'chunk':chunk,'cards':cards} for chunk,cards in work],
                 'coverage_gap_pages':sorted({n for chunk,_ in work for n,_ in chunk}),
                 'phase':phase,'absence_checks':absence_checks,'evidence_retries':evidence_retries}
        c.execute('INSERT OR REPLACE INTO cdp_group_jobs VALUES(?,?,?,?,?,?,?,?,datetime(\'now\'))',
                  (run_id,plan['pdf_id'],plan['plan_id'],group,prompt_hash,'partial',json.dumps(payload,ensure_ascii=False),json.dumps(selected)))
        c.commit()
    while queue:
        checkpoint('extract',queue)
        chunk,chunk_cards=queue.pop(0)
        result,usage,err,call_id=_call_and_store(c,provider,run_id,plan,group,prompt_hash,system,chunk,budget,chunk_cards)
        numbers=set(_page_numbers(chunk))
        if usage.get('finish_reason')=='length':
            discarded|=numbers;splits+=1
            if len(chunk_cards)>1:
                mid=len(chunk_cards)//2;queue[0:0]=[(chunk,chunk_cards[:mid]),(chunk,chunk_cards[mid:])]
            else:
                failed|=numbers;errors.append('Output-capped single-question chunk requires review');retry_work.append((chunk,chunk_cards))
        elif err or result is None:
            failed|=numbers;errors.append(err or ('budget or call cap reached' if usage else 'provider returned no response'))
            retry_work.append((chunk,chunk_cards))
            if budget.budget_reached or budget.halted or budget.used>=budget.max_calls:
                failed.update(n for remaining,_ in queue for n,_ in remaining);retry_work.extend(queue);queue=[];halted=True
        else:
            ids=[r.get('question_id') for r in result.get('answers',[])]
            expected={card['question_id'] for card in chunk_cards}
            if set(ids)!=expected or len(ids)!=len(expected):
                valid=[r for r in result.get('answers',[]) if r.get('question_id') in expected]
                if valid:successful.append({'call_id':call_id,'pages':sorted(numbers),'answers':valid})
                failed|=numbers;errors.append('Response question IDs missing, duplicated or unexpected; valid partial answers retained')
                retry_work.append((chunk,chunk_cards))
            else:completed|=numbers;successful.append({'call_id':call_id,'pages':sorted(numbers),'answers':result['answers'],'json_repaired':bool(result.get('_json_repaired'))})
    coverage=sorted((discarded-completed)|failed)
    if not errors and not coverage:
        checkpoint('postprocess',[])
        for card in group_cards:
            qid=card['question_id']
            if any(a.get('question_id')==qid and a.get('success') for a in absence_checks):continue
            vals=[a for response in successful for a in response['answers'] if a.get('question_id')==qid]
            if not vals or not all(a.get('disclosure_status')=='Not Disclosed' for a in vals):continue
            keywords=list(dict.fromkeys((keyword_sets or {}).get(qid,[])))
            matches=[(n,t) for n,t in pages if sum(1 for term in keywords if term and term.casefold() in (t or '').casefold())>=2]
            if not matches:continue
            result2,usage2,err2,call_id2=_call_and_store(c,provider,run_id,plan,group,prompt_hash,system,matches,budget,[card])
            answers2=(result2 or {}).get('answers',[])
            valid2=len(answers2)==1 and answers2[0].get('question_id')==qid and not err2 and usage2.get('finish_reason')!='length'
            found=valid2 and answers2[0].get('disclosure_status') in {'Answered','Explicit No'}
            absence_checks.append({'question_id':qid,'pages':_page_numbers(matches),'call_id':call_id2,'success':bool(valid2),'recovered':bool(found)})
            if found:successful.append({'call_id':call_id2,'pages':_page_numbers(matches),'answers':answers2,'absence_recovery':True})
            checkpoint('postprocess',[])
            if not valid2:
                errors.append(f'Absence check for {qid} did not complete');failed.update(_page_numbers(matches))
            if budget.budget_reached or budget.halted or budget.used>=budget.max_calls:
                errors.append('Absence checks stopped at the budget or plan call limit');failed.update(selected);break
        coverage=sorted(set(coverage)|failed)
    if not errors and not coverage and not budget.budget_reached and not budget.halted:
        from .cdp_consolidated_models import verify_evidence
        for card in group_cards:
            qid=card['question_id']; vals=[a for r in successful for a in r['answers'] if a.get('question_id')==qid]
            if any(a.get('question_id')==qid and not a.get('error') for a in evidence_retries):continue
            excerpts=[];missing_evidence=False
            for val in vals:
                excerpts.extend(val.get('evidence',[]))
                if (val.get('answer') or val.get('plan_level')) and not val.get('evidence'):missing_evidence=True
                for item in val.get('items',[]):
                    excerpts.extend(item.get('evidence',[]))
                    if item.get('content') and not item.get('evidence'):missing_evidence=True
            if not missing_evidence and not any(not verify_evidence(e.get('evidence_excerpt',''),e.get('page'),page_text)[1] for e in excerpts):continue
            chunks=page_chunks(pages)
            if len(chunks)!=1:
                evidence_retries.append({'question_id':qid,'error':'Full document exceeds one safe retry chunk'});continue
            instruction='Provide exact verbatim evidence and page numbers for this previously extracted answer: '+json.dumps(vals,ensure_ascii=False)
            result3,usage3,err3,call_id3=_call_and_store(c,provider,run_id,plan,group,prompt_hash,system,chunks[0],budget,[card],instruction)
            answers3=(result3 or {}).get('answers',[])
            valid3=len(answers3)==1 and answers3[0].get('question_id')==qid and not err3 and usage3.get('finish_reason')!='length'
            evidence_retries.append({'question_id':qid,'call_id':call_id3,'answers':answers3 if valid3 else [],'error':(err3 or 'Evidence retry incomplete') if not valid3 else ''})
            checkpoint('postprocess',[])
            if budget.budget_reached or budget.halted or budget.used>=budget.max_calls:
                errors.append('Evidence checks stopped at the budget or plan call limit');break
    result={'question_ids':sorted(allowed),'responses':successful,'coverage_gap_pages':coverage,
            'output_cap_splits':splits,'errors':errors,'selected_pages':selected,'prompt_hash':prompt_hash,'absence_checks':absence_checks,
            'evidence_retries':evidence_retries,'pending_work':[{'chunk':chunk,'cards':cards} for chunk,cards in retry_work],
            'phase':'extract' if retry_work else 'postprocess'}
    status='done' if not errors and not coverage else 'partial'
    c.execute('INSERT OR REPLACE INTO cdp_group_jobs VALUES(?,?,?,?,?,?,?,?,datetime(\'now\'))',
              (run_id,plan['pdf_id'],plan['plan_id'],group,prompt_hash,status,json.dumps(result,ensure_ascii=False),json.dumps(selected)))
    c.commit();return result


def seed_approved_pilot(c,run_id,manifest,groups,source_run,model):
    """Reuse only exact prompt/document/page matches; leave source rows untouched."""
    source=c.execute('SELECT status,model,prompt_version FROM cdp_runs WHERE run_id=?',(source_run,)).fetchone()
    if not source or source['status']!='finished' or source['model']!=model or source['prompt_version']!=PROMPT_VERSION:
        raise ValueError('Approved pilot run/model/prompt version mismatch')
    pilot=json.loads((OUT/'consolidated_pilot_manifest.json').read_text(encoding='utf8'))
    indexed={p['pdf_id']:p for p in manifest};cap=open_cap_readonly();rows=[]
    try:
        for p in pilot:
            target=indexed.get(p['pdf_id'])
            if not target or target['plan_id']!=p['plan_id'] or target['source_file']!=p['source_file']:
                raise ValueError(f"Pilot identity mismatch: {p['pdf_id']}")
            pages=[r['page_no'] for r in cap.execute('SELECT page_no FROM pages WHERE plan_id=? ORDER BY page_no',(p['plan_id'],))]
            for group,cards in groups.items():
                prompt_hash=hashlib.sha256(system_prompt(cards).encode('utf8')).hexdigest()
                job=c.execute('SELECT * FROM cdp_group_jobs WHERE run_id=? AND pdf_id=? AND group_id=? AND prompt_hash=? AND status=\'done\'',
                              (source_run,p['pdf_id'],group,prompt_hash)).fetchone()
                if not job or json.loads(job['selected_pages'])!=pages:
                    raise ValueError(f"Pilot prompt or page coverage mismatch: {p['plan_id']} {group}")
                payload=json.loads(job['payload']);payload['source_run_id']=source_run
                rows.append((run_id,p['pdf_id'],p['plan_id'],group,prompt_hash,'done',json.dumps(payload,ensure_ascii=False),job['selected_pages']))
    finally:cap.close()
    for row in rows:
        c.execute('INSERT OR IGNORE INTO cdp_group_jobs VALUES(?,?,?,?,?,?,?,?,datetime(\'now\'))',row)
    c.commit();return len(rows)


def run(manifest,cards,keywords,budget_usd,max_calls,model,effort,concurrency=8,allow_corpus=False,reuse_from=None):
    if not manifest or (len(manifest)>10 and not allow_corpus):raise ValueError('Full corpus requires explicit full mode')
    groups=cards_by_group(cards)
    static_hashes={g:hashlib.sha256(system_prompt(v).encode('utf8')).hexdigest() for g,v in groups.items()}
    keyword_hash=hashlib.sha256(json.dumps(keywords,sort_keys=True,ensure_ascii=False).encode('utf8')).hexdigest()
    signature=json.dumps({'version':PROMPT_VERSION,'pdfs':[p['pdf_id'] for p in manifest],
                          'groups':static_hashes,'model':model,'effort':effort,
                          'keyword_hash':keyword_hash,'page_selection':'full-document-v2-650000-utf8-bytes'},sort_keys=True)
    run_id=hashlib.sha256(signature.encode()).hexdigest()[:24]
    c=SingleSQLiteWriter(OUT/'cdp.sqlite')
    prior=c.execute('SELECT status FROM cdp_runs WHERE run_id=?',(run_id,)).fetchone()
    if not prior:c.execute('INSERT INTO cdp_runs VALUES(?,datetime(\'now\'),NULL,?,?,?,?)',
                           (run_id,'running',model,budget_usd,PROMPT_VERSION));c.commit()
    if reuse_from:
        seeded=seed_approved_pilot(c,run_id,manifest,groups,reuse_from,model)
        print(f'Approved pilot groups verified for reuse: {seeded}',flush=True)
    spent=float(c.execute('SELECT COALESCE(SUM(cost_usd+COALESCE(uncertain_reserved_usd,0)),0) FROM cdp_calls WHERE run_id=?',(run_id,)).fetchone()[0])
    gate=RequestBudget(100000,spent,budget_usd,0,wait_for_reservations=True)
    provider=DeepSeekProvider(model,max_tokens=8192,reasoning_effort=effort,validation_retries=1,
                              pricing_mode='time-aware' if allow_corpus else 'peak')
    def process_plan_inner(plan):
        if gate.halted or gate.budget_reached:return
        cap=open_cap_readonly()
        try:pages,selection=selected_pages_for_plan(cap,plan['plan_id'],groups,keywords)
        finally:cap.close()
        selection=full_document_selection(pages,groups)
        previous=int(c.execute('SELECT COALESCE(SUM(api_calls),0) FROM cdp_calls WHERE run_id=? AND pdf_id=?',(run_id,plan['pdf_id'])).fetchone()[0])
        local=RequestBudget(max(0,max_calls-previous),0,0,0,parent=gate)
        for group,group_cards in groups.items():
            result=run_group(c,provider,run_id,plan,group,group_cards,pages,selection[group],local,keywords)
            print(f"plan={plan['plan_id']} group={group} calls={previous+local.used} splits={result['output_cap_splits']} gaps={result['coverage_gap_pages']}",flush=True)
            if gate.halted or gate.budget_reached:
                break
            if local.used>=local.max_calls:
                print(f"plan={plan['plan_id']} call cap reached; continuing with next plan",flush=True);break
    def process_plan(plan):
        try:process_plan_inner(plan)
        except Exception as exc:
            gate.halt(f"Unexpected {type(exc).__name__} while processing {plan['plan_id']}; inspect log and resume")
            print(f"STOP: unexpected {type(exc).__name__} in plan {plan['plan_id']}: {exc}",flush=True)
    with ThreadPoolExecutor(max_workers=max(1,concurrency),thread_name_prefix='cdp-plan') as pool:
        list(pool.map(process_plan,manifest))
    stopped=(gate.stop_reason or 'Run budget reached') if gate.halted or gate.budget_reached else None
    completed=int(c.execute("SELECT COUNT(*) FROM cdp_group_jobs WHERE run_id=? AND status='done'",(run_id,)).fetchone()[0])
    status='stopped' if stopped else ('finished' if completed==len(manifest)*len(groups) else 'partial')
    c.execute('UPDATE cdp_runs SET status=?,ended_at=datetime(\'now\') WHERE run_id=?',(status,run_id));c.commit()
    summary=dict(c.execute('SELECT COALESCE(SUM(api_calls),0) calls,COALESCE(SUM(retries),0) retries,COALESCE(SUM(cost_usd),0) cost FROM cdp_calls WHERE run_id=?',(run_id,)).fetchone())
    summary.update(run_id=run_id,status=status,stop_reason=stopped);c.close()
    print(json.dumps(summary,ensure_ascii=False));return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--provider',choices=['deepseek'],required=True);p.add_argument('--effort',choices=['none'],required=True)
    p.add_argument('--budget-usd',type=float,required=True);p.add_argument('--max-calls-per-plan',type=int,required=True)
    p.add_argument('--limit',type=int,default=10);p.add_argument('--model',default='deepseek-flash')
    p.add_argument('--concurrency',type=int,default=8)
    p.add_argument('--full',action='store_true');p.add_argument('--reuse-run')
    args=p.parse_args()
    if args.full:
        if not args.reuse_run:raise ValueError('Full mode requires an approved pilot run to reuse')
        from .cdp_full import load_or_create_manifest
        manifest=load_or_create_manifest()
    else:manifest=json.loads((OUT/'consolidated_pilot_manifest.json').read_text(encoding='utf8'))[:args.limit]
    cards=json.loads((OUT/'consolidated_question_cards_prepared.json').read_text(encoding='utf8'))['cards']
    keywords=json.loads((OUT/'keywords_draft.json').read_text(encoding='utf8'))
    print(json.dumps(estimate(manifest,cards,keywords),ensure_ascii=False),flush=True)
    run(manifest,cards,keywords,args.budget_usd,args.max_calls_per_plan,args.model,args.effort,args.concurrency,args.full,args.reuse_run)


if __name__=='__main__':main()
