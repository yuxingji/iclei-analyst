import json

from cap_extract.cdp_consolidated import cards_by_group, group_for, select_pages
from cap_extract.cdp_consolidated_export import prepare_plan
from cap_extract.cdp_consolidated_models import Evidence, split_answer_segments, verify_evidence
from cap_extract.cdp_consolidated_run import open_store, run_group, system_prompt
from cap_extract.providers import RequestBudget


def test_page_selection_adds_opening_and_contents():
    pages=[(1,'intro'),(2,'summary'),(3,'opening'),(4,'Table of Contents'),(5,'flood flood'),(6,'other')]
    assert select_pages(pages,{},['flood'],1)==[1,2,3,4,5]


def test_prompt_omits_crosswalk_and_real_response_fields():
    groups=['1.1','2.1','3.1','4.1','5.1','5.6','6.1','7.1','9.1','10.1','12.1','13.7']
    cards=[{'question_id':q,'module':q.split('.')[0],'question_text':'Question',
            'cdp_2025_question_text':'PRIVATE_CROSSWALK_MARKER','real_2025_answer':'PRIVATE_ANSWER_MARKER'} for q in groups]
    prompt=system_prompt(cards_by_group(cards)['M1'])
    assert 'PRIVATE_CROSSWALK_MARKER' not in prompt
    assert 'PRIVATE_ANSWER_MARKER' not in prompt
    assert group_for('13.7')=='M13b'


def test_verbatim_excerpt_and_page_correction():
    pages={1:'Unrelated page.',2:'The city will retrofit its municipal buildings by 2030.'}
    assert verify_evidence('retrofit its municipal buildings by 2030',1,pages)==(2,True,True)
    assert verify_evidence('This text is not present in either page.',1,pages)==(1,False,False)
    assert len(Evidence(page=1,evidence_excerpt='word '*100,confidence='High').evidence_excerpt)<=300


def test_split_answer_preserves_items_and_limit():
    result=split_answer_segments(['Item 1: '+'a'*35,'Item 2: '+'b'*35],max_chars=50)
    assert len(result)==2 and all(len(p)<=50 for p in result)
    assert result[0].startswith('Item 1:') and result[1].startswith('Item 2:')


def test_capped_response_splits_and_accounts_for_all_pages(tmp_path):
    class FakeProvider:
        def __init__(self):self.calls=0
        def extract(self,task,messages,schema_model,call_budget,projected_cost):
            assert call_budget.acquire(projected_cost)
            self.calls+=1;call_budget.observe(.001,projected_cost)
            usage={'api_calls':1,'responses_received':1,'retries':0,'input_tokens':10,'cached_input_tokens':0,'output_tokens':10,'cost_usd':.001}
            if self.calls==1:usage['finish_reason']='length';return None,usage,'capped'
            usage['finish_reason']='stop'
            cards=json.loads(messages[-1]['content'].split('\n',1)[1])
            return {'answers':[{'question_id':x['question_id'],'disclosure_status':'Not Disclosed'} for x in cards]},usage,'{}'
    db=open_store(tmp_path/'test.sqlite');global_gate=RequestBudget(20,0,1,0)
    plan_gate=RequestBudget(5,0,0,0,parent=global_gate)
    plan={'pdf_id':'pdf','plan_id':'p'}
    result=run_group(db,FakeProvider(),'run',plan,'M3',[{'question_id':'3.1'},{'question_id':'3.2'}],[(1,'first'),(2,'second')],[1,2],plan_gate)
    assert result['output_cap_splits']==1 and result['coverage_gap_pages']==[]
    assert len(result['responses'])==2 and plan_gate.used==3
    assert db.execute('select sum(api_calls) from cdp_calls').fetchone()[0]==3


def test_incomplete_group_does_not_fabricate_not_disclosed():
    plan={'pdf_id':'pdf','plan_id':'p','municipality':'Town','state':'NY','gov_type':'Town','source_file':'x.pdf','disclosure_year':2021}
    cards=[{'question_id':'3.2','question_text':'Hazards','not_applicable_rule':''}]
    jobs={'M3':{'status':'partial','payload':json.dumps({'selected_pages':[1],'responses':[],'coverage_gap_pages':[1]})}}
    answers,evidence,flags=prepare_plan(plan,cards,jobs,{1:'No source data'}, {})
    assert answers[0]['disclosure_status']=='Unclear'
    assert any(x['flag_type']=='incomplete_group' for x in flags)
    assert not evidence


def test_full_pilot_resume_isolated_and_budgeted(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import cap_extract.cdp_consolidated_run as runner
    from cap_extract.cdp_consolidated import GROUPS,cards_by_group
    question_ids=['1.1','2.1','3.1','4.1','5.1','5.6','6.1','7.1','9.1','10.1','12.1','13.7']
    cards=[{'question_id':q,'question_text':'Question','module':q.split('.')[0]} for q in question_ids]
    grouped=cards_by_group(cards)
    class FakeProvider:
        total_calls=0
        def __init__(self,*args,**kwargs):pass
        def extract(self,task,messages,schema_model,call_budget,projected_cost):
            assert call_budget.acquire(projected_cost)
            self.__class__.total_calls+=1;call_budget.observe(.0001,projected_cost)
            return {'answers':[{'question_id':q['question_id'],'disclosure_status':'Not Disclosed'} for q in grouped[task]]}, {'api_calls':1,'responses_received':1,'retries':0,'input_tokens':10,'cached_input_tokens':0,'output_tokens':10,'cost_usd':.0001,'finish_reason':'stop'},'{}'
    monkeypatch.setattr(runner,'OUT',tmp_path)
    monkeypatch.setattr(runner,'DeepSeekProvider',FakeProvider)
    monkeypatch.setattr(runner,'open_cap_readonly',lambda:SimpleNamespace(close=lambda:None))
    monkeypatch.setattr(runner,'selected_pages_for_plan',lambda c,pid,groups,keys: ([(1,'Source text')],{g:[1] for g in GROUPS}))
    manifest=[{'pdf_id':f'pdf{i}','plan_id':f'plan{i}'} for i in range(10)]
    result=runner.run(manifest,cards,{},1.5,25,'fake','none')
    assert result['status']=='finished' and result['calls']==120 and FakeProvider.total_calls==120
    result2=runner.run(manifest,cards,{},1.5,25,'fake','none')
    assert result2['status']=='finished' and FakeProvider.total_calls==120


def test_budget_gate_counts_attempts_and_prevents_overspend():
    global_gate=RequestBudget(10,0,.02,.01)
    per_plan=RequestBudget(2,0,0,0,parent=global_gate)
    assert per_plan.acquire(.01)
    per_plan.observe(.01,.01)
    assert per_plan.acquire(.01)
    per_plan.observe(.01,.01)
    assert not per_plan.acquire(.001)
    another=RequestBudget(2,0,0,0,parent=global_gate)
    assert not another.acquire(.001) and another.budget_reached


def test_consolidated_items_keep_individual_evidence_and_reject_unknown_choices():
    plan={'pdf_id':'pdf','plan_id':'p','municipality':'Town','state':'NY','gov_type':'Town','source_file':'x.pdf','disclosure_year':2021}
    cards=[{'question_id':'3.2','question_text':'Hazards','not_applicable_rule':''}]
    response={'question_id':'3.2','disclosure_status':'Answered','items':[
        {'label':'Heat','content':'likelihood High','choice_values':['High'],
         'evidence':[{'page':1,'evidence_excerpt':'Heat creates risks for local residents','confidence':'High'}]},
        {'label':'Flood','content':'likelihood Low','choice_values':['Invented value'],
         'evidence':[{'page':2,'evidence_excerpt':'Flooding disrupts the local roads','confidence':'Medium'}]}]}
    payload={'selected_pages':[1,2],'responses':[{'answers':[response]}]}
    jobs={'M3':{'status':'done','payload':json.dumps(payload)}}
    pages={1:'Heat creates risks for local residents',2:'Flooding disrupts the local roads'}
    answers,evidence,flags=prepare_plan(plan,cards,jobs,pages,{'3.2':{'high','low'}})
    assert answers[0]['consolidated_answer']=='Heat: likelihood High; Flood: likelihood Low'
    assert [e['item_label'] for e in evidence]==['Heat','Flood']
    assert all(e['evidence_verified']=='Yes' for e in evidence)
    assert any(f['flag_type']=='choice_outside_vocabulary' for f in flags)


def test_not_applicable_requires_a_dependency():
    plan={'pdf_id':'pdf','plan_id':'p','municipality':'Town','state':'NY','gov_type':'Town','source_file':'x.pdf','disclosure_year':2021}
    cards=[{'question_id':'3.2','question_text':'Hazards','not_applicable_rule':''}]
    payload={'selected_pages':[1],'responses':[{'answers':[{'question_id':'3.2','disclosure_status':'Not Applicable'}]}]}
    answers,_,flags=prepare_plan(plan,cards,{'M3':{'status':'done','payload':json.dumps(payload)}},{1:'Source'}, {})
    assert answers[0]['disclosure_status']=='Unclear'
    assert any(x['flag_type']=='invalid_not_applicable' for x in flags)


def test_ambiguous_failed_attempt_reserves_budget_and_402_stops(tmp_path):
    from cap_extract.providers import ProviderCallError
    class FailingProvider:
        def extract(self,task,messages,schema_model,call_budget,projected_cost):
            assert call_budget.acquire(projected_cost)
            raise ProviderCallError('insufficient balance',{'api_calls':1,'responses_received':0,'cost_usd':0},stop_reason='HTTP 402')
    db=open_store(tmp_path/'failed.sqlite');root=RequestBudget(10,0,1.5,0);local=RequestBudget(5,0,0,0,parent=root)
    result=run_group(db,FailingProvider(),'run',{'pdf_id':'pdf','plan_id':'plan'},'M3',[{'question_id':'3.2'}],[(1,'text')],[1],local)
    reserved=db.execute('select uncertain_reserved_usd from cdp_calls').fetchone()[0]
    assert root.halted and root.stop_reason=='HTTP 402'
    assert reserved>0 and root.reserved_cost>0 and result['coverage_gap_pages']==[1]


def test_missing_question_keeps_other_valid_answers(tmp_path):
    class PartialProvider:
        def extract(self,task,messages,schema_model,call_budget,projected_cost):
            assert call_budget.acquire(projected_cost);call_budget.observe(.001,projected_cost)
            return {'answers':[{'question_id':'3.1','disclosure_status':'Not Disclosed'}]}, {'api_calls':1,'responses_received':1,'input_tokens':1,'output_tokens':1,'cost_usd':.001,'finish_reason':'stop'},'{}'
    db=open_store(tmp_path/'partial.sqlite');root=RequestBudget(5,0,1,0);local=RequestBudget(5,0,0,0,parent=root)
    result=run_group(db,PartialProvider(),'run',{'pdf_id':'pdf','plan_id':'plan'},'M3',[{'question_id':'3.1'},{'question_id':'3.2'}],[(1,'text')],[1],local)
    assert result['responses'][0]['answers'][0]['question_id']=='3.1'
    assert result['coverage_gap_pages']==[1] and result['errors']
    assert db.execute('select status from cdp_group_jobs').fetchone()[0]=='partial'


def test_offline_full_export_has_620_rows_and_isolated_storage(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import cap_extract.cdp_consolidated_run as runner
    import cap_extract.cdp_consolidated_export as exporter
    from cap_extract.cdp_consolidated import GROUPS,cards_by_group
    ids=['1.1','2.1','3.1','4.1','5.1','5.6','6.1','7.1','9.1','10.1','12.1','13.7']+[f'1.{i}' for i in range(2,52)]
    cards=[{'question_id':q,'question_text':'Question','module':q.split('.')[0],'not_applicable_rule':''} for q in ids]
    groups=cards_by_group(cards)
    class FakeProvider:
        def __init__(self,*args,**kwargs):pass
        def extract(self,task,messages,schema_model,call_budget,projected_cost):
            assert call_budget.acquire(projected_cost);call_budget.observe(.0001,projected_cost)
            return {'answers':[{'question_id':x['question_id'],'disclosure_status':'Not Disclosed'} for x in groups[task]]}, {'api_calls':1,'responses_received':1,'input_tokens':10,'output_tokens':10,'cost_usd':.0001,'finish_reason':'stop'},'{}'
    class FakeCap:
        def execute(self,*args):return [{'page_no':1,'text':'A sufficiently long source text for this test.'}]
        def close(self):pass
    monkeypatch.setattr(runner,'OUT',tmp_path);monkeypatch.setattr(exporter,'OUT',tmp_path)
    monkeypatch.setattr(runner,'DeepSeekProvider',FakeProvider);monkeypatch.setattr(runner,'open_cap_readonly',FakeCap)
    monkeypatch.setattr(exporter,'open_cap_readonly',FakeCap);monkeypatch.setattr(exporter,'choice_vocabulary',lambda:{})
    monkeypatch.setattr(runner,'selected_pages_for_plan',lambda c,pid,g,k: ([(1,'A sufficiently long source text for this test.')],{name:[1] for name in GROUPS}))
    manifest=[{'pdf_id':f'pdf{i}','plan_id':f'plan{i}','municipality':f'Town {i}','state':'NY','gov_type':'Town','source_file':f'{i}.pdf','disclosure_year':2021} for i in range(10)]
    (tmp_path/'consolidated_pilot_manifest.json').write_text(json.dumps(manifest),encoding='utf8')
    (tmp_path/'consolidated_question_cards_prepared.json').write_text(json.dumps({'cards':cards}),encoding='utf8')
    run=runner.run(manifest,cards,{},1.5,25,'fake','none')
    result=exporter.prepare_export(run['run_id'])
    assert len(result['answers'])==620
    assert all(r['disclosure_status']=='Not Disclosed' and not r['consolidated_answer'] for r in result['answers'])
    db=open_store(tmp_path/'cdp.sqlite')
    assert db.execute('select count(*) from cdp_answers').fetchone()[0]==620
    assert db.execute('select count(*) from cdp_calls').fetchone()[0]==120
    assert len(list(tmp_path.glob('CDP_consolidated_pilot_*.parquet')))==5

def test_full_document_chunks_include_every_page_and_oversized_page():
    from cap_extract.cdp_consolidated import page_chunks,full_document_selection
    pages=[(1,'a'*60),(2,'b'*110),(3,'c'*40)]
    chunks=page_chunks(pages,max_chars=100)
    rebuilt={}
    for chunk in chunks:
        for number,text in chunk:rebuilt[number]=rebuilt.get(number,'')+text
    assert list(rebuilt.items())==pages
    assert full_document_selection(pages,{'M1':[]})=={'M1':[1,2,3]}

def test_missing_page_text_does_not_become_not_disclosed():
    from cap_extract.cdp_consolidated_export import prepare_plan
    card={'question_id':'1.1','question_text':'Language','not_applicable_rule':''}
    payload={'selected_pages':[1,2], 'responses':[{'answers':[{'question_id':'1.1','disclosure_status':'Not Disclosed'}]}]}
    job={'status':'done','payload':json.dumps(payload)}
    plan={'pdf_id':'p','plan_id':'p','municipality':'X','state':'NY','gov_type':'City','source_file':'p.pdf','disclosure_year':2020}
    answers,_,flags=prepare_plan(plan,[card],{'M1':job},{1:'Readable words on a real page',2:''},{})
    assert answers[0]['disclosure_status']=='Unclear'
    assert any(f['flag_type']=='incomplete_text_coverage' for f in flags)

def test_not_disclosed_keyword_followup_recovers_answer(tmp_path):
    class Provider:
        calls=0
        def extract(self,task,messages,schema_model,call_budget,projected_cost):
            assert call_budget.acquire(projected_cost);call_budget.observe(.001,projected_cost)
            self.calls+=1
            status='Not Disclosed' if self.calls==1 else 'Answered'
            return {'answers':[{'question_id':'3.2','disclosure_status':status,'answer':'Flood' if self.calls>1 else ''}]}, {'api_calls':1,'responses_received':1,'cost_usd':.001,'finish_reason':'stop'},'{}'
    db=open_store(tmp_path/'absence.sqlite');root=RequestBudget(10,0,1,0);local=RequestBudget(10,0,0,0,parent=root)
    result=run_group(db,Provider(),'run',{'pdf_id':'p','plan_id':'p'},'M3',[{'question_id':'3.2'}],[(1,'Flood and drought affect local residents.')],[1],local,{'3.2':['flood','drought']})
    assert result['absence_checks'][0]['recovered'] and len(result['responses'])==2
    assert db.execute('select status from cdp_group_jobs').fetchone()[0]=='done'

def test_retry_evidence_only_replaces_identical_answer_with_verified_quote():
    from cap_extract.cdp_consolidated_export import prepare_plan
    plan={'pdf_id':'p','plan_id':'p','municipality':'X','state':'NY','gov_type':'City','source_file':'p.pdf','disclosure_year':2020}
    card={'question_id':'3.2','question_text':'Hazards','not_applicable_rule':''}
    old={'question_id':'3.2','disclosure_status':'Answered','answer':'Flood','evidence':[{'page':1,'evidence_excerpt':'Wrong made up quotation here','confidence':'High'}]}
    fixed={'question_id':'3.2','disclosure_status':'Answered','answer':'Flood','evidence':[{'page':1,'evidence_excerpt':'Flooding threatens the local community','confidence':'High'}]}
    payload={'selected_pages':[1],'responses':[{'answers':[old]}],'evidence_retries':[{'question_id':'3.2','answers':[fixed]}]}
    answers,evidence,flags=prepare_plan(plan,[card],{'M3':{'status':'done','payload':json.dumps(payload)}},{1:'Flooding threatens the local community and local roads.'},{})
    assert answers[0]['disclosure_status']=='Answered' and evidence[0]['evidence_verified']=='Yes'

def test_pdf_is_common_prefix_before_group_questions(tmp_path):
    class Provider:
        messages=[]
        def extract(self,task,messages,schema_model,call_budget,projected_cost):
            assert call_budget.acquire(projected_cost);call_budget.observe(.001,projected_cost)
            self.messages.append(messages)
            cards=json.loads(messages[-1]['content'].split('\n',1)[1].strip())
            return {'answers':[{'question_id':c['question_id'],'disclosure_status':'Not Disclosed'} for c in cards]}, {'api_calls':1,'responses_received':1,'cost_usd':.001,'finish_reason':'stop'},'{}'
    p=Provider();db=open_store(tmp_path/'prefix.sqlite');root=RequestBudget(10,0,1,0);local=RequestBudget(10,0,0,0,parent=root)
    plan={'pdf_id':'p','plan_id':'p'};pages=[(1,'A long enough document page to include as shared context.')]
    run_group(db,p,'run',plan,'M1',[{'question_id':'1.1'}],pages,[1],local)
    run_group(db,p,'run',plan,'M3',[{'question_id':'3.2'}],pages,[1],local)
    assert p.messages[0][:2]==p.messages[1][:2]
    assert p.messages[0][2]['content']!=p.messages[1][2]['content']
    assert '=== Page 1 ===' in p.messages[0][1]['content']

def test_interrupted_provider_leaves_conservative_budget_reserve(tmp_path):
    import pytest
    class Interrupted:
        def extract(self,*args,**kwargs):raise KeyboardInterrupt()
    db=open_store(tmp_path/'interrupted.sqlite');root=RequestBudget(10,0,1,0);local=RequestBudget(10,0,0,0,parent=root)
    with pytest.raises(KeyboardInterrupt):
        run_group(db,Interrupted(),'run',{'pdf_id':'p','plan_id':'p'},'M1',[{'question_id':'1.1'}],[(1,'readable source document text')],[1],local)
    row=db.execute('select error,uncertain_reserved_usd,api_calls from cdp_calls').fetchone()
    assert 'IN_FLIGHT' in row['error'] and row['uncertain_reserved_usd']>0 and row['api_calls']==12
    assert db.execute('select status from cdp_group_jobs').fetchone()[0]=='partial'
def test_malformed_json_is_not_repaired():
    from cap_extract.cdp_consolidated_models import GroupResponse
    raw='{"answers":[{"question_id":"9.1.1","disclosure_status":"Answered","items":[{"label":"a","content":"b"},{"question_id":"9.2","disclosure_status":"Not Disclosed"}]}'
    import pytest
    with pytest.raises(ValueError):GroupResponse.model_validate_json(raw)

