import hashlib,json
from datetime import datetime,timezone
import pytest
from cap_extract.providers import billing_factor
from cap_extract.cdp_consolidated_run import open_store,seed_approved_pilot,system_prompt


def test_peak_and_offpeak_billing_is_conservative_across_boundaries():
    def t(hour,minute=0):return datetime(2026,11,11,hour,minute,tzinfo=timezone.utc)
    assert billing_factor(t(0),t(0,30))==.5
    assert billing_factor(t(0,59),t(1,1))==1
    assert billing_factor(t(4),t(5,30))==.5
    assert billing_factor(t(5,59),t(6,1))==1


def test_pilot_reuse_requires_exact_identity_hash_and_pages(tmp_path,monkeypatch):
    import cap_extract.cdp_consolidated_run as runner
    monkeypatch.setattr(runner,'OUT',tmp_path)
    plan={'pdf_id':'p','plan_id':'plan','source_file':'one.pdf'}
    (tmp_path/'consolidated_pilot_manifest.json').write_text(json.dumps([plan]),encoding='utf8')
    groups={'M1':[{'question_id':'1.1'}]}
    class Cap:
        def execute(self,*args):return [{'page_no':1},{'page_no':2}]
        def close(self):pass
    monkeypatch.setattr(runner,'open_cap_readonly',lambda:Cap())
    db=open_store(tmp_path/'db.sqlite')
    db.execute('insert into cdp_runs values(?,?,?,?,?,?,?)',('old','start','end','finished','deepseek-flash',1,'cdp-consolidated-v2-full-document'))
    h=hashlib.sha256(system_prompt(groups['M1']).encode()).hexdigest()
    payload={'selected_pages':[1,2],'responses':[{'call_id':'original','answers':[]}]}
    db.execute('insert into cdp_group_jobs values(?,?,?,?,?,?,?,?,?)',('old','p','plan','M1',h,'done',json.dumps(payload),'[1, 2]','now'));db.commit()
    assert seed_approved_pilot(db,'new',[plan],groups,'old','deepseek-flash')==1
    reused=db.execute('select payload from cdp_group_jobs where run_id=?',('new',)).fetchone()
    assert json.loads(reused[0])['source_run_id']=='old'
    with pytest.raises(ValueError):seed_approved_pilot(db,'other',[{**plan,'source_file':'changed.pdf'}],groups,'old','deepseek-flash')
    with pytest.raises(ValueError):seed_approved_pilot(db,'other',[plan],{'M1':[{'question_id':'1.2'}]},'old','deepseek-flash')

def test_resume_after_interrupted_followup_does_not_repeat_completed_extraction(tmp_path):
    from cap_extract.cdp_consolidated_run import run_group
    from cap_extract.providers import RequestBudget
    class Interrupted:
        calls=0
        def extract(self,*args,**kw):
            self.calls+=1
            if self.calls==2:raise KeyboardInterrupt()
            gate=kw['call_budget'];assert gate.acquire(kw['projected_cost']);gate.observe(.001,kw['projected_cost'])
            return {'answers':[{'question_id':'3.2','disclosure_status':'Not Disclosed'}]}, {'api_calls':1,'responses_received':1,'cost_usd':.001,'finish_reason':'stop'},'{}'
    class Resume:
        calls=0
        def extract(self,*args,**kw):
            self.calls+=1;gate=kw['call_budget'];assert gate.acquire(kw['projected_cost']);gate.observe(.001,kw['projected_cost'])
            return {'answers':[{'question_id':'3.2','disclosure_status':'Not Disclosed'}]}, {'api_calls':1,'responses_received':1,'cost_usd':.001,'finish_reason':'stop'},'{}'
    db=open_store(tmp_path/'resume.sqlite');plan={'pdf_id':'p','plan_id':'p'};cards=[{'question_id':'3.2'}];pages=[(1,'Flood and drought affect this locality.')]
    gate=RequestBudget(10,0,5,0);local=RequestBudget(10,0,0,0,parent=gate)
    with pytest.raises(KeyboardInterrupt):run_group(db,Interrupted(),'run',plan,'M3',cards,pages,[1],local,{'3.2':['flood','drought']})
    payload=json.loads(db.execute('select payload from cdp_group_jobs').fetchone()[0]);assert payload['phase']=='postprocess' and len(payload['responses'])==1
    gate2=RequestBudget(10,0,5,0);local2=RequestBudget(10,0,0,0,parent=gate2);provider=Resume()
    result=run_group(db,provider,'run',plan,'M3',cards,pages,[1],local2,{'3.2':['flood','drought']})
    assert provider.calls==1 and len(result['responses'])==1 and result['absence_checks'][0]['success']

def test_full_run_402_stops_and_resume_skips_completed_groups(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import cap_extract.cdp_consolidated_run as runner
    from cap_extract.providers import ProviderCallError
    from cap_extract.cdp_consolidated import cards_by_group,GROUPS
    ids=['1.1','2.1','3.1','4.1','5.1','5.6','6.1','7.1','9.1','10.1','12.1','13.7']
    cards=[{'question_id':q,'question_text':'Question','module':q.split('.')[0]} for q in ids]
    groups=cards_by_group(cards)
    class Fake:
        fail=True;calls=[]
        def __init__(self,*args,**kwargs):pass
        def extract(self,task,messages,schema_model,call_budget,projected_cost):
            assert call_budget.acquire(projected_cost);self.calls.append(task)
            if task=='M2' and self.fail:
                raise ProviderCallError('insufficient balance',{'api_calls':1,'responses_received':0,'cost_usd':0},stop_reason='HTTP 402')
            call_budget.observe(.001,projected_cost)
            return {'answers':[{'question_id':c['question_id'],'disclosure_status':'Not Disclosed'} for c in groups[task]]}, {'api_calls':1,'responses_received':1,'cost_usd':.001,'finish_reason':'stop'},'{}'
    monkeypatch.setattr(runner,'OUT',tmp_path);monkeypatch.setattr(runner,'DeepSeekProvider',Fake)
    monkeypatch.setattr(runner,'open_cap_readonly',lambda:SimpleNamespace(close=lambda:None))
    monkeypatch.setattr(runner,'selected_pages_for_plan',lambda c,pid,g,k: ([(1,'A document page with usable text for testing.')],{x:[1] for x in GROUPS}))
    manifest=[{'pdf_id':'p','plan_id':'p'}]
    first=runner.run(manifest,cards,{},5,50,'fake','none',1,True)
    assert first['status']=='stopped' and first['stop_reason']=='HTTP 402'
    Fake.fail=False;Fake.calls=[]
    second=runner.run(manifest,cards,{},5,50,'fake','none',1,True)
    assert second['run_id']==first['run_id'] and second['status']=='finished'
    assert 'M1' not in Fake.calls and 'M2' in Fake.calls
