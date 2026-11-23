# 한국선급 용접 규칙 검색

선급 규칙은 361페이지짜리 2026년판 한국선급 제2편 통합 PDF 전체를 조항 단위로 나눈 추출본에서 찾는다. 검색은 키워드 순위와 임베딩 순위를 합치는 하이브리드 방식이다. API를 쓰지 않는 실행에서는 키워드 순위만 쓴다.

| 파일 | 내용 |
| --- | --- |
| `references-local/kr-2026-part2-rules.jsonl` | 기본 추출본. 규칙·적용지침·부록 전체를 991개 조각으로 나눔 |
| `config/rule-topics.json` | 기본 주제 설정. 관측 종류별 검색어와 장·절 범위 |
| `references-local/kr-2026-welding-rules.jsonl` | 이전 18개 발췌본. 비교용 |
| `config/rule-topics.curated.json` | 18개 발췌본용 주제 설정 |
| `artifacts/rule-embeddings.sqlite3` | 임베딩 캐시(Git 제외). 처음 쓸 때 자동으로 만든다 |

## 추출

```powershell
uv run --no-project --with pypdf==7.1.1 python data/extract_kr_part2_rules.py '<한국선급 2026 제2편 통합 PDF 경로>' references-local/kr-2026-part2-rules.jsonl
```

| 범위 | PDF 페이지 | ID 형식 |
| --- | --- | --- |
| 규칙 1장 재료, 2장 용접 | 7–219 | `KR-2026-2-<장>-<조>-<항>` (예: `KR-2026-2-2-304-1`) |
| 적용지침 1·2장 | 225–275 | `KR-2026-2-<장>-GUIDE-<조>-<항>` |
| 적용지침 부록 2-1–2-12 | 276–360 | `KR-2026-2-APP-<부록>-<항>` (예: `KR-2026-2-APP-2-7-9`) |

- **조각 단위.** 조(예: 304)를 항(1., 2., …)마다 하나의 조각으로 만든다.
- **긴 항.** 2,400자를 넘으면 호((1), (2), …)로 나눠 `-<항>-<호>`를 붙인다. 그래도 길면 줄 단위로 잘라 `-P1`, `-P2`를 붙인다.
- **짧은 머리말.** 300자 미만인 조 머리말과 "4. 인장시험" 같은 제목 한 줄은 첫 항이나 첫 호에 붙인다.
- **제목과 검색어.** 항·호 제목(예: "이음의 형태")은 조각의 `title`에, 장·절 이름과 함께 `keywords`에도 넣는다.
- **목차 대조.** 규칙과 적용지침의 목차를 읽어 추출한 조·부록 목록과 비교하고, 다르면 실패한다. 조 번호로 시작하는 줄은 목차의 제목과 같을 때만 조 제목으로 본다. "405.에 규정하는…" 같은 상호 참조를 조 제목으로 착각하지 않기 위해서다.
- **수식 문자.** 사용자 정의 영역에 있는 HancomEQN 수식 글리프 79종은 설치된 글꼴에서 읽은 대응표로 일반 문자로 바꾼다.
- **재현성.** 같은 PDF와 pypdf 버전이면 바이트까지 같은 파일이 나온다.
- **알려진 한계.** 표는 pypdf가 칸 순서대로 풀어 쓴 텍스트로 남는다. 표 아래 비고의 (1), (2)…가 호로 인식되어 303처럼 조각 경계가 어긋나는 곳이 있다.

이전 18개 발췌본은 `data/extract_kr_welding_rules.py`로 만든다. 303·304·402·407조와 그 적용지침만 손으로 골라 나눈 것이다.

## 검색 방식

한 번의 검색은 다음 순서로 진행한다.

1. **후보 범위를 정한다.** 주제에 `id_prefixes`가 있으면 그 범위의 조각만 후보로 둔다. 질의에 조 번호(예: "304조")가 있으면 그 조의 조각으로 좁힌다.
2. **키워드 순위.** SQLite FTS5 bm25가 제목·검색어·본문을 비교한다. 한글 단어는 접두 일치로 찾아 조사가 붙은 형태("간격이")도 잡는다.
3. **임베딩 순위.** 하이브리드 모드에서만 쓴다. 조각의 제목과 본문을 임베딩한 벡터와 질의 벡터의 코사인 유사도로 순위를 매긴다. 수백 개 조각이 공유하는 장·절 이름은 넣지 않는다. 넣으면 본문이 짧은 조각끼리 비슷해지기 때문이다.
4. **순위 결합(RRF).** 각 순위의 상위 50개를 `1/(60+순위)` 점수로 합친다.
5. **조당 상한.** 한 조에서 최대 2개 조각만 고른다. 한 조의 조각 여섯 개보다 여러 조를 보여주는 편이 판단에 유리하다.
6. **본문 보충.** 표(`…-TABLE-…`)나 `parent_id`가 있는 조각이 뽑히면 그 조의 본문을 함께 보여준다.

보고 처리에서는 키워드 쪽은 주제의 검색어를, 임베딩 쪽은 주제 검색어와 보고 문장을 함께 쓴다. 키워드 쪽은 Agent의 분류만으로 재현되고, 임베딩 쪽은 보고의 표현도 반영한다.

| 실행 | 검색 방식 |
| --- | --- |
| `run --report-mode fake`, 테스트 | 키워드만 |
| `run --report-mode responses`, `run-live` | 하이브리드가 기본. `--rule-search keyword`로 끌 수 있다 |
| `analyze-report` | 하이브리드가 기본. `--rule-search keyword` 가능 |
| `search-rules` | 키워드가 기본. `--allow-paid`를 주면 하이브리드 |

임베딩 모델은 `text-embedding-3-large`가 기본이며 `--rule-embed-model`로 바꾼다. 처음 실행할 때 조각 991개를 한 번 임베딩해 캐시에 넣는다. 비용은 약 $0.05다. 이후에는 새 질의만 임베딩한다. 캐시는 모델과 임베딩한 문장의 SHA-256으로 찾으므로 추출본이 바뀌면 바뀐 조각만 다시 임베딩한다. 임베딩 호출이 실패하면 해당 보고만 `RETRIEVAL_ERROR`로 멈추고 실행은 계속된다.

```powershell
uv run --locked python -m shipyard search-rules '루트 간격'
uv run --locked python -m shipyard search-rules --observation-kind FITUP_RESULT 'W17 핏업 불합격'
uv run --locked --extra llm python -m shipyard search-rules --allow-paid '맞춤 틈새가 너무 벌어졌다'
```

## 주제 설정: 어떤 보고에 어떤 조항을 줄지

[`config/rule-topics.json`](../config/rule-topics.json)이 보고 분류와 검색어를 연결한다. 코드를 바꾸지 않고 이 파일만 고쳐 조항 선택을 바꾼다.

| 필드 | 의미 |
| --- | --- |
| `topic_id` | 주제 이름 |
| `observation_kinds` | Agent가 근거 조회에서 고른 관측 종류. 한 종류는 한 주제에만 연결한다 |
| `patterns` | 단독 분석처럼 관측 종류가 없을 때 본문에서 주제를 알아보는 정규식. 위에서부터 차례로 본다 |
| `query` | 그 주제의 검색어 |
| `id_prefixes` | 선택. 후보로 둘 조각 ID의 접두어(장·절 범위) |
| `limit` | 반환할 최대 항목 수(기본 5) |

| 주제 | 관측 종류 | 범위 |
| --- | --- | --- |
| RECHECK | `RECHECK_RESULT` | 2장 3절, 적용지침 2장 3절, 부록 2-7 |
| REPAIR | `REPAIR_COMPLETED` | 2장 3절, 적용지침 2장 3절, 부록 2-7 |
| FITUP | `FITUP_RESULT` | 2장 3·4절과 각 적용지침 |
| CONSUMABLE | `MATERIAL_READY` | 2장 3·4절과 각 적용지침 |

연결된 주제가 없으면 본문 패턴을 보고, 그것도 없으면 범위 제한 없이 본문 자체로 검색한다. 시작할 때 키워드만으로 각 주제를 검사하며, 범위 안에서 아무 항목도 찾지 못하는 주제가 있으면 오류를 낸다. 이 검사는 API를 호출하지 않는다.

추출본과 주제 설정은 하나의 해시(`rule_knowledge_sha256`)로 승인한다. 이 승인에는 추출본 전체를 임베딩 API에 보내는 것도 포함된다. 둘 중 하나를 바꾸면 `prepare-case-policy`를 다시 실행한다. 18개 발췌본을 쓰려면 `--rules references-local/kr-2026-welding-rules.jsonl --rule-topics config/rule-topics.curated.json`을 함께 준다.

## 검색 평가

[`evaluation/rule-retrieval/gold.json`](../evaluation/rule-retrieval/gold.json)에 질의 12개와 관련 조항을 손으로 정했다. D01–D04 보고 4개와 자유 문장 8개다. 시스템 출력이 아니라 규칙 본문을 보고 정했으며, 조각 나누기가 다른 추출본끼리 비교할 수 있도록 조 단위로 채점한다.

```powershell
uv run --locked python evaluation/rule_retrieval.py                          # 키워드 구성만
uv run --locked --extra llm python evaluation/rule_retrieval.py --allow-paid  # 임베딩·하이브리드 추가
```

2026-11-24 결과(상위 5개)는 다음과 같다.

| 구성 | 재현율 | 정밀도 | 적중률 |
| --- | --- | --- | --- |
| 18개 발췌본, 키워드 | 0.479 | 0.422 | 0.667 |
| 전체, 키워드 | 0.696 | 0.633 | 1.000 |
| 전체, 임베딩만(large) | 0.643 | 0.467 | 0.917 |
| 전체, 하이브리드(small) | 0.721 | 0.533 | 1.000 |
| **전체, 하이브리드(large)** | **0.782** | 0.617 | 1.000 |

- **임베딩만으로는 부족하다.** 이 문서에서는 키워드보다 약하다. 키워드와 합칠 때 가장 좋다.
- **결과 전에 바꾼 설정이 있다.** 처음 측정한 하이브리드(small)는 키워드보다 낮았다(0.621). 그래서 조당 상한, 장·절 이름 제외, large 모델 비교를 적용했다. 모두 원인이 확인된 수정이지만 질의 12개로 고른 설정이므로 질의를 늘려 다시 확인해야 한다.
- **아직 약한 질의가 있다.** 재검사(0.6)와 방사선투과검사(0.33)다. 부록 2-12 같은 인접 부록이 섞이거나 필요한 조가 범위 밖에 있는 경우다.

## 보고 처리에서 사용

`analyze-report`는 현재 보고에 맞는 조항을 검색하고, 선택한 발췌와 과거 실행 결과를 GPT에 전달한다. 리포트 결과의 `analysis.rule_basis`에 실제 인용한 조항과 적용 이유가, `rule_sources`에 검색한 출처와 페이지가 나온다. [단독 분석 및 시뮬레이션 실행](report-rag.md)

`fake` 및 `responses` 실행에 `--rules`를 주면 근거 조회에 선급 발췌를 추가한다. 인용한 `rule_refs`는 조회 목록과 대조하고 현재 관측의 `evidence_ids`는 기존 사건 검사를 거친다. Responses에서 규칙을 사용하는 제안에는 조항 인용이 필요하다. 조회·인용한 조항은 공용 결과 DB의 `rules_retrieved_json`, `rule_refs_json`에도 남는다. `manifest.json`의 `rule_knowledge.retrieval`에 검색 방식과 임베딩 모델이 기록된다.

```powershell
uv run --locked python -m shipyard run --case D01 --report-mode fake --rules references-local/kr-2026-part2-rules.jsonl --output outputs/D01-rules
```

Responses 실행은 사례별 로컬 정책의 `rule_knowledge_sha256`으로 전송할 추출본과 주제 설정을 확인한다. `trace.jsonl`의 조회·인용 기록과 `manifest.json`의 해시로 출처를 추적한다. W17의 실제 허용오차는 해당 작업에 적용되는 WPS와 도면에서 확인한다.
