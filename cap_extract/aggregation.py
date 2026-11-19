"""Schema v2.3 action aggregation helpers."""
from __future__ import annotations

DETAIL_FIELDS = {
    'responsible_owner': 'responsible_owner',
    'implementation_status': 'implementation_status_normalized',
    'target_value': 'target_value',
    'timeline_milestone': 'timeline_milestone',
    'kpi_metric': 'kpi_metric',
    'funding_source': 'funding_source',
    'funding_status': 'funding_status',
    'budget_CIP_link': 'budget_CIP_link',
    'financial_appraisal': 'financial_appraisal',
}

def _present(value):
    return value not in (None, '') and str(value).strip().casefold() not in {'not disclosed','none','null','nan'}

def aggregate_actions(plan_id: str, actions: list[dict]) -> list[dict]:
    """Build at most one traceable aggregate row per actionable orientation."""
    unclear = [r for r in actions if str(r.get('action_orientation') or '').casefold() == 'unclear']
    result=[]
    for orientation in ('Mitigation','Adaptation'):
        rows=[r for r in actions if r.get('action_orientation') == orientation]
        if not rows:
            continue
        row={
            'plan_id':plan_id,'orientation':orientation,'action_count':len(rows),
            'action_ids':';'.join(str(r.get('action_id') or '') for r in rows if _present(r.get('action_id'))),
            'action_titles':' | '.join(str(r.get('action_title') or '') for r in rows if _present(r.get('action_title'))),
            'unclear_action_count':len(unclear),
        }
        cost_types=[str(r.get('cost_type') or 'Not Disclosed') for r in rows]
        row['cost_disclosed_count']=sum(t!='Not Disclosed' for t in cost_types)
        for label,key in (('Exact Monetary','exact_monetary_cost_count'),('Monetary Range','monetary_range_cost_count'),('Qualitative','qualitative_cost_count'),('Staff/Existing Resources','staff_existing_resource_cost_count'),('No Additional Cost','no_additional_cost_count')):
            row[key]=sum(t==label for t in cost_types)
        row['cost_details_raw']=' || '.join(f"{r.get('action_id','')}: {r.get('cost_raw','')} [{r.get('cost_type','Not Disclosed')}; {r.get('cost_basis','Unclear')}]" for r in rows if _present(r.get('cost_raw')))
        # The extraction does not establish whether estimates are explicitly additive.
        row['aggregated_cost_total']=None
        row['aggregated_cost_total_note']='Additivity and non-overlap are not explicitly established'
        for output,source in DETAIL_FIELDS.items():
            values=[(r.get('action_id',''),r.get(source)) for r in rows if _present(r.get(source))]
            row[output+'_disclosed_count']=len(values)
            row[output+'_details_raw']=' || '.join(f'{aid}: {value}' for aid,value in values)
        result.append(row)
    return result
