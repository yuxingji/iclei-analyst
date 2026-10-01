from __future__ import annotations

import json


def truncate_at_word_boundary(value, limit):
    value=str(value or '')
    if len(value)<=limit: return value
    cut=value.rfind(' ',0,limit+1)
    if cut<=0: cut=limit
    return value[:cut].rstrip()


def truncate_capped_fields(data):
    if isinstance(data,list): return [truncate_capped_fields(value) for value in data]
    if not isinstance(data,dict): return data
    result={key:truncate_capped_fields(value) for key,value in data.items()}
    for field,limit in (('evidence_text',200),('action_description',300)):
        if isinstance(result.get(field),str): result[field]=truncate_at_word_boundary(result[field],limit)
    return result


def parse_truncated_json(raw, schema_model):
    parsed=json.loads(raw)
    return schema_model.model_validate(truncate_capped_fields(parsed)).model_dump(exclude_none=True)
