"""Offline cost and call-volume estimate; never contacts a model endpoint."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from .llm import select_plan_pages, rank_risk_action_pages

PRICE = {"input_miss": 0.30, "input_hit": 0.006, "output": 1.20}  # USD / 1M tokens at peak; discount separately for reporting

def cost_usd(input_tokens, cached_tokens, output_tokens, discount=1.0):
    miss=max(0,int(input_tokens)-int(cached_tokens))
    return (miss*PRICE['input_miss']+int(cached_tokens)*PRICE['input_hit']+int(output_tokens)*PRICE['output'])/1_000_000*discount

def is_offpeak(now=None):
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None: now=now.replace(tzinfo=timezone.utc)
    now=now.astimezone(timezone.utc)
    return now.weekday()>=5 or not (1<=now.hour<4 or 6<=now.hour<10)

def _chunks(pages, budget):
    chunks=[]; size=0
    for p in pages:
        tokens=max(1,(len(str(p['text']))+2)//3)
        left=tokens
        while left:
            room=budget-size
            if room<=0: chunks.append(size); size=0; room=budget
            take=min(room,left); size+=take; left-=take
            if size>=budget: chunks.append(size); size=0
    if size: chunks.append(size)
    return chunks

def estimate(c, provider='deepseek', model='deepseek-flash', top_k=25, max_calls=12):
    rows=[]; total_pages=total_calls=total_in=total_out=0
    budget=12000 if provider=='deepseek' else 4500
    plans=c.execute('SELECT plan_id FROM plans WHERE status_inventory="done" ORDER BY plan_id').fetchall()
    for p in plans:
        pid=p['plan_id']; pages=c.execute('SELECT p.page_no,p.text,COALESCE(h.relevance,0) relevance,COALESCE(h.action_table,0) action_table,COALESCE(h.hits_json,"{}") hits FROM pages p LEFT JOIN page_hits h USING(plan_id,page_no) WHERE p.plan_id=? ORDER BY p.page_no',(pid,)).fetchall()
        if not pages: continue
        total_pages+=len(pages); pp=select_plan_pages(pages); rp,ap=rank_risk_action_pages(pages,top_k)
        plan_input=sum((len(str(x['text']))+2)//3 for x in pp); risk_chunks=_chunks(rp,budget); action_chunks=_chunks(ap,budget)
        task_inputs=[plan_input,sum(risk_chunks[:2]),sum(action_chunks)]
        calls=min(max_calls,1+(min(2,len(risk_chunks)) if rp else 0)+(len(action_chunks) if ap else 0))
        inp=min(sum(task_inputs),calls*budget); out=calls*512
        # Assume a conservative 50% cached static prefix after initial warming.
        cost=cost_usd(inp,inp//2,out)
        rows.append({'plan_id':pid,'pages':len(pages),'selected_plan':len(pp),'selected_risk':len(rp),'selected_action':len(ap),'chunks_calls':calls,'input_tokens_est':inp,'output_tokens_est':out,'cost_usd_est':cost})
    rows.sort(key=lambda x:x['cost_usd_est'],reverse=True)
    for x in rows: total_calls+=x['chunks_calls']; total_in+=x['input_tokens_est']; total_out+=x['output_tokens_est']
    cost=cost_usd(total_in,total_in//2,total_out)
    print(f'Estimate provider={provider} model={model} plans={len(rows)} pages={total_pages} calls={total_calls} input_tokens~{total_in} output_tokens~{total_out} cost_usd~{cost:.4f}')
    print('Peak pricing USD/1M tokens: cache_miss=0.30, cache_hit=0.006, output=1.20; assumes 50% input cache hits and 512 output tokens/call. Off-peak discount is reported separately.')
    print('Top 10 estimated plans:')
    for x in rows[:10]: print(json.dumps(x,ensure_ascii=False))
    return {'plans':len(rows),'pages':total_pages,'calls':total_calls,'input_tokens':total_in,'output_tokens':total_out,'cost_usd':cost}
