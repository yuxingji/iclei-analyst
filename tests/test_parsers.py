import unittest
from cap_extract.stages import target_hits
from cap_extract.core import split_filename
from cap_extract.llm import select_plan_pages,rank_risk_action_pages,plan_task_chunks

class ParserTests(unittest.TestCase):
    def test_target_parser(self):
        cases = [
            ('reduce greenhouse gas emissions 50% below 2005 levels by 2030', '50', '%', '2005', '2030'),
            ('achieve carbon neutrality by 2050', None, None, None, '2050'),
            ('80 percent reduction from 1990 levels by 2050', '80', '%', '1990', '2050'),
            ('20% below 2008 by 2020', '20', '%', '2008', '2020'),
            ('100% renewable electricity by 2035', '100', '%', None, '2035'),
        ]
        for sentence, value, unit, baseline, year in cases:
            with self.subTest(sentence=sentence):
                hits=target_hits(sentence)
                self.assertTrue(any(h.get('value')==value and h.get('unit')==unit and h.get('baseline')==baseline and h.get('target_year')==year for h in hits))

    def test_filename_keeps_underscores_after_title(self):
        self.assertEqual(split_filename("NSW__City__München__O'Neil,_&_Sons__2022__A_Title_With_Underscores__abc123.pdf"),
                         ('NSW', 'City', "München__O'Neil,_&_Sons", '2022', 'A_Title_With_Underscores', 'abc123'))

    def test_llm_page_selection_is_bounded_and_task_specific(self):
        import json
        pages=[
            {'page_no':1,'relevance':1,'action_table':0,'hits':json.dumps({'hazards':{'flood':1}})},
            {'page_no':2,'relevance':2,'action_table':1,'hits':json.dumps({'hazards':{},'implementation':['planned'],'finance_count':1})},
            {'page_no':3,'relevance':3,'action_table':0,'hits':json.dumps({'hazards':{'heat':5}})},
            {'page_no':4,'relevance':9,'action_table':0,'hits':json.dumps({'hazards':{},'implementation':[],'finance_count':0})},
        ]
        self.assertEqual([p['page_no'] for p in select_plan_pages(pages)],[1,2,3,4])
        risk,action=rank_risk_action_pages(pages,1)
        self.assertEqual([p['page_no'] for p in risk],[3])
        self.assertEqual([p['page_no'] for p in action],[2])

    def test_plan_task_always_builds_one_chunk(self):
        pages=[{'page_no':i,'text':'x'*100,'relevance':i,'action_table':0,'hits':'{}'} for i in range(1,11)]
        chunks=plan_task_chunks(pages,120)
        self.assertEqual(len(chunks),1)
        self.assertIn('[PAGE 1]',chunks[0]); self.assertIn('[PAGE 2]',chunks[0]); self.assertIn('[PAGE 3]',chunks[0]); self.assertIn('[PAGE 10]',chunks[0])

    def test_task_prompts_filter_dictionary_and_controlled_values(self):
        from cap_extract.llm import model_for,make_prompt
        dict_rows=[('Dimension','Sub-dimension','Variable','Extraction level','What to extract','Format','Rule'),
                   ('Plan','X','plan_year','Plan Level','Plan year','integer','explicit'),
                   ('Action','Y','cost_amount','Action Level','Action cost','number','explicit')]
        controls=[('Field','Allowed / normalized values','Notes'),
                  ('plan_year','integer years',''),('cost_amount','USD amount',''),('hazard_normalized','flood, heat','')]
        schema=model_for('action',['action_title','cost_amount','source_page','evidence_text']).model_json_schema()
        prompt=make_prompt('action',['action_title','cost_amount','source_page','evidence_text'],dict_rows,controls,[],schema)
        self.assertIn('cost_amount: Action cost',prompt)
        self.assertIn('- cost_amount: USD amount',prompt)
        self.assertNotIn('plan_year',prompt)
        self.assertNotIn('hazard_normalized',prompt)

    def test_context_budget_arithmetic_and_minimum_guard(self):
        from cap_extract.llm import token_estimate,task_configs,validate_ollama_budgets
        cols={'Plan Level':['plan_year','source_page','evidence_text'],
              'Risk Level':['hazard_normalized','source_page','evidence_text'],
              'Action Level':['action_title','source_page','evidence_text']}
        dicts={'Variable Dictionary':[('Dimension','Sub-dimension','Variable','Extraction level','What to extract','Format','Rule')],
               'Controlled Values':[('Field','Allowed / normalized values','Notes')],
               'Extraction Protocol':[('Protocol','Rule','Text')]}
        configs=task_configs(cols,dicts,4096,ollama_num_ctx=12288,ollama_output_reserve=2048)
        for cfg in configs.values():
            self.assertEqual(cfg['ollama_available'],12288-cfg['system_tokens']-cfg['schema_tokens']-2048-300)
        with self.assertRaisesRegex(ValueError,'fewer than 1500 input tokens'):
            validate_ollama_budgets({'plan':{'ollama_available':1499}})

    def test_validator_parses_supplied_page_markers(self):
        from cap_extract.output import _page_refs
        self.assertEqual(_page_refs('[PAGE 20]; [PAGE 3]; [PAGE 20]'),['20','3'])

    def test_action_title_and_description_are_required_nonempty(self):
        from pydantic import ValidationError
        from cap_extract.llm import model_for
        model=model_for('action',['action_title','action_description','source_page','evidence_text'])
        schema=model.model_json_schema(); item=next(v for name,v in schema['$defs'].items() if name.endswith('Item'))
        self.assertEqual(set(item['required']),{'action_title','action_description','source_page','evidence_text'})
        valid={'action_title':'Replace chiller','action_description':'Replace the arena chiller.','source_page':'21','evidence_text':'$250,000; 15.6 years'}
        model.model_validate({'records':[valid]})
        for bad in ('', '   '):
            with self.assertRaises(ValidationError):
                model.model_validate({'records':[valid|{'action_title':bad}]})

    def test_action_dedup_uses_title_and_description_threshold(self):
        from cap_extract.output import _merge_action_records
        rows=[{'action_title':'Replace arena chiller','action_description':'Replace the ice arena chiller for $250,000.','source_page':'21','evidence_text':'Quote one'},
              {'action_title':'Replace arena chiller','action_description':'Replace the ice arena chiller for $250,000.','source_page':'22','evidence_text':'Quote two'},
              {'action_title':'Install solar arrays','action_description':'Explore solar on town buildings.','source_page':'18','evidence_text':'Quote three'}]
        merged,duplicates=_merge_action_records(rows,85)
        self.assertEqual(duplicates,1); self.assertEqual(len(merged),2)
        self.assertEqual(merged[0]['source_page'],'21;22')

    def test_ollama_provider_captures_eval_counts(self):
        from unittest.mock import patch,Mock
        from pydantic import create_model
        from cap_extract.providers import OllamaProvider
        schema=create_model('CountSchema',value=(str,...))
        response=Mock(); response.raise_for_status.return_value=None
        response.json.return_value={'model':'qwen2.5:7b-instruct','message':{'content':'{"value":"ok"}'},'done_reason':'stop','prompt_eval_count':1234,'eval_count':56}
        with patch('requests.post',return_value=response):
            _,usage,_=OllamaProvider('qwen2.5:7b-instruct').extract('action',[],schema)
        self.assertEqual(usage['input_tokens'],1234); self.assertEqual(usage['output_tokens'],56)

    def test_adaptive_split_preserves_all_page_markers(self):
        from cap_extract.llm import chunk_page_numbers,split_capped_chunk
        original=['[PAGE 21]\n'+'a'*600,'[PAGE 22]\n'+'b'*600,'[PAGE 23]\n'+'c'*600]
        children,error=split_capped_chunk(original)
        self.assertIsNone(error)
        self.assertEqual(set.union(*(chunk_page_numbers(c) for c in children)),chunk_page_numbers(original))
        children,error=split_capped_chunk(['[PAGE 24]\n'+'d'*600])
        self.assertIsNone(error)
        self.assertEqual(set.union(*(chunk_page_numbers(c) for c in children)),{24})

    def test_cost_calculation(self):
        from cap_extract.estimate import cost_usd
        self.assertAlmostEqual(cost_usd(1000,600,1000), (400*.30+600*.006+1000*1.20)/1_000_000)

    def test_offpeak_windows_are_utc_weekdays(self):
        from datetime import datetime,timezone
        from cap_extract.estimate import is_offpeak
        self.assertFalse(is_offpeak(datetime(2026,9,28,2,tzinfo=timezone.utc)))
        self.assertFalse(is_offpeak(datetime(2026,9,28,7,tzinfo=timezone.utc)))
        self.assertTrue(is_offpeak(datetime(2026,9,28,5,tzinfo=timezone.utc)))
        self.assertTrue(is_offpeak(datetime(2026,9,27,2,tzinfo=timezone.utc)))

    def test_provider_validation_retries_count_against_call_gate(self):
        from types import SimpleNamespace
        from pydantic import create_model
        from cap_extract.providers import DeepSeekProvider,RequestBudget
        schema=create_model('RetrySchema',value=(str,...))
        def response(text):
            usage=SimpleNamespace(prompt_tokens=10,prompt_cache_hit_tokens=4,prompt_cache_miss_tokens=6,completion_tokens=2,completion_tokens_details=SimpleNamespace(reasoning_tokens=1))
            choice=SimpleNamespace(message=SimpleNamespace(content=text),finish_reason='stop')
            return SimpleNamespace(choices=[choice],usage=usage,model='deepseek-flash')
        class Completions:
            def __init__(self): self.items=[response('{}'),response('{"value":"ok"}')]; self.calls=0; self.requests=[]
            def create(self,**kwargs): self.calls+=1; self.requests.append(kwargs); return self.items.pop(0)
        provider=object.__new__(DeepSeekProvider); provider.model='deepseek-flash'; provider.max_tokens=8192; provider.reasoning_effort='none'
        provider.client=SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        gate=RequestBudget(2,0,1,.001)
        parsed,usage,raw=provider.extract('plan',[{'role':'system','content':'static json schema'}],schema,gate,.001)
        self.assertEqual(parsed,{'value':'ok'}); self.assertEqual(usage['api_calls'],2); self.assertEqual(usage['retries'],1)
        self.assertEqual(usage['model_id'],'deepseek-flash'); self.assertEqual(gate.used,2)
        self.assertEqual(usage['reasoning_tokens'],2); self.assertAlmostEqual(usage['reasoning_cost_usd'],2*1.2/1_000_000)
        self.assertTrue(all(x['reasoning_effort']=='none' and x['max_tokens']==8192 for x in provider.client.chat.completions.requests))

    def test_call_gate_is_thread_safe_and_reserves_budget(self):
        from concurrent.futures import ThreadPoolExecutor
        from cap_extract.providers import RequestBudget
        gate=RequestBudget(8,0,.01,.001)
        with ThreadPoolExecutor(max_workers=24) as pool:
            accepted=list(pool.map(lambda _:gate.acquire(.001),range(40)))
        self.assertEqual(sum(accepted),8)
        expensive=RequestBudget(8,0,.001,.0006)
        self.assertTrue(expensive.acquire(.0006))
        self.assertFalse(expensive.acquire(.0006))
        self.assertTrue(expensive.budget_reached)

    def test_evidence_matcher(self):
        from rapidfuzz.fuzz import partial_ratio
        self.assertGreaterEqual(partial_ratio('annual progress report','The city publishes an annual progress report each year.'),85)

    def test_schema_v2_2_fields_and_row_controls(self):
        from cap_extract.core import template_data
        from cap_extract.llm import model_for
        cols,_=template_data()
        self.assertIn('plan_orientation_controlled',cols['Plan Level'])
        self.assertNotIn('plan_status',cols['Plan Level'])
        self.assertIn('fiscal_consequence_disclosed',cols['Risk Level'])
        self.assertIn('M_or_A',cols['Risk Level'])
        self.assertEqual(cols['Action Level'][cols['Action Level'].index('action_description')],'action_description')
        risk_model=model_for('risk',cols['Risk Level'])
        risk_item=next(v for name,v in risk_model.model_json_schema()['$defs'].items() if name.endswith('Item'))
        self.assertIn('fiscal_consequence_disclosed',risk_item['required'])
        self.assertIn('M_or_A',risk_item['required'])
        self.assertIn('hazard_normalized',risk_item['required'])

    def test_action_description_v2_2_truncates_at_500(self):
        from cap_extract.core import template_data
        from cap_extract.llm import model_for
        cols,_=template_data(); model=model_for('action',cols['Action Level'])
        row={'M_or_A':'Mitigation','action_title':'Efficiency programme','action_description':('word '*130),'source_page':'1','evidence_text':'quote'}
        result=model.model_validate({'records':[row]}).records[0]
        self.assertLessEqual(len(result.action_description),500)
        self.assertFalse(result.action_description.endswith(' '))

    def test_provider_balance_stop_is_detected(self):
        from types import SimpleNamespace
        from cap_extract.providers import _balance_stop_reason,DeepSeekProvider,ProviderCallError
        self.assertTrue(_balance_stop_reason(SimpleNamespace(status_code=402,code='')))
        self.assertTrue(_balance_stop_reason(SimpleNamespace(status_code=400,code='insufficient_balance')))
        self.assertIsNone(_balance_stop_reason(SimpleNamespace(status_code=503,code='')))
        class BalanceError(Exception): status_code=402
        class Completions:
            def create(self,**kwargs): raise BalanceError('Payment Required')
        provider=object.__new__(DeepSeekProvider); provider.model='deepseek-flash'; provider.max_tokens=1024; provider.reasoning_effort='none'
        provider.client=SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        from pydantic import create_model
        schema=create_model('BalanceSchema',value=(str,...))
        with self.assertRaises(ProviderCallError) as caught: provider.extract('plan',[],schema)
        self.assertIsNotNone(caught.exception.stop_reason)
        self.assertEqual(caught.exception.usage['api_calls'],1)
        self.assertEqual(caught.exception.usage['retries'],0)

    def test_adopting_body_requires_explicit_approval_link(self):
        from cap_extract.output import _adopting_body_supported
        self.assertTrue(_adopting_body_supported('Town Board','Adopted by the Town Board on May 15, 2019.'))
        self.assertFalse(_adopting_body_supported('Town of Saugerties','Adopted on May 15, 2019.'))

if __name__=='__main__': unittest.main()
