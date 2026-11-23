# 한국선급 용접 규칙 검색

## 추출

첨부된 361페이지짜리 2026년판 한국선급 제2편 통합 PDF를 기준으로 한다. 스크립트는 다음 범위만 읽어 JSONL을 만든다.

| 조항 | 규칙 인쇄 페이지 | PDF 페이지 | 적용지침 인쇄 페이지 |
| --- | --- | --- | --- |
| 303 용접용재료의 사용구분 및 표 2.2.3 | 123~125 | 128~130 | 41 |
| 304 용접준비 | 125 | 130 | 41 |
| 402 용접절차 시방서 | 128 | 133 | |
| 407 승인된 용접절차 시방서의 허용범위 | 143~147 | 148~152 | 47 |

적용지침 41쪽과 47쪽은 각각 PDF 265번째, 271번째 페이지다. 각 검색 항목은 ID, 판본, 조항, 인쇄 페이지, PDF 페이지, 원본 해시, 본문을 포함한다. 303조 표는 두 페이지의 내용을 분리하며 이어지는 비고를 양쪽에 붙인다. 407조는 모재, 두께, 용접자세, 용접법, 용접용재료, 용접조건, 입열량, 이음 형태 등으로 나눈다. PDF와 추출한 본문은 로컬 자료로 두며 `references-local/`은 Git에서 제외한다.

```powershell
uv run --with pypdf python data/extract_kr_welding_rules.py '<한국선급 2026 제2편 통합 PDF 경로>' references-local/kr-2026-welding-rules.jsonl
```

## 검색

조항 번호로 요청하면 해당 조항의 조각과 적용지침을 모두 반환한다. 인식하는 조항 번호는 불러온 추출본에서 정하며 코드에 고정된 조항 목록은 없다. 일반 검색은 SQLite FTS5가 제목, 검색어, 본문을 비교해 관련 항목을 반환한다. 표(`…-TABLE-…`)나 `parent_id`가 있는 하위 항목이 검색되면 그 조항의 본문도 함께 보여준다. 예를 들어 강종 기호로 표 2.2.3이 검색되면 303조 본문이 붙는다. FTS5 기본 토크나이저는 한국어 조사를 분리하지 않으므로("도장이"≠"도장") 구어체 보고는 아래 주제 설정의 패턴으로 분류한다.

```powershell
uv run --locked python -m shipyard search-rules --rules references-local/kr-2026-welding-rules.jsonl 'AH36'
uv run --locked python -m shipyard search-rules --rules references-local/kr-2026-welding-rules.jsonl '루트 간격'
```

## 주제 설정: 어떤 보고에 어떤 조항을 줄지

[`config/rule-topics.json`](../config/rule-topics.json)이 보고 분류와 검색어를 연결한다. 코드를 바꾸지 않고 이 파일만 고쳐 조항 선택을 바꾼다.

| 필드 | 의미 |
| --- | --- |
| `topic_id` | 주제 이름 |
| `observation_kinds` | Agent가 근거 조회에서 고른 관측 종류. 한 종류는 한 주제에만 연결한다 |
| `patterns` | 단독 분석처럼 관측 종류가 없을 때 본문에서 주제를 알아보는 정규식 |
| `query` | 그 주제의 FTS 검색어 |
| `limit` | 반환할 최대 항목 수(기본 5) |

시뮬레이션에서는 Agent가 `lookup_evidence`로 고른 `observation_kind`가 주제를 정한다. 현재 설정은 `FITUP_RESULT`·`REPAIR_COMPLETED`·`RECHECK_RESULT`를 FITUP 주제(304조, 402조, 407조 이음 형태와 적용지침)에, `MATERIAL_READY`를 용접용재료 주제(303조, 407조 용접용재료)에 연결한다. 연결된 주제가 없으면 본문 패턴을 보고, 그것도 없으면 본문 자체로 검색한다. 불러온 추출본에서 아무 항목도 찾지 못하는 주제가 있으면 시작할 때 오류를 낸다.

추출본과 주제 설정은 하나의 해시(`rule_knowledge_sha256`)로 승인한다. 둘 중 하나를 바꾸면 `prepare-case-policy`를 다시 실행한다. 다른 경로의 주제 설정은 `--rule-topics`로 지정한다.

## 보고 처리에서 사용

`analyze-report`는 기본 추출본에서 현재 보고에 맞는 조항을 로컬로 검색하고, 선택한 발췌와 과거 실행 결과를 GPT에 전달한다. 리포트 결과의 `analysis.rule_basis`에 실제 인용한 조항과 적용 이유가, `rule_sources`에 검색한 출처와 페이지가 나온다. [단독 분석 및 시뮬레이션 실행](report-rag.md)

`fake` 및 `responses` 실행에 `--rules`를 주면 근거 조회에 선급 발췌를 추가한다. 인용한 `rule_refs`는 조회 목록과 대조하고 현재 관측의 `evidence_ids`는 기존 사건 검사를 거친다. Responses에서 규칙을 사용하는 제안에는 조항 인용이 필요하다. 조회·인용한 조항은 공용 결과 DB의 `rules_retrieved_json`, `rule_refs_json`에도 남는다.

```powershell
uv run --locked python -m shipyard run --case D01 --report-mode fake --rules references-local/kr-2026-welding-rules.jsonl --output outputs/D01-rules
```

Responses 실행은 사례별 로컬 정책의 `rule_knowledge_sha256`으로 전송할 추출본과 주제 설정을 확인한다. `trace.jsonl`의 조회·인용 기록과 `manifest.json`의 해시로 출처를 추적한다. W17의 실제 허용오차는 해당 작업에 적용되는 WPS와 도면에서 확인한다.
