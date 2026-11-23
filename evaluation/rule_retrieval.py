"""Compare rule-retrieval set-ups against hand-judged relevant clauses.

Each gold query goes through the same entry points a run uses (`for_report`
with the agent's observation kind, `for_analysis` for free text), and the top
k excerpts are scored per clause: recall (relevant clauses found), precision
(excerpts that belong to a relevant clause) and hit (any relevant clause).

    uv run --locked python evaluation/rule_retrieval.py
    uv run --locked --extra llm python evaluation/rule_retrieval.py --allow-paid   # adds embedding and hybrid
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shipyard.rule_knowledge import RuleKnowledge, open_semantic_index  # noqa: E402

CLAUSE_KEY = re.compile(r"^KR-\d{4}-2-(?:(?P<app>APP-2-\d+)-|(?P<chapter>\d)-(?P<guide>GUIDE-)?(?P<clause>\d{3})-)")
CURATED = (ROOT / "references-local/kr-2026-welding-rules.jsonl", ROOT / "config/rule-topics.curated.json")
FULL = (ROOT / "references-local/kr-2026-part2-rules.jsonl", ROOT / "config/rule-topics.json")


def clause_key(ref: str) -> str:
    match = CLAUSE_KEY.match(ref)
    if not match:
        raise ValueError(f"Cannot read a clause from rule ID {ref}")
    if match["app"]:
        return match["app"]
    return f"{match['chapter']}-{match['guide'] or ''}{match['clause']}"


def retrieve(knowledge: RuleKnowledge, query: dict) -> list[str]:
    kind = query.get("observation_kind")
    rows = knowledge.for_report(query["text"], kind) if kind else knowledge.for_analysis(query["text"])
    return [row["id"] for row in rows]


def score(refs: list[str], relevant: set[str], k: int) -> dict:
    top = refs[:k]
    keys = [clause_key(ref) for ref in top]
    found = relevant & set(keys)
    return {"recall": len(found) / len(relevant), "precision": (sum(key in relevant for key in keys) / len(top)) if top else 0.0,
            "hit": float(bool(found)), "retrieved": top, "missing": sorted(relevant - found)}


def evaluate(knowledge: RuleKnowledge, gold: dict, k: int) -> dict:
    per_query = {}
    for query in gold["queries"]:
        per_query[query["query_id"]] = score(retrieve(knowledge, query), set(query["relevant_clauses"]), k)
    count = len(per_query)
    means = {metric: round(sum(row[metric] for row in per_query.values()) / count, 3)
             for metric in ("recall", "precision", "hit")}
    return {"retrieval": knowledge.describe(), "mean": means, "queries": per_query}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gold", type=Path, default=ROOT / "evaluation/rule-retrieval/gold.json")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--allow-paid", action="store_true", help="also run embedding-only and hybrid retrieval")
    parser.add_argument("--embed-model", default="text-embedding-3-small")
    parser.add_argument("--cache", type=Path, default=ROOT / "artifacts/rule-embeddings.sqlite3")
    parser.add_argument("--output", type=Path, help="write the full JSON result here")
    args = parser.parse_args(argv)
    gold = json.loads(args.gold.read_text(encoding="utf-8"))

    setups = [("18-row excerpt, keyword", CURATED, {}), ("full Part 2, keyword", FULL, {})]
    semantic = None
    if args.allow_paid:
        from shipyard.report_retrieval import OpenAIReportClient
        client = OpenAIReportClient(allow_paid=True, dotenv_path=ROOT / ".env")
        semantic = open_semantic_index(client, args.embed_model, args.cache)
        setups += [("full Part 2, embedding", FULL, {"semantic": semantic, "keyword": False}),
                   ("full Part 2, hybrid", FULL, {"semantic": semantic})]
    results = {}
    try:
        for name, (rules, topics), options in setups:
            if not rules.is_file():
                print(f"skip {name}: {rules.relative_to(ROOT)} not found", file=sys.stderr)
                continue
            knowledge = RuleKnowledge(rules, topics, **options)
            try:
                results[name] = evaluate(knowledge, gold, args.k)
            finally:
                knowledge.close()
    finally:
        if semantic is not None:
            semantic.cache.close()

    names = list(results)
    print(f"| query | " + " | ".join(names) + " |")
    print("| --- |" + " --- |" * len(names))
    for query in gold["queries"]:
        cells = [f"{results[n]['queries'][query['query_id']]['recall']:.2f}" for n in names]
        print(f"| {query['query_id']} | " + " | ".join(cells) + " |")
    for metric in ("recall", "precision", "hit"):
        print(f"| **mean {metric}@{args.k}** | " + " | ".join(f"{results[n]['mean'][metric]:.3f}" for n in names) + " |")
    if semantic is not None:
        print(f"\nembedded {semantic.embedded_texts} new texts with {args.embed_model}", file=sys.stderr)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"k": args.k, "gold": str(args.gold.relative_to(ROOT)), "results": results},
                                          ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
