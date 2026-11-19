"""LLM provider adapters with per-request attempt and usage accounting."""
from __future__ import annotations

import json, os, random, time, threading
from datetime import datetime,timezone,timedelta
from typing import Any
from pydantic import ValidationError
from .text_utils import parse_truncated_json

PRICE = {"input_miss": 0.30, "input_hit": 0.006, "output": 1.20}  # USD / 1M tokens; peak rate, off-peak discount is reporting-only

class ProviderCallError(RuntimeError):
    def __init__(self, message, usage=None, raw_text="", stop_reason=None):
        super().__init__(message)
        self.usage = usage or {"api_calls": 0, "retries": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        self.raw_text = raw_text
        self.stop_reason = stop_reason

def _balance_stop_reason(exc):
    status=getattr(exc,'status_code',None)
    code=str(getattr(exc,'code','') or '').casefold()
    message=str(exc).casefold()
    if status==402 or code in {'insufficient_balance','insufficient_quota','balance_not_enough'} or any(x in message for x in ('insufficient balance','insufficient quota','account balance')):
        return 'provider reported insufficient account balance (HTTP 402 or equivalent)'
    return None

def _usage():
    return {"api_calls": 0, "responses_received": 0, "retries": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "reasoning_cost_usd": 0.0, "cost_usd": 0.0, "finish_reason": None, "model_id": None}

class RequestBudget:
    """API-call gate; per-plan gates can share a run-wide budget gate."""
    def __init__(self, max_calls, cumulative_cost, budget_usd, projected_call_cost, parent=None, wait_for_reservations=False):
        self.max_calls=max_calls; self.used=0; self.cumulative_cost=cumulative_cost; self.budget_usd=budget_usd
        self.projected_call_cost=projected_call_cost; self.observed_cost=0.0; self.reserved_cost=0.0; self.budget_reached=False; self.lock=threading.Lock(); self.parent=parent; self.halted=False; self.stop_reason=None
        self.condition=threading.Condition(self.lock)
        self.wait_for_reservations=wait_for_reservations
    def acquire(self, projected_cost=None):
        with self.lock:
            if self.halted or self.used>=self.max_calls: return False
            projection=self.projected_call_cost if projected_cost is None else projected_cost
            if self.parent is not None:
                if not self.parent.acquire(projection):
                    self.budget_reached=self.parent.budget_reached
                    self.halted=self.parent.halted
                    self.stop_reason=self.parent.stop_reason
                    return False
                self.used+=1
                return True
            deadline=time.monotonic()+30
            while self.cumulative_cost+self.observed_cost+self.reserved_cost+projection>self.budget_usd:
                if self.halted:return False
                if not self.wait_for_reservations:self.budget_reached=True;return False
                if self.cumulative_cost+self.observed_cost+projection>self.budget_usd or self.reserved_cost<=0:
                    self.budget_reached=True;return False
                remaining=deadline-time.monotonic()
                if remaining<=0:self.budget_reached=True;return False
                self.condition.wait(remaining)
            self.used+=1; self.reserved_cost+=projection; return True
    def observe(self, cost, projected_cost=None):
        if self.parent is not None:
            self.parent.observe(cost,projected_cost)
            return
        with self.lock:
            self.observed_cost+=cost
            if projected_cost is not None: self.reserved_cost=max(0,self.reserved_cost-projected_cost)
            self.condition.notify_all()
    def halt(self, reason):
        with self.lock:
            self.halted=True; self.budget_reached=True; self.stop_reason=reason; self.condition.notify_all()

def billing_factor(start,end):
    """Peak if any hour of the request crosses a weekday peak window (UTC).

    Chinese public holidays are conservatively treated as ordinary weekdays.
    """
    cursor=start.replace(minute=0,second=0,microsecond=0)
    while cursor<=end:
        if cursor.weekday()<5 and cursor.hour in {1,2,3,6,7,8,9}:return 1.0
        cursor+=timedelta(hours=1)
    return .5

def _add_response_usage(total, response, factor=1.0):
    total['responses_received']=int(total.get('responses_received',0))+1
    u=response.usage
    cached=int(getattr(u,"prompt_cache_hit_tokens",0) or getattr(getattr(u,"prompt_tokens_details",None),"cached_tokens",0) or 0)
    inp=int(getattr(u,"prompt_cache_miss_tokens",0) or max(0,int(getattr(u,"prompt_tokens",0) or 0)-cached))
    out=int(getattr(u,"completion_tokens",0) or 0)
    details=getattr(u,"completion_tokens_details",None)
    reasoning=int(getattr(details,"reasoning_tokens",0) or 0)
    total["input_tokens"]+=inp+cached; total["cached_input_tokens"]+=cached; total["output_tokens"]+=out
    reasoning_cost=reasoning*PRICE["output"]*factor/1_000_000
    total["reasoning_tokens"]+=reasoning; total["reasoning_cost_usd"]+=reasoning_cost
    delta=(inp*PRICE["input_miss"]+cached*PRICE["input_hit"]+out*PRICE["output"])*factor/1_000_000
    total["cost_usd"]+=delta
    total["model_id"]=getattr(response,"model",None)
    return inp,cached,out,delta

class OllamaProvider:
    def __init__(self, model: str, *, num_ctx: int = 8192, temperature: float = 0, seed: int = 42, max_tokens: int = 4096):
        self.model=model; self.options={"num_ctx":num_ctx,"temperature":temperature,"seed":seed,"num_predict":max_tokens}
    def extract(self, task: str, messages: list[dict[str,str]], schema_model: Any, call_budget: int | None=None, projected_cost: float | None=None):
        import requests
        total=_usage()
        if call_budget is not None and hasattr(call_budget,"acquire"):
            if not call_budget.acquire(projected_cost or 0):
                total["cap_reached"]=not call_budget.budget_reached; total["budget_reached"]=call_budget.budget_reached; return None,total,""
        elif call_budget is not None and call_budget<1: total["cap_reached"]=True; return None,total,""
        total["api_calls"]=1
        try:
            r=requests.post("http://localhost:11434/api/chat",json={"model":self.model,"stream":False,"format":schema_model.model_json_schema(),"options":self.options,"messages":messages},timeout=(10,600)); r.raise_for_status()
            body=r.json(); raw=body["message"]["content"]; total["finish_reason"]=body.get("done_reason"); total["model_id"]=body.get("model") or self.model
            total["input_tokens"]=int(body.get("prompt_eval_count") or 0)
            total["output_tokens"]=int(body.get("eval_count") or 0)
            if total["finish_reason"]=="length": return None,total,raw
            return parse_truncated_json(raw,schema_model),total,raw
        except Exception as e: raise ProviderCallError(str(e),total,locals().get("raw","")) from e

class DeepSeekProvider:
    def __init__(self, model: str="deepseek-flash", *, max_tokens: int=8192, reasoning_effort: str="none", validation_retries: int=3,pricing_mode: str='peak'):
        key=os.environ.get("DEEPSEEK_API_KEY")
        if not key: raise RuntimeError("DEEPSEEK_API_KEY is not set; set it in this PowerShell session before using --provider deepseek.")
        from openai import OpenAI
        if reasoning_effort not in {"none","low","high","max"}: raise ValueError("reasoning_effort must be none, low, high, or max")
        self.client=OpenAI(api_key=key,base_url="https://api.deepseek.com",max_retries=0); self.model=model; self.max_tokens=max_tokens; self.reasoning_effort=reasoning_effort
        self.validation_retries=validation_retries
        self.pricing_mode=pricing_mode
    def extract(self, task: str, messages: list[dict[str,str]], schema_model: Any, call_budget: int | None=None, projected_cost: float | None=None):
        total=_usage(); msgs=[dict(m) for m in messages]
        validation_retries=getattr(self,'validation_retries',3)
        # Caller supplies the JSON Schema in the stable task-level system message.
        last_raw=""; last_error=""
        for validation_try in range(validation_retries+1):
            validated=False
            for retry in range(6):
                try:
                    request_projection=projected_cost or 0
                    if call_budget is not None and hasattr(call_budget,"acquire"):
                        if not call_budget.acquire(request_projection):
                            total["cap_reached"]=not call_budget.budget_reached; total["budget_reached"]=call_budget.budget_reached
                            return None,total,last_raw
                    elif call_budget is not None and total["api_calls"]>=call_budget:
                        total["cap_reached"]=True; return None,total,last_raw
                    total["api_calls"]+=1
                    if total["api_calls"]>1: total["retries"]+=1
                    request_started=datetime.now(timezone.utc)
                    response=self.client.chat.completions.create(model=self.model,messages=msgs,temperature=0,max_tokens=self.max_tokens,reasoning_effort=self.reasoning_effort,response_format={"type":"json_object"})
                    factor=billing_factor(request_started,datetime.now(timezone.utc)) if getattr(self,'pricing_mode','peak')=='time-aware' else 1.0
                    choice=response.choices[0]; last_raw=choice.message.content or ""; _,_,_,delta=_add_response_usage(total,response,factor); total["finish_reason"]=choice.finish_reason
                    if call_budget is not None and hasattr(call_budget,"observe"): call_budget.observe(delta,request_projection)
                    if choice.finish_reason=="length": return None,total,last_raw
                    try:
                        result=parse_truncated_json(last_raw,schema_model)
                        return result,total,last_raw
                    except json.JSONDecodeError as e:
                        last_error=str(e); validated=True; break
                    except ValidationError as e:
                        missing_required=any(issue.get("type")=="missing" or (issue.get("type")=="string_too_short" and issue.get("loc",())[-1] in {"source_page","evidence_text","action_title","action_description"}) for issue in e.errors())
                        if not missing_required and validation_retries!=1:
                            raise ProviderCallError(str(e),total,last_raw) from e
                        last_error=str(e); validated=True; break
                except Exception as e:
                    if isinstance(e,ProviderCallError): raise
                    status=getattr(e,"status_code",None); retryable=status==429 or (status is not None and status>=500) or status is None
                    if not retryable or retry==5: raise ProviderCallError(str(e),total,last_raw,_balance_stop_reason(e)) from e
                    headers=getattr(getattr(e,"response",None),"headers",{}) or {}; ra=headers.get("Retry-After")
                    time.sleep(float(ra) if ra else min(30,2**retry)+random.random())
            if validated and validation_try<validation_retries:
                if validation_retries!=1:
                    msgs.extend([{"role":"assistant","content":last_raw},{"role":"user","content":"The JSON failed schema validation. Fix these validation errors and return the complete JSON object: "+last_error}])
        raise ProviderCallError(f"DeepSeek response failed schema validation after {validation_retries} retries: "+last_error,total,last_raw)

def get_provider(provider: str, model: str, **options):
    if provider=="ollama": return OllamaProvider(model,**options)
    if provider=="deepseek": return DeepSeekProvider(model,max_tokens=options.get("max_tokens",8192),reasoning_effort=options.get("reasoning_effort","none"))
    raise ValueError(f"Unknown provider: {provider}")
