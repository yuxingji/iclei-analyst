from __future__ import annotations
import json,re
from pathlib import Path
from .core import *
from .stages import selected
from .text_utils import truncate_at_word_boundary

def _merge_evidence(target,source):
    for field in ('source_page','risk_evidence_page'):
        pages=set(filter(None,(str(target.get(field) or '')+';'+str(source.get(field) or '')).split(';')))
        if pages: target[field]=';'.join(sorted(pages,key=lambda x:(0,int(x)) if x.isdigit() else (1,x)))
    for field in ('evidence_text','action_evidence_text','risk_evidence_text'):
        quotes=[]
        for value in (target.get(field),source.get(field)):
            if value and value not in quotes: quotes.append(value)
        if quotes: target[field]=' | '.join(quotes)[:200]

def _normalize_evidence(value):
    return re.sub(r'\s+',' ',re.sub(r'[\-\u2010-\u2015\u2212]+',' ',clean(str(value or '')).casefold())).strip()

def _page_refs(value):
    return list(dict.fromkeys(re.findall(r'\d+',str(value or ''))))

RISK_LEVEL_COLUMNS=('plan_id','M_or_A','hazard_count','hazard_name','sector_count','sector_name','fiscal_link','expenditure_consequence','source_page','evidence_text','confidence')

def _nonempty_distinct(values, *, casefold=False):
    out=[]; seen=set()
    for raw in values:
        if raw is None: continue
        value=str(raw).strip()
        if not value or value.casefold() in {'not disclosed','none','null','nan'}: continue
        key=value.casefold() if casefold else value
        if key not in seen: seen.add(key); out.append(value)
    return out

def _risk_level_rows(plan_ids,detail_rows):
    grouped={}
    for row in detail_rows:
        pid=str(row.get('plan_id') or '')
        orientation=str(row.get('M_or_A') or '').strip()
        grouped.setdefault(pid,{}).setdefault(orientation,[]).append(row)
    output=[]
    for pid in plan_ids:
        orientations=grouped.get(str(pid)) or {'':[]}
        for orientation,risks in orientations.items():
            hazards=_nonempty_distinct((r.get('hazard_normalized') for r in risks),casefold=True)
            sectors=[]
            for r in risks:
                sectors.extend(re.split(r'[;,]',str(r.get('affected_sector') or '')))
            sectors=_nonempty_distinct(sectors,casefold=True)
            fiscal=_nonempty_distinct((r.get('municipal_fiscal_link') for r in risks),casefold=True)
            expenditure=_nonempty_distinct((r.get('expenditure_consequence') for r in risks),casefold=True)
            pages=set()
            for r in risks:
                pages.update(int(n) for n in re.findall(r'\d+',str(r.get('source_page') or '')))
            quotes=[str(r.get('evidence_text')).strip() for r in risks if r.get('evidence_text') and str(r.get('evidence_text')).strip().casefold() not in {'not disclosed','none','null','nan'}]
            evidence=' || '.join(quotes)
            if len(evidence)>32767:
                marker='[truncated]'; evidence=evidence[:32767-len(marker)].rstrip()+marker
            confidences=_nonempty_distinct((r.get('confidence') for r in risks),casefold=True)
            rank={'low':0,'medium':1,'high':2}
            recognized=[x for x in confidences if x.casefold() in rank]
            confidence=min(recognized,key=lambda x:rank[x.casefold()]) if recognized else ''
            output.append({'plan_id':pid,'M_or_A':orientation,'hazard_count':len(hazards),'hazard_name':'; '.join(hazards),'sector_count':len(sectors),'sector_name':'; '.join(sectors),'fiscal_link':'; '.join(fiscal),'expenditure_consequence':'; '.join(expenditure),'source_page':'; '.join(map(str,sorted(pages))),'evidence_text':evidence,'confidence':confidence})
    return output

_PLAN_CONTROLLED_FIELDS=('plan_orientation_controlled','document_type_controlled','plan_scope_controlled','plan_formal_status','new_or_update')
_ACTION_CONTROLLED_FIELDS=('climate_relevance','action_orientation')
_PRESERVE_WITHOUT_FIELD_EVIDENCE=set(_PLAN_CONTROLLED_FIELDS+_ACTION_CONTROLLED_FIELDS+('action_title','action_description','issue_type'))

def _missing_value(value):
    return value is None or (isinstance(value,str) and value.strip().casefold() in {'','not disclosed','none','null','nan'})

def _map_document_type(verbatim, fallback=''):
    text=' '.join(str(x or '') for x in (verbatim,fallback)).casefold()
    if 'heat resilience playbook' in text: return 'Resilience Plan'
    if 'municipal energy action plan' in text: return 'Energy Plan'
    if 'sustainability action plan' in text: return 'Sustainability Plan'
    return None

def _first_clause(text):
    value=re.sub(r'^\s*(?:[-*•]+\s*)?','',str(text or '')).strip()
    clause=re.split(r'(?:[.!?;]\s+|\r?\n+)',value,maxsplit=1)[0].strip()
    return truncate_at_word_boundary(clause,200) if clause else ''

def _derive_issue_type(action):
    orientation=str(action.get('action_orientation') or '').strip().casefold()
    hazards=action.get('hazards')
    if not hazards:
        hazards=action.get('hazard_verbatim') or action.get('linked_risk_keys')
    has_hazard=bool(hazards and str(hazards).strip().casefold() not in {'not disclosed','none','[]'})
    mitigation=action.get('mitigation_issue')
    has_mitigation=bool(mitigation and str(mitigation).strip().casefold() not in {'not disclosed','none','[]'})
    if has_hazard and (orientation=='both' or has_mitigation): return 'Both'
    if has_hazard and orientation=='adaptation': return 'Physical Risk'
    if not has_hazard and orientation=='mitigation': return 'Transition / Mitigation Issue'
    return None

def _adopting_body_supported(body,source_text):
    if not body: return True
    term=r'(?:adopted|approved|passed)'
    name=re.escape(str(body).strip())
    return bool(re.search(r'\b'+term+r'\b[^.\n]{0,100}\b'+name+r'\b',source_text,re.I) or re.search(r'\b'+name+r'\b[^.\n]{0,100}\b'+term+r'\b',source_text,re.I))

def _merge_action_records(records,threshold=85):
    from rapidfuzz.fuzz import ratio
    kept=[]; duplicates=0
    for record in records:
        title=str(record.get('action_title') or '').strip()
        description=str(record.get('action_description') or '').strip()
        signature=(title+' '+description).strip()
        match=None
        if signature:
            for existing in kept:
                other=(str(existing.get('action_title') or '').strip()+' '+str(existing.get('action_description') or '').strip()).strip()
                if other and ratio(signature,other)>=threshold:
                    match=existing; break
        if match is None:
            kept.append(record); continue
        duplicates+=1
        for key,value in record.items():
            if value not in (None,'') and match.get(key) in (None,''):
                match[key]=value
        _merge_evidence(match,record)
    return kept,duplicates

def validate_stage(c,force=False,limit=None,only=None):
    from rapidfuzz.fuzz import partial_ratio,ratio
    cols,sheets=template_data(); allowed={}
    for row in sheets['Controlled Values'][1:]:
        if len(row)>1 and row[0] and row[1]: allowed[str(row[0])]=[v.strip() for v in str(row[1]).split('|')]
    canon=lambda value: re.sub(r'[\s_-]+',' ',str(value).strip().casefold())
    n=0
    for plan in selected(c,only,limit):
        pid=plan['plan_id']
        if done(c,pid,'validate') and not force:
            expected={sheet:set(fields) for sheet,fields in cols.items()}
            compatible=True; found_any=False
            for sheet in ('Plan Level','Action Level','Evidence Log'):
                row=c.execute('SELECT payload FROM exports WHERE table_name=? AND payload LIKE ? LIMIT 1',(sheet,f'%"plan_id": "{pid}"%')).fetchone()
                if row:
                    found_any=True
                    if set(json.loads(row[0]))!=expected[sheet]: compatible=False; break
            if compatible and found_any: continue
        mark(c,pid,'validate','running')
        try:
            pages={r['page_no']:r['text'] for r in c.execute('SELECT page_no,text FROM pages WHERE plan_id=?',(pid,))}; lookup={str(k):v for k,v in pages.items()}
            records={t:[] for t in ('plan','action')}; action_payload={}; risk_payload={}
            for task in records:
                row=c.execute('SELECT payload FROM extracted WHERE plan_id=? AND task=?',(pid,task)).fetchone()
                if row:
                    payload=json.loads(row[0])
                    records[task]=payload.get('records',[])
                    if task=='action': action_payload=payload
            risk_row=c.execute('SELECT payload FROM extracted WHERE plan_id=? AND task=?',(pid,'risk')).fetchone()
            if risk_row: risk_payload=json.loads(risk_row[0])
            for task in records:
                for r in records[task]:
                    for list_field in ('hazards','mitigation_issue','discovery_tag'):
                        if isinstance(r.get(list_field),list): r[list_field]=' | '.join(str(x) for x in r[list_field] if x not in (None,''))
                    evidence_specs=[('evidence_text','source_page')] if task=='plan' else [('action_evidence_text','source_page'),('risk_evidence_text','risk_evidence_page')]
                    evidence_scores=[]
                    for evidence_field,page_field in evidence_specs:
                        if r.get(evidence_field): r[evidence_field]=truncate_at_word_boundary(str(r[evidence_field]),200)
                        refs=_page_refs(r.get(page_field)); r[page_field]=';'.join(refs)
                        quote=_normalize_evidence(r.get(evidence_field)); text=_normalize_evidence(' '.join(lookup[x] for x in refs if x in lookup))
                        score=partial_ratio(quote,text) if quote and text else 0
                        if score<85:
                            for pno,pt in pages.items():
                                if any(abs(pno-(int(ref) if ref.isdigit() else 0))<=1 for ref in refs): score=max(score,partial_ratio(quote,_normalize_evidence(pt)) if quote else 0)
                        if quote: evidence_scores.append(score)
                    if task=='action' and r.get('action_description'): r['action_description']=truncate_at_word_boundary(str(r['action_description']),500)
                    score=min(evidence_scores) if evidence_scores else 0
                    r['confidence']='Low' if evidence_scores and score<85 else r.get('confidence') or 'Medium'
                    if evidence_scores and score<85: r['_review_status']='evidence_not_found'
                    r['_notes']=''
                    for field,values in allowed.items():
                        if r.get(field) not in (None,''):
                            if field in {'hazards','mitigation_issue'} and isinstance(r[field],str):
                                normalized=[]; invalid=False
                                for item in r[field].split(' | '):
                                    match=next((v for v in values if canon(v)==canon(item)),None)
                                    if match is None: invalid=True
                                    elif match not in normalized: normalized.append(match)
                                if invalid: r['_notes']+=f'{field} contains an uncontrolled value; '; r[field]='Unclear'
                                else: r[field]=' | '.join(normalized)
                            elif isinstance(r[field],list):
                                r[field]=[next((v for v in values if canon(v)==canon(item)), 'Unclear') for item in r[field]]
                            else:
                                match=next((v for v in values if canon(v)==canon(r[field])),None)
                                if match is None: r['_notes']+=f'{field} original={r[field]}; '; r[field]='Unclear'
                                else: r[field]=match
                    for f in ('risk_value_numeric','cost_numeric_min','cost_numeric_max','target_value','baseline_value'):
                        v=r.get(f)
                        if v and re.search(r'\d',str(v)):
                            amount=re.compile(r'\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(million|billion|thousand|[mbk])?\b',re.I)
                            def numbers(s):
                                vals=[]
                                for m in amount.finditer(s):
                                    number=float(m.group(1).replace(',','')); unit=(m.group(2) or '').casefold()
                                    multiplier=1e9 if unit in ('billion','b') else 1e6 if unit in ('million','m') else 1e3 if unit in ('thousand','k') else 1
                                    vals.append(round(number*multiplier,2))
                                return vals
                            expected=numbers(str(v)); found=numbers(text)
                            if expected and not any(abs(a-b)<=max(1,abs(a)*0.001) for a in expected for b in found): r['_notes']+=f'{f} numeric cross-check failed; '
            action_before=len(records['action'])
            # Preserve returned titles and descriptions. Only synthesize a title when the model
            # genuinely omitted it, using the action's own evidence and marking it Low confidence.
            for r in records['action']:
                if _missing_value(r.get('action_title')):
                    title=_first_clause(r.get('action_evidence_text'))
                    if title:
                        r['action_title']=title
                        r['confidence']='Low'
                        r['_notes']=' '.join(filter(None,[r.get('_notes'),'action_title derived from action_evidence_text']))
            actions,action_duplicates=_merge_action_records(records['action'],threshold=85)
            print(f'VALIDATE ACTION DEDUP {pid}: before={action_before}, near_duplicate_rows={action_duplicates}, after={len(actions)}, threshold=85',flush=True)
            records['action']=actions
            # Build normalized rows with template order and trusted Stage 1 identity.
            identity={k:plan[k] for k in ('plan_id','state','jurisdiction','gov_type','plan_year','plan_title','source_file')}
            plan_rows=records['plan']; pl={}
            if plan_rows:
                for r in plan_rows:
                    for key,value in r.items():
                        if value not in (None,'') and pl.get(key) in (None,''): pl[key]=value
                if pl.get('adopting_body') and not _adopting_body_supported(pl['adopting_body'],'\n'.join(pages.values())):
                    pl['adopting_body']=None
                pl['source_page']=';'.join(sorted({p.strip() for r in plan_rows for p in str(r.get('source_page') or '').split(';') if p.strip()},key=lambda x:(0,int(x)) if x.isdigit() else (1,x)))
                pl['evidence_text']=' | '.join(dict.fromkeys(str(r.get('evidence_text') or '') for r in plan_rows if r.get('evidence_text')))[:200]
            for field in _PLAN_CONTROLLED_FIELDS:
                if _missing_value(pl.get(field)): pl[field]='Unclear'
            doc_type=_map_document_type(pl.get('document_type_verbatim'),pl.get('plan_title'))
            if doc_type: pl['document_type_controlled']=doc_type
            unmatched=pl.get('unaddressed_local_risks')
            if isinstance(unmatched,list): pl['unaddressed_local_risks']=' | '.join(map(str,unmatched))
            selected_risk_pages=set(risk_payload.get('selected_pages',[])); processed_risk_pages=set(risk_payload.get('processed_pages',[]))
            if selected_risk_pages: pl['risk_register_coverage']='Complete' if selected_risk_pages.issubset(processed_risk_pages) else 'Partial'
            prow={**identity,**pl}
            out={'Plan Level':[prow]}
            out['Action Level']=[]
            contextual_fields={'action_title','action_description','climate_relevance','action_orientation','action_evidence_text','source_page'}
            for i,r in enumerate(records['action'],1):
                if str(r.get('climate_relevance','')).casefold()=='contextual':
                    r={k:v for k,v in r.items() if k in contextual_fields}
                    r['action_orientation']='N/A'
                for field in _ACTION_CONTROLLED_FIELDS:
                    if _missing_value(r.get(field)): r[field]='Unclear'
                issue_type=_derive_issue_type(r)
                if issue_type: r['issue_type']=issue_type
                elif _missing_value(r.get('issue_type')): r['issue_type']='Unclear'
                r['plan_id']=pid; r['action_id']=f'A{i:04d}'; out['Action Level'].append(r)
            evidence=[]; seq=1
            excluded={'plan_id','action_id','evidence_id','state','jurisdiction','gov_type','plan_year','plan_title','source_file','source_page','risk_evidence_page','evidence_text','action_evidence_text','risk_evidence_text','risk_register_coverage','confidence','field_evidence','linked_risk_keys','link_evidence','unaddressed_local_risks','unaddressed_risk_evidence','unaddressed_risk_keys','_review_status','_notes'}
            for sheet in ('Plan Level','Action Level'):
                for r in out[sheet]:
                    candidates=[(k,v) for k,v in r.items() if k not in excluded and v not in (None,'')]
                    for field,value in candidates:
                        detail=(r.get('field_evidence') or {}).get(field) or {}
                        if sheet=='Action Level' and field in {'action_title','action_description','climate_relevance','action_orientation','issue_type'} and r.get('action_evidence_text'):
                            detail={'evidence_text':r.get('action_evidence_text'),'source_page':r.get('source_page'),'source_section':r.get('source_section')}
                        elif sheet=='Plan Level' and field in _PLAN_CONTROLLED_FIELDS and pl.get('evidence_text'):
                            detail={'evidence_text':pl.get('evidence_text'),'source_page':pl.get('source_page')}
                        row_quote=detail.get('evidence_text'); row_page=detail.get('source_page')
                        if field=='source_section' and not row_quote:
                            row_quote=r.get('action_evidence_text'); row_page=r.get('source_page')
                        if not row_quote or not row_page:
                            if field not in _PRESERVE_WITHOUT_FIELD_EVIDENCE:
                                r[field]='Not Disclosed'; r['_review_status']='evidence_not_found'
                            continue
                        row_section=detail.get('source_section') or r.get('source_section')
                        refs=_page_refs(row_page); source_text=' '.join(lookup[x] for x in refs if x in lookup)
                        if not source_text or partial_ratio(_normalize_evidence(row_quote),_normalize_evidence(source_text))<85:
                            if field not in _PRESERVE_WITHOUT_FIELD_EVIDENCE:
                                r[field]='Not Disclosed'; r['_review_status']='evidence_not_found'
                            else:
                                r['_review_status']='evidence_not_found'
                            continue
                        evidence.append({'evidence_id':f'E{seq:04d}','plan_id':pid,'dimension_bucket':sheet,'variable_candidate':field,'source_page':row_page,'source_section':row_section,'evidence_text':truncate_at_word_boundary(row_quote,200),'structured_interpretation':json.dumps(value,ensure_ascii=False) if isinstance(value,(list,dict)) else str(value),'confidence':r.get('confidence'),'review_status':r.get('_review_status','validated'),'notes':r.get('_notes')}); seq+=1
            # Keep each internal risk register record traceable even when it is linked to an action.
            unmatched_keys=set((plan_rows[0].get('unaddressed_risk_keys') or []) if plan_rows else [])
            for risk in risk_payload.get('records',[]):
                field_ev=risk.get('field_evidence') or {}
                for field,value in risk.items():
                    if field in {'risk_key','field_evidence','evidence_text','source_page','source_section','confidence'} or value in (None,''): continue
                    detail=field_ev.get(field) or {}
                    quote=detail.get('evidence_text')
                    if not quote or not detail.get('source_page'): continue
                    evidence.append({'evidence_id':f'E{seq:04d}','plan_id':pid,'dimension_bucket':'Action Level','variable_candidate':field,'source_page':detail.get('source_page') or risk.get('source_page'),'source_section':detail.get('source_section') or risk.get('source_section'),'evidence_text':truncate_at_word_boundary(quote,200),'structured_interpretation':json.dumps(value,ensure_ascii=False) if isinstance(value,(list,dict)) else str(value),'confidence':risk.get('confidence'),'review_status':'validated','notes':f"internal risk register {risk.get('risk_key','')}"}); seq+=1
                if risk.get('risk_key') in unmatched_keys and risk.get('evidence_text'):
                    hazards=risk.get('hazards') or []
                    hazard_text=' | '.join(hazards) if isinstance(hazards,list) else str(hazards)
                    for candidate,interpretation in (('unaddressed_local_risks',hazard_text),('unaddressed_risk_evidence',risk.get('evidence_text'))):
                        evidence.append({'evidence_id':f'E{seq:04d}','plan_id':pid,'dimension_bucket':'Plan Level','variable_candidate':candidate,'source_page':risk.get('source_page'),'source_section':risk.get('source_section'),'evidence_text':truncate_at_word_boundary(risk.get('evidence_text'),200),'structured_interpretation':interpretation,'confidence':risk.get('confidence'),'review_status':'validated','notes':f"unaddressed internal risk {risk.get('risk_key','')}"}); seq+=1
            out['Evidence Log']=evidence
            numeric_fields={'plan_year','prior_plan_year','risk_value_numeric','target_value','baseline_value','baseline_year','target_year','cost_numeric_min','cost_numeric_max','financial_appraisal_value'}
            for sheet,rows in out.items():
                for row in rows:
                    for field in cols[sheet]:
                        value=row.get(field)
                        if value is None or (isinstance(value,str) and value.strip().casefold() in {'none','null','nan',''}): row[field]='Not Disclosed'
            for sheet,rows in out.items():
                c.executemany('INSERT INTO exports VALUES(?,?)',[(sheet,json.dumps({k:r.get(k) for k in cols[sheet]})) for r in rows])
            c.commit(); mark(c,pid,'validate','done'); n+=1
        except Exception as e: error(c,pid,'validate','',e)
    return n

def export_stage(c,model='qwen2.5:7b-instruct',only=None,export_name='CAP_extraction_output.xlsx'):
    import pandas as pd
    from openpyxl import load_workbook
    cols,metadata=template_data(); dest=OUT/export_name; w=load_workbook(TEMPLATE)
    def excel_cell(value):
        if isinstance(value,dict): value=json.dumps(value,ensure_ascii=False,sort_keys=True)
        if isinstance(value,(list,tuple)):
            value=' | '.join(json.dumps(item,ensure_ascii=False,sort_keys=True) if isinstance(item,(dict,list,tuple)) else str(item) for item in value)
        if isinstance(value,str):
            value=re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F]','',value)
            if len(value)>32767: value=value[:32753]+'...[truncated]'
        return value
    for legacy_sheet in ('Risk Level','Discovery Log'):
        if legacy_sheet in w.sheetnames: del w[legacy_sheet]
    for s in cols:
        ws=w[s]; ws.delete_rows(1,ws.max_row); ws.append(cols[s])
        rows=[obj for x in c.execute('SELECT payload FROM exports WHERE table_name=?',(s,)) if (obj:=json.loads(x[0])) and (not only or obj.get('plan_id') in only)]
        for r in rows: ws.append([excel_cell(r.get(k)) for k in cols[s]])
    for s in ('Variable Dictionary','Controlled Values','Extraction Protocol'):
        ws=w[s]; ws.delete_rows(1,ws.max_row)
        for row in metadata[s]: ws.append([excel_cell(value) for value in row])
    ws=w.create_sheet('Rule Features') if 'Rule Features' not in w.sheetnames else w['Rule Features']; ws.delete_rows(1,ws.max_row); ws.append(['plan_id','features_json'])
    feature_rows=[]; feature_names=set()
    for r in c.execute('SELECT plan_id,features_json FROM rule_features ORDER BY plan_id'):
        if only and r['plan_id'] not in only: continue
        values=json.loads(r['features_json'] or '{}'); feature_names.update(values)
        feature_rows.append({'plan_id':r['plan_id'],**values})
    feature_columns=sorted(feature_names)
    ws.delete_rows(1,ws.max_row); ws.append(['plan_id']+feature_columns)
    for row in feature_rows: ws.append([excel_cell(row.get('plan_id'))]+[excel_cell('Not Disclosed' if row.get(k) is None else row.get(k)) for k in feature_columns])
    changes=w.create_sheet('Schema Changes') if 'Schema Changes' not in w.sheetnames else w['Schema Changes']; changes.delete_rows(1,changes.max_row); changes.append(['field','change','reason'])
    for change in schema_config()['schema_changes']: changes.append([change['field'],change['change'],change['reason']])
    ws=w.create_sheet('Run Info') if 'Run Info' not in w.sheetnames else w['Run Info']; ws.delete_rows(1,ws.max_row); ws.append(['key','value'])
    runs=list(c.execute('SELECT * FROM llm_runs ORDER BY started_at'))
    latest_run=c.execute('SELECT run_id FROM llm_runs ORDER BY started_at DESC LIMIT 1').fetchone()
    hashes={task:' | '.join(sorted({r[0] for r in c.execute('SELECT DISTINCT prompt_hash FROM llm_raw WHERE run_id=? AND task=? AND prompt_hash IS NOT NULL',(latest_run[0],task))})) if latest_run else '' for task in schema_config()['prompts']}
    ws.append(['provider',', '.join(sorted({r['provider'] for r in runs if r['provider']})) or ''])
    ws.append(['schema_version',schema_config()['version']])
    ws.append(['missingness_rule','Omitted optional fields are normalized to Not Disclosed; explicit N/A and Unclear are preserved. Numeric placeholders are converted to null in Parquet to retain numeric types.'])
    ws.append(['requested_model',', '.join(sorted({r['requested_model'] for r in runs if r['requested_model']})) or model])
    ws.append(['exact_api_model_id',', '.join(sorted({r['actual_model'] for r in runs if r['actual_model']})) or 'not yet returned by API'])
    ws.append(['run_dates',', '.join(f"{r['started_at']} to {r['ended_at'] or 'running'}" for r in runs)])
    for task in schema_config()['prompts']: ws.append([f'prompt_hash_{task}',hashes.get(task,'')])
    ws.append(['input_tokens_cached',sum(r['input_cached'] or 0 for r in runs)])
    ws.append(['input_tokens_uncached',sum(r['input_uncached'] or 0 for r in runs)])
    ws.append(['output_tokens',sum(r['output_tokens'] or 0 for r in runs)])
    if latest_run:
        for task in schema_config()['prompts']:
            usage=c.execute('SELECT COALESCE(SUM(output_tokens),0),COALESCE(SUM(api_calls),0),COALESCE(SUM(retry_count),0),COALESCE(SUM(cost_usd),0) FROM llm_raw WHERE run_id=? AND task=?',(latest_run[0],task)).fetchone()
            ws.append([f'output_tokens_{task}',usage[0]])
            ws.append([f'api_calls_{task}',usage[1]])
            ws.append([f'retries_{task}',usage[2]])
            ws.append([f'cost_usd_{task}',usage[3]])
    ws.append(['reasoning_tokens',sum(r['reasoning_tokens'] or 0 for r in runs)])
    ws.append(['reasoning_cost_usd',sum(r['reasoning_cost_usd'] or 0 for r in runs)])
    ws.append(['api_calls',sum(r['api_calls'] or 0 for r in runs)])
    ws.append(['retries',sum(r['retries'] or 0 for r in runs)])
    ws.append(['total_cost_usd',sum(r['cost_usd'] or 0 for r in runs)])
    ws.append(['reported_discounted_cost_usd',sum(r['cost_discounted_usd'] or 0 for r in runs)])
    ws.append(['plans',c.execute('SELECT COUNT(*) FROM plans').fetchone()[0]])
    ws.append(['errors',c.execute('SELECT COUNT(*) FROM errors').fetchone()[0]])
    w.save(dest)
    check=load_workbook(dest,read_only=True,data_only=True)
    mismatches={s:(list(next(check[s].values)),cols[s]) for s in cols if list(next(check[s].values))!=cols[s]}
    check.close()
    if mismatches: raise RuntimeError(f'Export column mismatch against template: {mismatches}')
    numeric_parquet_fields={'plan_year','prior_plan_year','risk_value_numeric','target_value','baseline_value','baseline_year','target_year','cost_numeric_min','cost_numeric_max','financial_appraisal_value'}
    for s in cols:
        rows=[obj for x in c.execute('SELECT payload FROM exports WHERE table_name=?',(s,)) if (obj:=json.loads(x[0])) and (not only or obj.get('plan_id') in only)]
        frame=pd.DataFrame(rows,columns=cols[s])
        for field in numeric_parquet_fields.intersection(frame.columns): frame[field]=pd.to_numeric(frame[field].replace('Not Disclosed',None),errors='coerce')
        frame.to_parquet(OUT/(s.lower().replace(' ','_')+'.parquet'),index=False)
    pd.DataFrame(feature_rows,columns=['plan_id']+feature_columns).to_parquet(OUT/'rule_features.parquet',index=False)
    return dest

def eval_stage(c):
    import pandas as pd
    from rapidfuzz.fuzz import token_set_ratio
    config=schema_config()
    w=__import__('openpyxl').load_workbook(TEMPLATE,data_only=True,read_only=True)
    pilot_rows=list(w['Plan Level'].values); headers=pilot_rows[0]
    source_ix=headers.index('source_file'); pilot_id_ix=headers.index('plan_id')
    mappings=config.get('eval_mappings',{})
    field_sources={
        'plan_orientation_controlled':('plan_orientation',),
        'document_type_controlled':('document_genre',),
        'plan_scope_controlled':('plan_scope',),
        'plan_formal_status':('plan_status',),
    }
    report=[]; pilot_id_map={}; mismatch_rows=[]
    row_counts={}
    for sheet in ('Action Level',):
        counts={}
        for rec in c.execute('SELECT payload FROM exports WHERE table_name=?',(sheet,)):
            obj=json.loads(rec[0]); counts[obj.get('plan_id')]=counts.get(obj.get('plan_id'),0)+1
        row_counts[sheet]=counts
    for pilot in pilot_rows[1:]:
        if not pilot[source_ix]: continue
        row=next((r for r in c.execute('SELECT payload FROM exports WHERE table_name=?',('Plan Level',)) if json.loads(r[0]).get('source_file')==pilot[source_ix]),None)
        got=json.loads(row['payload']) if row else {}
        machine_id=got.get('plan_id') if row else None
        extracted_counts={'action_rows_extracted':row_counts['Action Level'].get(machine_id,0)}
        if not row:
            report.append({'source_file':pilot[source_ix],'field':'pilot_plan','expected':'found in inventory','actual':'not found','verbatim_value':'','similarity':0,'pass':False,**extracted_counts})
            continue
        if pilot[pilot_id_ix]: pilot_id_map[pilot[pilot_id_ix]]=machine_id
        for field,sources in field_sources.items():
            source=next((x for x in sources if x in headers),None)
            if source is None: continue
            expected=pilot[headers.index(source)]
            recode=mappings.get(field,{})
            expected_mapped=recode.get(str(expected),expected) if expected is not None else None
            actual=got.get(field)
            score=100 if str(expected_mapped or '')==str(actual or '') else 0
            passed=score==100
            if not passed:
                verbatim=got.get('document_type_verbatim','') if field=='document_type_controlled' else got.get('formal_status_verbatim','') if field=='plan_formal_status' else got.get('plan_scope_verbatim','') if field=='plan_scope_controlled' else ''
                item={'source_file':pilot[source_ix],'field':field,'hand_coded_field':source,'expected':expected_mapped,'hand_coded_raw':expected,'actual':actual,'verbatim_value':verbatim,'similarity':score,'pass':passed,**extracted_counts}
                report.append(item); mismatch_rows.append(item)
        unchanged_excluded={'plan_id','state','jurisdiction','gov_type','plan_year','plan_title','source_file','source_page','evidence_text','confidence','discovery_tag','plan_orientation','document_genre','plan_scope','plan_status'}
        for source in headers:
            if source in unchanged_excluded or source not in got: continue
            expected=pilot[headers.index(source)]; actual=got.get(source)
            if expected in (None,'') and actual in (None,''): continue
            score=100 if str(expected or '')==str(actual or '') else 0
            if not score:
                item={'source_file':pilot[source_ix],'field':source,'hand_coded_field':source,'expected':expected,'hand_coded_raw':expected,'actual':actual,'verbatim_value':'','similarity':score,'pass':False,**extracted_counts}
                report.append(item); mismatch_rows.append(item)
    print('EVAL MAPPINGS')
    for field,values in mappings.items():
        print(f'{field}: '+('; '.join(f'{k} -> {v}' for k,v in values.items())))
    for sheet,keyfield,threshold in [('Action Level','action_title',80)]:
        gold=list(w[sheet].values)
        if not gold: continue
        sh=gold[0]; pidx=sh.index('plan_id'); kidx=sh.index(keyfield)
        mids={}
        for rec in c.execute('SELECT payload FROM exports WHERE table_name=?',(sheet,)):
            obj=json.loads(rec[0]); mids.setdefault(obj.get('plan_id'),[]).append(obj.get(keyfield) or '')
        for pid in dict.fromkeys(r[pidx] for r in gold[1:] if r[pidx]):
            expected=[str(r[kidx]) for r in gold[1:] if r[pidx]==pid and r[kidx]]
            machine_id=pilot_id_map.get(pid,pid); actual=mids.get(machine_id,[])
            matched=0; used=set()
            for e in expected:
                candidates=[(token_set_ratio(e,a),i) for i,a in enumerate(actual) if i not in used]
                if candidates:
                    score,i=max(candidates)
                    if score>=threshold: matched+=1; used.add(i)
            precision=matched/len(actual) if actual else 0; recall=matched/len(expected) if expected else 1
            f1=2*precision*recall/(precision+recall) if precision+recall else 0
            report.append({'source_file':None,'plan_id':pid,'field':f'{sheet} {keyfield} set overlap','expected':len(expected),'actual':len(actual),'precision':precision,'recall':recall,'f1':f1,'pass':f1>=0.8,'action_rows_extracted':row_counts['Action Level'].get(machine_id,0)})
    p=OUT/'eval_report.csv'; pd.DataFrame(report).to_csv(p,index=False)
    mismatch_path=OUT/'eval_plan_mismatches.csv'; pd.DataFrame(mismatch_rows).to_csv(mismatch_path,index=False)
    print(f'PLAN-LEVEL MISMATCHES ({len(mismatch_rows)}): source_file | field | machine | hand_coded | verbatim')
    for r in mismatch_rows: print(f"{r['source_file']} | {r['field']} | {r.get('actual')} | {r.get('hand_coded_raw')} | {r.get('verbatim_value','')}")
    return p

def _validate_v23_stage(c,force=False,limit=None,only=None):
    from .llm import _derive_financial_fields,task_configs
    from .aggregation import aggregate_actions
    from .plan_years import _fallback_values
    cols,dicts=template_data(); n=0
    risk_prompt_hash=task_configs(cols,dicts,8192,12288,2048)['risk']['prompt_hash']
    for plan in selected(c,only,limit):
        pid=plan['plan_id']
        try:
            ident={k:plan[k] for k in ('plan_id','state','jurisdiction','gov_type','plan_year','plan_title','source_file')}
            def payload(task):
                found=c.execute('SELECT payload FROM extracted WHERE plan_id=? AND task=?',(pid,task)).fetchone()
                return json.loads(found[0]) if found else {'records':[]}
            plan_records=payload('plan').get('records',[])
            pr=dict(plan_records[0]) if plan_records else {}
            plan_row={**ident,**pr}
            if plan_row.get('document_type_controlled') in (None,''):
                mapped=_map_document_type(plan_row.get('document_type_verbatim'),plan_row.get('plan_title'))
                if mapped: plan_row['document_type_controlled']=mapped
            for field in ('plan_orientation_controlled','document_type_controlled','plan_scope','plan_formal_status','plan_implementation_status','new_or_update'):
                if not _present_v23(plan_row.get(field)): plan_row[field]='Unclear'
            year_entry=c.execute('SELECT payload FROM plan_year_results WHERE plan_id=?',(pid,)).fetchone()
            plan_years=json.loads(year_entry[0]) if year_entry else _fallback_values(dict(plan),plan_row,None)
            year_fields={'plan_publish_year':plan_years.get('plan_publish_year'),'plan_publish_year_source':plan_years.get('plan_publish_year_source') or 'ICLEI metadata','plan_active_year':plan_years.get('plan_active_year') or 'Not Disclosed'}
            plan_row.update(year_fields)
            actions=[]
            for index,item in enumerate(payload('action').get('records',[]),1):
                row=dict(item); row['plan_id']=pid; row['action_id']=row.get('action_id') or f'A{index:04d}'
                row.update(year_fields)
                orientation=str(row.get('action_orientation') or '').strip().title()
                row['action_orientation']=orientation if orientation in {'Mitigation','Adaptation'} else 'Unclear'
                row['action_description']=truncate_at_word_boundary(str(row.get('action_description') or ''),500)
                row['evidence_text']=truncate_at_word_boundary(str(row.get('evidence_text') or ''),200)
                actions.append(row)
            _derive_financial_fields(actions,'action')
            classified={r['action_orientation'] for r in actions if r['action_orientation'] in {'Mitigation','Adaptation'}}
            expected='Both' if classified=={'Mitigation','Adaptation'} else next(iter(classified)) if classified else None
            actual=plan_row.get('plan_orientation_controlled')
            check='Not Checkable' if expected is None else 'Match' if actual==expected else 'Mismatch'
            plan_row['plan_orientation_check']=check
            plan_rows=[{k:plan_row.get(k) for k in cols['Plan Level']}]
            detail_rows=[{k:r.get(k) for k in cols['Action Detail']} for r in actions]
            aggregate_rows=aggregate_actions(pid,actions)
            aggregate_rows=[{**r,**year_fields} for r in aggregate_rows]
            aggregate_rows=[{k:r.get(k) for k in cols['Action Level']} for r in aggregate_rows]
            # Prefer results produced with the active v2.3 risk prompt. Retain
            # legacy v2.2 exports only when no successful response exists for
            # this plan under the current prompt hash.
            legacy_risks=[]
            for item in c.execute('SELECT payload FROM exports WHERE table_name=?',( 'Risk Level',)):
                candidate=json.loads(item[0])
                if candidate.get('plan_id')==pid: legacy_risks.append(candidate)
            current_risk_raw=c.execute("SELECT 1 FROM llm_raw WHERE plan_id=? AND task='risk' AND prompt_hash=? AND response IS NOT NULL AND COALESCE(error,'')='' LIMIT 1",(pid,risk_prompt_hash)).fetchone()
            if current_risk_raw:
                stored_risks=[]
                for index,risk in enumerate(payload('risk').get('records',[]),1):
                    converted=dict(risk); converted.setdefault('risk_id',f'R{index:04d}'); converted['plan_id']=pid
                    stored_risks.append(converted)
            else:
                stored_risks=legacy_risks
            risk_rows=[{k:r.get(k) for k in cols['Risk Detail']} for r in stored_risks]
            evidence=[]; seq=1
            for sheet,rows,quote_field in (('Plan Level',plan_rows,'evidence_text'),('Action Detail',detail_rows,'evidence_text'),('Risk Detail',risk_rows,'evidence_text')):
                for row in rows:
                    quote=row.get(quote_field); page=row.get('source_page')
                    if not _present_v23(quote) or not _present_v23(page): continue
                    evmap=row.get('field_evidence') if isinstance(row.get('field_evidence'),dict) else {}
                    for field,value in row.items():
                        if field in {'field_evidence','evidence_text','source_page','confidence','plan_id','action_id','risk_id','state','jurisdiction','gov_type','plan_year','plan_title','source_file','plan_publish_year','plan_publish_year_source','plan_active_year'} or not _present_v23(value): continue
                        detail=evmap.get(field,{}) if isinstance(evmap.get(field),dict) else {}
                        evidence.append({'evidence_id':f'E{seq:05d}','plan_id':pid,'dimension_bucket':sheet,'variable_candidate':field,'source_page':detail.get('source_page') or page,'source_section':detail.get('source_section') or row.get('source_section'),'evidence_text':truncate_at_word_boundary(str(detail.get('evidence_text') or quote),200),'structured_interpretation':json.dumps(value,ensure_ascii=False) if isinstance(value,(dict,list)) else str(value),'confidence':row.get('confidence'),'review_status':'validated','notes':''}); seq+=1
            pub_quote=plan_years.get('publication_year_source_quote') or ''
            pub_page=plan_years.get('publication_year_source_page') or ''
            if pub_quote and pub_page and plan_years.get('plan_publish_year'):
                evidence.append({'evidence_id':f'E{seq:05d}','plan_id':pid,'dimension_bucket':'Plan Level','variable_candidate':'plan_publish_year','source_page':pub_page,'source_section':'Publication/adoption date','evidence_text':truncate_at_word_boundary(str(pub_quote),200),'structured_interpretation':f"{plan_years['plan_publish_year']} ({plan_years['plan_publish_year_source']})",'confidence':'Medium','review_status':'validated','notes':''}); seq+=1
            active_quote=plan_years.get('active_quote') or ''
            active_page=plan_years.get('active_page') or ''
            if active_quote and active_page and plan_years.get('plan_active_year') not in (None,'Not Disclosed'):
                evidence.append({'evidence_id':f'E{seq:05d}','plan_id':pid,'dimension_bucket':'Plan Level','variable_candidate':'plan_active_year','source_page':active_page,'source_section':'Plan period / planning horizon','evidence_text':truncate_at_word_boundary(str(active_quote),200),'structured_interpretation':str(plan_years['plan_active_year']),'confidence':'Medium','review_status':'validated','notes':''}); seq+=1
            qa=[]
            for r in actions:
                if r['action_orientation']=='Unclear': qa.append({'plan_id':pid,'qa_type':'Unclear action orientation','record_id':r.get('action_id'),'value':r.get('action_title'),'evidence_text':r.get('evidence_text'),'source_page':r.get('source_page')})
                if r.get('cost_type')=='Unclear': qa.append({'plan_id':pid,'qa_type':'Unclear cost type','record_id':r.get('action_id'),'value':r.get('cost_raw'),'evidence_text':r.get('evidence_text'),'source_page':r.get('source_page')})
            if check=='Mismatch': qa.append({'plan_id':pid,'qa_type':'Plan orientation mismatch','record_id':'','value':f"plan={actual}; actions={expected}",'evidence_text':plan_row.get('evidence_text'),'source_page':plan_row.get('source_page')})
            output={'Plan Level':plan_rows,'Risk Detail':risk_rows,'Action Detail':detail_rows,'Action Level':aggregate_rows,'Evidence Log':evidence}
            for sheet,rows in output.items():
                normalized=[]
                for row in rows:
                    if sheet=='Action Level':
                        normalized.append(row)
                    else:
                        normalized.append({k:('Not Disclosed' if v is None or v=='' else v) for k,v in row.items()})
                for index,row in enumerate(normalized,1):
                    if sheet=='Action Level': key=str(row.get('orientation') or index)
                    elif sheet in {'Action Detail','Risk Detail'}: key=str(row.get('action_id' if sheet=='Action Detail' else 'risk_id') or index)
                    elif sheet=='Evidence Log': key=str(row.get('evidence_id') or index)
                    else: key='plan'
                    c.execute('INSERT INTO exports_v23 VALUES(?,?,?,?) ON CONFLICT(table_name,plan_id,record_key) DO UPDATE SET payload=excluded.payload',(sheet,pid,key,json.dumps(row,ensure_ascii=False)))
            for index,row in enumerate(qa,1):
                key='|'.join(str(row.get(k) or '') for k in ('qa_type','record_id','value')) or str(index)
                c.execute('INSERT INTO exports_v23 VALUES(?,?,?,?) ON CONFLICT(table_name,plan_id,record_key) DO UPDATE SET payload=excluded.payload',('QA',pid,key,json.dumps(row,ensure_ascii=False)))
            c.commit(); mark(c,pid,'validate','done'); n+=1
        except Exception as exc:
            error(c,pid,'validate','',exc)
    return n

def _present_v23(value):
    return value not in (None,'') and str(value).strip().casefold() not in {'not disclosed','none','null','nan'}

def _excel_safe(value):
    """Remove XML-disallowed control characters from values written to XLSX."""
    if isinstance(value, str):
        return ''.join(ch for ch in value if ch in '\t\n\r' or ord(ch) >= 32 and not 0xD800 <= ord(ch) <= 0xDFFF)
    return value

def validate_stage(c,force=False,limit=None,only=None):
    return _validate_v23_stage(c,force,limit,only)

def _export_v23_stage(c,model='qwen2.5:7b-instruct',only=None,export_name='CAP_extraction_output.xlsx'):
    from openpyxl import load_workbook,Workbook
    from .core import OUT
    from .llm import task_configs
    cols,metadata=template_data(); OUT.mkdir(parents=True,exist_ok=True); dest=OUT/export_name
    risk_prompt_hash=task_configs(cols,metadata,8192,12288,2048)['risk']['prompt_hash']
    if TEMPLATE.exists(): w=load_workbook(TEMPLATE)
    else: w=Workbook()
    for sheet in cols:
        ws=w[sheet] if sheet in w.sheetnames else w.create_sheet(sheet)
        ws.delete_rows(1,ws.max_row); ws.append(RISK_LEVEL_COLUMNS if sheet=='Risk Level' else cols[sheet])
        records=[]
        if sheet=='Risk Detail':
            source_rows=list(c.execute("SELECT payload FROM exports_v23 WHERE table_name='Risk Detail'"))
            if not source_rows:
                source_rows=list(c.execute("SELECT payload FROM exports WHERE table_name='Risk Level'"))
            for item in source_rows:
                row=json.loads(item[0])
                if not only or row.get('plan_id') in only: records.append(row)
        elif sheet=='Risk Level':
            detail_source=list(c.execute("SELECT payload FROM exports_v23 WHERE table_name='Risk Detail'"))
            if not detail_source:
                detail_source=list(c.execute("SELECT payload FROM exports WHERE table_name='Risk Level'"))
            detail_rows=[json.loads(item[0]) for item in detail_source]
            if only: detail_rows=[row for row in detail_rows if row.get('plan_id') in only]
            plan_ids=[r[0] for r in c.execute("SELECT DISTINCT plan_id FROM exports_v23 WHERE table_name='Plan Level' ORDER BY plan_id") if not only or r[0] in only]
            if not plan_ids: plan_ids=[r[0] for r in c.execute('SELECT plan_id FROM plans ORDER BY plan_id') if not only or r[0] in only]
            records=_risk_level_rows(plan_ids,detail_rows)
        else:
            source_rows=c.execute('SELECT payload FROM exports_v23 WHERE table_name=?',(sheet,))
            for item in source_rows:
                row=json.loads(item[0])
                if not only or row.get('plan_id') in only: records.append(row)
        headers=RISK_LEVEL_COLUMNS if sheet=='Risk Level' else cols[sheet]
        for row in records: ws.append([_excel_safe(json.dumps(row.get(k),ensure_ascii=False) if isinstance(row.get(k),(dict,list)) else row.get(k)) for k in headers])
    for sheet in ('QA','Rule Features','Controlled Values','Schema Changes','Run Info'):
        ws=w[sheet] if sheet in w.sheetnames else w.create_sheet(sheet); ws.delete_rows(1,ws.max_row)
        if sheet=='QA':
            ws.append(['plan_id','qa_type','record_id','value','evidence_text','source_page'])
            for item in c.execute('SELECT payload FROM exports_v23 WHERE table_name=?',('QA',)):
                row=json.loads(item[0])
                if not only or row.get('plan_id') in only: ws.append([_excel_safe(row.get(k)) for k in ('plan_id','qa_type','record_id','value','evidence_text','source_page')])
        elif sheet=='Rule Features':
            def feature_cell(value):
                if isinstance(value,(dict,list,tuple)):
                    return ' | '.join(map(str,value)) if isinstance(value,(list,tuple)) else json.dumps(value,ensure_ascii=False)
                return value
            feature_rows=[]; names=set()
            for r in c.execute('SELECT plan_id,features_json FROM rule_features ORDER BY plan_id'):
                if only and r['plan_id'] not in only: continue
                feat=json.loads(r['features_json'] or '{}'); names.update(feat); feature_rows.append((r['plan_id'],feat))
            fields=sorted(names); ws.append(['plan_id']+fields)
            for pid,feat in feature_rows: ws.append([pid]+[feature_cell(feat.get(k)) for k in fields])
        elif sheet=='Controlled Values':
            for row in metadata['Controlled Values']: ws.append(list(row))
        elif sheet=='Schema Changes':
            ws.append(['field','change','reason'])
            for row in schema_config().get('schema_changes',[]): ws.append([row.get(k) for k in ('field','change','reason')])
        else:
            ws.append(['key','value']); ws.append(['schema_version','2.3'])
            latest_id=c.execute('SELECT run_id FROM llm_runs ORDER BY started_at DESC LIMIT 1').fetchone()
            latest=c.execute('SELECT * FROM llm_runs WHERE run_id=?',(latest_id[0],)).fetchone() if latest_id else None
            ws.append(['provider',latest['provider'] if latest else ''])
            ws.append(['requested_model',latest['requested_model'] if latest else model])
            ws.append(['exact_api_model_id',(latest['actual_model'] or 'not yet returned by API') if latest else 'not yet returned by API'])
            ws.append(['run_dates',f"{latest['started_at']} to {latest['ended_at'] or 'running'}" if latest else ''])
            if latest:
                for task in ('plan','risk','action'):
                    if task=='risk':
                        # Risk exports are rebuilt from the current v2.3 risk prompt,
                        # including plans refreshed in separate risk-only runs.
                        vals=sorted({r[0] for r in c.execute("SELECT DISTINCT prompt_hash FROM llm_raw WHERE task='risk' AND prompt_hash=? AND response IS NOT NULL AND COALESCE(error,'')=''",(risk_prompt_hash,))})
                    else:
                        vals=sorted({r[0] for r in c.execute('SELECT DISTINCT prompt_hash FROM llm_raw WHERE run_id=? AND task=? AND prompt_hash IS NOT NULL',(latest['run_id'],task))})
                    ws.append([f'prompt_hash_{task}',' | '.join(vals)])
                for field,key in (('input_tokens_cached','input_cached'),('input_tokens_uncached','input_uncached'),('output_tokens','output_tokens'),('reasoning_tokens','reasoning_tokens'),('api_calls','api_calls'),('retries','retries'),('total_cost_usd','cost_usd'),('reported_discounted_cost_usd','cost_discounted_usd')):
                    ws.append([field,latest[key] or 0])
            ws.append(['reasoning_mode','effort none requested; confirm API usage separately'])
            ws.append(['risk_source',f'Rebuilt from successful risk responses using current prompt hash {risk_prompt_hash}; prior 100-plan results archived in SQLite'])
            ws.append(['plan_orientation_check','Action orientations: Mitigation / Adaptation / Unclear; Both is plan-level only.'])
    required_sheets=['Plan Level','Action Level','Action Detail','Risk Level','Risk Detail','Evidence Log','QA','Rule Features','Controlled Values','Schema Changes','Run Info']
    for name in list(w.sheetnames):
        if name not in required_sheets: del w[name]
    w._sheets=[w[name] for name in required_sheets]
    w.save(dest)
    try:
        import pandas as pd
        parquet_sheets=('Plan Level','Action Level','Action Detail','Risk Level','Risk Detail','Evidence Log','QA','Rule Features')
        for sheet in parquet_sheets:
            ws=w[sheet]; values=list(ws.values)
            if not values: continue
            header=list(values[0]); frame=pd.DataFrame(values[1:],columns=header)
            # Excel sheets can contain mixed representations (for example a
            # numeric prior year beside a textual "unknown"). Arrow requires
            # a single physical type per column, so normalize only those mixed
            # columns to strings while preserving homogeneous numeric columns.
            for column in frame.columns:
                kinds={type(v) for v in frame[column].dropna()}
                if len(kinds)>1:
                    frame[column]=frame[column].map(lambda v: None if pd.isna(v) else str(v))
            slug=sheet.lower().replace(' ','_')
            parquet_path=OUT/f'{Path(export_name).stem}_{slug}.parquet'
            frame.to_parquet(parquet_path,index=False)
    except ImportError as exc:
        raise RuntimeError('Parquet export requires pandas and a Parquet engine (pyarrow or fastparquet)') from exc
    return dest

def export_stage(c,model='qwen2.5:7b-instruct',only=None,export_name='CAP_extraction_output.xlsx'):
    return _export_v23_stage(c,model,only,export_name)
