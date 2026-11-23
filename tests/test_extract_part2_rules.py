import importlib.util
import json
from pathlib import Path

import pytest

from shipyard.rule_knowledge import RuleKnowledge

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "references-local" / "kr-2026-part2-rules.jsonl"
spec = importlib.util.spec_from_file_location("extract_part2", ROOT / "data" / "extract_kr_part2_rules.py")
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)


def numbered(*lines, page=1):
    return [(page, line) for line in lines]


def test_items_split_and_short_preamble_joins_first_item():
    lines = numbered("304. 용접준비", "1. 홈 가공", "(1) 홈은 정밀하게 가공한다.", "2. 가용접", "(1) 기량자격자")
    chunks = extract.chunks(lines)
    assert [(label, title) for label, title, _ in chunks] == [("1", "홈 가공"), ("2", "가용접")]
    assert extract.text_of(chunks[0][2]).startswith("304. 용접준비\n1. 홈 가공")


def test_only_consecutive_numbers_start_items():
    # "3." out of sequence (e.g. a table cell) stays inside item 1.
    labels = [label for label, _, _ in extract.chunks(numbered("101. 적용", "1. 적용 범위", "3. 표 값", "2. 승인"))]
    assert labels == ["1", "2"]


def test_long_item_splits_at_sub_items_and_keeps_headings():
    body = "가" * (extract.MAX_CHARS // 2)
    lines = numbered("407. 허용범위", "1. 필수변수", f"(1) 모재  {body}", f"(2) 모재두께  {body}")
    chunks = extract.chunks(lines)
    assert [(label, title) for label, title, _ in chunks] == [("1-1", "필수변수 모재"), ("1-2", "필수변수 모재두께")]
    assert all(len(extract.text_of(part)) <= extract.MAX_CHARS for _, _, part in chunks)


def test_oversized_unnumbered_text_is_cut_into_parts():
    lines = numbered("101. 적용", *("나" * 100 for _ in range(60)))
    chunks = extract.chunks(lines)
    assert [label for label, _, _ in chunks] == [f"BODY-P{i}" for i in range(1, len(chunks) + 1)]
    assert len(chunks) > 1 and all(len(extract.text_of(part)) <= extract.MAX_CHARS for _, _, part in chunks)


def test_equation_glyphs_map_to_plain_text():
    assert "".translate(extract.GLYPH_TABLE) == "L(e)=5.6σ"


@pytest.mark.skipif(not CORPUS.exists(), reason="full Part 2 corpus not extracted")
def test_full_corpus_loads_and_covers_rules_guidance_and_appendices():
    rows = [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines()]
    assert len({row["source_sha256"] for row in rows}) == 1
    assert all(len(row["text"]) <= extract.MAX_CHARS for row in rows)
    ids = {row["id"] for row in rows}
    assert {"KR-2026-2-2-304-1", "KR-2026-2-2-407-2-8", "KR-2026-2-2-GUIDE-304-1",
            "KR-2026-2-APP-2-7-1-1", "KR-2026-2-1-301-1"} <= ids
    knowledge = RuleKnowledge(CORPUS)
    assert len(knowledge.documents) == len(rows)
