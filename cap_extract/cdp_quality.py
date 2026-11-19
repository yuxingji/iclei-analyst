"""Deterministic CDP coding and quality checks; no outside data or API calls."""
from __future__ import annotations
import re
import unicodedata
from decimal import Decimal, InvalidOperation


def normalise_choice(value):
    value=unicodedata.normalize('NFKC',str(value or '')).casefold()
    value=value.translate(str.maketrans({'’':"'",'‘':"'",'–':'-','—':'-','‐':'-','‑':'-'}))
    return re.sub(r'\s+',' ',value).strip()


CONVERSIONS={
    ('square miles','km2'):Decimal('2.589988110336'),
    ('acres','km2'):Decimal('0.0040468564224'),
    ('short tons','metric tonnes'):Decimal('0.90718474'),
    ('mmbtu','mwh'):Decimal('0.2930710701722222'),
    ('therms','mwh'):Decimal('0.02930710701722222'),
    ('kwh','mwh'):Decimal('0.001'),
}
UNIT_ALIASES={'square mile':'square miles','sq mi':'square miles','mi²':'square miles',
              'acre':'acres','short ton':'short tons','us tons':'short tons',
              'square kilometres':'km2','square kilometers':'km2','square km':'km2','km²':'km2',
              'metric ton':'metric tonnes','metric tons':'metric tonnes','metric tonne':'metric tonnes',
              'megawatt hours':'mwh','megawatt-hours':'mwh','kilowatt hours':'kwh',
              'kilowatt-hours':'kwh','therm':'therms'}


def convert_quantity(value,source_unit,target_unit):
    """Convert only explicit numeric quantities using documented factors."""
    try: amount=Decimal(str(value).replace(',',''))
    except InvalidOperation: return None
    source=normalise_choice(source_unit);target=normalise_choice(target_unit)
    key=(UNIT_ALIASES.get(source,source),UNIT_ALIASES.get(target,target))
    factor=CONVERSIONS.get(key)
    if factor is None:return None
    return amount*factor


def clean_item_separators(value):
    """Only repair semicolons inside balanced brackets, not item boundaries."""
    depth=0;chars=[]
    for ch in str(value or ''):
        if ch in '([':depth+=1
        elif ch in ')]':depth=max(0,depth-1)
        chars.append(',' if ch==';' and depth else ch)
    return re.sub(r',\s*',', ',''.join(chars)).strip()


def apply_quantities(answer,quantities,verified_quotes):
    """Replace an explicitly present source quantity only when the quote supports it."""
    output=answer;derived=[];problems=[]
    quotes=' '.join(verified_quotes)
    for q in quantities:
        raw=str(q.get('original_value',''));unit=str(q.get('original_unit',''));target=str(q.get('target_unit',''))
        value=convert_quantity(raw,unit,target)
        if value is None:
            problems.append(('unit_not_converted',f'{raw} {unit} -> {target}'));continue
        pattern=re.compile(r'(?<!\w)'+re.escape(raw)+r'\s*'+re.escape(unit)+r'\b',re.I)
        if not pattern.search(quotes):
            problems.append(('quantity_evidence_missing',f'{raw} {unit}'));continue
        if not pattern.search(output):
            problems.append(('quantity_not_in_answer',f'{raw} {unit}'));continue
        rounded=value.quantize(Decimal('0.000001')).normalize()
        replacement=f'{rounded:f} {target}'
        output=pattern.sub(replacement,output,count=1);derived.append(replacement)
    return output,derived,problems


def numeric_tokens(value):
    """Return comparable numbers; labels such as Item 1 are removed by caller."""
    text=unicodedata.normalize('NFKC',str(value or ''))
    found=re.findall(r'(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w.])',text)
    result=set()
    for number in found:
        try:result.add(Decimal(number.replace(',','')).normalize())
        except InvalidOperation:pass
    return result


def unsupported_numbers(answer,verified_quotes,derived_values=()):
    supported=numeric_tokens(' '.join(verified_quotes))
    for x in derived_values:supported.update(numeric_tokens(x))
    return sorted(numeric_tokens(answer)-supported)


def document_language(pages):
    """Conservative English detection from common function words, not city facts."""
    stop={'the','and','of','to','in','for','is','with','that','this','as','by','from','on','are','will','be','or'}
    tokens=re.findall(r"[a-z]+",' '.join(t or '' for _,t in pages).casefold())
    if len(tokens)<40:return None
    matches=sum(word in stop for word in tokens)
    if matches/len(tokens)<0.19:return None
    for n,text in pages:
        text=' '.join((text or '').split())
        if len(text)>=50:return {'language':'English','page':n,'excerpt':text[:250], 'basis':'Document-evident rule: language detected from document text'}
    return None


def document_evident_answers(plan,pages,question_id):
    """Use only narrowly supported document features; no hierarchy guess."""
    name=str(plan.get('municipality') or '').strip()
    if not name:return []
    cover=[]
    for n,text in pages:
        if n>3:continue
        match=re.search(re.escape(name),text or '',re.I)
        if match:
            excerpt=' '.join((text or '')[max(0,match.start()-100):match.end()+100].split())
            cover.append((n,excerpt))
    if not cover:return []
    page,quote=cover[0]
    evidence=[{'page':page,'evidence_excerpt':quote,'confidence':'High'}]
    if question_id=='1.2':
        government=str(plan.get('gov_type') or '').casefold()
        boundary={'city':'City/Municipality','town':'Town','county':'County / Province'}.get(government)
        result=[]
        if boundary:
            result.append({'question_id':'1.2','disclosure_status':'Answered','answer':boundary,'evidence':evidence,
                           'choice_fields':[{'column_id':'1.2_c01','value':boundary}]})
        us_states=set('AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC'.split())
        if str(plan.get('state') or '').upper() in us_states:
            for n,text in pages:
                match=re.search(r'\$\s*\d[\d,]*(?:\.\d+)?',text or '')
                if match:
                    excerpt=' '.join((text or '')[max(0,match.start()-80):match.end()+100].split())
                    result.append({'question_id':'1.2','disclosure_status':'Answered','answer':'USD US Dollar',
                                   'choice_fields':[{'column_id':'1.2_c09','value':'USD US Dollar'}],
                                   'evidence':[{'page':n,'evidence_excerpt':excerpt,'confidence':'Medium'}]})
                    break
        return result
    if question_id=='9.1':
        for n,text in pages:
            if n>3:continue
            match=re.search(r'\bclimate\s+action\s+(?:plan|strategy)\b',text or '',re.I)
            if match:
                title=' '.join((text or '')[max(0,match.start()-80):match.end()+100].split())
                return [{'question_id':'9.1','disclosure_status':'Answered',
                         'answer':'Yes, our jurisdiction has a climate action plan or strategy',
                         'choice_fields':[{'column_id':'9.1_c01','value':'Yes, our jurisdiction has a climate action plan or strategy'}],
                         'evidence':evidence+[{'page':n,'evidence_excerpt':title,'confidence':'High'}]}]
    return []
