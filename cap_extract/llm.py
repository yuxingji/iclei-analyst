from __future__ import annotations
import hashlib,json,re,time,sqlite3
from collections import Counter
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from pydantic import BaseModel,constr,create_model,model_validator
from typing import Literal
from .core import *
from .stages import selected
from .providers import get_provider
from .providers import ProviderCallError, RequestBudget
from .text_utils import truncate_at_word_boundary,parse_truncated_json

MODEL_DEFAULT='qwen2.5:7b-instruct'

class SerializedConnection:
    """A worker-owned SQLite connection with writes serialized across workers."""
    _WRITE_PREFIXES=('INSERT','UPDATE','DELETE','REPLACE','CREATE','ALTER','DROP')
    def __init__(self, connection, write_lock):
        self._connection=connection; self._write_lock=write_lock; self._owns_write_lock=False
    def execute(self, sql, parameters=()):
        if sql.lstrip().upper().startswith(self._WRITE_PREFIXES) and not self._owns_write_lock:
            self._write_lock.acquire(); self._owns_write_lock=True
        try:
            return self._connection.execute(sql,parameters)
        except Exception:
            self.rollback(); raise
    def executemany(self, sql, seq_of_parameters):
        if sql.lstrip().upper().startswith(self._WRITE_PREFIXES) and not self._owns_write_lock:
            self._write_lock.acquire(); self._owns_write_lock=True
        try:
            return self._connection.executemany(sql,seq_of_parameters)
        except Exception:
            self.rollback(); raise
    def commit(self):
        try: self._connection.commit()
        finally:
            if self._owns_write_lock:
                self._owns_write_lock=False; self._write_lock.release()
    def rollback(self):
        try: self._connection.rollback()
        finally:
            if self._owns_write_lock:
                self._owns_write_lock=False; self._write_lock.release()
    def close(self):
        self.rollback(); self._connection.close()
    def __getattr__(self,name): return getattr(self._connection,name)

class CappedTextModel(BaseModel):
    @model_validator(mode='before')
    @classmethod
    def clip_capped_fields(cls, data):
        if isinstance(data,dict):
            data=dict(data)
            for field,limit in (('evidence_text',200),('action_evidence_text',250),('risk_evidence_text',200),('action_description',500),('link_evidence',150)):
                if isinstance(data.get(field),str): data[field]=truncate_at_word_boundary(data[field],limit)
            evidence=data.get('field_evidence')
            if isinstance(evidence,dict):
                normalized={}
                for key,value in evidence.items():
                    if isinstance(value,str):
                        if value.strip().casefold() in {'not disclosed','none','null','nan',''}: continue
                        value={'evidence_text':value,'source_page':str(data.get('source_page') or ''),'source_section':str(data.get('source_section') or '')}
                    if isinstance(value,dict):
                        quote=value.get('evidence_text') or value.get('quote') or value.get('evidence')
                        if isinstance(quote,str) and quote.strip().casefold() in {'not disclosed','none','null','nan',''}: continue
                        if quote and not value.get('evidence_text'): value['evidence_text']=quote
                        value.setdefault('source_page',str(data.get('source_page') or ''))
                        value.setdefault('source_section',str(data.get('source_section') or ''))
                        normalized[key]=value
                data['field_evidence']=normalized
            for key,value in list(data.items()):
                if key!='field_evidence' and isinstance(value,str) and value.strip().casefold()=='not disclosed':
                    data.pop(key,None)
            relevance=data.get('climate_relevance')
            if isinstance(relevance,str) and relevance.strip().casefold() in {'adaptation','mitigation','both'}:
                data['climate_relevance']='Direct'
            qualitative={'low','medium','high','other','not stated','not disclosed','unclear'}
            for field in ('risk_value_numeric','target_value','baseline_value','baseline_year','target_year','cost_numeric_min','cost_numeric_max','financial_appraisal_value'):
                value=data.get(field)
                if isinstance(value,str):
                    lowered=value.strip().casefold()
                    if lowered in {'none','null','nan','not disclosed',''}: data[field]=None; continue
                    if field.startswith('cost_numeric') and lowered in qualitative:
                        if lowered in {'low','medium','high','other','not stated'} and not data.get('cost_qualitative'): data['cost_qualitative']=value.strip().title()
                        data[field]=None; continue
                    matches=list(re.finditer(r'-?\d[\d,]*(?:\.\d+)?\s*(million|billion|thousand|[mbk])?',value,re.I))
                    if matches:
                        scales={'million':1_000_000,'m':1_000_000,'billion':1_000_000_000,'b':1_000_000_000,'thousand':1_000,'k':1_000}
                        nums=[(float(re.search(r'-?\d[\d,]*(?:\.\d+)?',m.group(0)).group(0).replace(',','')), (m.group(1) or '').casefold()) for m in matches]
                        common_scale=next((scales[u] for _,u in reversed(nums) if u in scales),1)
                        values=[n*(scales[u] if u in scales else (common_scale if field.startswith('cost_numeric') else 1)) for n,u in nums]
                        data[field]=values[-1] if field.endswith('_max') else values[0]
        return data

def model_for(task,cols):
    config=schema_config()
    identity={'plan_id','action_id','risk_id','evidence_id','state','jurisdiction','gov_type','plan_year','plan_title','source_file'}
    numeric_fields={'prior_plan_year','target_value','target_year'}
    list_fields={'discovery_tag'}
    required={'evidence_text','source_page'} if task in {'plan','risk'} else {'action_title','action_description','action_orientation','source_page','evidence_text'}
    # Publication and active-period years are extracted by the dedicated
    # plan_years pass and copied in post-processing, never generated by the
    # ordinary Plan/Action calls.
    cols=[x for x in cols if x not in {'plan_publish_year','plan_publish_year_source','plan_active_year'}]
    if task=='risk':
        cols=list(config['schemas']['Risk Level'])
        required={'hazard_normalized','evidence_text','source_page'}
    if task=='action':
        cols=list(config['schemas']['Action Detail'])
        # Numeric cost values and currency are code-derived and are never model output.
        cols=[x for x in cols if x not in {'cost_numeric_min','cost_numeric_max','cost_currency'}]
    cols=[x for x in dict.fromkeys(list(cols)+list(required)) if x not in identity]
    fields={}
    for x in cols:
        if x in identity: continue
        allowed=config.get('fields',{}).get(x,{}).get('allowed')
        if x in {'hazards','hazard_normalized'}:
            import yaml
            allowed=list(yaml.safe_load((Path(__file__).parent/'dictionaries.yaml').read_text(encoding='utf-8')).get('hazards',{}))
        if x in numeric_fields: field_type=float
        elif x in {'hazards','mitigation_issue','linked_risk_keys','discovery_tag'}: field_type=list[Literal.__getitem__(tuple(allowed))] if x in {'hazards','mitigation_issue'} else list[str]
        elif x in {'field_evidence','evidence_mentions'}: field_type=dict[str,dict[str,str]] if x=='field_evidence' else list[dict[str,str]]
        elif isinstance(allowed,list): field_type=Literal.__getitem__(tuple(allowed))
        elif x in {'evidence_text','risk_evidence_text','unaddressed_risk_evidence'}: field_type=constr(strip_whitespace=True,min_length=1,max_length=200)
        elif x=='action_description': field_type=constr(strip_whitespace=True,min_length=1,max_length=500)
        elif x=='action_title': field_type=constr(strip_whitespace=True,min_length=1)
        else: field_type=str
        fields[x]=(field_type,...) if x in required else (field_type,[]) if x in list_fields else (field_type|None,None)
    item=create_model(task.title()+'Item',__base__=CappedTextModel,**fields)
    return create_model(task.title()+'Response',records=(list[item],[]))
def make_prompt(task,cols,dict_rows,controlled_rows,protocol_rows,schema,include_schema=True):
    config=schema_config()
    base=(Path(__file__).parent/'prompts'/f"{task}_{config['prompt_version']}.txt").read_text(encoding='utf-8')
    fields=[]
    for row in dict_rows[1:]:
        if len(row)>6 and row[2] in cols: fields.append(f'- {row[2]}: {row[4]}; allowed/format: {row[5]}; rule: {row[6]}')
    controls=[f'- {r[0]}: {r[1]} ({r[2] or ""})' for r in controlled_rows[1:] if len(r)>1 and str(r[0]) in cols]
    protocol=[f'- {r[1]} Rule: {r[2]}' for r in protocol_rows[1:] if len(r)>2 and not str(r[0]).startswith('Schema v2.')]
    evidence_rule='Include exact source evidence in evidence_text, cite the supplied page marker, and omit optional keys without evidence.'
    prompt=config.get('principle','')+'\n'+config.get('overarching_rules','')+'\n'+base+'\n'+config['prompts'].get(task,'')+'\nField guide:\n'+'\n'.join(fields)+'\nControlled values:\n'+'\n'.join(controls)+'\nProtocol:\n'+'\n'.join(protocol)+'\n'+evidence_rule+' Return JSON with a records array. Omit optional keys with no value; never emit placeholder strings such as Not Disclosed for empty fields; omit the key instead. Keep explicit N/A or Unclear only when source-supported. Do not emit literal None, null, or nan. Cite only supplied [PAGE n] markers. Evidence quotes must be verbatim and no longer than '+('250' if task=='action' else '200')+' characters. A funding source alone does not establish secured funding.'
    if task=='plan':
        prompt+=' Extract plan-level author, governance, monitoring/reporting, and policy integration from every supplied page. Use controlled values and preserve exact source wording. The plan-level implementation status must be supported by explicit plan-level evidence.'
    if task=='action': prompt+=' Require non-empty title, description, action orientation, source page, and evidence. Do not emit model-derived numeric cost values or currency; code derives these from cost_raw.'
    return prompt+'\n\nJSON Schema:\n'+json.dumps(schema,ensure_ascii=False,sort_keys=True) if include_schema else prompt

def token_estimate(text):
    return (len(text)+2)//3

def task_configs(cols,dicts,max_tokens,ollama_num_ctx=12288,ollama_output_reserve=2048):
    configs={}
    for task,sheet in (('plan','Plan Level'),('risk','Risk Level'),('action','Action Detail')):
        task_cols=cols.get(sheet,cols.get('Action Level',[]))
        response_model=model_for(task,task_cols); schema=response_model.model_json_schema()
        item_def=next((v for v in schema.get('$defs',{}).values() if 'properties' in v),{})
        task_cols=list(item_def.get('properties',{}))
        system=make_prompt(task,task_cols,dicts['Variable Dictionary'],dicts['Controlled Values'],dicts['Extraction Protocol'],schema,include_schema=False)
        system_tokens=token_estimate(system); schema_tokens=token_estimate(json.dumps(schema,ensure_ascii=False,sort_keys=True))
        risk_reserve=0
        available=ollama_num_ctx-system_tokens-schema_tokens-ollama_output_reserve-300-risk_reserve
        deepseek_input=12000-risk_reserve; deepseek_output=max_tokens
        deepseek_context=system_tokens+schema_tokens+deepseek_output+300+deepseek_input
        configs[task]={'response_model':response_model,'schema':schema,'system':system,'system_tokens':system_tokens,'schema_tokens':schema_tokens,'ollama_available':available,'deepseek_available':deepseek_input,'deepseek_context':deepseek_context,'risk_register_reserve':risk_reserve,'prompt_hash':hashlib.sha256(system.encode('utf-8')).hexdigest()}
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
        for pattern,weight in ((r'\$\s?[\d,]+(?:\.\d+)?',20),(r'\bcost\b',10),(r'\bpayback\b',25),(r'\bfunding\b',7),(r'\btime[- ]frame\b',6),(r'\bresponsible\b',6),(r'annual report',8),(r'\binventory\b',7),(r'implementation',7),(r'\bmonitoring\b',6)):
            action_score+=min(5,len(re.findall(pattern,text)))*weight
        action_score+=_table_bonus(text)
        risk_rank.append((float(risk_score),page['page_no'],page))
        action_rank.append((float(action_score),page['page_no'],page))
    return risk_rank,action_rank

def rank_risk_action_pages(pages,top_k):
    risk_rank,action_rank=score_risk_action_pages(pages)
    # Always give the risk task a bounded page set when a plan has usable text.
    # Pages with detected hazards rank first; ties (including zero-score pages)
    # use the general page relevance score so silent zero-coverage cannot occur.
    risk_scores={pno:score for score,pno,_ in risk_rank}
    risk_pages=sorted(pages,key=lambda p:(-risk_scores.get(p['page_no'],0),-float(_page_value(p,'relevance',0) or 0),p['page_no']))[:top_k]
    action_pages=[x[2] for x in sorted((x for x in action_rank if x[0]>0),key=lambda x:(-x[0],x[1]))[:top_k]]
    return risk_pages,action_pages

def rank_action_pages(pages,top_k):
    risk_rank,action_rank=score_risk_action_pages(pages)
    risk={pno:score for score,pno,_ in risk_rank}
    combined=[(score+risk.get(pno,0),pno,page) for score,pno,page in action_rank]
    return [row[2] for row in sorted((row for row in combined if row[0]>0),key=lambda x:(-x[0],x[1]))[:top_k]]

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
        replacing=task in (replace_tasks or set())
        if not new_records and old and not replacing: continue
        old_payload=json.loads(old[0]) if old and not replacing else {}
        old_records=old_payload.get('records',[])
        seen=set(); merged=[]
        for record in old_records+new_records:
            key=json.dumps(record,ensure_ascii=False,sort_keys=True)
            if key not in seen: seen.add(key); merged.append(record)
        out=old_payload|payload|{'records':merged}
        if 'unaddressed_local_risks' in payload or 'unaddressed_local_risks' in old_payload:
            unique={json.dumps(x,ensure_ascii=False,sort_keys=True):x for x in old_payload.get('unaddressed_local_risks',[])+payload.get('unaddressed_local_risks',[])}
            out['unaddressed_local_risks']=[unique[k] for k in unique]
        c.execute('INSERT OR REPLACE INTO extracted VALUES(?,?,?)',(pid,task,json.dumps(out,ensure_ascii=False)))

def _canonicalize_risk_records(records):
    from rapidfuzz.fuzz import token_set_ratio
    unique=[]
    def pages(value): return list(dict.fromkeys(re.findall(r'\d+',str(value or ''))))
    def labels(row):
        value=row.get('hazards') or []
        values=value if isinstance(value,list) else str(value).split(' | ')
        return {str(x).casefold() for x in values if x}
    def chain(row):
        fields=('affected_sector','affected_asset_or_infrastructure','affected_population','local_exposure_description','economic_consequence','municipal_revenue_consequence','municipal_expenditure_consequence','municipal_asset_consequence','service_delivery_consequence','future_capital_need')
        return ' '.join(str(row.get(k) or '') for k in fields).casefold()
    for original in sorted(records,key=lambda r:(min([int(x) for x in pages(r.get('source_page'))] or [10**9]),str(r.get('hazard_verbatim') or ''))):
        risk=dict(original); quote=risk.get('evidence_text'); page_refs=pages(risk.get('source_page'))
        mentions=list(risk.get('evidence_mentions') or [])
        if quote: mentions.append({'evidence_text':quote,'source_page':';'.join(page_refs),'source_section':str(risk.get('source_section') or '')})
        risk['evidence_mentions']=mentions
        rlabels=labels(risk); rchain=chain(risk)
        existing=next((x for x in unique if rlabels and rlabels.intersection(labels(x)) and (not rchain or not chain(x) or token_set_ratio(rchain,chain(x))>=90)),None)
        if existing is None:
            unique.append(risk); continue
        old_mentions=existing.get('evidence_mentions') or []
        for mention in mentions:
            if mention not in old_mentions: old_mentions.append(mention)
        existing['evidence_mentions']=old_mentions
        existing_pages=pages(existing.get('source_page'))
        for page in page_refs:
            if page not in existing_pages: existing_pages.append(page)
        existing['source_page']=';'.join(existing_pages)
        for field,value in risk.items():
            if field in {'risk_key','evidence_mentions','field_evidence','evidence_text','source_page','source_section'} or value in (None,''): continue
            old=existing.get(field); vals=[]
            for item in ([old] if old not in (None,'') else [])+([value] if not isinstance(value,list) else value):
                if item in (None,''): continue
                for part in (item if isinstance(item,list) else [item]):
                    part=str(part).strip()
                    if part and part not in vals: vals.append(part)
            if vals: existing[field]=vals if field=='hazards' else ' | '.join(vals)
        for field,entry in (risk.get('field_evidence') or {}).items():
            target=(existing.setdefault('field_evidence',{})).setdefault(field,{})
            if not isinstance(entry,dict): continue
            for k,v in entry.items():
                if not v: continue
                prior=target.get(k)
                if k=='source_page': target[k]=';'.join(dict.fromkeys(pages(str(prior or '')+';'+str(v))))
                elif k=='evidence_text': target[k]=' | '.join(dict.fromkeys(x for x in (prior,v) if x))[:200]
                elif not prior: target[k]=v
    for index,risk in enumerate(unique,1): risk['risk_key']=f'R{index}'
    return unique

def _parse_amount(raw):
    """Parse only numbers identified as money by currency or an explicit scale."""
    if not isinstance(raw,str) or not raw.strip(): return None,None,None
    text=raw.strip()
    currency='USD' if re.search(r'\$|\bUSD\b',text,re.I) else 'CAD' if re.search(r'\bCAD\b|C\$',text,re.I) else 'AUD' if re.search(r'\bAUD\b|A\$',text,re.I) else None
    num=r'\d[\d,]*(?:\.\d+)?'; scale=r'(million|billion|thousand|m|b|k)(?![A-Za-z])'
    multipliers={'million':1e6,'m':1e6,'billion':1e9,'b':1e9,'thousand':1e3,'k':1e3}; values=[]
    money=re.compile(r"(?:\b(?:USD|CAD|AUD)\b|C\$|A\$|\$)\s*(\d[\d,]*(?:\.\d+)?)(?:\s*(?:-|to)\s*\$?(\d[\d,]*(?:\.\d+)?))?(?:\s*(million|billion|thousand|m|b|k)(?![A-Za-z]))?",re.I)
    money_matches=list(money.finditer(text))
    for match in money_matches:
        mult=multipliers.get((match.group(3) or '').casefold(),1)
        values.extend((float(match.group(1).replace(',',''))*mult,float((match.group(2) or match.group(1)).replace(',',''))*mult))
    scaled=re.compile(r'(?<![A-Za-z\d])('+num+r')\s*('+scale+r')',re.I)
    excluded=r'\s*(?:meters?\b|metres?\b|kwh\b|kw\b|mwh\b|mw\b|tons?\b|years?\b|months?\b|weeks?\b|days?\b|hours?\b|units?\b|items?\b|buildings?\b|vehicles?\b|people\b|residents\b|percent\b|%)'
    for match in scaled.finditer(text):
        pos=match.end(2)
        if pos<len(text) and text[pos].isalpha(): continue
        if re.match(excluded,text[pos:],re.I): continue
        if re.search(r'(?:USD|CAD|AUD|EUR|GBP|JPY|C\$|A\$|\$|€|£|¥)\s*$',text[max(0,match.start()-12):match.start()],re.I): continue
        values.append(float(match.group(1).replace(',',''))*multipliers[match.group(2).casefold()])
    if len(money_matches)>1 and not any(match.group(2) for match in money_matches):
        return None,None,currency
    return (min(values),max(values),currency) if values else (None,None,currency)

def _reconcile_risk_links_core(plan_payload,risk_payload,action_payload,finalize_unaddressed=True):
    risks=_derive_financial_fields(_canonicalize_risk_records(risk_payload.get('records',[])),'risk'); actions=action_payload.get('records',[])
    risk_payload['records']=risks
    by_key={risk['risk_key']:risk for risk in risks}
    linked=set()
    mapping={'hazards':'hazards','hazard_verbatim':'hazard_verbatim','affected_sector':'affected_sector','affected_asset_or_infrastructure':'affected_asset_or_infrastructure','affected_population':'affected_population','local_exposure_description':'local_exposure_description','economic_consequence':'economic_consequence','municipal_revenue_consequence':'municipal_revenue_consequence','municipal_expenditure_consequence':'municipal_expenditure_consequence','municipal_asset_consequence':'municipal_asset_consequence','service_delivery_consequence':'service_delivery_consequence','future_capital_need':'future_capital_need','risk_quantified':'risk_quantified','risk_value_raw':'risk_value_raw','risk_value_numeric':'risk_value_numeric','risk_unit':'risk_unit','risk_currency':'risk_currency','risk_scenario':'risk_scenario','risk_time_horizon':'risk_time_horizon'}
    used_link_evidence=set()
    for action in actions:
        keys=list(dict.fromkeys(action.get('linked_risk_keys') or []))
        link_quote=str(action.get('link_evidence') or '').strip()
        action_quotes=[str(action.get('action_evidence_text') or '')]+[str(v.get('evidence_text') or '') for v in (action.get('field_evidence') or {}).values() if isinstance(v,dict)]
        norm=lambda s: re.sub(r'\s+',' ',s).strip().casefold()
        evidence_specific=bool(link_quote and any(norm(link_quote) in norm(quote) for quote in action_quotes if quote))
        if not evidence_specific: keys=[]
        keys=[key for key in keys if (key,norm(link_quote)) not in used_link_evidence]
        for key in keys: used_link_evidence.add((key,norm(link_quote)))
        matches=[by_key[k] for k in keys if k in by_key]
        if not matches: action.pop('linked_risk_keys',None); continue
        linked.update(r['risk_key'] for r in matches)
        action['linked_risk_keys']=keys
        for source,target in mapping.items():
            values=[]
            for value in [action.get(target)]+[risk.get(source) for risk in matches]:
                if value in (None,'','Not Disclosed'): continue
                items=value if isinstance(value,list) else [value]
                for item in items:
                    item=str(item).strip()
                    if item and item not in values: values.append(item)
            if values:
                # Keep numeric columns numeric when several linked risks disagree.
                # Their source wording remains available in risk_value_raw and Evidence Log.
                if target=='risk_value_numeric':
                    numeric=[]
                    for value in values:
                        try: numeric.append(float(value))
                        except (TypeError,ValueError): pass
                    action[target]=numeric[0] if numeric and all(x==numeric[0] for x in numeric) and len(numeric)==len(values) else None
                else:
                    action[target]=' | '.join(values)
        evidence=[]; pages=[]
        field_evidence=dict(action.get('field_evidence') or {})
        for risk in matches:
            quote=risk.get('evidence_text')
            if quote and quote not in evidence: evidence.append(quote)
            for page in re.findall(r'\d+',str(risk.get('source_page') or '')):
                if page not in pages: pages.append(page)
            for field,entry in (risk.get('field_evidence') or {}).items():
                mapped=field_evidence.setdefault(field,{})
                if isinstance(entry,dict):
                    for k,v in entry.items():
                        if v and not mapped.get(k): mapped[k]=v
        if evidence:
            action['risk_evidence_text']=' | '.join(evidence)[:200]
            field_evidence['risk_evidence_text']={'evidence_text':evidence[0][:200],'source_page':';'.join(pages)}
        if pages: action['risk_evidence_page']=';'.join(pages)
        if field_evidence: action['field_evidence']=field_evidence
        physical=bool(action.get('hazards'))
        mitigation=bool(action.get('mitigation_issue'))
        if physical:
            action['environmental_issue_addressed']=' | '.join(action['hazards'])
        if physical and mitigation: action['issue_type']='Both'
        elif physical: action['issue_type']='Physical Risk'
        elif mitigation: action['issue_type']='Transition / Mitigation Issue'
        action.pop('link_evidence',None)
    _derive_financial_fields(actions,'action')
    unaddressed=[risk for risk in risks if risk['risk_key'] not in linked]
    action_payload['average_links_per_risk']=sum(len(set(a.get('linked_risk_keys') or [])) for a in actions)/len(risks) if risks else 0.0
    plan_records=plan_payload.setdefault('records',[])
    supplied=set(action_payload.get('risk_register_keys_supplied',[]))
    register_complete=supplied==set(by_key) and risk_payload.get('coverage_complete',True)
    if finalize_unaddressed and register_complete and plan_records:
        plan=plan_records[0]
        if unaddressed:
            hazard_values=[]
            for risk in unaddressed:
                vals=risk.get('hazards') or []
                for hazard in (vals if isinstance(vals,list) else str(vals).split(' | ')):
                    if hazard and hazard not in hazard_values: hazard_values.append(hazard)
            plan['unaddressed_local_risks']=' | '.join(hazard_values)
            plan['unaddressed_risk_evidence']=' | '.join(f"{r.get('evidence_text','')} (p.{r.get('source_page','')})" for r in unaddressed)[:200]
            plan['unaddressed_risk_keys']=[r['risk_key'] for r in unaddressed]
        else:
            plan.pop('unaddressed_local_risks',None); plan.pop('unaddressed_risk_evidence',None)
            plan.pop('unaddressed_risk_keys',None)
    return risks,linked,unaddressed

_legacy_reconcile_risk_links = _reconcile_risk_links_core
def _derive_financial_fields(records,kind):
    raw_field='cost_raw' if kind=='action' else 'risk_value_raw'
    low_field,high_field,currency_field=('cost_numeric_min','cost_numeric_max','cost_currency') if kind=='action' else ('risk_value_numeric','risk_value_numeric','risk_currency')
    for row in records:
        raw=str(row.get(raw_field) or '')
        if kind=='action':
            lowered=raw.casefold().strip()
            finance_terms=bool(re.search(r'\b(?:savings?|avoided\s+cost|revenue|bill savings?)\b',raw,re.I))
            if finance_terms:
                row['financial_appraisal']=raw
                row['cost_raw']=None
                row['cost_type']='Not Disclosed'
                row.pop(low_field,None); row.pop(high_field,None); row.pop(currency_field,None)
                continue
            if not lowered:
                row['cost_type']='Not Disclosed'
                row.pop(low_field,None); row.pop(high_field,None); row.pop(currency_field,None)
                continue
            if re.search(r'\b(?:no additional cost|no new funding required|within (?:the )?existing budget|no incremental (?:cost|expenditure))\b',raw,re.I):
                row['cost_type']='No Additional Cost'
                row.pop(low_field,None); row.pop(high_field,None); row.pop(currency_field,None)
                continue
            if re.search(r'\b(?:existing staff time|staff time|staff resources?|existing departmental resources?|in-kind staff)\b',raw,re.I):
                row['cost_type']='Staff/Existing Resources'
                row.pop(low_field,None); row.pop(high_field,None); row.pop(currency_field,None)
                continue
            low,high,currency=_parse_amount(raw)
            if low is not None:
                row['cost_type']='Monetary Range' if re.search(r'\d\s*(?:-|–|—|to)\s*(?:\$\s*)?\d',raw,re.I) else 'Exact Monetary'
                row['cost_numeric_min']=low; row['cost_numeric_max']=high
                if currency: row['cost_currency']=currency
                payback=re.search(r'\b\d+(?:\.\d+)?\s*[- ]?year(?:s)?\s+payback\b|\bpayback\s+(?:period\s+of\s+)?\d+(?:\.\d+)?\s*[- ]?years?\b',raw,re.I)
                if payback and not row.get('financial_appraisal'): row['financial_appraisal']=payback.group(0)
                row.pop('cost_qualitative',None)
                continue
            if re.fullmatch(r'\s*(?:low|medium|high|\$+|not stated)\s*',raw,re.I):
                row['cost_type']='Qualitative'; row['cost_qualitative']=raw.strip()
                row.pop(low_field,None); row.pop(high_field,None); row.pop(currency_field,None)
                continue
            row['cost_type']='Unclear'
            row.pop(low_field,None); row.pop(high_field,None); row.pop(currency_field,None)
            continue
        finance_terms=False
        appraisal_values=[]; explicit_costs=[]
        currency=None
        if finance_terms:
            token=re.compile(r'(?:\b(?:USD|CAD|AUD)\b|C\$|A\$|\$)\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:-|to)\s*\$?\d[\d,]*(?:\.\d+)?)?(?:\s*(?:million|billion|thousand|m|b|k)(?![A-Za-z]))?',re.I)
            matches=list(token.finditer(raw))
            for index,match in enumerate(matches):
                amount=_parse_amount(match.group(0))
                if amount[0] is None: continue
                nearby=[]
                for label in re.finditer(r'\b(?:savings?|avoided\s+cost|revenue|project\s+costs?|costs?|net)\b',raw[max(0,match.start()-45):min(len(raw),match.end()+45)],re.I):
                    absolute_start=max(0,match.start()-45)+label.start(); absolute_end=max(0,match.start()-45)+label.end()
                    distance=match.start()-absolute_end if absolute_end<=match.start() else absolute_start-match.end() if absolute_start>=match.end() else 0
                    nearby.append((distance,label.group(0).casefold()))
                role=min(nearby)[1] if nearby else ''
                if re.search(r'savings?|avoided\s+cost|revenue',role): appraisal_values.append(amount)
                if re.search(r'^(?:project\s+)?costs?$',role): explicit_costs.append(amount)
            if explicit_costs:
                lows=[item[0] for item in explicit_costs if item[0] is not None]
                highs=[item[1] for item in explicit_costs if item[1] is not None]
                low=min(lows) if lows else None; high=max(highs) if highs else None
                currency=next((item[2] for item in explicit_costs if item[2]),None)
            else:
                low=high=None
            if appraisal_values:
                distinct={(item[0],item[1],item[2]) for item in appraisal_values}
                if len(distinct)==1:
                    low_app,high_app,currency_app=next(iter(distinct))
                    row['financial_appraisal_value']=low_app if low_app==high_app else f'{low_app:g}-{high_app:g}'
                if re.search(r'\bavoided\s+cost\b',raw,re.I): row['financial_appraisal_type']='Avoided Cost'
                elif re.search(r'\brevenue\b',raw,re.I): row['financial_appraisal_type']='Revenue'
                elif re.search(r'\bbill\s+savings?\b',raw,re.I): row['financial_appraisal_type']='Utility Bill Savings'
                else: row['financial_appraisal_type']='Savings'
                horizon=re.search(r'\b(annual(?:ly)?|yearly|every year|per year|\d+\s*[- ]year(?:s)?)\b',raw,re.I)
                if horizon: row['financial_appraisal_horizon']=horizon.group(1)
            if low is None:
                row.pop(low_field,None); row.pop(high_field,None); row.pop(currency_field,None)
        else:
            low,high,currency=_parse_amount(raw)
        if low is not None:
            row[low_field]=low
            row[high_field]=high
        if currency: row[currency_field]=currency
        evidence=row.get('field_evidence')
        evidence_key='cost_raw' if kind=='action' else 'risk_value_raw'
        if isinstance(evidence,dict) and evidence.get(evidence_key):
            for derived in ((low_field,high_field,currency_field) if kind=='action' else (low_field,currency_field)):
                if row.get(derived) is not None: evidence[derived]=dict(evidence[evidence_key])
            if kind=='action' and finance_terms:
                for derived in ('financial_appraisal_type','financial_appraisal_value','financial_appraisal_horizon'):
                    if row.get(derived) is not None: evidence[derived]=dict(evidence[evidence_key])
            if kind=='action' and re.search(r'\bpayback\b',str(row.get('cost_raw') or ''),re.I):
                for derived in ('financial_appraisal_type','financial_appraisal_value','financial_appraisal_horizon'):
                    if row.get(derived) is not None: evidence[derived]=dict(evidence[evidence_key])
    return records

def _set_plan_schema_status(c,pid,schema_hash,status):
    c.execute('INSERT OR REPLACE INTO plan_schema_versions VALUES(?,?,?,datetime(\'now\'))',(pid,schema_hash,status))

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

def llm_stage(c,model=MODEL_DEFAULT,top_k=25,force=False,limit=None,only=None,num_ctx=12288,temperature=0,seed=42,max_calls_per_plan=15,provider_name='ollama',max_tokens=8192,budget_usd=10,offpeak_discount=1.0,concurrency=8,output_reserve=2048,effort='none',tasks_filter=None,risk_missing_only=False):
    import requests
    if top_k<1 or max_calls_per_plan<1 or concurrency<1 or num_ctx<1 or output_reserve<1 or max_tokens<1: raise ValueError('--top-k, --max-calls-per-plan, --concurrency, --num-ctx, --output-reserve and --max-tokens must be positive integers')
    if tasks_filter is not None and (not tasks_filter or not set(tasks_filter).issubset({'plan','risk','action'})): raise ValueError('--tasks must contain one or more of plan, risk, action')
    if risk_missing_only and tasks_filter is not None and 'risk' not in tasks_filter: raise ValueError('--risk-missing-only requires risk in --tasks')
    if effort not in {'none','low','high','max'}: raise ValueError('--effort must be none, low, high, or max')
    if budget_usd<=0 or not 0<=offpeak_discount<=1: raise ValueError('--budget-usd must be positive and --offpeak-discount must be between 0 and 1')
    from .estimate import cost_usd
    cols,dicts=template_data(); n=0; start=time.time(); todo=selected(c,only,limit)
    config=schema_config()
    print(f'LLM CONCURRENCY plan_workers={concurrency}; chunk_workers=1; sqlite_writer=serialized_lock',flush=True)
    task_config=task_configs(cols,dicts,max_tokens,num_ctx,output_reserve)
    v22_risk_cached_plans=set()
    if risk_missing_only:
        for cached_row in c.execute("SELECT payload FROM exports WHERE table_name='Risk Level'"):
            try:
                cached_plan=json.loads(cached_row[0]).get('plan_id')
                if cached_plan: v22_risk_cached_plans.add(cached_plan)
            except (TypeError,ValueError):
                continue
        # In refresh mode, reuse only responses produced with this exact static
        # risk prompt hash. Legacy exports stay cached unless explicitly forced.
        current_risk_hash=task_config['risk']['prompt_hash']
        current_hash_risk_plans={r[0] for r in c.execute("SELECT DISTINCT plan_id FROM llm_raw WHERE task='risk' AND provider=? AND model=? AND prompt_hash=? AND response IS NOT NULL AND COALESCE(error,'')=''",(provider_name,model,current_risk_hash))}
        stored_risk_plans=set(v22_risk_cached_plans)|current_hash_risk_plans
        text_plans={r[0] for r in c.execute("SELECT DISTINCT plan_id FROM pages WHERE TRIM(COALESCE(text,''))<>''")}
        before=len(todo)
        todo=[p for p in todo if p['plan_id'] in text_plans and p['plan_id'] not in stored_risk_plans]
        print(f'LLM RISK CACHE legacy_v2.2={len(v22_risk_cached_plans)}; current_prompt_hash={current_risk_hash}; current_hash_plans={len(current_hash_risk_plans)}; usable_text={len(text_plans)}; risk_missing_only plans={len(todo)} (filtered {before-len(todo)})',flush=True)
    schema_hash=hashlib.sha256(json.dumps({'version':config['version'],'schemas':config['schemas'],'prompts':config['prompts'],'static_prompt_hashes':{task:cfg['prompt_hash'] for task,cfg in task_config.items()}},ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()
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
    previous=c.execute("SELECT * FROM llm_runs WHERE provider=? AND requested_model=? AND schema_hash=? AND status IN ('running','budget','interrupted') ORDER BY started_at DESC LIMIT 1",(provider_name,model,schema_hash)).fetchone()
    if previous:
        run_id=previous['run_id']; run_cost=float(previous['cost_usd'] or 0); initial_actual_model=previous['actual_model']
        c.execute("UPDATE llm_runs SET ended_at=NULL,status='running' WHERE run_id=?",(run_id,))
    else:
        run_id=uuid.uuid4().hex; run_cost=0.0; initial_actual_model=None
        c.execute('INSERT INTO llm_runs(run_id,provider,requested_model,started_at,status,schema_hash) VALUES(?,?,?,datetime(\'now\'),?,?)',(run_id,provider_name,model,'running',schema_hash))
    c.commit(); budget_stop=run_cost>=budget_usd
    if budget_stop:
        c.execute("UPDATE llm_runs SET ended_at=datetime('now'),status='budget' WHERE run_id=?",(run_id,)); c.commit()
        print(f'LLM BUDGET STOP before work: cumulative=${run_cost:.6f}, limit=${budget_usd:.2f}',flush=True); return 0
    db_write_lock=threading.RLock()
    run_budget=RequestBudget(2**31-1,run_cost,budget_usd,0.0)
    def process_plan(ix,plan):
        raw_connection=sqlite3.connect(DB,timeout=60); raw_connection.row_factory=sqlite3.Row
        c_plan=SerializedConnection(raw_connection,db_write_lock)
        try: return process_plan_body(ix,plan,c_plan)
        finally: c_plan.close()
    def process_plan_body(ix,plan,c_plan):
        plan_actual_model=initial_actual_model
        pid=plan['plan_id']; plan_start=time.time(); calls_made=0; retries=0; split_calls=0; pages_total=0; selected_counts={'plan':0,'action':0}; total_input=total_cached=total_output=total_reasoning=total_reasoning_cost=total_cost=plan_retries=0; discarded_pages={}; successful_response_pages={}; failed_chunks=[]; reused_raw_chunks=0; account_stop_reason=None; budget_stop=False
        def coverage_gaps_by_task():
            return {task:sorted(pages-successful_response_pages.get(task,set())) for task,pages in discarded_pages.items() if pages-successful_response_pages.get(task,set())}
        status=c_plan.execute('SELECT status_llm FROM plans WHERE plan_id=?',(pid,)).fetchone()
        stored_schema=c_plan.execute('SELECT schema_hash FROM plan_schema_versions WHERE plan_id=?',(pid,)).fetchone()
        schema_mismatch=not stored_schema or stored_schema[0]!=schema_hash
        if schema_mismatch:
            old_action=c_plan.execute('SELECT payload FROM extracted WHERE plan_id=? AND task=?',(pid,'action')).fetchone()
            old_risk=c_plan.execute('SELECT payload FROM extracted WHERE plan_id=? AND task=?',(pid,'risk')).fetchone()
            action_rows=len(json.loads(old_action[0]).get('records',[])) if old_action else 0
            risk_rows=len(json.loads(old_risk[0]).get('records',[])) if old_risk else 0
            c_plan.execute('INSERT OR IGNORE INTO schema_baselines VALUES(?,?,?,?,?,datetime(\'now\'))',(pid,config['version'],stored_schema[0] if stored_schema else 'unknown',action_rows,risk_rows)); c_plan.commit()
        if force or schema_mismatch:
            prior_hash=stored_schema[0] if stored_schema else 'unknown'
            baseline=c_plan.execute('SELECT schema_version FROM schema_baselines WHERE plan_id=? ORDER BY created_at DESC LIMIT 1',(pid,)).fetchone()
            if stored_schema and baseline:
                prior_hash=f"v{baseline[0]}:{stored_schema[0]}"
            elif not stored_schema:
                old_plan=c_plan.execute('SELECT payload FROM extracted WHERE plan_id=? AND task=?',(pid,'plan')).fetchone()
                old_action=c_plan.execute('SELECT payload FROM extracted WHERE plan_id=? AND task=?',(pid,'action')).fetchone()
                plan_sample=json.loads(old_plan[0]).get('records',[{}])[0] if old_plan and json.loads(old_plan[0]).get('records') else {}
                action_sample=json.loads(old_action[0]).get('records',[{}])[0] if old_action and json.loads(old_action[0]).get('records') else {}
                prior_version='v3.0' if 'plan_scope_controlled' in plan_sample or 'climate_relevance' in action_sample else 'v2.2' if 'plan_scope' in plan_sample or 'M_or_A' in action_sample else 'unknown'
                prior_hash=f'{prior_version}:{hashlib.sha256(json.dumps([plan_sample,action_sample],sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:16]}'
            for old_task in task_config:
                old_payload=c_plan.execute('SELECT payload FROM extracted WHERE plan_id=? AND task=?',(pid,old_task)).fetchone()
                if old_payload:
                    c_plan.execute('INSERT OR IGNORE INTO extracted_archive VALUES(?,?,?,?,datetime(\'now\'))',(pid,prior_hash,old_task,old_payload[0]))
            c_plan.commit()
        if not risk_missing_only and not force and not schema_mismatch and (done(c_plan,pid,'llm') or (status and status[0]=='capped')):
            print(f'LLM SKIP {pid}: {status[0] if status and status[0]=="capped" else "already done"}',flush=True); return 0
        replace_tasks=set(task_config) if schema_mismatch or force else set()
        mark(c_plan,pid,'llm','running'); c_plan.execute("UPDATE plans SET status_llm='running',failed_pages=NULL WHERE plan_id=?",(pid,)); c_plan.commit()
        try:
            pages=c_plan.execute('SELECT p.page_no,p.text,COALESCE(h.relevance,0) relevance,COALESCE(h.action_table,0) action_table,COALESCE(h.hits_json,\'{}\') hits FROM pages p LEFT JOIN page_hits h USING(plan_id,page_no) WHERE p.plan_id=? ORDER BY p.page_no',(pid,)).fetchall()
            if not pages: raise ValueError('No extracted pages; run text and rules first')
            pages_total=len(pages)
            plan_pages=select_plan_pages(pages)
            risk_pages,action_pages=rank_risk_action_pages(pages,top_k)
            selected_counts={'plan':len(plan_pages),'risk':len(risk_pages),'action':len(action_pages)}
            risk_scores,action_scores=score_risk_action_pages(pages)
            risk_score_map={pno:score for score,pno,_ in risk_scores}; action_score_map={pno:score for score,pno,_ in action_scores}
            score_text=lambda rows:', '.join(f"{pno}:{score:g}" for score,pno,_ in sorted(rows,key=lambda x:(-x[0],x[1])))
            print(f'LLM SELECT {pid}: pages_total={pages_total}, plan={len(plan_pages)} pages={[p["page_no"] for p in plan_pages]}, risk={len(risk_pages)} pages={[p["page_no"] for p in risk_pages]}, action={len(action_pages)} pages={[p["page_no"] for p in action_pages]}, top_k={top_k}',flush=True)
            print(f'LLM RANK {pid}: plan={score_text([(plan_page_score(p),p["page_no"],p) for p in pages])}; risk={score_text(risk_scores)}; action={score_text(action_scores)}',flush=True)
            selected_tasks=set(tasks_filter) if tasks_filter is not None else set(task_config)
            reused_v22_risk=risk_missing_only and 'risk' in selected_tasks and pid in v22_risk_cached_plans
            if reused_v22_risk: selected_tasks.remove('risk')
            replacement_tasks=set(selected_tasks) if force or schema_mismatch else (set(selected_tasks) if risk_missing_only else set())
            new_results={task:{'records':[]} for task in replacement_tasks}; capped=False; risk_page_coverage_complete=True; call_gate=RequestBudget(max_calls_per_plan,0,budget_usd,0.0,parent=run_budget)
            task_pages_all=(('plan',plan_pages),('risk',risk_pages),('action',action_pages))
            task_pages_all=tuple((t,p) for t,p in task_pages_all if t in selected_tasks)
            for task,task_pages in task_pages_all:
                if not task_pages: continue
                cols_task=cols['Plan Level'] if task=='plan' else cols['Action Detail'] if task=='action' else cols['Risk Level']
                cfg=task_config[task]; response_model=cfg['response_model']; schema=cfg['schema']
                prompt_base=cfg['system']+'\n\nJSON Schema:\n'+json.dumps(schema,ensure_ascii=False,sort_keys=True)
                prompt_hash=cfg['prompt_hash']
                c_plan.execute('INSERT OR REPLACE INTO llm_prompt_versions VALUES(?,?,?)',(run_id,task,prompt_hash)); c_plan.commit()
                available=cfg['ollama_available'] if provider_name=='ollama' else cfg['deepseek_available']
                content_budget=available if provider_name=='ollama' else min(12000,available)
                risk_context=''; risk_context_hash=''; supplied_risk_keys=[]
                char_budget=content_budget*3
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
                    all_chunks=list(chunks)
                    if task=='risk' and len(all_chunks)>3:
                        all_chunks=all_chunks[:3]; risk_page_coverage_complete=False
                        print(f'LLM RISK CHUNK CAP {pid}: selected_pages={len(task_pages)}, chunks_selected=3, chunks_required={len(chunks)}',flush=True)
                    chunk_queue=list(all_chunks)
                raw_rows=c_plan.execute('SELECT id,run_id,response,options FROM llm_raw WHERE plan_id=? AND task=? AND provider=? AND model=? AND prompt_hash=? ORDER BY id',(pid,task,provider_name,model,cfg['prompt_hash'])).fetchall()
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
                        payload=new_results.setdefault(task,{'records':[]}); payload['records'].extend(records)
                        if task=='action': payload.setdefault('unaddressed_local_risks',[]).extend(parsed.get('unaddressed_local_risks',[]))
                        successful_response_pages.setdefault(task,set()).update(allowed_pages); remaining.discard(match_index); reused_raw_chunks+=1
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
                with ThreadPoolExecutor(max_workers=1) as pool:
                    while chunk_queue and not (budget_stop or capped):
                        batch=[]
                        while chunk_queue and len(batch)<1 and call_gate.used+len(batch)<max_calls_per_plan:
                            batch.append(chunk_queue.pop(0))
                        if not batch: capped=True; break
                        responses=list(pool.map(request_chunk,batch))
                        splits=[]
                        for ch,result,usage,raw,err,duration,stop_reason in responses:
                            usage=usage or {}; used=usage.get('api_calls',1 if not err else 0); calls_made+=used
                            retries_used=usage.get('retries',0); plan_retries+=retries_used
                            total_input+=usage.get('input_tokens',0); total_cached+=usage.get('cached_input_tokens',0); total_output+=usage.get('output_tokens',0); total_reasoning+=usage.get('reasoning_tokens',0); total_reasoning_cost+=usage.get('reasoning_cost_usd',0)
                            uncached=usage.get('input_tokens',0)-usage.get('cached_input_tokens',0); request_cost=usage.get('cost_usd',0); total_cost+=request_cost
                            plan_actual_model=usage.get('model_id') or plan_actual_model
                            stored_error=err or None
                            call_options=options|usage|{'chunk_pages':sorted(chunk_page_numbers(ch)),'chunk_sha256':hashlib.sha256('\n'.join(ch).encode('utf-8')).hexdigest()}
                            c_plan.execute('INSERT INTO llm_raw(plan_id,task,model,prompt_hash,created_at,duration,response,error,options,run_id,provider,actual_model,input_cached,input_uncached,output_tokens,reasoning_tokens,reasoning_cost_usd,cost_usd,retry_count,api_calls) VALUES(?,?,?,?,datetime(\'now\'),?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(pid,task,model,prompt_hash,duration,raw,stored_error,json.dumps(call_options),run_id,provider_name,usage.get('model_id'),usage.get('cached_input_tokens',0),uncached,usage.get('output_tokens',0),usage.get('reasoning_tokens',0),usage.get('reasoning_cost_usd',0),request_cost,retries_used,used))
                            c_plan.execute('UPDATE llm_runs SET actual_model=COALESCE(?,actual_model),input_cached=input_cached+?,input_uncached=input_uncached+?,output_tokens=output_tokens+?,reasoning_tokens=reasoning_tokens+?,reasoning_cost_usd=reasoning_cost_usd+?,cost_usd=cost_usd+?,cost_discounted_usd=cost_discounted_usd+?,api_calls=api_calls+?,retries=retries+? WHERE run_id=?',(plan_actual_model,usage.get('cached_input_tokens',0),uncached,usage.get('output_tokens',0),usage.get('reasoning_tokens',0),usage.get('reasoning_cost_usd',0),request_cost,request_cost*offpeak_discount,used,retries_used,run_id)); c_plan.commit()
                            if usage.get('budget_reached'): budget_stop=True
                            if usage.get('cap_reached'): capped=True
                            if stop_reason:
                                account_stop_reason=stop_reason; run_budget.halt(stop_reason); budget_stop=True
                                failed_chunks.append({'task':task,'pages':sorted(chunk_page_numbers(ch)),'error':stop_reason})
                            if err:
                                failed_chunks.append({'task':task,'pages':sorted(chunk_page_numbers(ch)),'error':err})
                                continue
                            if usage.get('finish_reason')=='length':
                                discarded_pages.setdefault(task,set()).update(chunk_page_numbers(ch))
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
                            successful_response_pages.setdefault(task,set()).update(allowed_pages)
                            payload=new_results.setdefault(task,{'records':[]}); payload['records'].extend(result['records'])
                        chunk_queue[0:0]=splits
                    if chunk_queue and (budget_stop or capped):
                        for pending in chunk_queue: failed_chunks.append({'task':task,'pages':sorted(chunk_page_numbers(pending)),'error':'budget limit reached' if budget_stop else 'per-plan call cap reached'})
                if task=='risk' and not budget_stop and not capped:
                    new_results.setdefault('risk',{'records':[]})['records']=_canonicalize_risk_records(new_results.get('risk',{}).get('records',[]))
                    new_results['risk']['coverage_complete']=risk_page_coverage_complete
                if budget_stop or capped: break
            elapsed=time.time()-plan_start; coverage_gaps=coverage_gaps_by_task()
            failure_payload=[]
            for task_name in dict.fromkeys(item['task'] for item in failed_chunks):
                matching=[item for item in failed_chunks if item['task']==task_name]
                pages=sorted({page for item in matching for page in item['pages']})
                failure_payload.append({'task':task_name,'pages':pages,'page_ranges':page_ranges(pages),'errors':list(dict.fromkeys(item['error'] for item in matching))})
            failed_json=json.dumps(failure_payload,ensure_ascii=False) if failure_payload else None
            if config.get('version','').startswith('3.') and (tasks_filter is None or {'risk','action'}.issubset(set(tasks_filter))):
                def stored_payload(task):
                    if task in new_results: return new_results[task]
                    if force or schema_mismatch: return {'records':[]}
                    row=c_plan.execute('SELECT payload FROM extracted WHERE plan_id=? AND task=?',(pid,task)).fetchone()
                    return json.loads(row[0]) if row else {'records':[]}
                plan_payload=stored_payload('plan'); risk_payload=stored_payload('risk'); action_payload=stored_payload('action')
                risk_rows,linked_keys,unaddressed=_reconcile_risk_links(plan_payload,risk_payload,action_payload,finalize_unaddressed=not failure_payload and not budget_stop and not capped and tasks_filter is None,page_texts={str(_page_value(p,'page_no')):str(_page_value(p,'text','') or '') for p in pages})
                if 'risk' in new_results:
                    new_results['risk']['records']=risk_rows
                    new_results['risk']['selected_pages']=sorted(r['page_no'] for r in risk_pages)
                    new_results['risk']['processed_pages']=sorted(successful_response_pages.get('risk',set()))
                    new_results['risk']['coverage_complete']=set(new_results['risk']['selected_pages']).issubset(set(new_results['risk']['processed_pages']))
                if 'action' in new_results: new_results['action']['records']=action_payload.get('records',[])
                if 'plan' in new_results:
                    new_results['plan']['records']=plan_payload.get('records',[])
                    if new_results['plan']['records']:
                        complete=set(risk_pages).issubset(successful_response_pages.get('risk',set())) and risk_page_coverage_complete
                        new_results['plan']['records'][0]['risk_register_coverage']='Complete' if complete else 'Partial'
                link_complete=not failure_payload and not budget_stop and not capped and tasks_filter is None and set(action_payload.get('risk_register_keys_supplied',[]))=={r['risk_key'] for r in risk_rows} and risk_payload.get('coverage_complete',True)
                print(f'LLM RISK LINK {pid}: register={len(risk_rows)}, linked={len(linked_keys)}, unaddressed={len(unaddressed) if link_complete else "pending"}',flush=True)
            if budget_stop:
                _persist_results(c_plan,pid,new_results,replacement_tasks)
                c_plan.commit()
                c_plan.execute("UPDATE plans SET status_llm='budget',failed_pages=? WHERE plan_id=?",(failed_json,pid)); mark(c_plan,pid,'llm','budget')
                _set_plan_schema_status(c_plan,pid,schema_hash,'budget')
                if account_stop_reason:
                    print(f'LLM BALANCE STOP: {account_stop_reason}; plan={pid} remains not-done, completed chunk results are retained, no further provider calls will be made, and the run can resume after account funding.',flush=True)
                else:
                    print(f'LLM BUDGET STOP {pid}: calls={calls_made}, retries={plan_retries}, input_tokens={total_input}, cached_input_tokens={total_cached}, output_tokens={total_output}, reasoning_tokens={total_reasoning}, reasoning_cost_usd={total_reasoning_cost:.6f}, cost_usd={total_cost:.6f}, coverage_gap_pages={coverage_gaps}, seconds={elapsed:.1f}',flush=True)
                c_plan.execute("UPDATE llm_runs SET ended_at=datetime('now'),status='budget' WHERE run_id=?",(run_id,)); c_plan.commit(); return 0
            if capped:
                _persist_results(c_plan,pid,new_results,replacement_tasks)
                c_plan.commit()
                c_plan.execute("UPDATE plans SET status_llm='capped',failed_pages=? WHERE plan_id=?",(failed_json,pid)); mark(c_plan,pid,'llm','capped')
                _set_plan_schema_status(c_plan,pid,schema_hash,'capped'); c_plan.commit()
                print(f'LLM CAPPED {pid}: pages_total={pages_total}, selected={selected_counts}, calls={calls_made}, retries={plan_retries}, input_tokens={total_input}, cached_input_tokens={total_cached}, uncached_input_tokens={total_input-total_cached}, output_tokens={total_output}, reasoning_tokens={total_reasoning}, reasoning_cost_usd={total_reasoning_cost:.6f}, cost_usd={total_cost:.6f}, discounted_cost_usd={total_cost*offpeak_discount:.6f}, coverage_gap_pages={coverage_gaps}, seconds={elapsed:.1f}',flush=True); return 0
            _persist_results(c_plan,pid,new_results,replacement_tasks)
            full_task_set=set(tasks_filter) if tasks_filter is not None else set(task_config)
            has_v22_risk_export=c_plan.execute("SELECT 1 FROM exports WHERE table_name='Risk Level' AND payload LIKE ? LIMIT 1",(f'%"plan_id": "{pid}"%',)).fetchone() is not None
            has_stored_risk=c_plan.execute("SELECT 1 FROM extracted WHERE plan_id=? AND task='risk'",(pid,)).fetchone() is not None
            reused_v22_risk=(risk_missing_only and 'risk' in full_task_set and pid in v22_risk_cached_plans) or ('risk' not in full_task_set and (has_v22_risk_export or has_stored_risk))
            tasks_incomplete=full_task_set!=set(task_config) and not (full_task_set=={'plan','action'} and reused_v22_risk)
            plan_status='partial' if failure_payload or tasks_incomplete else 'done'
            c_plan.execute("UPDATE plans SET status_llm=?,failed_pages=? WHERE plan_id=?",(plan_status,failed_json,pid)); c_plan.commit(); mark(c_plan,pid,'llm',plan_status)
            _set_plan_schema_status(c_plan,pid,('incomplete:'+schema_hash) if tasks_incomplete else schema_hash,plan_status); c_plan.commit()
            if failure_payload: print(f'LLM PARTIAL {pid}: failed_page_ranges={failure_payload}, reused_raw_chunks={reused_raw_chunks}',flush=True)
            else: print(f'LLM DONE {pid}: reused_raw_chunks={reused_raw_chunks}',flush=True)
            print(f'LLM TELEMETRY {pid}: pages_total={pages_total}, selected={selected_counts}, calls={calls_made}, retries={plan_retries}, split_calls={split_calls}, input_tokens={total_input}, cached_input_tokens={total_cached}, uncached_input_tokens={total_input-total_cached}, output_tokens={total_output}, reasoning_tokens={total_reasoning}, reasoning_cost_usd={total_reasoning_cost:.6f}, cost_usd={total_cost:.6f}, discounted_cost_usd={total_cost*offpeak_discount:.6f}, coverage_gap_pages={coverage_gaps}, seconds={elapsed:.1f}',flush=True)
            return 1
        except KeyboardInterrupt:
            run_budget.halt('interrupted'); c_plan.commit(); c_plan.execute("UPDATE llm_runs SET ended_at=datetime('now'),status='interrupted' WHERE run_id=?",(run_id,)); c_plan.commit(); print('Interrupted; completed plans are checkpointed and can be resumed.',flush=True); return 0
        except Exception as e:
            c_plan.execute("UPDATE plans SET status_llm='error' WHERE plan_id=?",(pid,)); c_plan.commit(); mark(c_plan,pid,'llm','error'); _set_plan_schema_status(c_plan,pid,schema_hash,'error'); c_plan.commit(); error(c_plan,pid,'llm','',e)
            elapsed=time.time()-plan_start; coverage_gaps=coverage_gaps_by_task(); print(f'LLM ERROR {pid}: pages_total={pages_total}, selected={selected_counts}, calls={calls_made}, retries={plan_retries}, input_tokens={total_input}, cached_input_tokens={total_cached}, uncached_input_tokens={total_input-total_cached}, output_tokens={total_output}, reasoning_tokens={total_reasoning}, reasoning_cost_usd={total_reasoning_cost:.6f}, cost_usd={total_cost:.6f}, coverage_gap_pages={coverage_gaps}, seconds={elapsed:.1f}: {e}',flush=True)
            return 0
    with ThreadPoolExecutor(max_workers=concurrency) as plan_pool:
        futures=[plan_pool.submit(process_plan,ix,plan) for ix,plan in enumerate(todo,1)]
        for future in as_completed(futures): n+=future.result() or 0
    c.execute("UPDATE llm_runs SET ended_at=COALESCE(ended_at,datetime('now')),status=COALESCE(NULLIF(status,'running'),'done') WHERE run_id=?",(run_id,)); c.commit()
    summary=c.execute('SELECT actual_model,input_cached,input_uncached,output_tokens,reasoning_tokens,reasoning_cost_usd,cost_usd,cost_discounted_usd,api_calls,retries FROM llm_runs WHERE run_id=?',(run_id,)).fetchone()
    print(f"LLM RUN SUMMARY run_id={run_id}, provider={provider_name}, model={summary['actual_model'] or model}, calls={summary['api_calls']}, retries={summary['retries']}, cached_input_tokens={summary['input_cached']}, uncached_input_tokens={summary['input_uncached']}, output_tokens={summary['output_tokens']}, reasoning_tokens={summary['reasoning_tokens']}, reasoning_cost_usd={summary['reasoning_cost_usd']:.6f}, cost_usd={summary['cost_usd']:.6f}, discounted_cost_usd={summary['cost_discounted_usd']:.6f}",flush=True)
    return n


# Final definition validates each risk link against the cited source page and risk terms.
_legacy_reconcile_risk_links = _reconcile_risk_links_core
def _reconcile_risk_links(plan_payload,risk_payload,action_payload,finalize_unaddressed=True,page_texts=None):
    page_texts=page_texts or {}
    normalize=lambda value: re.sub(r'\s+',' ',str(value or '')).strip().casefold()
    stop={'climate','weather','municipal','public','local','system','systems','infrastructure','services','property','buildings','residents','risks','risk','hazard','hazards','facilities','plan','improve','mitigation','adaptation','assess','assessment','action','operations','city','county','town','community','communities','exposure','affected'}
    def terms(value): return {w for w in re.findall(r'[a-z]{5,}',normalize(value)) if w not in stop}
    risks=risk_payload.get('records',[]); used=set()
    for action in action_payload.get('records',[]):
        refs=re.findall(r'\d+',str(action.get('source_page') or ''))
        full_text=' '.join(str(page_texts.get(ref,'') or '') for ref in refs)
        quote=str(action.get('link_evidence') or '').strip()
        quote_ok=bool(quote and ((normalize(quote) in normalize(full_text)) or (not full_text and normalize(quote) in normalize(action.get('action_evidence_text')))))
        context=' '.join(str(action.get(k) or '') for k in ('action_title','action_description','source_section','purpose'))
        qterms=terms(quote) if quote_ok else set(); cterms=terms(context); page_terms=terms(full_text)
        candidate_keys=list(dict.fromkeys(action.get('linked_risk_keys') or [])); verified_keys=[]; evidence_quote=''
        # First prefer model-provided source text that names each risk's hazard or exposed asset.
        for risk in risks:
            key=risk.get('risk_key')
            if key not in candidate_keys: continue
            risk_terms=set()
            for field in ('hazards','hazard_verbatim','affected_sector','affected_asset_or_infrastructure','affected_population','local_exposure_description'):
                value=risk.get(field)
                risk_terms |= terms(' '.join(map(str,value)) if isinstance(value,list) else value)
            overlaps={a for a in qterms for b in risk_terms if a==b or (len(a)>=6 and len(b)>=6 and (a.startswith(b) or b.startswith(a)))}
            if quote_ok and overlaps:
                verified_keys.append(key); evidence_quote=quote
        # If the quote is absent or generic, infer a link only from a specific shared token that is on this full page.
        if not verified_keys and refs and full_text:
            candidates=[]
            for risk in risks:
                key=risk.get('risk_key')
                if key not in candidate_keys: continue
                risk_terms=set()
                for field in ('hazards','hazard_verbatim','affected_sector','affected_asset_or_infrastructure','affected_population','local_exposure_description'):
                    value=risk.get(field)
                    risk_terms |= terms(' '.join(map(str,value)) if isinstance(value,list) else value)
                matches={a for a in cterms & page_terms for b in risk_terms if a==b or (len(a)>=6 and len(b)>=6 and (a.startswith(b) or b.startswith(a)))}
                if matches:
                    candidates.append((key,sorted(matches,key=lambda x:(-len(x),x))[0]))
            if candidates:
                common=Counter(term for _,term in candidates).most_common(1)[0][0]
                evidence_quote=common
                verified_keys=[key for key,term in candidates if term==common]
        quote_key=normalize(evidence_quote)
        kept=[]
        for key in verified_keys:
            pair=(key,quote_key)
            if pair not in used: kept.append(key); used.add(pair)
        if kept:
            action['linked_risk_keys']=kept; action['link_evidence']=evidence_quote
            action['action_evidence_text']=' '.join(filter(None,[action.get('action_evidence_text'),evidence_quote]))
        else:
            action.pop('linked_risk_keys',None); action.pop('link_evidence',None)
    result=_legacy_reconcile_risk_links(plan_payload,risk_payload,action_payload,finalize_unaddressed)
    keep={'action_title','action_description','climate_relevance','action_evidence_text','source_page'}
    for action in action_payload.get('records',[]):
        if str(action.get('climate_relevance','')).casefold()=='contextual':
            for key in list(action):
                if key not in keep: action.pop(key,None)
    return result
