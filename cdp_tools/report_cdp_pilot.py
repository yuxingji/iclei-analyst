import json,sqlite3,collections,statistics
from pathlib import Path
out=Path('outputs/cdp')
d=json.loads((out/'consolidated_pilot_export.json').read_text(encoding='utf8'))
manifest=json.loads((out/'consolidated_pilot_manifest.json').read_text(encoding='utf8'))
c=sqlite3.connect('file:outputs/cdp/cdp.sqlite?mode=ro',uri=True);c.row_factory=sqlite3.Row
calls=list(c.execute('''select pdf_id,sum(api_calls) calls,sum(retries) retries,sum(input_cached) cached,
sum(input_uncached) uncached,sum(output_tokens) output,sum(reasoning_tokens) reasoning,
sum(cost_usd) cost,sum(uncertain_reserved_usd) reserved from cdp_calls where run_id=? group by pdf_id''',(d['run_id'],)))
cost={x['pdf_id']:dict(x) for x in calls}
statuses=['Answered','Explicit No','Not Disclosed','Not Applicable','Unclear']
answers=[x for x in d['answers'] if x['answer_part']==1]
overall=collections.Counter(x['disclosure_status'] for x in answers)
lines=['# CDP consolidated pilot report','','Run ID: '+d['run_id'],'',
       'All answers use selected CAP pages only. `Not Disclosed` means not found in the selected pages, not a verified absence across the entire PDF.','',
       '## Pilot list, cost, and statuses','',
       '| PDF ID | Municipality | State | Government | Year | Orientation | CDP 2025 name match | Calls | Retries | Cached input | Uncached input | Output | Peak USD | Off-peak USD | Answered | Explicit No | Not Disclosed | Not Applicable | Unclear |',
       '|---|---|---|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
for p in manifest:
 x=cost.get(p['pdf_id'],{}); counts=collections.Counter(a['disclosure_status'] for a in answers if a['pdf_id']==p['pdf_id'])
 lines.append('| '+ ' | '.join(map(str,[p['pdf_id'],p['municipality'],p['state'],p['gov_type'],p['disclosure_year'],p['orientation'],'Yes' if p['in_cdp_2025'] else 'No',x.get('calls',0),x.get('retries',0),x.get('cached',0),x.get('uncached',0),x.get('output',0),f"{x.get('cost',0):.5f}",f"{x.get('cost',0)*.5:.5f}"]+[counts[s] for s in statuses]))+' |')
lines.extend(['','## Overall disclosure statuses','']+[f'- {s}: {overall[s]}' for s in statuses])
lines.extend(['','## Disclosure status by question','','| Question | '+ ' | '.join(statuses)+' |','|---|---:|---:|---:|---:|---:|'])
for card in d['cards']:
 q=card['question_id'];counts=collections.Counter(a['disclosure_status'] for a in answers if a['question_id']==q)
 lines.append('| '+q+' | '+' | '.join(str(counts[s]) for s in statuses)+' |')
fl=collections.Counter(f['flag_type'] for f in d['flags'])
lines.extend(['','## Review flags','']+[f'- {k}: {v}' for k,v in fl.most_common()])
priority=['incomplete_group','coverage_gap','unverified_evidence','answer_item_without_evidence','invalid_not_applicable','not_applicable_unverified','unclear_status','low_confidence','choice_outside_vocabulary','repaired_model_json']
lines.extend(['','### Ten flags for review',''])
flaglist=sorted(d['flags'],key=lambda f:priority.index(f['flag_type']) if f['flag_type'] in priority else 999)
for f in flaglist[:10]:lines.append(f"- {f['pdf_id']} / {f['question_id']} / {f['flag_type']}: {f['reason']}")
e=d['evidence'];verified=sum(x['evidence_verified']=='Yes' for x in e)
lines.extend(['',f'Evidence verified: {verified}/{len(e)} ({100*verified/len(e) if e else 0:.1f}%). Answer items without evidence: {fl["answer_item_without_evidence"]}.','',
              '## Five complete examples',''])
for q in ['1.1','1.2','3.2','7.1.1','10.1']:
 candidates=[a for a in answers if a['question_id']==q]
 candidates.sort(key=lambda a:(a['disclosure_status'] not in ['Answered','Explicit No'],len(a['consolidated_answer'])==0))
 if not candidates:continue
 a=candidates[0]; parts=[x for x in d['answers'] if x['pdf_id']==a['pdf_id'] and x['question_id']==q]
 lines.extend([f"### {q} — {a['municipality']} ({a['pdf_id']})",'',a['question_text'],'',f"Status: {a['disclosure_status']}",''])
 for part in parts:lines.extend([f"Answer part {part['answer_part']}: {part['consolidated_answer'] or '(empty)'}",''])
 for v in e:
  if v['pdf_id']==a['pdf_id'] and v['question_id']==q:
   lines.append(f"- {v['item_label']}, page {v['evidence_page']}, confidence {v['confidence']}, verified {v['evidence_verified']}: {v['evidence_excerpt']}")
 lines.append('')
caps=list(c.execute("select group_id,count(*) n from cdp_calls where run_id=? and finish_reason='length' group by group_id",(d['run_id'],)))
lines.extend(['## Output-capped calls','']+([f"- {x['group_id']}: {x['n']}" for x in caps] or ['- None']))
tot=d['run_info'];peak=tot['cost'];avg=peak/len(manifest);projected=avg*1069*1.3
lines.extend(['','## Cost and projection','',f"- Recorded calls: {tot['calls']}; retries: {tot['retries']}; reasoning tokens: {tot['reasoning']}.",
              f"- Cached input: {tot['cached']}; uncached input: {tot['uncached']}; output: {tot['output']}.",
              f"- Recorded peak cost USD {peak:.5f}; off-peak reporting estimate USD {peak*.5:.5f}; uncertain interrupted-request reserve USD {tot['uncertain_reserved_usd']:.5f} (not known actual cost).",
              f"- Mechanical projection for 1,069 indexed text-layer plans including 1.3 safety factor: peak USD {projected:.2f} / CNY {projected*7.1:.2f}; off-peak USD {projected*.5:.2f} / CNY {projected*.5*7.1:.2f}. Incomplete groups and the non-random pilot limit reliability.",''])
(out/'consolidated_pilot_report.md').write_text('\n'.join(lines),encoding='utf8')
print('Statuses:',dict(overall));print('Flags:',dict(fl));print('Evidence:',verified,'/',len(e));print('Caps:',[(x['group_id'],x['n']) for x in caps]);print('Costs:',dict(tot));print('Projection peak/offpeak USD:',projected,projected*.5)
c.close()
