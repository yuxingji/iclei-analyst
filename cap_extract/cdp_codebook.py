"""Offline CDP questionnaire preparation; never invokes an LLM provider.

Question and keyword candidates require human review before use as a codebook.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'outputs' / 'cdp'
PAGE_MARKER = re.compile(r'^===== PDF PAGE (\d+) =====$', re.MULTILINE)
QUESTION = re.compile(r'^\s*(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)\s*\*?\s*(?:\(mandatory(?: columns)?\))?\s*$', re.I)
SCORING = re.compile(r'^\s*(?:Scoring\s*[-–]|Disclosure scoring criteria|Awareness scoring criteria|Management scoring criteria|Leadership scoring criteria|Scoring - Point Allocation)', re.I)


def option_line_geometry(page, left, top, bottom, page_number):
    """Extract printed lines and drawn list markers, not inferred bullet text."""
    words = [w for w in page.extract_words() if w['x0'] >= left and top <= w['top'] < bottom]
    words.sort(key=lambda w: (w['top'], w['x0']))
    lines = []
    for word in words:
        if not lines or abs(word['top'] - lines[-1]['y']) > 2:
            lines.append({'y': word['top'], 'words': []})
        lines[-1]['words'].append(word)
    dots = [o for o in page.curves + page.rects if left <= o['x0'] < page.width
            and top <= o['top'] < bottom and .8 <= o['x1']-o['x0'] <= 3.5
            and .8 <= o['bottom']-o['top'] <= 3.5 and o.get('fill')]
    result = []
    for line in lines:
        ws = sorted(line['words'], key=lambda w: w['x0'])
        marker = [d for d in dots if abs(d['top'] - line['y']) <= 4 and 0 <= ws[0]['x0']-d['x1'] <= 12]
        result.append({'text': ' '.join(w['text'] for w in ws), 'page': page_number,
                       'x0': ws[0]['x0'], 'x1': max(w['x1'] for w in ws),
                       'bullet_x': min((m['x0'] for m in marker), default=None)})
    return result


def assemble_options(lines):
    """Group drawn bullets and continuation lines; retain hierarchy and QA."""
    options, duplicates, warnings = [], [], []
    for index, line in enumerate(lines):
        text = line['text'].strip()
        if not text or re.fullmatch(r'Row\s+\d+', text, re.I):
            continue
        bullet = line['bullet_x']
        following = lines[index+1] if index+1 < len(lines) else None
        # Unbulleted, less-indented headings precede a nested bulleted list.
        parent = bool(bullet is None and following and following['bullet_x'] is not None
                      and following['x0'] - line['x0'] >= 8)
        if bullet is not None or parent or not options:
            level = 1
            parent_option = None
            if bullet is not None and options:
                previous_parent = next((o for o in reversed(options) if not o['is_selectable']), None)
                if previous_parent and line['x0'] - previous_parent['x0'] >= 8:
                    level, parent_option = 2, previous_parent['text']
            opts = {'text': text, 'option_level': level, 'parent_option': parent_option,
                    'is_selectable': not parent, 'source_pages': [line['page']],
                    'source_truncated': False, 'reconstructed_text': '', 'x0': line['x0']}
            options.append(opts)
        else:
            # A hyphen at the end of a printed line is part of a broken word.
            separator = '' if options[-1]['text'].endswith('-') and text[:1].islower() else ' '
            options[-1]['text'] += separator + text
            if line['page'] not in options[-1]['source_pages']:
                options[-1]['source_pages'].append(line['page'])
        if line['x1'] >= 566:
            warnings.append({'page': line['page'], 'verbatim_line': text, 'x1': round(line['x1'], 2),
                             'reason': 'Text reaches printed table edge; inspect source clipping'})
            options[-1]['source_truncated'] = True
    # Exact duplicates in one column are logged, not silently removed.
    seen, unique = {}, []
    for option in options:
        text_key = option['text'].casefold()
        parent_key = (option['parent_option'] or '').casefold()
        # Identical labels under different parents represent different choices.
        # A heading repeated verbatim as its own child is one choice.
        key = (text_key, '' if parent_key == text_key else parent_key)
        if key in seen:
            prior = unique[seen[key]]
            if option['is_selectable'] and not prior['is_selectable']:
                duplicates.append({'text': prior['text'], 'pages': prior['source_pages'], 'removed': 'non-selectable duplicate heading'})
                option.update(option_level=1, parent_option=None)
                unique[seen[key]] = option
            else:
                duplicates.append({'text': option['text'], 'pages': option['source_pages'], 'removed': 'duplicate option'})
            continue
        seen[key] = len(unique); unique.append(option)
    return unique, duplicates, warnings


def parse_choice_question(page, page_number, question_text, lower=0, upper=None):
    """Read a printed select-one/select-all list, including multi-column lists."""
    upper = page.height if upper is None else upper
    headers = [h for h in page.search(r'Select (?:one option|all that apply)', regex=True) if lower <= h['top'] < upper]
    if not headers:
        radios = sorted([o for o in page.curves if o.get('stroke') and not o.get('fill')
                         and lower < o['top'] < upper and 7 <= o['x1']-o['x0'] <= 12
                         and 7 <= o['bottom']-o['top'] <= 12], key=lambda o:o['top'])
        options = []
        for radio in radios:
            nearby = [w for w in page.extract_words() if radio['x1'] <= w['x0'] and abs(w['top']-radio['top']) < 5]
            if nearby:
                text = ' '.join(w['text'] for w in sorted(nearby,key=lambda w:w['x0']))
                options.append({'text':text,'option_level':1,'parent_option':None,'is_selectable':True,'source_pages':[page_number]})
        return {'column_name':question_text,'options':options,'review_status':'UNVERIFIED: inspect radio controls'}
    start = headers[0]['bottom']
    ends = [m['top'] for phrase in ('Tags', 'Requested Content', 'Questionnaire')
            for m in page.search(phrase) if m['top'] > start and m['x0'] < 60]
    end = min(ends + [upper], default=page.height-30)
    dots = [o for o in page.curves + page.rects if o.get('fill') and start < o['top'] < end
            and .8 <= o['x1']-o['x0'] <= 3.5 and .8 <= o['bottom']-o['top'] <= 3.5]
    columns = sorted({round(o['x0']) for o in dots})
    options = []
    for i, x in enumerate(columns):
        column_dots = sorted([o for o in dots if abs(o['x0']-x)<2], key=lambda o:o['top'])
        right = columns[i+1]-3 if i+1<len(columns) else page.width
        for j, dot in enumerate(column_dots):
            y0 = max(start, dot['top']-4)
            y1 = column_dots[j+1]['top']-4 if j+1<len(column_dots) else end-3
            text = page.crop((dot['x1']+.1,y0,right,y1)).extract_text() or ''
            text = re.sub(r'\s+', ' ', text).strip()
            if text:
                options.append({'text':text, 'option_level':1, 'parent_option':None,
                                'is_selectable':True, 'source_pages':[page_number]})
    return {'column_name': question_text, 'options': options,
            'review_status': 'UNVERIFIED: compare list and layout against source PDF'}


def audit_questionnaire(pdf, candidates):
    """Audit candidate response regions without reading any municipal answers."""
    results, problems, clipped = {}, [], []
    for candidate in candidates:
        q, page = candidate['question_id_candidate'], candidate['source_page']
        regions, limits, boundary_error = response_section(pdf, q, page)
        if boundary_error:
            problems.append({'question_id':q,'page':page,'issue':boundary_error})
            continue
        rows = parse_dark_table(regions, limits)
        if not rows:
            for number, p in regions:
                lo, hi = limits[number]
                row = parse_choice_question(p,number,' '.join(candidate['following_lines_verbatim'][:2]),lo,hi)
                if row['options']:
                    row['source_pages']=[number]
                    rows=[row]
                    break
        if not rows:
            problems.append({'question_id':q,'page':page,'issue':'No dark table or select list detected; input field or layout requires review'})
        for row in rows:
            if not row['column_name']:
                problems.append({'question_id':q,'page':row['source_pages'],'issue':'Unattached continuation'})
            for option in row['options']:
                value = option['text'].strip()
                if option.get('source_truncated'):
                    clipped.append({'question_id':q,'column':row['column_name'], 'page':option['source_pages'], 'verbatim_text':value})
                    continue
                if value and (value[0].islower() or value.endswith('-') or re.search(r'\b(?:and|or|of|by|to|the)$',value,re.I)):
                    problems.append({'question_id':q,'column':row['column_name'], 'page':option['source_pages'], 'issue':'Potential wrap error', 'verbatim_text':value})
        results[q]=rows
    return {'questions':results,'problems':problems,'potential_source_clipping':clipped,
            'notice':'Automatic checks are screening only; page-edge flags require source inspection.'}


def parse_dark_table(page_objects, limits=None):
    """Read dark label cells and their aligned content, retaining page breaks."""
    rows = []
    for page_number, page in page_objects:
        lower, upper = (limits or {}).get(page_number, (0, page.height))
        cells = []
        for rect in page.rects:
            color = rect.get('non_stroking_color')
            if not isinstance(color, (tuple, list)) or len(color) != 3:
                continue
            if any(abs(a-b) > .015 for a,b in zip(color, (.2784,.3294,.3882))):
                continue
            x0,x1,top,bottom = (rect[k] for k in ('x0','x1','top','bottom'))
            if top < lower or bottom > upper + .5:
                continue
            if x0 > 30.5 or not 150 < x1-x0 < 285 or bottom-top < 10:
                continue
            label = (page.crop((x0+1,top+.2,x1-.5,bottom-.2)).extract_text() or '').strip()
            right = (page.crop((x1+.5,top+.2,page.width,bottom-.2)).extract_text() or '').strip()
            edge_words = [w for w in page.extract_words() if w['x0'] >= x1 and top <= w['top'] < bottom and w['x1'] >= 566]
            warnings = [{'page': page_number, 'word': w['text'], 'x1': round(w['x1'], 2),
                         'reason': 'Text reaches or crosses the printed table edge; inspect the source PDF for clipping'} for w in edge_words]
            geometry = option_line_geometry(page, x1+.5, top+.2, bottom-.2, page_number)
            matrix_header = any(other is not rect and isinstance(other.get('non_stroking_color'), (tuple,list))
                and len(other['non_stroking_color']) == 3
                and all(abs(a-b)<.015 for a,b in zip(other['non_stroking_color'],(.2784,.3294,.3882)))
                and other['x0'] > x0+1 and other['x1'] < x1-1
                and other['top'] > top+1 and other['bottom'] < bottom-1
                for other in page.rects)
            cells.append((top,bottom,label,right,warnings,geometry,matrix_header))
        for top,bottom,label,right,warnings,geometry,matrix_header in sorted(cells):
            # Printed web tables repeat a "Row 1" banner at page breaks;
            # it is a table header, not an option or a data field.
            if not label and re.fullmatch(r'Row\s+\d+', right, re.I):
                continue
            if not label and not any(x['bullet_x'] is not None for x in geometry) and bottom-top <= 29 and right and not re.search(r'\d|answer|select|attach',right,re.I):
                # Printed table section banner, sometimes repeated on the next page.
                continue
            if not label and rows and top < 65 and rows[-1]['ends_at_page_bottom'] and rows[-1]['source_pages'][-1] == page_number-1:
                rows[-1]['response_lines'].extend(right.splitlines())
                rows[-1]['source_pages'].append(page_number)
                rows[-1]['clip_warnings'].extend(warnings)
                rows[-1]['_geometry'].extend(geometry)
                rows[-1]['ends_at_page_bottom'] = bottom > page.height-35
            elif label:
                rows.append({'column_name': re.sub(r'\s+', ' ', label),
                             'response_lines': right.splitlines(),
                             'source_pages': [page_number],
                             'clip_warnings': warnings,
                             'matrix_header': matrix_header,
                             '_geometry': geometry,
                             'ends_at_page_bottom': bottom > page.height-35})
            else:
                rows.append({'column_name': '', 'response_lines': right.splitlines(),
                             'source_pages': [page_number], 'clip_warnings': warnings, 'matrix_header': matrix_header, '_geometry': geometry, 'ends_at_page_bottom': bottom > page.height-35,
                             'review_status': 'Unattached continuation'})
    seen_matrix_headers = {}
    filtered_rows = []
    for row in rows:
        row['options'], row['duplicate_options_removed'], row['line_warnings'] = assemble_options(row.pop('_geometry'))
        if row['matrix_header'] and len(row['options']) == 1 and ',' in row['options'][0]['text']:
            # These are fixed matrix row/column labels, not selectable choices.
            original = row['options'][0]
            pieces = [x.strip() for x in re.split(r',\s*(?![^()]*\))',original['text']) if x.strip()]
            axis = []
            for piece in pieces:
                if axis and (piece[:1].islower() or piece.casefold().startswith(('and/or ', 'i.e. ', 'e.g. '))):
                    axis[-1] += ', ' + piece
                else:
                    axis.append(piece)
            row['matrix_axis_values'] = axis
            row['matrix_axis_verbatim'] = original['text']
            original['is_selectable'] = False
            key = (row['column_name'], original['text'])
            if key in seen_matrix_headers and row['source_pages'][0] > seen_matrix_headers[key]['source_pages'][0]:
                seen_matrix_headers[key].setdefault('repeated_header_pages', []).extend(row['source_pages'])
                continue
            seen_matrix_headers[key] = row
        for option in row['options']:
            if option['text'] == 'Write your answer here' or re.fullmatch(r'1234(?:\.\d+)?',option['text']):
                option['is_selectable'] = False
        for option in row['options']:
            if option['text'].endswith('select applicable m'):
                option.update(is_selectable=False, source_truncated=True,
                              reconstructed_text='(select applicable methodology) - Proposed completion, researcher to confirm')
            elif option['text'].endswith('select futur'):
                option.update(is_selectable=False, source_truncated=True,
                              reconstructed_text='(select future commitment) - Proposed completion, researcher to confirm')
        filtered_rows.append(row)
    return filtered_rows


def response_section(pdf, question_id, page_number):
    """Locate one candidate response area, stopping before guidance/scoring."""
    page = pdf.pages[page_number-1]
    pattern = r'(?m)^' + re.escape(question_id) + r'(?:\*\(mandatory(?: columns)?\))?\s*$'
    headings = page.search(pattern, regex=True)
    if len(headings) > 1:
        sentence = re.compile(r'^(?:what|which|where|how|has|have|does|do|is|are|if|attach|provide|report|describe|identify|select|use|please|c40 requested question)\b', re.I)
        headings = [h for h in headings if sentence.match(((page.crop((0,h['bottom']+1,page.width,min(page.height,h['bottom']+40))).extract_text() or '').splitlines() or [''])[0].strip())]
    if len(headings) != 1:
        return [], {}, f'Expected one structurally plausible heading, found {len(headings)}'
    start = headings[0]['bottom']
    regions, limits = [], {}
    for number in range(page_number, min(page_number+12, len(pdf.pages))+1):
        current = pdf.pages[number-1]
        lo = start if number == page_number else 25
        markers = []
        for pattern in (r'(?m)^Tags\s*$', r'(?m)^Requested Content\s*$',
                        r'(?m)^Questionnaire\s*$', r'(?m)^Scoring\s*[-–].*$',
                        r'(?m)^\d{1,2}\.\d{1,2}(?:\.\d{1,2})?\*?(?:\(mandatory(?: columns)?\))?\s*$'):
            markers += [m['top'] for m in current.search(pattern, regex=True) if m['top'] > lo and m['x0'] < 60]
        hi = min(markers, default=current.height-25)
        regions.append((number,current)); limits[number]=(lo,hi)
        if markers:
            return regions,limits,''
    return regions,limits,'Response boundary not found within 12 pages'


def pages_from_text(path: Path) -> dict[int, str]:
    text = path.read_text(encoding='utf-8')
    markers = list(PAGE_MARKER.finditer(text))
    return {int(m.group(1)): text[m.end():markers[i + 1].start() if i + 1 < len(markers) else len(text)].strip()
            for i, m in enumerate(markers)}


def extract_candidates(pages: dict[int, str]) -> list[dict]:
    """Collect heading candidates, preserving source page and original text.

    A candidate is not a verified question: scoring and cross-page layouts can
    still cause false headings. No options, columns, or applicability are guessed.
    """
    result = []
    for page, text in pages.items():
        lines = text.splitlines()
        for index, line in enumerate(lines):
            match = QUESTION.fullmatch(line)
            if not match:
                continue
            following = [s.strip() for s in lines[index + 1:] if s.strip()]
            if not following or SCORING.match(following[0]):
                continue
            result.append({'question_id_candidate': match.group(1),
                           'source_page': page, 'heading_verbatim': line.strip(),
                           'following_lines_verbatim': following[:8],
                           'review_status': 'UNVERIFIED: check question, columns and options against PDF'})
    return result


def select_question_candidates(candidates: list[dict], pages: dict[int, str]) -> tuple[list[dict], list[dict]]:
    """Use heading context; uncertain candidates remain flagged for review."""
    selected, dropped, seen = [], [], set()
    sentence = re.compile(r'^(?:what|which|where|how|has|have|does|do|is|are|if|attach|provide|report|describe|identify|select|use|please|c40 requested question)\b', re.I)
    for row in candidates:
        q, page = row['question_id_candidate'], row['source_page']
        following = [x.strip() for x in row['following_lines_verbatim'] if x.strip() and not x.startswith('https://') and 'CDP 2026 questionnaire' not in x]
        if not following:
            following = [x.strip() for x in pages.get(page+1, '').splitlines() if x.strip() and not x.startswith('https://') and 'CDP 2026 questionnaire' not in x]
        first = following[0] if following else ''
        reason = None
        if not sentence.match(first):
            reason = 'Not followed by a question sentence; likely scoring or indicator cross-reference'
        elif q in seen:
            reason = 'Duplicate candidate question identifier'
        if reason:
            dropped.append({**row, 'drop_reason': reason})
        else:
            seen.add(q)
            selected.append(row)
    return selected, dropped


def draft_keywords(candidates: list[dict], pages: dict[int, str]) -> dict[str, list[str]]:
    """Return curated, question-specific draft phrases, never generic bigrams."""
    from .cdp_keywords import CURATED_KEYWORDS
    identifiers = [row['question_id_candidate'] for row in candidates]
    missing = sorted(set(identifiers) - CURATED_KEYWORDS.keys())
    if missing:
        raise ValueError(f'Curated keywords missing for {missing}; manual review required')
    return {q: CURATED_KEYWORDS[q] for q in identifiers}


def screen(c: sqlite3.Connection, keyword_sets: dict[str, list[str]]) -> list[dict]:
    """Count pages with literal, case-insensitive phrases per plan.

    The median includes plans with zero matches. A mention is not evidence that
    a questionnaire field can be answered. Only plans with page text are used.
    """
    patterns = {}
    for question_id, phrases in keyword_sets.items():
        cleaned = [p.strip() for p in phrases if isinstance(p, str) and p.strip()]
        if not cleaned:
            raise ValueError(f'No keywords provided for {question_id}')
        patterns[question_id] = (cleaned, [p.casefold() for p in cleaned])
    counts: dict[str, dict[str, int]] = {}
    for plan_id, text in c.execute('SELECT plan_id, text FROM pages WHERE COALESCE(n_chars,0)>0'):
        plan_counts = counts.setdefault(plan_id, {q: 0 for q in patterns})
        normalised = (text or '').casefold()
        for q, (_, literals) in patterns.items():
            if any(term in normalised for term in literals):
                plan_counts[q] += 1
    result = []
    for q, (phrases, _) in patterns.items():
        per_plan = [plan[q] for plan in counts.values()]
        result.append({'question_id': q, 'detection_keywords': phrases,
                       'plans_screened': len(per_plan),
                       'plans_with_match': sum(n > 0 for n in per_plan),
                       'corpus_hit_rate': sum(n > 0 for n in per_plan) / len(per_plan) if per_plan else None,
                       'median_matching_pages_all_plans': statistics.median(per_plan) if per_plan else None,
                       'caveat': 'Keyword mentions only; not evidence of answerability.'})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('candidates')
    sub.add_parser('draft-keywords')
    sub.add_parser('audit')
    s = sub.add_parser('screen')
    s.add_argument('--keywords', required=True, type=Path, help='Reviewed JSON mapping question IDs to literal phrases')
    s.add_argument('--db', type=Path, default=ROOT / 'outputs' / 'cap_extract' / 'cap.sqlite')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.command == 'candidates':
        rows = extract_candidates(pages_from_text(OUT / 'questionnaire_pages.txt'))
        target = OUT / 'question_candidates_for_review.json'
    elif args.command == 'draft-keywords':
        pages = pages_from_text(OUT / 'questionnaire_pages.txt')
        selected, dropped = select_question_candidates(extract_candidates(pages), pages)
        rows = draft_keywords(selected, pages)
        target = OUT / 'keywords_draft.json'
        (OUT / 'dropped_candidates_draft.json').write_text(json.dumps(dropped, ensure_ascii=False, indent=2), encoding='utf-8')
        missing = {q: values for q, values in rows.items() if len(values) < 3}
        print(f'{len(selected)} candidate questions; {len(dropped)} dropped; {len(missing)} keyword sets have fewer than three phrases and need manual review')
    elif args.command == 'audit':
        import pdfplumber
        pages = pages_from_text(OUT / 'questionnaire_pages.txt')
        selected, dropped = select_question_candidates(extract_candidates(pages), pages)
        with pdfplumber.open(ROOT / 'inputs' / 'CDP_2026_questionnaire.pdf') as pdf:
            rows = audit_questionnaire(pdf, selected)
        rows['dropped_candidates'] = dropped
        target = OUT / 'questionnaire_geometry_audit.json'
        print(f"Audited {len(rows['questions'])} questions, {len(rows['problems'])} potential parse problems, {len(rows['potential_source_clipping'])} page-edge flags")
    else:
        keyword_sets = json.loads(args.keywords.read_text(encoding='utf-8'))
        with sqlite3.connect(f'file:{args.db.as_posix()}?mode=ro', uri=True) as c:
            rows = screen(c, keyword_sets)
        target = OUT / 'keyword_screening.json'
    target.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{len(rows)} records written to {target}')


if __name__ == '__main__':
    main()
