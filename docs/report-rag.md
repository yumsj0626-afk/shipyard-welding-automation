# 리포트 처리 사용법: 선급 조항과 앞선 실행 결과

## 흐름

1. 문장형 리포트가 정해진 tick에 도착한다. Agent는 리포트가 도착한 tick에만 호출되고, 작업 단계 종료 같은 실행 관측은 LLM 없이 코드와 solver가 처리한다.
2. **1회차 생성 요청.** Agent(GPT)는 리포트 본문, 현재 작업·로봇·구역 ID, 허용 사건 종류, 앞선 실행의 유사 결과를 받는다. 어떤 근거를 볼지 `lookup_evidence(대상, observation_kind)`로 답한다. 이 선택이 분류다.
3. **조회(로컬).** 코드가 해당 근거와 활성 제한을 찾고, `observation_kind`를 [주제 설정](welding-rule-retrieval.md#주제-설정-어떤-보고에-어떤-조항을-줄지)의 검색어로 바꿔 선급 조항을 고른다.
4. **2회차 생성 요청.** Agent가 조회 결과를 받고 `propose(사건 종류, evidence_ids, restriction_ids, rule_refs)`로 사건을 제안한다. 근거가 모자라면 `clarify`로 확인을 요청한다.
5. **결정(코드).** 검증기가 Agent가 인용한 ID가 실제 조회 결과에 있는지 대조한다. 사건 검사(`events`)가 근거 값을 확인해 제한을 열거나 해제한다. CP-SAT가 현재 상태로 재계획하고 독립 검사기가 확인한다.
6. 실행이 끝나면 리포트별 결과를 **공용 결과 DB**에 쌓는다. 다음 실행의 Agent가 그 결과를 유사 사례로 받는다.

Agent는 사건을 제안할 뿐 보류·해제·배정을 결정하지 않는다. 구역 점유나 로봇 위치 같은 현재 상태 판단은 과거 사례가 아니라 solver가 현재 DB 상태로 한다. 과거 결과는 Agent가 리포트 유형을 알아보는 참고 자료다.

### 예: D02

D02는 D01과 리포트 본문이 같고 초기 상태만 다르다(W19·W21 자재 미준비). 리포트 본문과 도착 tick은 `simulation/reports/D02.jsonl`, 함께 도착하는 검사 기록은 `simulation/dev/D02/events.jsonl`에 있다.

| tick | Agent | 코드·solver |
| --- | --- | --- |
| 0 | "W17 핏업 불합격" → `FITUP_RESULT` 조회 → `FITUP_FAILED` 제안 | W17에 `FITUP_HOLD`. W18은 R2가 계속. R1은 대체 작업이 없어 이유를 남기고 대기. W20은 선행 미완료로 차단 |
| 2 | – | W18 종료. W19·W21은 자재가 없어 시작하지 않음 |
| 6 | "보수 완료" → `REPAIR_REPORTED`, "재검사 합격" → `RECHECK_REPORTED` | 수리와 연결된 재검사 PASS로 보류 해제(0→6). R1이 W17 준비·용접(6–11) |
| 11 | – | W17 실제 완료 뒤 R1이 W20 준비·용접(11–14) |

결과는 `WAITING`이다. W17·W18·W20이 완료되고 W19·W21은 자재 미준비로 남는다. D02의 정답 처리가 이것이다.

## 실행

### 준비 (처음 한 번)

저장소 루트의 `.env`에 프로젝트 ID와 API 키를 설정한다. `.env`는 Git에서 제외되며 같은 이름의 환경 변수가 있으면 그 값을 우선한다.

```dotenv
OPENAI_PROJECT_ID=proj_...
OPENAI_API_KEY=sk-...
```

```cmd
cd /d C:\github\shipyard-welding-automation
set UV_LINK_MODE=copy
set PYTHONUTF8=1
uv sync --locked --extra llm
uv run --locked --extra llm python -m shipyard llm-preflight
```

선급 추출본 `references-local\kr-2026-part2-rules.jsonl`이 필요하다. 선급 조항은 키워드와 임베딩을 합친 하이브리드 검색으로 찾는다. [추출과 검색 방법](welding-rule-retrieval.md)

### 사례 실행

```cmd
uv run --locked python -m shipyard prepare-case-policy --case D02 --approve
uv run --locked --extra llm python -m shipyard run-live --case D02 --allow-paid --output outputs\D02-live
```

`prepare-case-policy`는 API를 호출하지 않는다. 다음 값을 `config\llm-policy.<사례>.local.json`에 기록한다.

- 사례 입력 5개의 SHA-256
- 보고 건수
- 선급 추출본과 주제 설정을 합친 해시
- `history_retrieval_approved`

`--approve` 없이 만들면 유료 호출과 과거 결과 전송이 모두 꺼진 정책이 된다. 입력 파일·추출본·`config/rule-topics.json` 중 하나라도 바뀌면 정책 파일을 지우고 다시 만든다. 공용 템플릿 `config/llm-policy.json`은 특정 사례를 고정하지 않으며, 사례별 정책이 없으면 `run-live`는 실행하지 않는다.

`run-live`는 결과 DB에서 앞선 결과를 검색하고, 실행이 끝나면 자기 결과를 추가한다. 같은 사례 ID의 `oracle.json`과 `truth.jsonl`이 있으면 독립 채점까지 한다. 옵션은 다음과 같다.

| 옵션 | 기본값 | 의미 |
| --- | --- | --- |
| `--output` | `outputs\<사례>-live-<임의값>` | 실행 폴더. 이미 있으면 거부 |
| `--history` | `artifacts\report-history.sqlite3` | 공용 결과 DB |
| `--history-include-same-case` | 꺼짐 | 같은 사례의 앞선 결과도 검색 |
| `--top-k` | 3 | 붙일 유사 결과 수(1–5) |
| `--embed-model` | `text-embedding-3-small` | 임베딩 모델. 바꾸면 벡터를 새로 계산해 따로 캐시 |
| `--rules`, `--rule-topics` | `references-local\kr-2026-part2-rules.jsonl`, `config\rule-topics.json` | 선급 추출본과 주제 설정 |
| `--rule-search` | `hybrid` | 선급 조항 검색 방식. `keyword`면 임베딩을 쓰지 않음 |
| `--rule-embed-model` | `text-embedding-3-large` | 선급 조항 임베딩 모델. 벡터는 `artifacts\rule-embeddings.sqlite3`에 캐시 |
| `--llm-policy` | `config\llm-policy.<사례>.local.json` | 승인 정책 |

`run --report-mode responses`도 같은 처리를 하지만 `--llm-policy`, `--rules`, `--history`를 직접 지정해야 하고 채점은 별도로 실행한다.

### API 호출 수와 비용

리포트 1건마다 생성 요청 2회(조회 선택, 사건 제안)다. 리포트 수는 D01·D02·D03이 3건, D04가 2건이다. 생성마다 같은 입력의 토큰 수를 먼저 확인하는 요청이 1회 더 있다. 결과 DB 검색의 임베딩 요청은 리포트마다 1회이고, 아직 벡터가 없는 행이 처음 검색될 때 추가된다. 검색 대상이 없으면 호출하지 않는다. 선급 조항 검색은 근거 조회마다 질의 임베딩 1회가 붙는다. 처음 실행할 때만 추출본 991개 조각을 한 번 임베딩한다(약 $0.05).

생성 비용 원장(`artifacts\llm-budget.sqlite3`)은 보고·실행·프로젝트당 $0.005·$0.05·$0.10을 상한으로 예약·정산한다. 임베딩과 토큰 계산 요청은 이 상한에 포함되지 않는다.

### 결과를 API 없이 쌓기

fake 모드는 결과를 기록만 하고 검색하지 않는다. 다른 사례의 결과를 먼저 쌓아 두면 이후 `run-live`에서 유사 사례로 붙는다.

```cmd
uv run --locked python -m shipyard run --case D02 --report-mode fake --rules references-local\kr-2026-part2-rules.jsonl --history artifacts\report-history.sqlite3 --output outputs\D02-fake
```

## 결과 보기

```cmd
type outputs\D02-live\summary.json
type outputs\D02-live\evaluation.json
type outputs\D02-live\execution.csv
findstr SIMILAR_REPORTS outputs\D02-live\trace.jsonl
```

- **판정이 맞았는지.** `evaluation.json`의 `acceptance_passed`, 사건별 `correct_action`, `issues`를 본다. D04처럼 사람 확인이 정답인 사례는 `completed_without_additional_human: false`가 정상이다.
- **Agent가 어떻게 판단했는지.** `trace.jsonl`에서 `"REPORT_RESULT"`를 검색한다. 리포트별로 `MODEL` 단계의 `request`·`response`, `LOOKUP`에서 돌려준 근거·제한·조항 ID, `RULE_CITATIONS`(인용 조항)가 있다. 바로 앞의 `"EVENT"` 줄에 사건 검사 결과(`APPLIED`, `INSUFFICIENT_EVIDENCE` 등)가 있다.
- **어떤 조항이 검색됐는지.** `LOOKUP` 단계의 `rule_refs`가 검색된 조항, `RULE_CITATIONS`가 인용한 조항이다. `manifest.json`의 `rule_knowledge.retrieval`에 검색 방식(`hybrid`/`keyword`)과 임베딩 모델이, `embedded_texts`에 이번에 새로 임베딩한 문장 수가 있다.
- **유사 사례가 붙었는지.** `SIMILAR_REPORTS` 단계의 `matches`에 결과 ID(`<run_id>:<report_id>`)와 점수가 있다. `manifest.json`의 `history.retrieval.eligible_outcomes`가 검색 대상 행 수이고, 0이면 붙을 결과가 없었다는 뜻이다. `history.recorded_outcomes`는 이번에 기록한 건수다.

### 상태 코드

| 리포트 상태(`summary.reports.statuses`) | 의미 |
| --- | --- |
| `READY` | Agent가 검증을 통과한 사건 제안을 만들었다. 상태 변경 승인은 아니며 사건 검사가 따로 판단한다 |
| `NEEDS_CLARIFICATION` | Agent가 `clarify`로 확인을 요청했다 |
| `INVALID_PROPOSAL` | 제안이 검증에 실패했다. `reason_codes`에 원인이 있다. 대상이 확실하면 `IDENTITY_UNCERTAIN` 사건과 사람 확인 요청으로 바뀌고 보류는 유지된다 |
| `MODEL_ERROR`, `MODEL_TIMEOUT`, `MODEL_REFUSAL` | 공급자 오류·시간 초과·거절. 같은 확인 필요 경로로 간다 |
| `MODEL_SESSION_STOPPED` | 같은 실행에서 앞선 공급자 오류가 있어 남은 보고는 모델에 보내지 않았다 |
| `RETRIEVAL_ERROR` | 과거 결과 검색(`REPORT_RETRIEVAL_FAILED`)이나 선급 조항 검색(`RULE_RETRIEVAL_FAILED`)의 임베딩 호출이 실패했다 |

`INVALID_PROPOSAL`의 대표 원인은 다음과 같다.

| `reason_codes` | 원인 |
| --- | --- |
| `UNGROUNDED_RULE_REFERENCE` | `rule_refs`에 조회되지 않은 ID가 있다. `rule_refs`에는 `lookup_result.rule_excerpts[].id`만 넣을 수 있다 |
| `UNGROUNDED_REFERENCE` | 조회되지 않은 근거·제한 ID를 인용했다 |
| `TARGET_NOT_GROUNDED` | 리포트 대상과 다른 작업·로봇을 지목했다 |

2026-11-23 D04 1차 실행에서 Agent가 조회 결과의 내부 규칙 이름표 `rules-v1:release:FITUP_HOLD`를 `rule_refs`에 넣어 `UNGROUNDED_RULE_REFERENCE`가 났다. 프롬프트 `report-v2`부터 조회 결과의 제한 정보에서 `rule_id`·`release_rule_id`를 빼고, 지시문과 도구 스키마에 `rule_refs` 범위를 명시했다. 같은 사례의 2차 실행은 통과했다. [검증 기록](next-steps.md#2026-11-23-d04-실제-api-실행)

## 공용 결과 DB

기본 위치는 `artifacts/report-history.sqlite3`이다(`artifacts/`는 Git 제외). 실행마다 새로 만드는 `run.sqlite3`와 달리 모든 사례와 실행이 같은 파일에 결과를 추가한다.

`report_outcomes`에는 리포트 1건당 한 행이 들어간다.

| 열 | 내용 |
| --- | --- |
| `run_id`, `case_id`, `run_started_at`, `run_finished_at`, `run_termination` | 어느 실행의 결과인지, 실행이 끝난 시각과 종료 상태 |
| `report_id`, `incident_id`, `text`, `target_json` | 리포트 원문과 대상 |
| `adapter_status`, `reason_codes_json` | Agent 해석 결과(`READY`, `NEEDS_CLARIFICATION` 등) |
| `event_kind`, `event_outcome`, `evidence_ids_json` | 판정된 사건과 사건 검사 결과 |
| `rules_retrieved_json`, `rule_refs_json` | 조회된 조항과 Agent가 인용한 조항 |
| `restrictions_json` | 이 리포트가 열거나 해제한 제한, `opened_tick`·`released_tick`·`hold_ticks`(보류 기간)·실행 종료 시 상태 |
| `job_id`, `job_completed`, `job_weld_end_tick` | 대상 작업의 실제 완료 여부와 용접 종료 tick |
| `data_policy` | 기록 당시 자료 정책. 개발 사례는 모두 `SYNTHETIC_DEV_ONLY` |

결과는 실행이 예외 없이 끝났을 때만 기록한다. `termination`이 `FAILED`·`WAITING`인 실행도 결과로 남는다. 임베딩 벡터는 `outcome_embeddings`에 모델별로 캐시한다.

### 검색 범위

검색은 OpenAI 임베딩의 코사인 유사도로 한다. 대상 행은 다음 조건을 모두 만족해야 한다.

- **현재 실행이 시작되기 전에 끝난 실행의 결과**(`run_finished_at < 현재 실행 started_at`). 자기 실행이나 이후 실행의 결과는 보이지 않는다.
- 같은 `data_policy`.
- 기본으로 **같은 `case_id`는 제외**한다. 같은 사례를 다시 돌릴 때 이전 결과가 정답처럼 새는 것을 막는다. 포함하려면 `--history-include-same-case`를 준다.

벡터가 없는 행(예: fake 실행으로 기록된 행)은 처음 검색될 때 임베딩하고 캐시한다. 같은 본문은 한 번만 반환한다. Agent에 가는 항목은 `id`, `score`, `text`와 `outcome`(대상, 판정 사건, 인용 조항, 제한별 보류 기간, 작업 완료 여부, 실행 종료 상태)이다.

실험 조건(프롬프트 버전·모델)을 바꿔 결과를 섞고 싶지 않으면 `--history artifacts\history-exp2.sqlite3`처럼 새 파일을 지정한다. DB 파일을 지우면 쌓인 결과가 모두 사라진다.

## 리포트 한 건 분석

solver를 거치지 않고 리포트 한 건을 결과 DB와 선급 조항으로 분석한다. 일정은 바꾸지 않는다.

```cmd
uv run --locked --extra llm python -m shipyard analyze-report --allow-paid --text "W17 핏업 검사에서 루트 간격이 맞지 않습니다." --top-k 3
uv run --locked --extra llm python -m shipyard search-reports --allow-paid --text "와이어가 입고되지 않았습니다."
```

기본 결과 DB는 `artifacts/report-history.sqlite3`이며 `--history`로 바꿀 수 있다. 결과 DB 파일이 없으면 실행하지 않는다. 지금 시각 이전에 끝난 실행의 결과를 검색하고 `--exclude-case`로 한 사례를 뺄 수 있다. 관측 종류가 없으므로 선급 조항은 리포트 본문을 주제 설정의 `patterns`에 맞춰 고른다.

GPT는 적용한 조항과 이유를 `analysis.rule_basis`에, 참고한 과거 결과 ID를 `similar_report_ids`에 반환한다. 코드는 두 ID 모두 실제 조회 결과에 대조한다. 관련 조항을 찾지 못하면 GPT를 호출하지 않고 `INSUFFICIENT_BASIS`를 반환한다.

Windows에서 이미 준비된 가상환경을 사용하는 경우 `uv run --locked --extra llm python` 부분을 `.venv\Scripts\python.exe`로 바꿔 실행할 수 있다.
