"""Prepare a reviewable offline CDP codebook from questionnaire geometry."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from .cdp_codebook import OUT, extract_candidates, pages_from_text, select_question_candidates

MODULES = {'1':'Jurisdiction profile','2':'Governance','3':'Risks and vulnerability','4':'Emissions inventories',
           '5':'Sector data','6':'Adaptation goals','7':'Emissions targets','8':'Other targets',
           '9':'Plans','10':'Actions and projects','12':'Further information','13':'C40 questions'}
RESEARCHER_SINGLE_SELECT = frozenset({
    '2.2_c01','2.7_c02','2.7_c03','2.7_c08','9.1.1_c01','9.1.1_c05',
    '10.1_c03','10.1_c21','10.1_c23','10.2_c03','10.2_c17',
})
RESEARCHER_SINGLE_STATUS = 'Single select by codebook review (mutually exclusive options)'
SECTIONS = ('Tags','Questionnaire','Question Dependencies','Change From Last Year','Requested Content',
            'Example Response','Explanation of Terms','Additional Information','GCoM Guidance',
            'GCoM Compliance Guidance','Scoring - Question Level','Scoring - Point Allocation')
TRIAGE = [
 ('4.3','',69,'No select-list heading','B','Recognized yes/no radio controls','Do you have an emissions inventory'),
 ('4.3.1','',70,'Unattached continuation','B','Recognized table section banner','Emissions methodology'),
 ('4.3.1','Year covered by inventory',71,'Unattached continuation','B','Skipped repeated header and joined continuation','2018/2019'),
 ('4.3.1','Emissions sources included',72,'Unattached continuation','B','Skipped repeated header and joined continuation','Upstream production of materials'),
 ('4.3.2','',73,'Unattached continuation','B','Recognized table section banner','Government operations emissions'),
 ('5.1','',75,'Ambiguous question heading','B','Selected heading followed by question sentence','Provide details of your jurisdiction-wide energy consumption'),
 ('5.1.1','',79,'Unattached continuation','B','Recognized table section banner','Electricity consumption'),
 ('5.1.2','',80,'Unattached continuation','B','Recognized table section banner','Thermal (heating/cooling) consumption'),
 ('5.1.2','Data source used to provide percentage breakdown of consumption by energy type',81,'Unattached continuation','B','Skipped repeated header and joined continuation','Regional/State-level data'),
 ('5.1.3','Energy source',83,'Spurious wrap / duplicate header','B','Discarded nested duplicate rectangle; retain matrix axis','Energy source'),
 ('5.4','Indicator',90,'Spurious wrap / duplicate header','B','Discarded nested duplicate rectangle; retain matrix axis','Indicator'),
 ('5.4','Indicator^',90,'Lowercase option','A','Unit starts with lowercase k; source retained','kWh/year/person'),
 ('5.5','',92,'Unattached continuation','B','Recognized table section banner','Mode share data'),
 ('5.5','Freight mode share',93,'Unattached continuation','B','Skipped repeated header and joined continuation','Mode share data'),
 ('5.6','Mode of transport',96,'Spurious wrap / duplicate header','B','Discarded nested duplicate rectangle; retain matrix axis','Mode of transport'),
 ('5.7','Waste-related data area (unit)',97,'Spurious wrap / duplicate header','B','Discarded nested duplicate rectangle; retain matrix axis','Waste-related data area'),
 ('13.4','Indicate your city’s use of datasets or mapping to identify vulnerable or frontline populations',209,'Option ends in to','A','PDF option ends in to; retained verbatim','No datasets or mapping identifying vulnerable or frontline populations are used and we do not plan to'),
 ('13.7','Type of spatial developmentt',213,'Hyphenated line / duplicate header','B','Joined printed line-break hyphen and removed nested duplicate rectangle','previously developed land'),
 ('13.7','Type of spatial developmentt',214,'Repeated table header','B','Joined printed line-break hyphen and collapsed repeated matrix header','previously developed land'),
]


def _clip(value, qa, q, field):
    value = str(value or '')
    if len(value)>32700:
        qa.append([q,field,'','C','Excel cell too long; full source remains in PDF',value[:200],'Manual review'])
        return value[:32680]+' [truncated]'
    return value


def _question_segments(pages, selected):
    flat = [(n,line) for n in sorted(pages) for line in pages[n].splitlines()]
    starts = []
    for item in selected:
        q,n = item['question_id_candidate'], item['source_page']
        indices = [i for i,(p,line) in enumerate(flat) if p==n and line.strip()==item['heading_verbatim']]
        if len(indices)>1:
            expected = item['following_lines_verbatim'][0].strip()
            indices = [i for i in indices if any(flat[k][1].strip()==expected for k in range(i+1,min(i+5,len(flat))))]
        if len(indices)!=1:
            raise ValueError(f'Ambiguous source heading {q} page {n}: {len(indices)}')
        starts.append(indices[0])
    segments = {}
    for i,item in enumerate(selected):
        lines=flat[starts[i]+1:starts[i+1] if i+1<len(starts) else len(flat)]
        clean=[]
        for page,line in lines:
            if re.match(r'^Scoring\s*[-–]\s*Question Level',line.strip(),re.I): break
            if line.strip().startswith('https://') or 'CDP 2026 questionnaire' in line: continue
            if line.strip(): clean.append((page,line.strip()))
        segments[item['question_id_candidate']]=clean
    return segments


def _section(lines, heading):
    for i,(_,text) in enumerate(lines):
        if text.strip()==heading:
            result=[]
            for _,line in lines[i+1:]:
                if line.strip() in SECTIONS: break
                result.append(line)
            return '\n'.join(result)
    return ''


def _title(lines):
    parts=[]
    for _,line in lines:
        if line in SECTIONS or line.startswith(('Select one option','Select all that apply','Row 1')): break
        parts.append(line)
        if re.search(r'[?.!]\^*$',line): break
        if len(parts)>=5: break
    return '\n'.join(parts)


def _column_guidance(lines, number):
    """Return verbatim guidance under a printed '(column N)' heading."""
    heading = re.compile(r'\(columns?\s+'+str(number)+r'\)\s*$',re.I)
    any_heading = re.compile(r'\(columns?\s+\d+(?:\s*[-–,]\s*\d+)*\)\s*$',re.I)
    for i,(_,line) in enumerate(lines):
        if not heading.search(line):
            continue
        result=[]
        for _,text in lines[i+1:]:
            if text in SECTIONS or any_heading.search(text): break
            result.append(text)
        return '\n'.join(result)
    return ''


def _judgement(q):
    module=q.split('.')[0]
    if q=='1.1' or module=='12': return ('Not applicable','Administrative response or supplemental attachment is not a fact in a CAP.','Not applicable')
    if module in {'2','3','6','7','9','10'}: return ('High','Plans commonly describe governance, hazards, targets, plans or actions; field-level evidence still varies.','Core')
    if module=='8': return ('Medium','Other environmental targets are present in some plans.','Optional')
    if q in {'4.1','4.1.1','4.3'}: return ('Medium','Some plans report an inventory or a summary.','Optional')
    if module in {'4','5','13'}: return ('Low','Detailed sector statistics or initiative-specific information may be absent.','Unlikely')
    return ('Medium','Jurisdiction profile information is present in some plans.','Optional')


def infer_exclusive_choice(column_name, selectable_options):
    """Return evidence for a single-choice inference, or None if inconclusive."""
    name=column_name.casefold()
    values=[re.sub(r'\s+',' ',str(v)).strip() for v in selectable_options if str(v).strip()]
    if not values:
        return None
    # The request explicitly retains these co-occurring categories as multi-select,
    # including labels such as 'primary author(s)' and 'primary framework'.
    multi_categories=('sector','hazard(s) that','hazards that','hazard(s) addressed','hazards addressed',
        'funding source','finance source','co-benefit',
        'gases covered','emissions sources','population groups','communities engaged',
        'author(s)','primary author','primary framework','frameworks','resilience attribute','attributes of resilience',
        'impact indicators','energy access indicators','energy access and/or poverty indicator')
    yn_other=re.compile(r'^(?:yes(?:\b|,)|no(?:\b|,)|do not know|undetermined(?:\s*/\s*do not know)?|not applicable|other,? please specify)\b',re.I)
    if all(yn_other.match(x) for x in values) and any(re.match(r'^yes\b',x,re.I) for x in values) and any(re.match(r'^no\b',x,re.I) for x in values):
        return 'Mutually exclusive Yes / No choices'
    if len(values)>=2 and all(re.fullmatch(r'(?:19|20)\d{2}(?:/(?:19|20)\d{2})?',x) for x in values):
        return 'Year choices'
    if name.startswith('select a reference id'):
        return 'One reference ID per row'
    if len(values)>=2 and all(re.fullmatch(r'[A-Za-z][A-Za-z /-]*\s+\d+',x) for x in values):
        return 'Numbered reference IDs'
    bin_pattern=re.compile(r'^(?:(?:(?:greater|less) than\s+|[<>≤≥]=?\s*)?\d+(?:[.,]\d+)?(?:\s*(?:-|–|to)\s*\d+(?:[.,]\d+)?)?\s*%?(?:\s*(?:years?|months?|days?))?|(?:i do not have (?:this|the) data|not estimated|not available|do not know|unknown|not applicable))$',re.I)
    numeric_pattern=re.compile(r'^(?:(?:greater|less) than\s+|[<>≤≥]=?\s*)?\d',re.I)
    if len(values)>=2 and all(bin_pattern.fullmatch(x) for x in values) and sum(bool(numeric_pattern.match(x)) for x in values)>=2:
        return 'Mutually exclusive numeric or percentage bins'
    scale=re.compile(r'^(?:high|medium(?: high| low)?|low|increasing|decreasing|none|new|underway|achieved|completed|do not know|not expected|not known|major limiting factor|moderate limiting factor|minor limiting factor|significantly challenges|moderately challenges|somewhat challenges|significantly supports|moderately supports|somewhat supports|immediate|short-term|medium-term|long-term|not started|in progress|fully integrated)\b',re.I)
    if len(values)>=2 and all(scale.match(x) for x in values):
        return 'Ordered or mutually exclusive scale'
    if any(term in name for term in multi_categories):
        return None
    exclusive_name=('primary','status','boundary of','level of government','administrative boundary',
                    'currency','common reporting framework level','target type','alignment with',
                    'formally adopted','confirm attachment')
    if any(term in name for term in exclusive_name):
        return 'Field names one primary value, boundary, status or other exclusive choice'
    return None


def build():
    pages=pages_from_text(OUT/'questionnaire_pages.txt')
    candidates,dropped=select_question_candidates(extract_candidates(pages),pages)
    segments=_question_segments(pages,candidates)
    audit=json.loads((OUT/'questionnaire_geometry_audit.json').read_text(encoding='utf8'))
    stats={x['question_id']:x for x in json.loads((OUT/'keyword_screening.json').read_text(encoding='utf8'))}
    code,options,answerability,qa=[],[],[],[]
    for q,col,page,issue,category,resolution,anchor in TRIAGE:
        lines=pages[page].splitlines()
        heading_positions=[i for i,x in enumerate(lines) if re.fullmatch(re.escape(q)+r'(?:\*\(mandatory(?: columns)?\))?',x.strip())]
        if heading_positions:
            lines=lines[heading_positions[-1]+1:]
        i=next((i for i,x in enumerate(lines) if anchor.casefold() in x.casefold()),None)
        excerpt='\n'.join(lines[max(0,i-1):min(len(lines),i+3)]) if i is not None else '[anchor not found; inspect questionnaire page]'
        qa.append([q,col,str(page),category,issue,excerpt,resolution])
    for item in audit['potential_source_clipping']:
        qa.append([item['question_id'],item['column'],str(item['page']),'C','Truncated source heading',item['verbatim_text'],'Retained verbatim; proposed completion requires researcher confirmation'])
    for item in audit['problems']:
        # Remaining two checks are literal unit and source wording, documented above.
        if item['question_id'] not in {'5.4','13.4'}:
            qa.append([item['question_id'],item.get('column',''),str(item.get('page','')),'C',item['issue'],item.get('verbatim_text',''),'Manual review'])
    for candidate in candidates:
        q=candidate['question_id_candidate']; mod=q.split('.')[0]; lines=segments[q]; title=_title(lines)
        questionnaire=_section(lines,'Questionnaire'); cities=bool(re.search(r'(?m)^Cities(?:\s*:|\s*$)',questionnaire)); states=bool(re.search(r'(?m)^States and Regions(?:\s*:|\s*$)',questionnaire))
        applies='both' if cities and states else ('Cities' if cities else ('States and Regions' if states else 'Unclear - review'))
        dependency=_section(lines,'Question Dependencies'); defs=_section(lines,'Explanation of Terms')
        judgement,reason,scope=_judgement(q); stat=stats.get(q,{})
        answerability.append([q,title,judgement,reason,' | '.join(stat.get('detection_keywords',[])),stat.get('corpus_hit_rate'),stat.get('median_matching_pages_all_plans'),scope,'Screening mentions only, not answerability.' + (' Common terms may inflate hits.' if q in {'2.5','5.1'} else '')])
        rows=audit['questions'].get(q,[])
        if not rows:
            qa.append([q,'',str(candidate['source_page']),'C','No parsed response columns','','Manual review'])
        for i,row in enumerate(rows,1):
            name=row['column_name']; cid=f'{q}_c{i:02d}'; opts=row['options']
            guidance=_column_guidance(lines,i)
            general_text=' '.join(text for _,text in lines)
            one_at_time=re.findall(r'only select and report on one\s+(\w+)\s+at a time',general_text,re.I)
            vals=[o['text'] for o in opts if o['is_selectable']]
            placeholder=[o['text'] for o in opts if not o['is_selectable'] and (o['text']=='Write your answer here' or re.fullmatch(r'1234(?:\.\d+)?',o['text']))]
            if row.get('matrix_axis_verbatim'): typ='fixed table axis - review'
            elif placeholder: typ='text' if placeholder[0]=='Write your answer here' else ('percentage' if '%' in name else 'numeric')
            elif not opts: typ='attachment' if 'attachment' in name.casefold() else 'Unclear - review'
            elif len(vals)==1 and re.fullmatch(r'MM/DD/YYYY',vals[0],re.I):
                typ='date'
                for option in opts: option['is_selectable']=False
                vals=[]
            elif q in {'1.1','3.1','4.1','4.2','4.3','6.1','7.1','9.1'}: typ='single select'
            elif re.search(r'select all.{0,40}apply|select multiple',guidance,re.I): typ='multi select'
            elif any(re.search(r'\b'+re.escape(noun)+r's?\b',name,re.I) for noun in one_at_time): typ='single select'
            elif re.search(r'only select and report on one|select (?:only )?one (?:option|hazard|target)|select an option from the list',guidance,re.I): typ='single select'
            elif 'select an option from each group' in name.casefold(): typ='multi select - one per group'
            else: typ='choice - cardinality review'
            cardinality_status='Not applicable'
            if typ=='choice - cardinality review':
                inference=infer_exclusive_choice(name,vals)
                if inference:
                    typ='single select'
                    cardinality_status='Inferred from mutually exclusive options'
                    qa.append([q,name,str(row.get('source_pages',[])),'Review','Inferred single-select cardinality','',inference])
                else:
                    typ='multi select'
                    cardinality_status=('Inferred from guidance wording; confirm against CDP portal' if q=='3.2' and i==2
                                        else 'Unverified - treated as multi select')
                    qa.append([q,name,str(row.get('source_pages',[])),'Review','Unverified selection cardinality','',cardinality_status])
            elif typ in {'single select','multi select','multi select - one per group'}:
                cardinality_status=('From printed response control' if q in {'1.1','3.1','4.1','4.2','4.3','6.1','7.1','9.1'} else 'From guidance text')
            elif typ in {'Unclear - review','fixed table axis - review'}:
                qa.append([q,name,str(row.get('source_pages',[])),'Review','Response type / matrix structure needs confirmation','','Check source guidance'])
            if cid in RESEARCHER_SINGLE_SELECT:
                typ='single select'
                cardinality_status=RESEARCHER_SINGLE_STATUS
                # Replace the previous unverified flag with the researcher's decision.
                qa[:]=[entry for entry in qa if not (entry[0]==q and entry[1]==name and entry[4]=='Unverified selection cardinality')]
                qa.append([q,name,str(row.get('source_pages',[])),'Review','Researcher-reviewed single-select override','',cardinality_status])
            unit_match=re.search(r'\((%|MWh|square km|metric tonnes CO2e|currency|hours/day|hours/week|hours/year|days/year)\)',name,re.I)
            unit=unit_match.group(1) if unit_match else ('%' if '(%)' in name else '')
            response_options=' | '.join(vals)
            glines=guidance.splitlines(); conditional_lines=[]
            for gi,gline in enumerate(glines):
                if re.search(r'(?:column (?:is|appears|will be)|only (?:if|presented|shown)|presented if|shown if)',gline,re.I):
                    sentence=[gline]
                    for next_line in glines[gi+1:]:
                        if re.search(r'[.?!]\s*$',sentence[-1]): break
                        sentence.append(next_line)
                    conditional_lines.append('\n'.join(sentence))
            conditional='\n'.join(conditional_lines)
            max_chars=re.search(r'(?:limit(?:ed)? (?:of|to)|maximum of|up to)\s*([\d,]+)\s*characters',guidance,re.I)
            mandatory='mandatory' in name.casefold() or bool(re.search(r'\(mandatory\)',candidate['heading_verbatim'],re.I))
            code.append([q,mod,MODULES.get(mod,''),_clip(title,qa,q,'question_text'),_clip('\n'.join(x for x in (dependency,conditional) if x),qa,q,'dependency'),applies,'^^' if '^^' in name else ('^' if '^' in name else 'none'), 'Yes' if mod=='13' else 'No', 'Yes' if mandatory else 'No',cid,name,typ,_clip(response_options,qa,q,'response_options'),unit,max_chars.group(1) if max_chars else '',_clip(defs,qa,q,'definitions'),'Leaving a response blank is interpreted as a non-disclosure.', 'Yes' if any('Row 1' in x for _,x in lines) else 'No',', '.join(map(str,row.get('source_pages',[candidate['source_page']]))),judgement,reason,scope,'UNVERIFIED - inspect source',_clip(guidance,qa,q,'column_guidance'),cardinality_status,
                         ' | '.join(str(o['option_level']) for o in opts),' | '.join(o['parent_option'] or '' for o in opts),
                         ' | '.join('Yes' if o['is_selectable'] else 'No' for o in opts),
                         'Yes' if any(o.get('source_truncated') for o in opts) else 'No',
                         ' | '.join(o['reconstructed_text'] for o in opts if o.get('reconstructed_text'))])
            for option in opts:
                options.append([q,cid,name,option['text'],option['option_level'],option['parent_option'] or '', 'Yes' if option['is_selectable'] else 'No','Yes' if option.get('source_truncated') else 'No',option.get('reconstructed_text',''),', '.join(map(str,option.get('source_pages',[])))])
            if row.get('matrix_axis_verbatim'):
                qa.append([q,name,str(row['source_pages']),'Review','Fixed matrix axis stored as source text',row['matrix_axis_verbatim'],'Check row boundaries in PDF'])
    # Non-scoring matters are reviewed without changing the researcher's scope.
    core_c=[x for x in qa if x[3]=='C' and x[0].split('.')[0] in {'2','3','6','7','9','10'} and x[4]!='Truncated source heading']
    core_format_pending=[x for x in qa if x[3]=='C' and x[0].split('.')[0] in {'2','3','6','7','9','10'} and x[4]!='Truncated source heading']
    payload={'codebook':code,'options':options,'answerability':answerability,'qa':qa,'core_c':core_c,'core_format_pending':core_format_pending,
             'counts':{'questions':len(candidates),'columns':len(code),'options':len(options),'by_module':dict(Counter(x[1] for x in code)),'by_scope':dict(Counter(x[7] for x in answerability))},'dropped':dropped}
    (OUT/'codebook_review_data.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(payload['counts'],ensure_ascii=False));print('unresolved_core_C',len(core_c),'pending_core_answer_formats',len(core_format_pending),'review_flags',len(qa))
    if core_c: raise SystemExit('Core C condition: do not build workbook')


if __name__=='__main__': build()
