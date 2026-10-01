from __future__ import annotations
import json,re
from pathlib import Path
from .core import *
from .stages import selected
from .text_utils import truncate_at_word_boundary

def _merge_evidence(target,source):
    pages=set(filter(None,(str(target.get('source_page') or '')+';'+str(source.get('source_page') or '')).split(';')))
    target['source_page']=';'.join(sorted(pages,key=lambda x:(0,int(x)) if x.isdigit() else (1,x)))
    quotes=[]
    for value in (target.get('evidence_text'),source.get('evidence_text')):
        if value and value not in quotes: quotes.append(value)
    target['evidence_text']=' | '.join(quotes)[:200]

def _normalize_evidence(value):
    return re.sub(r'\s+',' ',re.sub(r'[\-\u2010-\u2015\u2212]+',' ',clean(str(value or '')).casefold())).strip()

def _page_refs(value):
    return list(dict.fromkeys(re.findall(r'\d+',str(value or ''))))

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
        if done(c,pid,'validate') and not force: continue
        mark(c,pid,'validate','running')
        try:
            pages={r['page_no']:r['text'] for r in c.execute('SELECT page_no,text FROM pages WHERE plan_id=?',(pid,))}; lookup={str(k):v for k,v in pages.items()}
            records={t:[] for t in ('plan','risk','action')}
            for task in records:
                row=c.execute('SELECT payload FROM extracted WHERE plan_id=? AND task=?',(pid,task)).fetchone()
                if row: records[task]=json.loads(row[0]).get('records',[])
            for task in records:
                for r in records[task]:
                    refs=_page_refs(r.get('source_page'))
                    r['source_page']=';'.join(refs)
                    page=refs[0] if refs else ''; text=' '.join(lookup[x] for x in refs if x in lookup); evidence=truncate_at_word_boundary(str(r.get('evidence_text') or ''),200)
                    if task=='action' and r.get('action_description'): r['action_description']=truncate_at_word_boundary(str(r['action_description']),500)
                    normalized_evidence=_normalize_evidence(evidence); score=partial_ratio(normalized_evidence,_normalize_evidence(text)) if normalized_evidence and text else 0
                    if score<85:
                        for pno,pt in pages.items():
                            if any(abs(pno-(int(ref) if ref.isdigit() else 0))<=1 for ref in refs): score=max(score,partial_ratio(normalized_evidence,_normalize_evidence(pt)) if normalized_evidence else 0)
                    r['confidence']='Low' if score<85 else r.get('confidence') or 'Medium'
                    if score<85: r['_review_status']='evidence_not_found'
                    r['_notes']=''
                    for field,values in allowed.items():
                        if r.get(field) not in (None,''):
                            match=next((v for v in values if canon(v)==canon(r[field])),None)
                            if match is None: r['_notes']+=f'{field} original={r[field]}; '; r[field]='Unclear'
                            else: r[field]=match
                    for f in ('cost_amount','quantified_value','target_value'):
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
            # Collapse near-identical records with retained page references.
            risks=[]
            for r in records['risk']:
                chain=lambda x:' '.join(str(x.get(k) or '') for k in ('affected_asset_or_population','asset_consequence','service_consequence','revenue_consequence','expenditure_consequence')).strip()
                signature=chain(r)
                match=next((x for x in risks if x.get('hazard_normalized') and x.get('hazard_normalized')==r.get('hazard_normalized') and ((signature and ratio(signature,chain(x))>88) or (not signature and not chain(x)))),None)
                if match: _merge_evidence(match,r)
                else: risks.append(r)
            action_before=len(records['action'])
            actions,action_duplicates=_merge_action_records(records['action'],threshold=85)
            print(f'VALIDATE ACTION DEDUP {pid}: before={action_before}, near_duplicate_rows={action_duplicates}, after={len(actions)}, threshold=85',flush=True)
            records['risk']=risks; records['action']=actions
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
            prow={**identity,**pl};
            out={'Plan Level':[prow]}
            for task,sheet in [('risk','Risk Level'),('action','Action Level')]:
                out[sheet]=[]
                for i,r in enumerate(records[task],1): r['plan_id']=pid; r['risk_id' if task=='risk' else 'action_id']=('R' if task=='risk' else 'A')+f'{i:04d}'; out[sheet].append(r)
            evidence=[]; seq=1
            for sheet,task in [('Plan Level','plan'),('Risk Level','risk'),('Action Level','action')]:
                for r in out[sheet]:
                    ev=r.get('evidence_text')
                    if not ev: continue
                    excluded={'plan_id','risk_id','action_id','evidence_id','state','jurisdiction','gov_type','plan_year','plan_title','source_file','source_page','evidence_text','confidence','discovery_tag','_review_status','_notes'}
                    candidates=[(k,v) for k,v in r.items() if k not in excluded and v not in (None,'')]
                    for field,value in candidates:
                        evidence.append({'evidence_id':f'E{seq:04d}','plan_id':pid,'dimension_bucket':sheet,'variable_candidate':field,'source_page':r.get('source_page'),'source_section':None,'evidence_text':ev,'structured_interpretation':str(value),'confidence':r.get('confidence'),'review_status':r.get('_review_status','validated'),'notes':r.get('_notes')}); seq+=1
            out['Evidence Log']=evidence
            for sheet,rows in out.items():
                c.execute('DELETE FROM exports WHERE table_name=? AND payload LIKE ?', (sheet,f'%"plan_id": "{pid}"%'))
                c.executemany('INSERT INTO exports VALUES(?,?)',[(sheet,json.dumps({k:r.get(k) for k in cols[sheet]})) for r in rows])
            c.commit(); mark(c,pid,'validate','done'); n+=1
        except Exception as e: error(c,pid,'validate','',e)
    return n

def export_stage(c,model='qwen2.5:7b-instruct',only=None):
    import pandas as pd
    from openpyxl import load_workbook
    cols,metadata=template_data(); dest=OUT/'CAP_extraction_output.xlsx'; w=load_workbook(TEMPLATE)
    def excel_cell(value):
        if isinstance(value,(list,tuple)):
            value=' | '.join(str(item) for item in value)
        if isinstance(value,str):
            value=re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F]','',value)
            if len(value)>32767: value=value[:32753]+'...[truncated]'
        return value
    for s in cols:
        ws=w[s]; ws.delete_rows(1,ws.max_row); ws.append(cols[s])
        rows=[obj for x in c.execute('SELECT payload FROM exports WHERE table_name=?',(s,)) if (obj:=json.loads(x[0])) and (not only or obj.get('plan_id') in only)]
        for r in rows: ws.append([excel_cell(r.get(k)) for k in cols[s]])
    for s in ('Variable Dictionary','Controlled Values','Extraction Protocol'):
        ws=w[s]; ws.delete_rows(1,ws.max_row)
        for row in metadata[s]: ws.append([excel_cell(value) for value in row])
    ws=w.create_sheet('Rule Features') if 'Rule Features' not in w.sheetnames else w['Rule Features']; ws.delete_rows(1,ws.max_row); ws.append(['plan_id','features_json'])
    for r in c.execute('SELECT plan_id,features_json FROM rule_features ORDER BY plan_id'):
        if not only or r['plan_id'] in only: ws.append(list(r))
    changes=w.create_sheet('Schema Changes') if 'Schema Changes' not in w.sheetnames else w['Schema Changes']; changes.delete_rows(1,changes.max_row); changes.append(['field','change','reason'])
    for change in schema_config()['schema_changes']: changes.append([change['field'],change['change'],change['reason']])
    ws=w.create_sheet('Run Info') if 'Run Info' not in w.sheetnames else w['Run Info']; ws.delete_rows(1,ws.max_row); ws.append(['key','value'])
    runs=list(c.execute('SELECT * FROM llm_runs ORDER BY started_at'))
    hashes={r['task']:r['prompt_hash'] for r in c.execute('SELECT task,prompt_hash FROM llm_prompt_versions WHERE run_id=(SELECT run_id FROM llm_runs ORDER BY started_at DESC LIMIT 1)')}
    ws.append(['provider',', '.join(sorted({r['provider'] for r in runs if r['provider']})) or ''])
    ws.append(['requested_model',', '.join(sorted({r['requested_model'] for r in runs if r['requested_model']})) or model])
    ws.append(['exact_api_model_id',', '.join(sorted({r['actual_model'] for r in runs if r['actual_model']})) or 'not yet returned by API'])
    ws.append(['run_dates',', '.join(f"{r['started_at']} to {r['ended_at'] or 'running'}" for r in runs)])
    for task in ('plan','risk','action'): ws.append([f'prompt_hash_{task}',hashes.get(task,'')])
    ws.append(['input_tokens_cached',sum(r['input_cached'] or 0 for r in runs)])
    ws.append(['input_tokens_uncached',sum(r['input_uncached'] or 0 for r in runs)])
    ws.append(['output_tokens',sum(r['output_tokens'] or 0 for r in runs)])
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
    for s in cols:
        rows=[obj for x in c.execute('SELECT payload FROM exports WHERE table_name=?',(s,)) if (obj:=json.loads(x[0])) and (not only or obj.get('plan_id') in only)]
        pd.DataFrame(rows,columns=cols[s]).to_parquet(OUT/(s.lower().replace(' ','_')+'.parquet'),index=False)
    pd.DataFrame([{'plan_id':r['plan_id'],**json.loads(r['features_json'])} for r in c.execute('SELECT * FROM rule_features') if not only or r['plan_id'] in only]).to_parquet(OUT/'rule_features.parquet',index=False)
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
        'plan_scope':('plan_scope',),
        'plan_formal_status':('plan_status',),
    }
    report=[]; pilot_id_map={}; mismatch_rows=[]
    row_counts={}
    for sheet in ('Risk Level','Action Level'):
        counts={}
        for rec in c.execute('SELECT payload FROM exports WHERE table_name=?',(sheet,)):
            obj=json.loads(rec[0]); counts[obj.get('plan_id')]=counts.get(obj.get('plan_id'),0)+1
        row_counts[sheet]=counts
    for pilot in pilot_rows[1:]:
        if not pilot[source_ix]: continue
        row=next((r for r in c.execute('SELECT payload FROM exports WHERE table_name=?',('Plan Level',)) if json.loads(r[0]).get('source_file')==pilot[source_ix]),None)
        got=json.loads(row['payload']) if row else {}
        machine_id=got.get('plan_id') if row else None
        extracted_counts={'risk_rows_extracted':row_counts['Risk Level'].get(machine_id,0),'action_rows_extracted':row_counts['Action Level'].get(machine_id,0)}
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
                verbatim=got.get('document_type_verbatim','') if field=='document_type_controlled' else got.get('formal_status_verbatim','') if field=='plan_formal_status' else got.get('plan_scope_verbatim','') if field=='plan_scope' else ''
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
    for sheet,keyfield,threshold in [('Risk Level','hazard_normalized',80),('Action Level','action_title',80)]:
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
            report.append({'source_file':None,'plan_id':pid,'field':f'{sheet} {keyfield} set overlap','expected':len(expected),'actual':len(actual),'precision':precision,'recall':recall,'f1':f1,'pass':f1>=0.8,'risk_rows_extracted':row_counts['Risk Level'].get(machine_id,0),'action_rows_extracted':row_counts['Action Level'].get(machine_id,0)})
    p=OUT/'eval_report.csv'; pd.DataFrame(report).to_csv(p,index=False)
    mismatch_path=OUT/'eval_plan_mismatches.csv'; pd.DataFrame(mismatch_rows).to_csv(mismatch_path,index=False)
    print(f'PLAN-LEVEL MISMATCHES ({len(mismatch_rows)}): source_file | field | machine | hand_coded | verbatim')
    for r in mismatch_rows: print(f"{r['source_file']} | {r['field']} | {r.get('actual')} | {r.get('hand_coded_raw')} | {r.get('verbatim_value','')}")
    return p
