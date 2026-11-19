"""Write a transparent offline report from a completed or stopped v2 pilot."""
import collections,json,sqlite3
from pathlib import Path
out=Path('outputs/cdp');data=json.loads((out/'consolidated_pilot_v2_export.json').read_text(encoding='utf8'))
manifest=json.loads((out/'consolidated_pilot_manifest.json').read_text(encoding='utf8'))
answers=[a for a in data['answers'] if a.get('answer_part')==1]
statuses=['Answered','Explicit No','Not Disclosed','Not Applicable','Unclear']
db=sqlite3.connect('file:outputs/cdp/cdp.sqlite?mode=ro',uri=True);db.row_factory=sqlite3.Row
run=data['run_id']; costs={r['pdf_id']:dict(r) for r in db.execute('''select pdf_id,sum(api_calls) calls,sum(retries) retries,
sum(input_cached) cached,sum(input_uncached) uncached,sum(output_tokens) output,sum(reasoning_tokens) reasoning,
sum(cost_usd) cost,sum(uncertain_reserved_usd) reserved from cdp_calls where run_id=? group by pdf_id''',(run,))}
jobs=list(db.execute('select pdf_id,group_id,status,payload from cdp_group_jobs where run_id=?',(run,)))
flags=collections.Counter(f['flag_type'] for f in data['flags']);totals=collections.Counter(a['disclosure_status'] for a in answers)
evidence=data['evidence'];verified=sum(e['evidence_verified']=='Yes' for e in evidence)
lines=['# CDP consolidated pilot v2','','Run: '+run+'; status: '+data.get('run_status','unknown'),'','## Status and cost per PDF','',
       '| PDF | Municipality | Calls | Retries | Peak USD | Off-peak estimate USD | Answered | Explicit No | Not Disclosed | Not Applicable | Unclear |',
       '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
for p in manifest:
 x=costs.get(p['pdf_id'],{});cnt=collections.Counter(a['disclosure_status'] for a in answers if a['pdf_id']==p['pdf_id'])
 lines.append('| '+' | '.join(map(str,[p['pdf_id'],p['municipality'],x.get('calls',0),x.get('retries',0),f"{x.get('cost',0):.5f}",f"{x.get('cost',0)*.5:.5f}"]+[cnt[s] for s in statuses]))+' |')
lines+=['','## Status totals','']+[f'- {s}: {totals[s]}' for s in statuses]
lines+=['','## Review flags','']+[f'- {k}: {v}' for k,v in flags.most_common()]
lines+=['',f'Evidence verified {verified}/{len(evidence)}. Quote matching does not establish semantic entailment.','',
        '## Status changes','', '| PDF | Question | v1 | v2 | v2 evidence pages |','|---|---|---|---|---|']
for x in data.get('changed_answers',[]):
 lines.append(f"| {x['pdf_id']} | {x['question_id']} | {x['v1_status']} | {x['v2_status']} | {x['v2_pages']} |")
absence=[a for j in jobs for a in json.loads(j['payload']).get('absence_checks',[])]
retries=[a for j in jobs for a in json.loads(j['payload']).get('evidence_retries',[])]
splits=sum(json.loads(j['payload']).get('output_cap_splits',0) for j in jobs)
lines+=['','## Coverage and checks','',f'- Completed groups: {sum(j["status"]=="done" for j in jobs)}/120.',
        f'- Absence follow-up calls: {len(absence)}; recovered: {sum(bool(a.get("recovered")) for a in absence)}.',
        f'- Evidence retry attempts: {len(retries)}; output-cap splits: {splits}.',
        '- Low-text pages remain uncertain; passing quote checks is not a substitute for research review.']
t=data['run_info'];den=t['cached']+t['uncached'];projected=t['cost']/len(manifest)*1069*1.3
lines+=['','## Cost','',f"- Calls {t['calls']}; retries {t['retries']}; cached input {t['cached']}; uncached input {t['uncached']}; output {t['output']}; reasoning {t['reasoning']}.",
        f"- Cache hit rate: {100*t['cached']/den if den else 0:.1f}%.",
        f"- Recorded peak USD {t['cost']:.5f}; off-peak reporting estimate USD {t['cost']*.5:.5f}; unknown-charge reserve USD {t['uncertain_reserved_usd']:.5f}.",
        f'- Mechanical projection (1,069 indexed PDFs, 1.3 safety factor): peak USD {projected:.2f} / CNY {projected*7.1:.2f}; off-peak USD {projected*.5:.2f} / CNY {projected*.5*7.1:.2f}.',
        '- Projection is unreliable if any plan or group is incomplete; actual billing and cache behavior may differ.','']
(out/'consolidated_pilot_v2_report.md').write_text('\n'.join(lines),encoding='utf8')
print('Statuses',dict(totals),'flags',dict(flags),'evidence',verified,'/',len(evidence),'absence',len(absence),'recovered',sum(bool(a.get('recovered')) for a in absence),'splits',splits,'totals',t)
