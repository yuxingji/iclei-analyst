"""Consolidated CDP response schema and evidence checks."""
from __future__ import annotations

import re
from typing import Literal
from pydantic import BaseModel, Field, field_validator

Status = Literal['Answered','Explicit No','Not Disclosed','Not Applicable','Unclear']


class Evidence(BaseModel):
    page: int
    evidence_excerpt: str
    confidence: Literal['High','Medium','Low']

    @field_validator('evidence_excerpt',mode='before')
    @classmethod
    def cap_excerpt(cls,value):
        value=str(value or '')
        if len(value)<=300:return value
        at=value.rfind(' ',0,301)
        return value[:at if at>0 else 300].rstrip()


class FieldChoice(BaseModel):
    column_id: str
    value: str

class Quantity(BaseModel):
    original_value: str
    original_unit: str
    target_unit: str
    field_name: str=''

class DisclosureItem(BaseModel):
    label: str
    content: str
    evidence: list[Evidence]=Field(default_factory=list)
    choice_values: list[str]=Field(default_factory=list)
    choice_fields: list[FieldChoice]=Field(default_factory=list)
    quantities: list[Quantity]=Field(default_factory=list)


class QuestionAnswer(BaseModel):
    question_id: str
    disclosure_status: Status
    answer: str=''
    items: list[DisclosureItem]=Field(default_factory=list)
    evidence: list[Evidence]=Field(default_factory=list)
    choice_values: list[str]=Field(default_factory=list)
    choice_fields: list[FieldChoice]=Field(default_factory=list)
    plan_level: str=''
    missing_components: list[str]=Field(default_factory=list)
    quantities: list[Quantity]=Field(default_factory=list)


class GroupResponse(BaseModel):
    answers: list[QuestionAnswer]


def normalise_text(value):
    return re.sub(r'\s+',' ',str(value or '')).strip().casefold()


def verify_evidence(excerpt, page_number, pages):
    """Verify against the cited page first, then other selected page text."""
    from rapidfuzz.fuzz import partial_ratio
    needle=normalise_text(excerpt)
    if len(needle)<12:return page_number,False,False
    def found(value):
        haystack=normalise_text(value)
        return needle in haystack or (len(needle)>=25 and partial_ratio(needle,haystack)>=95)
    if page_number in pages and found(pages[page_number]):return page_number,True,False
    for other,text in pages.items():
        if other!=page_number and found(text):return other,True,True
    return page_number,False,False


def split_answer_segments(segments, max_chars=30000):
    """Split at item boundaries when possible, preserving long individual items."""
    parts=[];current=''
    for segment in segments:
        segment=str(segment).strip()
        if not segment:continue
        while len(segment)>max_chars:
            if current:parts.append(current);current=''
            cut=segment.rfind(' ',0,max_chars+1)
            if cut<max_chars//2:cut=max_chars
            parts.append(segment[:cut].rstrip());segment=segment[cut:].lstrip()
        proposed=(current+'; '+segment) if current else segment
        if len(proposed)>max_chars:
            parts.append(current);current=segment
        else:current=proposed
    if current:parts.append(current)
    return parts or ['']
