"""Offline preparation and controlled execution of CDP consolidated responses."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from .cdp_consolidated_prepare import ROOT, OUT, DOCUMENT_EVIDENT_RULES

GROUPS = ('M1','M2','M3','M4','M5a','M5b','M6_M8','M7','M9','M10','M12_M13a','M13b')
PROMPT_VERSION='cdp-consolidated-v2-full-document'
SAFE_CARD_FIELDS=('question_id','module','question_text','is_multi_item','item_unit',
                  'fields_in_order','option_vocabulary','key_guidance','answer_template',
                  'not_applicable_rule','inference_basis','document_evident_rule')


def group_for(question_id):
    parts=question_id.split('.');module=int(parts[0]);sub=int(parts[1]) if len(parts)>1 else 0
    if module in (1,2,3,4,7,9,10):return f'M{module}'
    if module==5:return 'M5a' if sub<=5 else 'M5b'
    if module in (6,8):return 'M6_M8'
    if module==12 or (module==13 and sub<=6):return 'M12_M13a'
    if module==13:return 'M13b'
    raise ValueError(f'Unexpected questionnaire question: {question_id}')


def cards_by_group(cards):
    grouped={g:[] for g in GROUPS}
    for card in cards:
        # Crosswalk information is intentionally omitted from every model prompt.
        safe={k:card.get(k,'') for k in SAFE_CARD_FIELDS}
        safe['document_evident_rule']=DOCUMENT_EVIDENT_RULES.get(card['question_id'],'')
        safe['answer_template']='Use a concise natural answer with supported information only.'
        grouped[group_for(card['question_id'])].append(safe)
    if any(not rows for rows in grouped.values()):raise ValueError('Empty question group')
    return grouped


def select_pages(pages, hits, keywords, top_k):
    """Rank with group-specific literals and existing page relevance signals."""
    scored=[]; contents=[]
    for number,text in pages:
        folded=(text or '').casefold()
        matches=sum(1 for term in keywords if term and term.casefold() in folded)
        hit=hits.get(number,{})
        score=matches*3+min(float(hit.get('relevance') or 0),10)*.1
        if hit.get('action_table') and top_k==25:score+=2
        scored.append((score,number))
        if re.search(r'(?im)^\s*(?:table of contents|contents)\s*$',text or ''):contents.append(number)
    selected={number for _,number in sorted(scored,key=lambda x:(-x[0],x[1]))[:top_k]}
    selected.update(number for number,_ in pages if number<=3)
    selected.update(contents)
    return sorted(selected)


def open_cap_readonly():
    path=(ROOT/'outputs'/'cap_extract'/'cap.sqlite').resolve()
    c=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
    return c


def selected_pages_for_plan(c, plan_id, groups, keyword_sets):
    pages=[(r['page_no'],r['text']) for r in c.execute('SELECT page_no,text FROM pages WHERE plan_id=? ORDER BY page_no',(plan_id,))]
    hits={r['page_no']:dict(r) for r in c.execute('SELECT page_no,relevance,action_table FROM page_hits WHERE plan_id=?',(plan_id,))}
    selection={}
    for group,cards in groups.items():
        terms=list(dict.fromkeys(t for card in cards for t in keyword_sets.get(card['question_id'],[])))
        selection[group]=select_pages(pages,hits,terms,25 if group=='M10' else 15)
    return pages,selection

def full_document_selection(pages,groups):
    """Every page is inspected for every question group, regardless of hits."""
    return {group:[number for number,_ in pages] for group in groups}

def page_chunks(pages,max_chars=650000):
    """Conservative UTF-8-byte bound below 70% of the 1,048,576-token context."""
    chunks=[]; current=[]; size=0
    for number,text in pages:
        text=text or ''
        page_size=len(text.encode('utf8'))
        if page_size>max_chars:
            if current:chunks.append(current);current=[];size=0
            # At most max_chars Unicode characters per slice; shrink on UTF-8
            # boundaries when multibyte characters would exceed the byte limit.
            while text:
                cut=min(len(text),max_chars)
                while len(text[:cut].encode('utf8'))>max_chars:cut=max(1,cut//2)
                chunks.append([(number,text[:cut])]);text=text[cut:]
            continue
        if current and size+page_size>max_chars:
            chunks.append(current);current=[];size=0
        current.append((number,text));size+=page_size
    if current:chunks.append(current)
    return chunks


def estimate(manifest,cards,keyword_sets):
    from .cdp_consolidated_run import system_prompt
    groups=cards_by_group(cards); cap=open_cap_readonly();plans=[];total_input=0
    for plan in manifest:
        pages,_=selected_pages_for_plan(cap,plan['plan_id'],groups,keyword_sets)
        selection=full_document_selection(pages,groups);chunks=page_chunks(pages)
        page_text=dict(pages);input_tokens=0
        for group,group_cards in groups.items():
            static=system_prompt(group_cards)
            input_tokens+=sum((len(static)+sum(len(text or '') for _,text in chunk)+2)//3 for chunk in chunks)
        total_input+=input_tokens
        plans.append({'plan_id':plan['plan_id'],'pages_total':len(pages),'selected':{k:len(v) for k,v in selection.items()},'baseline_calls':len(chunks)*len(groups),'input_tokens_est':input_tokens})
    cap.close()
    calls=sum(p['baseline_calls'] for p in plans);output_est=calls*2500
    peak=(total_input*.30+output_est*1.20)/1_000_000
    return {'groups':{k:[x['question_id'] for x in v] for k,v in groups.items()},
            'plans':plans,'baseline_calls':calls,'input_tokens_est':total_input,
            'output_tokens_est':output_est,'peak_usd_est':peak,'offpeak_usd_est':peak*.5,
            'peak_cny_est':peak*7.1,'offpeak_cny_est':peak*.5*7.1,
            'assumptions':'Approximate input characters / 3, 2500 output tokens per call, no cache savings; adaptive splits/retries add calls.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['estimate'])
    parser.add_argument('--limit',type=int,default=10)
    args=parser.parse_args()
    manifest=json.loads((OUT/'consolidated_pilot_manifest.json').read_text(encoding='utf8'))[:args.limit]
    cards=json.loads((OUT/'consolidated_question_cards_prepared.json').read_text(encoding='utf8'))['cards']
    keywords=json.loads((OUT/'keywords_draft.json').read_text(encoding='utf8'))
    result=estimate(manifest,cards,keywords)
    (OUT/'consolidated_pilot_estimate.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
