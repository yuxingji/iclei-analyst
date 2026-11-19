from decimal import Decimal
from cap_extract.cdp_quality import (normalise_choice,convert_quantity,clean_item_separators,
                                     unsupported_numbers,document_language,apply_quantities,document_evident_answers)

def test_choices_normalise_punctuation_without_changing_words():
    assert normalise_choice('Jurisdiction’s own – Funds')==normalise_choice("Jurisdiction's own - funds")

def test_conversions_and_unknown_units():
    assert convert_quantity('2','square miles','km2')==Decimal('5.179976220672')
    assert convert_quantity('1000','kWh','MWh')==Decimal('1.000')
    assert convert_quantity('1','people','MWh') is None

def test_conversion_requires_a_source_quote():
    q=[{'original_value':'2','original_unit':'square miles','target_unit':'km2'}]
    result,derived,problems=apply_quantities('Area: 2 square miles',q,['The area is 2 square miles.'])
    assert result=='Area: 5.179976 km2' and not problems and derived
    result,_,problems=apply_quantities('Area: 2 square miles',q,['No quantity in this quotation.'])
    assert result=='Area: 2 square miles' and problems[0][0]=='quantity_evidence_missing'

def test_item_numeric_support_and_bracket_repair():
    assert unsupported_numbers('50% by 2030',['50% by 2029'])==[Decimal('2030')]
    assert clean_item_separators('Heat (health; roads); Flood')=='Heat (health, roads); Flood'

def test_language_detection_is_conservative_and_requires_text():
    text=('The city and the community will work together to improve the quality of the environment in the area. ')*5
    assert document_language([(1,text)])['language']=='English'
    assert document_language([(1,'tiny')]) is None

def test_document_rules_require_cover_evidence_and_do_not_guess_hierarchy():
    plan={'municipality':'Town of Example','gov_type':'Town','state':'NY'}
    pages=[(1,'Town of Example\nClimate Action Plan\nThis document is a plan for the town.'),
           (2,'The approved budget is $2,000 for program delivery.')]
    answers=document_evident_answers(plan,pages,'1.2')
    assert {a['answer'] for a in answers}=={'Town','USD US Dollar'}
    assert all('State / regional' not in a['answer'] for a in answers)
    assert document_evident_answers(plan,pages,'9.1')[0]['answer'].startswith('Yes,')
    assert not document_evident_answers(plan,[(1,'Another municipality plan')],'9.1')
