"""Offline CDP question cards and proposed 2025 question-text crosswalk.

Only the question-number and question-text columns of the 2025 workbook are
read here. Disclosure responses never enter the generated question cards.
"""
from __future__ import annotations
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

from openpyxl import load_workbook
from rapidfuzz.fuzz import token_set_ratio

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'outputs' / 'cdp'
CODEBOOK = OUT / 'cdp_2026_codebook.xlsx'
PUBLIC_2025 = ROOT / 'inputs' / '2025_Full_Cities_Public_Data_Separated_by_Question_1.xlsx'

DOCUMENT_EVIDENT_RULES={
    '1.1':'Detect main written language from document text; cite a representative passage.',
    '1.2':'Use a jurisdiction named on the cover for administrative boundary; use USD only with a supported dollar amount. Validate each mapping against the questionnaire options.',
    '9.1':'Only if the document itself is a climate, energy, sustainability or resilience plan of the named jurisdiction, answer Yes with title evidence.',
    '9.1.1':'Describe this document first only when its title and jurisdiction establish that it is the relevant plan; cite title and adoption evidence.',
}


def read_table(sheet):
    iterator=sheet.iter_rows(values_only=True)
    headers=next(iterator)
    return [dict(zip(headers,row)) for row in iterator if any(value is not None for value in row)]


def load_question_texts_2025(path: Path):
    workbook=load_workbook(path,read_only=True,data_only=True)
    questions={}
    for sheet in workbook:
        if not re.fullmatch(r'Q\d+(?:\.\d+)+',sheet.title,re.I):
            continue
        iterator=sheet.iter_rows(values_only=True)
        headers=next(iterator,())
        positions={str(value):index for index,value in enumerate(headers) if value is not None}
        if not {'question_number','question_text'} <= positions.keys():
            continue
        for row in iterator:
            qid=row[positions['question_number']]; text=row[positions['question_text']]
            if qid and text:
                # Deliberately retain only these two approved columns.
                questions[str(qid).strip()]=str(text).strip()
                break
    workbook.close()
    return questions


def _normalise(value):
    return re.sub(r'\s+',' ',re.sub(r'[^a-z0-9\s]',' ',value.casefold())).strip()


def proposed_matches(cards, old_questions, threshold=70, max_matches=5):
    """Return text-similarity candidates, never confirmed mappings."""
    matches=[]
    for card in cards:
        scores=sorted(((token_set_ratio(_normalise(card['question_text']),_normalise(text)),qid,text)
                       for qid,text in old_questions.items()),reverse=True)
        for score,qid,text in [s for s in scores if s[0]>=threshold][:max_matches]:
            matches.append({'cdp_2025_question_id':qid,'cdp_2025_question_text':text,
                            'cdp_2026_question_id':card['question_id'],'cdp_2026_question_text':card['question_text'],
                            'match_score':round(score,2),'match_method':'rapidfuzz token_set_ratio on normalized question text',
                            'review_status':'Proposed - researcher to confirm'})
    return matches


def _select_guidance(fields, limit=1500):
    seen=set(); chosen=[]; length=0
    terms=re.compile(r'\b(?:only|must|shall|include|exclude|define|means|report|metric|unit|tonnes?|year|percent|boundary)\b',re.I)
    for field in fields:
        content='\n'.join(str(field.get(key) or '') for key in ('column_guidance_verbatim','definitions'))
        for raw in re.split(r'(?<=[.!?])\s+|\n+',content):
            sentence=' '.join(raw.split())
            if len(sentence)<18 or not terms.search(sentence) or sentence in seen:
                continue
            if length+len(sentence)+1>limit:
                continue
            chosen.append(sentence);seen.add(sentence);length+=len(sentence)+1
    return ' '.join(chosen)


def build_cards(rows, option_rows, question_dependencies):
    by_q=defaultdict(list);by_col=defaultdict(list)
    for row in rows: by_q[str(row['question_id'])].append(row)
    for row in option_rows:
        if str(row.get('is_selectable') or '').casefold()=='yes':by_col[str(row['column_id'])].append(str(row.get('option_text') or ''))
    cards=[]
    for q,fields in by_q.items():
        fields.sort(key=lambda f:int(str(f['column_id']).rsplit('c',1)[1]))
        first=fields[0]; names=[str(f.get('column_name') or '') for f in fields]
        multi=any(str(f.get('is_table_question') or '').casefold()=='yes' for f in fields) or any(str(f.get('response_type') or '').startswith('fixed table axis') for f in fields)
        first_meaningful=next((n for n in names if n.casefold() not in {'enter row header','row header'}),names[0])
        ref=re.search(r'\b(?:for the|for this)\s+([a-z-]+)',first_meaningful,re.I)
        unit=(ref.group(1) if ref else first_meaningful) if multi else ''
        vocab=[]; inference=[]
        for field in fields:
            label=str(field.get('column_name') or ''); values=by_col.get(str(field['column_id']),[])
            if values:
                if 'currency' in label.casefold() and len(values)>30: phrase='ISO currency, e.g. USD US Dollar'
                elif 'year' in label.casefold() and len(values)>15: phrase='Year as specified in questionnaire'
                elif 'reference id' in label.casefold() and len(values)>15: phrase='Numbered reference IDs as shown in questionnaire'
                else: phrase=' | '.join(values)
                vocab.append(f'{label}: {phrase}')
            status=str(field.get('cardinality_status') or '')
            if status and ('inferred' in status.casefold() or 'unverified' in status.casefold() or 'review' in status.casefold()): inference.append(f'{label}: {status}')
        ordered=' ; '.join(f"{f.get('column_id') or ''}: {f.get('column_name') or ''} [{f.get('response_type') or 'Unclear'}]" for f in fields)
        template='Concise supported items separated by semicolons; keep each item\'s attributes together.' if multi else 'A concise answer using only supported information.'
        cards.append({'question_id':q,'module':str(first.get('module') or ''),'question_text':str(first.get('question_text') or ''),
                      'is_multi_item':'Yes' if multi else 'No','item_unit':unit,'fields_in_order':ordered,
                      'option_vocabulary':'\n'.join(vocab),'key_guidance':_select_guidance(fields),
                      'answer_template':template,'not_applicable_rule':question_dependencies.get(q,''),
                      'inference_basis':'\n'.join(inference),'document_evident_rule':DOCUMENT_EVIDENT_RULES.get(q,'')})
    return cards


def prepare():
    from .cdp_codebook import pages_from_text, extract_candidates, select_question_candidates
    from .cdp_export_data import _question_segments, _section
    workbook=load_workbook(CODEBOOK,read_only=True,data_only=True)
    fields=read_table(workbook['Codebook']); options=read_table(workbook['Options']); workbook.close()
    pages=pages_from_text(OUT/'questionnaire_pages.txt')
    selected,_=select_question_candidates(extract_candidates(pages),pages)
    segments=_question_segments(pages,selected)
    dependencies={q:_section(lines,'Question Dependencies') for q,lines in segments.items()}
    cards=build_cards(fields,options,dependencies)
    old_questions=load_question_texts_2025(PUBLIC_2025)
    matches=proposed_matches(cards,old_questions)
    by_new=defaultdict(list)
    for match in matches:by_new[match['cdp_2026_question_id']].append(match)
    for card in cards:
        proposed=by_new.get(card['question_id'],[])
        card['cdp_2025_question_id_proposed']=' | '.join(m['cdp_2025_question_id'] for m in proposed)
        card['cdp_2025_match_score']=' | '.join(str(m['match_score']) for m in proposed)
        card['cdp_2025_match_method']='rapidfuzz token_set_ratio; Proposed - researcher to confirm' if proposed else 'Unmatched'
    matched_old={m['cdp_2025_question_id'] for m in matches}
    matched_new={m['cdp_2026_question_id'] for m in matches}
    unmatched_old=[{'cdp_2025_question_id':qid,'cdp_2025_question_text':text,'cdp_2026_question_id':'','cdp_2026_question_text':'','match_score':None,'match_method':'Unmatched','review_status':'Unmatched'} for qid,text in old_questions.items() if qid not in matched_old]
    unmatched_new=[{'cdp_2025_question_id':'','cdp_2025_question_text':'','cdp_2026_question_id':c['question_id'],'cdp_2026_question_text':c['question_text'],'match_score':None,'match_method':'Unmatched','review_status':'Unmatched'} for c in cards if c['question_id'] not in matched_new]
    payload={'cards':cards,'crosswalk':matches+unmatched_old+unmatched_new,
             'counts':{'questions_2026':len(cards),'questions_2025':len(old_questions),'proposed_matches':len(matches),'unmatched_2025':len(unmatched_old),'unmatched_2026':len(unmatched_new)}}
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/'consolidated_question_cards_prepared.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(payload['counts'],ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.parse_args();prepare()
