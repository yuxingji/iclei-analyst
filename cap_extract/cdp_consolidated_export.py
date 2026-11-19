"""Build auditable consolidated responses from saved pilot group results."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
import copy
from pathlib import Path

from openpyxl import load_workbook
from .cdp_consolidated import ROOT,OUT,group_for,open_cap_readonly
from .cdp_consolidated_models import normalise_text,split_answer_segments,verify_evidence
from .cdp_quality import normalise_choice,clean_item_separators,unsupported_numbers,apply_quantities,document_language,document_evident_answers


def choice_vocabulary():
    workbook=load_workbook(OUT/'cdp_2026_codebook.xlsx',read_only=True,data_only=True)
    iterator=workbook['Options'].iter_rows(values_only=True);headers=next(iterator);ix={x:i for i,x in enumerate(headers)}
    vocabulary={}
    for row in iterator:
        if row[ix['is_selectable']]=='Yes':
            vocabulary.setdefault(str(row[ix['question_id']]),set()).add(normalise_choice(row[ix['option_text']]))
            vocabulary.setdefault(str(row[ix['column_id']]),set()).add(normalise_choice(row[ix['option_text']]))
    workbook.close();return vocabulary


def _verify_rows(pdf_id,qid,label,evidence,pages,flags):
    out=[]
    for item in evidence:
        page,verified,corrected=verify_evidence(item.get('evidence_excerpt',''),item.get('page'),pages)
        out.append({'pdf_id':pdf_id,'question_id':qid,'item_label':label,'evidence_page':page,
                    'evidence_excerpt':item.get('evidence_excerpt',''),'confidence':item.get('confidence','Low') if verified else 'Low',
                    'evidence_verified':'Yes' if verified else 'No','page_corrected':'Yes' if corrected else 'No'})
        if not verified:flags.append((qid,'unverified_evidence',f'{label}: excerpt not found on selected pages'))
        if item.get('confidence')=='Low':flags.append((qid,'low_confidence',f'{label}: evidence marked Low'))
    return out


def _all_verified(evidence,pages):
    return bool(evidence) and all(verify_evidence(e.get('evidence_excerpt',''),e.get('page'),pages)[1] for e in evidence)


def prepare_plan(plan,cards,jobs,pages,vocabulary):
    """Materialize all questions; failed groups never become Not Disclosed."""
    answer_rows=[];evidence_rows=[];flags=[];status_by_question={};pending_dependencies=[]
    pdf_id=plan['pdf_id'];qid_cards={c['question_id']:c for c in cards}
    for card in cards:
        qid=card['question_id'];group=group_for(qid);job=jobs.get(group)
        payload=json.loads(job['payload']) if job else {}
        selected={n:pages[n] for n in payload.get('selected_pages',[]) if n in pages}
        values=[]
        for response in payload.get('responses',[]):
            values.extend(a for a in response.get('answers',[]) if a.get('question_id')==qid)
        values=copy.deepcopy(values)
        for retry in payload.get('evidence_retries',[]):
            if retry.get('question_id')!=qid:continue
            for candidate in retry.get('answers',[]):
                for value in values:
                    if (normalise_text(candidate.get('answer','')) and normalise_text(candidate.get('answer',''))==normalise_text(value.get('answer','')) and
                        not _all_verified(value.get('evidence',[]),pages) and _all_verified(candidate.get('evidence',[]),pages)):
                        value['evidence']=candidate['evidence']
                    for original in value.get('items',[]):
                        for replacement in candidate.get('items',[]):
                            if (normalise_text(original.get('label'))==normalise_text(replacement.get('label')) and
                                normalise_text(original.get('content'))==normalise_text(replacement.get('content')) and
                                not _all_verified(original.get('evidence',[]),pages) and _all_verified(replacement.get('evidence',[]),pages)):
                                original['evidence']=replacement['evidence']
        language=document_language(sorted(pages.items())) if qid=='1.1' else None
        if language:
            values=[{'question_id':'1.1','disclosure_status':'Answered','answer':language['language'],
                     'evidence':[{'page':language['page'],'evidence_excerpt':language['excerpt'],'confidence':'High'}]}]
        doc_values=document_evident_answers(plan,sorted(pages.items()),qid)
        existing_text=' '.join(str(v.get('answer') or '') for v in values).casefold()
        values.extend(v for v in doc_values if str(v['answer']).casefold() not in existing_text)
        complete=bool(job and job['status']=='done')
        statuses=[x.get('disclosure_status') for x in values]
        if any(x in ('Answered','Explicit No') for x in statuses):
            status='Answered' if 'Answered' in statuses else 'Explicit No'
        elif 'Unclear' in statuses:status='Unclear'
        elif complete and statuses and all(x=='Not Applicable' for x in statuses):status='Not Applicable'
        elif complete and statuses and all(x=='Not Disclosed' for x in statuses):status='Not Disclosed'
        else:status='Unclear'
        if 'Answered' in statuses and 'Explicit No' in statuses:
            status='Unclear';flags.append((qid,'conflicting_disclosures','Selected chunks returned both Answered and Explicit No'))
        if status=='Not Applicable' and not card.get('not_applicable_rule'):
            status='Unclear';flags.append((qid,'invalid_not_applicable','Question has no explicit dependency rule'))
        if status=='Not Applicable':pending_dependencies.append((qid,card.get('not_applicable_rule','')))
        if not complete:flags.append((qid,'incomplete_group','Selected group did not complete successfully'))
        if status=='Unclear':flags.append((qid,'unclear_status','Disclosure or group completion is uncertain'))
        if status=='Explicit No':flags.append((qid,'explicit_no','Explicit absence should be reviewed'))
        if payload.get('output_cap_splits'):flags.append((qid,'output_cap_split',f"Group split {payload['output_cap_splits']} times"))
        if payload.get('coverage_gap_pages'):flags.append((qid,'coverage_gap',str(payload['coverage_gap_pages'])))
        if any(a.get('question_id')==qid and a.get('recovered') for a in payload.get('absence_checks',[])):
            flags.append((qid,'recovered_by_absence_check','A full-document keyword follow-up identified an answer'))
        for retry in payload.get('evidence_retries',[]):
            if retry.get('question_id')==qid and retry.get('error'):
                flags.append((qid,'evidence_retry_incomplete',retry['error']))
        if any(r.get('json_repaired') for r in payload.get('responses',[])):
            flags.append((qid,'repaired_model_json','Closed incomplete JSON structure and moved a complete nested question to its proper level; review against the raw response'))
        if status=='Not Disclosed' and len(selected)<len(pages):flags.append((qid,'limited_page_search','Only selected pages were searched; absence is not verified across the full PDF'))
        if language:flags.append((qid,'document_evident_rule',language['basis']))
        if doc_values:flags.append((qid,'document_evident_rule','Conservative document and jurisdiction rule; inspect cover evidence'))
        unreadable=[n for n,t in selected.items() if len((t or '').strip())<20]
        if status=='Not Disclosed' and (len(selected)<len(pages) or unreadable):
            status='Unclear'
            flags.append((qid,'incomplete_text_coverage',f'Absence cannot be established; pages with little or no extracted text: {unreadable}'))
        seen=set();segments=[];segment_evidence=[];question_evidence=[];plan_level_seen=set();item_index=0;item_labels={}
        for value in values:
            if value.get('choice_values'):
                flags.append((qid,'choice_field_unchecked','Model supplied an option without its questionnaire column ID'))
            for choice in value.get('choice_fields',[]):
                column=str(choice.get('column_id',''));option=str(choice.get('value',''))
                if not column.startswith(qid+'_') or normalise_choice(option) not in vocabulary.get(column,set()):
                    flags.append((qid,'choice_outside_vocabulary',f'{column}: {option}'))
            for choice in value.get('choice_values',[]):
                if normalise_choice(choice) not in vocabulary.get(qid,set()):
                    flags.append((qid,'choice_outside_vocabulary',str(choice)))
            answer=str(value.get('answer') or '').strip()
            answer=re.sub(r'(?i)(?:^|[,;])\s*[^,;:]{0,80}:\s*(?:not disclosed|not available|not specified)\s*(?=[,;]|$)','',answer).strip(' ,;')
            if answer and normalise_text(answer) not in seen:
                seen.add(normalise_text(answer));segments.append(answer)
                ev=_verify_rows(pdf_id,qid,'Answer',value.get('evidence',[]),selected,flags)
                question_evidence.extend(ev);segment_evidence.append(ev)
                if not value.get('evidence'):flags.append((qid,'answer_item_without_evidence','Answer'))
            plan_level=str(value.get('plan_level') or '').strip()
            if plan_level and normalise_text(plan_level) not in plan_level_seen:
                plan_level_seen.add(normalise_text(plan_level));segments.append('Plan-level: '+plan_level)
                if not value.get('evidence'):
                    flags.append((qid,'answer_item_without_evidence','Plan-level'));segment_evidence.append([])
                else:
                    ev=_verify_rows(pdf_id,qid,'Plan-level',value['evidence'],selected,flags)
                    question_evidence.extend(ev);segment_evidence.append(ev)
            for item in value.get('items',[]):
                content=clean_item_separators(item.get('content') or '');label=str(item.get('label') or '').strip()
                content=re.sub(r'(?i)(?:^|[,;])\s*[^,;:]{0,80}:\s*(?:not disclosed|not available|not specified)\s*(?=[,;]|$)','',content).strip(' ,;')
                label=re.sub(r'(?i)^item\s*\d+\s*[-:]\s*','',label).strip()
                signature=normalise_text(label+'\0'+content)
                if not content or signature in seen:continue
                normal_label=normalise_text(label)
                if normal_label and normal_label in item_labels and item_labels[normal_label]!=normalise_text(content):
                    flags.append((qid,'potential_duplicate_items',f'Repeated item label with different text: {label}'))
                item_labels[normal_label]=normalise_text(content)
                seen.add(signature);item_index+=1;item_label=label or 'Unlabeled'
                if ';' in content:flags.append((qid,'separator_violation',f'{item_label}: semicolon inside item'))
                segments.append(f'{item_label}: {content}')
                for choice in item.get('choice_values',[]):
                    if normalise_choice(choice) not in vocabulary.get(qid,set()):flags.append((qid,'choice_outside_vocabulary',str(choice)))
                for choice in item.get('choice_fields',[]):
                    column=str(choice.get('column_id',''));option=str(choice.get('value',''))
                    if not column.startswith(qid+'_') or normalise_choice(option) not in vocabulary.get(column,set()):
                        flags.append((qid,'choice_outside_vocabulary',f'{column}: {option}'))
                item_evidence=item.get('evidence',[])
                if not item_evidence:flags.append((qid,'answer_item_without_evidence',item_label))
                ev=_verify_rows(pdf_id,qid,item_label,item_evidence,selected,flags)
                question_evidence.extend(ev);segment_evidence.append(ev)
        if status=='Not Disclosed':segments=[];segment_evidence=[];question_evidence=[]
        if status in {'Answered','Explicit No'} and not segments:
            status='Unclear';flags.append((qid,'missing_answer','Model supplied no answer for a disclosed status'))
        local_flags=[f for f in flags if f[0]==qid]
        if status in {'Answered','Explicit No'} and any(f[1] in {'answer_item_without_evidence','unverified_evidence','choice_outside_vocabulary'} for f in local_flags):
            status='Unclear'
            flags.append((qid,'answer_evidence_needs_review','At least one answer item, evidence excerpt, or option could not be verified'))
        answered_values=[v for v in values if v.get('disclosure_status')=='Answered']
        missing_sets=[{str(m).strip() for m in v.get('missing_components',[]) if str(m).strip()} for v in answered_values]
        # A chunk-level omission is not necessarily a document-level omission.
        missing=sorted(set.intersection(*missing_sets)) if missing_sets else []
        completeness=('Partial' if missing or not complete else 'Complete') if status=='Answered' else ''
        if status=='Answered' and not language:flags.append((qid,'completeness_review','Completeness is model-reported and requires verification across chunks'))
        if status in {'Answered','Explicit No'}:
            flags.append((qid,'semantic_evidence_review','Quote matching does not prove every answer claim; review the source context'))
        quantities=[q for v in values for q in v.get('quantities',[])]
        quantities.extend(q for v in values for item in v.get('items',[]) for q in item.get('quantities',[]))
        converted=[];derived=[]
        for segment,segment_rows in zip(segments,segment_evidence):
            verified_quotes=[e['evidence_excerpt'] for e in segment_rows if e['evidence_verified']=='Yes']
            relevant=[q for q in quantities if re.search(re.escape(str(q.get('original_value','')))+r'\s*'+re.escape(str(q.get('original_unit',''))),segment,re.I)]
            segment,created,problems=apply_quantities(segment,relevant,verified_quotes)
            converted.append(segment);derived.extend(created)
            for kind,reason in problems:flags.append((qid,kind,reason))
            for number in unsupported_numbers(segment,verified_quotes,created):
                flags.append((qid,'unsupported_value',f'Value {number} not found in this item\'s verified evidence'))
        if status in {'Answered','Explicit No'} and any(f[0]==qid and f[1] in {'unsupported_value','quantity_evidence_missing'} for f in flags):
            status='Unclear';completeness=''
        if status=='Answered' and qid in vocabulary and not language and not any(v.get('choice_fields') or any(i.get('choice_fields') for i in v.get('items',[])) for v in values):
            flags.append((qid,'choice_field_unchecked','No column-specific option was supplied for automated validation'))
        if status=='Unclear' and not any(f[0]==qid and f[1]=='unclear_status' for f in flags):
            flags.append((qid,'unclear_status','Evidence, option, value, or full-text coverage requires review'))
        parts=split_answer_segments(converted)
        if len(parts)>1:flags.append((qid,'answer_split',f'Answer uses {len(parts)} rows'))
        evidence_rows.extend(question_evidence)
        all_pages=sorted({e['evidence_page'] for e in question_evidence if isinstance(e['evidence_page'],int)})
        for index,part in enumerate(parts,1):
            answer_rows.append({'pdf_id':pdf_id,'plan_id':plan['plan_id'],'municipality':plan['municipality'],
                'state':plan['state'],'gov_type':plan['gov_type'],'source_file':plan['source_file'],
                'disclosure_year':plan['disclosure_year'],'question_id':qid,'question_text':card['question_text'],
                'consolidated_answer':part,'disclosure_status':status,'evidence_page':'; '.join(map(str,all_pages)),
                'answer_part':index,'missing_components':'; '.join(missing) if status=='Answered' else '',
                'answer_completeness':completeness,'inference_basis':language['basis'] if language else ('Document-evident rule' if doc_values else '')})
        status_by_question[qid]=status
    for qid,rule in pending_dependencies:
        triggers=[x for x in re.findall(r'\b\d+(?:\.\d+){1,2}\b',rule) if x in qid_cards and x!=qid]
        if not triggers or any(status_by_question.get(t) in {'Not Disclosed','Unclear',None} for t in triggers):
            flags.append((qid,'not_applicable_unverified',f'Dependency trigger unavailable or unclear: {rule}'))
            status_by_question[qid]='Unclear'
            for row in answer_rows:
                if row['question_id']==qid:
                    row['disclosure_status']='Unclear';row['answer_completeness']=''
            flags.append((qid,'unclear_status','Not Applicable dependency was not established'))
    by_q={r['question_id']:r for r in answer_rows}
    flag_rows=[{'pdf_id':pdf_id,'question_id':q,'flag_type':kind,'reason':reason,
                'consolidated_answer':by_q[q]['consolidated_answer'],'disclosure_status':by_q[q]['disclosure_status']}
               for q,kind,reason in flags]
    return answer_rows,evidence_rows,flag_rows


def prepare_export(run_id,output_prefix='CDP_consolidated_pilot',json_name='consolidated_pilot_export.json',limit=10,full=False):
    name='consolidated_full_manifest.json' if full else 'consolidated_pilot_manifest.json'
    manifest=json.loads((OUT/name).read_text(encoding='utf8'))
    if not full:manifest=manifest[:limit]
    cards=json.loads((OUT/'consolidated_question_cards_prepared.json').read_text(encoding='utf8'))['cards']
    from .cdp_consolidated_prepare import DOCUMENT_EVIDENT_RULES
    for card in cards:card['document_evident_rule']=DOCUMENT_EVIDENT_RULES.get(card['question_id'],'')
    from .cdp_consolidated_run import open_store
    vocabulary=choice_vocabulary();db=open_store(OUT/'cdp.sqlite')
    run_record=db.execute('SELECT prompt_version,status FROM cdp_runs WHERE run_id=?',(run_id,)).fetchone()
    if run_record is None:raise ValueError(f'Unknown run: {run_id}')
    cap=open_cap_readonly();answers=[];evidence=[];flags=[]
    for plan in manifest:
        jobs={r['group_id']:r for r in db.execute('SELECT * FROM cdp_group_jobs WHERE run_id=? AND pdf_id=?',(run_id,plan['pdf_id']))}
        pages={r['page_no']:r['text'] for r in cap.execute('SELECT page_no,text FROM pages WHERE plan_id=?',(plan['plan_id'],))}
        a,e,f=prepare_plan(plan,cards,jobs,pages,vocabulary);answers.extend(a);evidence.extend(e);flags.extend(f)
        for row in a:
            group=group_for(row['question_id']);job=jobs.get(group)
            payload=json.loads(job['payload']) if job else {}
            source_run=payload.get('source_run_id',run_id)
            raw=[dict(x) for x in db.execute('SELECT call_id,raw_response FROM cdp_calls WHERE run_id=? AND pdf_id=? AND group_id=? ORDER BY created_at,call_id',(source_run,plan['pdf_id'],group))]
            db.execute("""INSERT INTO cdp_answers VALUES(?,?,?,?,?,?,?,?,?,datetime('now'))
                ON CONFLICT(run_id,pdf_id,question_id,answer_part) DO UPDATE SET
                prompt_hash=excluded.prompt_hash,call_ids=excluded.call_ids,raw_response=excluded.raw_response,
                parsed_answer=excluded.parsed_answer,disclosure_status=excluded.disclosure_status,updated_at=excluded.updated_at""",
                (run_id,row['pdf_id'],row['question_id'],row['answer_part'],job['prompt_hash'] if job else '',
                 json.dumps([x['call_id'] for x in raw]),'' if full else json.dumps(raw,ensure_ascii=False),row['consolidated_answer'],row['disclosure_status']))
    cap.close()
    totals=dict(db.execute('SELECT COALESCE(SUM(api_calls),0) calls,COALESCE(SUM(retries),0) retries,COALESCE(SUM(input_cached),0) cached,COALESCE(SUM(input_uncached),0) uncached,COALESCE(SUM(output_tokens),0) output,COALESCE(SUM(reasoning_tokens),0) reasoning,COALESCE(SUM(cost_usd),0) cost,COALESCE(SUM(uncertain_reserved_usd),0) uncertain_reserved_usd FROM cdp_calls WHERE run_id=?',(run_id,)).fetchone())
    totals['cost_peak']=(totals['cached']*.006+totals['uncached']*.30+totals['output']*1.20)/1_000_000
    totals['cost_offpeak']=totals['cost_peak']*.5
    calls=[dict(r) for r in db.execute('SELECT pdf_id,group_id,prompt_hash,pages,api_calls,cost_usd,actual_model,input_cached,input_uncached,output_tokens,retries FROM cdp_calls WHERE run_id=?',(run_id,))]
    export_id=uuid.uuid4().hex
    for row in evidence:
        db.execute('INSERT INTO cdp_evidence VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (run_id,export_id,row['pdf_id'],row['question_id'],row['item_label'],row['evidence_page'],row['evidence_excerpt'],row['confidence'],row['evidence_verified'],row['page_corrected']))
    for row in flags:
        db.execute('INSERT INTO cdp_review_flags VALUES(?,?,?,?,?,?,?,?)',
                   (run_id,export_id,row['pdf_id'],row['question_id'],row['flag_type'],row['reason'],row['consolidated_answer'],row['disclosure_status']))
    db.commit();db.close()
    data={'answers':answers,'evidence':evidence,'flags':flags,'cards':cards,'run_info':totals,'calls':calls,'run_id':run_id,'prompt_version':run_record['prompt_version'],'run_status':run_record['status']}
    baseline=OUT/'consolidated_pilot_export.json'
    if not full and json_name!='consolidated_pilot_export.json' and baseline.exists():
        from .cdp_consolidated_compare import compare_exports,changed_answers,selected_comparisons
        old=json.loads(baseline.read_text(encoding='utf8'))
        selected_ids={p['pdf_id'] for p in manifest}
        old={**old,'answers':[a for a in old['answers'] if a['pdf_id'] in selected_ids],
             'flags':[f for f in old['flags'] if f['pdf_id'] in selected_ids],
             'evidence':[e for e in old['evidence'] if e['pdf_id'] in selected_ids]}
        data['comparison']=compare_exports(old,data);data['changed_answers']=changed_answers(old,data)
        data['selected_comparisons']=selected_comparisons(old,data,manifest)
    (OUT/json_name).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
    import pandas as pd
    columns={
        'answers':['pdf_id','plan_id','municipality','state','gov_type','source_file','disclosure_year','question_id','question_text','consolidated_answer','disclosure_status','evidence_page','answer_part','missing_components','answer_completeness','inference_basis'],
        'evidence':['pdf_id','question_id','item_label','evidence_page','evidence_excerpt','confidence','evidence_verified','page_corrected'],
        'flags':['pdf_id','question_id','flag_type','reason','consolidated_answer','disclosure_status'],
    }
    for key in ('answers','evidence','flags','cards'):
        pd.DataFrame(data[key],columns=columns.get(key)).to_parquet(OUT/f'{output_prefix}_{key}.parquet',index=False)
    run_rows=[{'key':'run_id','value':run_id},{'key':'prompt_version','value':run_record['prompt_version']},
              {'key':'full_guidance','value':'cdp_2026_codebook.xlsx'}]
    run_rows.extend({'key':key,'value':str(value)} for key,value in totals.items())
    run_rows.append({'key':'cost_offpeak_reporting_only','value':str(totals['cost']*.5)})
    run_rows.extend({'key':f"{x['pdf_id']} / {x['group_id']}",
                     'value':f"pages={x['pages']}; prompt_hash={x['prompt_hash']}; calls={x['api_calls']}; cost_usd={x['cost_usd']}; model={x['actual_model'] or ''}"} for x in calls)
    pd.DataFrame(run_rows).to_parquet(OUT/f'{output_prefix}_run_info.parquet',index=False)
    print(f"export data: answers={len(answers)} evidence={len(evidence)} flags={len(flags)}")
    return data


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run-id',required=True)
    parser.add_argument('--output-prefix',default='CDP_consolidated_pilot')
    parser.add_argument('--json-name',default='consolidated_pilot_export.json')
    parser.add_argument('--limit',type=int,default=10)
    parser.add_argument('--full',action='store_true')
    args=parser.parse_args();prepare_export(args.run_id,args.output_prefix,args.json_name,args.limit,args.full)
