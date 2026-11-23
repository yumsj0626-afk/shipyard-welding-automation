"""Hybrid rule retrieval: keyword and embedding ranks fused, filtered and cached."""
import json

import pytest

from shipyard.rule_knowledge import RuleKnowledge, SemanticIndex, VectorCache, document_text

ROWS = [
    # Two chapters share clause 304, as in the real Part 2.
    {"id": "KR-2026-2-1-304-1", "chapter": 1, "clause": "304", "title": "저온용 압연강재 (1) 적용",
     "keywords": "제1장 재료", "text": "저온용 압연강재에 적용한다."},
    {"id": "KR-2026-2-2-304-1", "chapter": 2, "clause": "304", "title": "용접준비 (1) 홈 가공",
     "keywords": "제2장 용접", "text": "홈 가공은 도면과 같은 모양으로 한다."},
    {"id": "KR-2026-2-2-304-2", "chapter": 2, "clause": "304", "title": "용접준비 (2) 가용접",
     "keywords": "제2장 용접", "text": "가용접은 기량자격자가 한다."},
    {"id": "KR-2026-2-2-304-3", "chapter": 2, "clause": "304", "title": "용접준비 (3) 고정구",
     "keywords": "제2장 용접", "text": "고정구로 홈 간격을 유지한다."},
    {"id": "KR-2026-2-2-407-2-8", "chapter": 2, "clause": "407", "title": "허용범위 (2-8) 이음의 형태",
     "keywords": "제2장 용접", "text": "루트 간격 허용오차는 선급이 인정하는 바에 따른다."},
    {"id": "KR-2026-2-2-310-1", "chapter": 2, "clause": "310", "title": "용접부의 보수 (1)",
     "keywords": "제2장 용접", "text": "유해한 결함은 가우징으로 제거한다."},
]
PARAPHRASE = "맞춤 틈새가 너무 벌어졌다"   # shares no keyword with 407-2-8


class Embedder:
    """Maps each text to a fixed direction; counts calls and texts."""

    def __init__(self, fail=False):
        self.calls, self.texts, self.fail = 0, [], fail
        self.table = {document_text(row): vector for row, vector in zip(ROWS, (
            [0, 0, 1], [1, 0, 0], [0.2, 1, 0], [0.9, 0.1, 0], [1, 0.05, 0], [0, 1, 0]))}

    def embed(self, model, texts):
        if self.fail:
            raise RuntimeError("provider down")
        self.calls += 1
        self.texts += texts
        return [self.table.get(text, [1, 0.04, 0]) for text in texts]


@pytest.fixture
def rules(tmp_path):
    path = tmp_path / "rules.jsonl"
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in ROWS), encoding="utf-8")
    return path


def semantic(tmp_path, embedder):
    return SemanticIndex(embedder, "test-embed", VectorCache(tmp_path / "vectors.sqlite3"))


def ids(rows):
    return [row["id"] for row in rows]


def test_embeddings_recover_a_paraphrase_that_keywords_miss(tmp_path, rules):
    keyword = RuleKnowledge(rules)
    assert keyword.search(PARAPHRASE) == []
    hybrid = RuleKnowledge(rules, semantic=semantic(tmp_path, Embedder()))
    found = ids(hybrid.search(PARAPHRASE, limit=3))
    assert "KR-2026-2-2-407-2-8" in found
    assert hybrid.describe()["mode"] == "hybrid"


def test_prefix_filter_separates_chapters_sharing_a_clause_number(tmp_path, rules):
    knowledge = RuleKnowledge(rules, semantic=semantic(tmp_path, Embedder()))
    everywhere = ids(knowledge.search("304조"))
    assert "KR-2026-2-1-304-1" in everywhere
    welding = ids(knowledge.search("304조", id_prefixes=["KR-2026-2-2-"]))
    assert welding and all(ref.startswith("KR-2026-2-2-304-") for ref in welding)


def test_one_clause_fills_at_most_two_slots(rules):
    found = ids(RuleKnowledge(rules).search("용접준비 홈 가공 가용접 고정구 루트 간격", limit=4))
    assert sum(ref.startswith("KR-2026-2-2-304-") for ref in found) == 2
    assert "KR-2026-2-2-407-2-8" in found


def test_vectors_are_cached_across_instances_and_topics_load_without_api(tmp_path, rules):
    topics = tmp_path / "topics.json"
    topics.write_text(json.dumps({"topics": [{"topic_id": "FITUP", "observation_kinds": ["FITUP_RESULT"],
                                              "query": "루트 간격 홈", "id_prefixes": ["KR-2026-2-2-"]}]},
                                 ensure_ascii=False), encoding="utf-8")
    first = Embedder()
    knowledge = RuleKnowledge(rules, topics, semantic=semantic(tmp_path, first))
    assert first.calls == 0
    found = ids(knowledge.for_report("W17 맞춤 불량", "FITUP_RESULT"))
    assert found and all(ref.startswith("KR-2026-2-2-") for ref in found)
    assert len(first.texts) == len(ROWS) + 1          # every chunk once, plus the query
    knowledge.semantic.cache.close()

    second = Embedder()
    again = RuleKnowledge(rules, topics, semantic=semantic(tmp_path, second))
    assert ids(again.for_report("W17 맞춤 불량", "FITUP_RESULT")) == found
    assert second.texts == []                         # chunks and query came from the cache
    again.semantic.cache.close()


def test_embedding_only_mode_requires_an_index(rules):
    with pytest.raises(ValueError, match="semantic index"):
        RuleKnowledge(rules, keyword=False)


def test_provider_failure_surfaces_as_runtime_error(tmp_path, rules):
    knowledge = RuleKnowledge(rules, semantic=semantic(tmp_path, Embedder(fail=True)))
    with pytest.raises(RuntimeError):
        knowledge.search("루트 간격")
