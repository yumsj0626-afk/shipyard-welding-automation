# 최소 자동 처리 흐름의 데이터 계약

2026-11-07, 계약 `schema_version="1.0"`, 개발 정책 `dev-v1`. **정형/fake 경로와 선택적 Responses API 어댑터의 현재 계약이다. 실제 유료 호출은 미실행이다.** DTO·저장·사건 처리·계획·검사·시뮬레이션 코드와 개발 데이터·테스트를 작성했다. 실제 실행 범위와 M01–M10의 최종 증거는 [구현 작업과 검증 기록](next-steps.md)에서 관리한다. 기술 결정은 [DEC-01–06](decisions.md), 모듈과 파일은 [아키텍처](architecture.md)를 따른다. R §4.2 표7·§4.3·4.5와 B §2–6의 요구를 구현 필드·처리 조건으로 구체화했다.

## 공통 표현과 ID

- 교환 형식은 UTF-8 JSON, 여러 사건/추적은 JSONL이다. unknown은 누락이나 임의 0이 아니라 명시적 `null` 또는 `UNKNOWN` enum이다. 필수 필드는 null 허용 여부도 명시한다.
- JSON 중복 key, NaN/Infinity, 미정의 필드는 거부한다. Pydantic strict 검사에서 문자열 숫자와 bool을 정수로 받지 않는다. 파싱 성공과 근거 확인 성공을 구별한다.
- 초기 데이터 `initial.json`·`evidence.json`의 최상위에는 `schema_version`, `case_id`, `data_revision`을 둔다. 공개 snapshot·사건·계획은 `schema_version`, `run_id`를 가지며 내부 작업·지시·관측 객체는 소속 snapshot/저장 행의 run 문맥과 객체 ID로 연결한다. ID는 비어 있지 않은 ASCII `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`이며 대소문자를 구별한다. W17/R1/A 같은 업무 ID는 실행 안에서 유일하다. 엔터티 관계는 스키마·도메인 검사에서, 저장 테이블의 run 및 plan 참조는 SQL FK로 검사한다.
- `run_id`는 실행마다 새 UUID 문자열. 업무 ID와 외부 `event_id`는 입력이 정한다. `incident_id`는 같은 문제의 발생 회차, `restriction_id`는 원인별 제한, `plan_id`는 후보, `instruction_id`는 phase 지시, `execution_id`는 실제 착수 구간을 식별한다. 삭제 후 ID 재사용은 금지한다.
- `tick`은 정수 추상 시간 단위다. 1 tick을 실제 초/분으로 해석하지 않는다. 구간은 `[start_tick,end_tick)`이며 종료와 다음 시작이 같은 tick이면 겹치지 않는다. 초기 W18의 이력 `start=-2`만 음수를 허용한다. 신규 수신/착수/복구 tick은 0 이상이다.
- UTC ISO-8601 `received_at`은 감사용이다. 솔버 결과의 `elapsed_ms`와 목적 단계별 `wall_time_ms`는 실제 계산시간이며 일정 계산에는 tick만 쓴다. 개발 시뮬레이션은 솔버 계산 동안 tick을 멈춘다. 모든 감사 행에 개별 처리시간을 측정한 것은 아니며, 본 평가의 전체 사건 처리시간 측정·계산시간 반영은 DEC-08에 남긴다.
- `state_version`은 run마다 0부터 시작하는 정수이며 작업·자원·공개 근거·제한·지시·현재 tick 같은 업무 상태가 트랜잭션으로 바뀔 때 1 증가한다. 조회/감사 로그/후보 저장만으로 증가시키지 않는다. `active_plan_version`은 최초 0, 성공한 계획 적용마다 1 증가한다. 정책/규칙은 각각 `policy_version`, `rule_version`으로 고정한다.

## 공개 초기 상태와 엔터티

`initial.json`은 `now_tick`, `jobs[]`, `robots[]`, `zones[]`, `travel_times[]`, `baseline_plan|null`, `execution_progress[]`, `provenance`와 파일 메타데이터를 포함한다. `evidence.json`은 현재까지 공개된 근거와 출처 등록부만 포함한다. 앞으로의 사건·정답·복구 시각은 이 파일들에 없다.

| 객체 | 필수 필드 | 의미·검사 |
| --- | --- | --- |
| Job | `job_id`, `zone_id`, `duration_ticks`, `setup_ticks`, `due_tick`, `weight`, `predecessor_ids[]`, `material_ready: TRUE/FALSE/UNKNOWN`, `lifecycle`, `assigned_robot_id|null`, `actual_weld_start|null`, `actual_weld_end|null`, `active_execution_id|null`, `fact_refs{field:evidence_ids}` | duration>0, setup>=0, weight>0. 선행 DAG·ID 검사. 실제 완료는 관측으로만 생성. READY/HOLD는 lifecycle 값이 아님 |
| Robot | `robot_id`, `capable_job_ids[]`, `allowed_zone_ids[]`, `availability: AVAILABLE/UNAVAILABLE/UNKNOWN`, `location_zone_id`, `phase`, `phase_job_id|null`, `phase_execution_id|null`, `phase_start_tick|null`, `phase_end_tick|null`, `destination_zone_id|null`, `fact_refs` | phase는 IDLE/MOVING/WAITING_FOR_ZONE/SETTING_UP/WELDING. 이동 중 location은 출발 위치, destination 별도; 도착 관측에서 위치 갱신. 재계획으로 순간 이동 금지 |
| Zone | `zone_id`, `capacity`, `availability`, `active_execution_ids[]`, `fact_refs` | 개발 capacity=1. 준비·용접의 실제 점유를 저장하고 계획 예약과 구별. 이동 경로 충돌은 모델 밖 |
| TravelTime | `from_zone_id`, `to_zone_id`, `duration_ticks`, `provenance` | 모든 사용 구역 쌍 명시. 동일 구역0/다른 구역1은 개발 가정. 누락 경로를 0으로 채우지 않음 |
| Source | `source_id`, `source_kind`, `allowed_observation_kinds[]`, `allowed_target_scopes[]`, `provenance` | 최소 source_kind는 DOCUMENT/EXTERNAL_INSPECTION/SIMULATED_EXTERNAL/EXECUTION_OBSERVER. 등록되지 않은 source가 복구를 확정하지 못함 |
| Provenance | `basis: CONFIRMED_SOURCE/INFERRED/DEV_ASSUMPTION`, `source_ref`, `section_or_record`, `rationale`, `review_status` | 면담 요약을 읽은 사실·추정·합성값 구별. 개발 fixture의 검사 신호도 현장 측정 사실이 아님. `review_status`는 실제 검토 상태만 기록 |

진행 W18은 R2, B구역, 용접 `[-2,2)`로 초기화한다. 준비·이동은 이미 완료된 이력이다. 다른 작업의 용접 실제 시각은 null이다. `baseline_plan`은 `{plan_id, assignments:[{job_id,robot_id,weld_start|null}]}` 형식의 일정 변경량 비교용 참고이며 실행 지시가 아니다. 성공한 활성 계획이 있으면 그 계획의 공통·유효·미착수 작업을 우선 비교 기준으로 쓰고, 없으면 initial baseline을 사용한다. 원본의 순서만 있고 시각이 없으면 기준 시각을 만들어 채우지 않고 null로 기록한다.

### 근거 Evidence

필수: `evidence_id`, `incident_id|null`, `target{scope,job_id|null,robot_id|null,zone_id|null}`, `observation_kind`, `value`, `source_id`, `observed_tick`, `available_tick`, `revision`, `supersedes_evidence_id|null`, `restriction_id|null`, `related_evidence_ids[]`, `provenance`.

`observation_kind`는 `FITUP_RESULT`, `REPAIR_COMPLETED`, `RECHECK_RESULT`, `PAIR_REACHABILITY`, `MATERIAL_READY`, `ROBOT_AVAILABILITY`, `ZONE_AVAILABILITY`, `IDENTITY_MATCH`처럼 등록한 종류만 허용한다. 값은 검사/도달/식별의 PASS/FAIL/UNKNOWN, 조치 완료/자재 준비의 TRUE/FALSE/UNKNOWN, 자원 가용성의 AVAILABLE/UNAVAILABLE/UNKNOWN으로 구별한다. 근거 본문·참조 파일은 자료이고 실행 지시가 아니다.

현재 조회 가능한 근거는 `available_tick <= now_tick`, `observed_tick <= available_tick`이고 출처가 대상/종류에 권한을 가져야 한다. 같은 출처·대상·종류·발생 회차의 개정은 증가해야 하며 이전 개정은 보존한다. `supersedes_evidence_id`는 같은 출처·대상·종류·회차의 앞선 개정만 가리킬 수 있고 관측 시각도 역행하지 않는다. 서로 다른 출처의 최신 근거가 충돌하면 마지막 수신값으로 덮지 않고 EVIDENCE_CONFLICT로 남긴다. 신선도는 회차·개정·관측 순서·반증 근거로 확인하며 근거 없는 시간 만료 규칙을 만들지 않는다. 오래된 PASS가 새 FAIL을 자동 해제하지 않는다.

## 문장형 보고 어댑터 (가짜 모델 구현)

정형 `ArrivalBatch`는 변경하지 않는다. `Controller.handle_report_batch(tick, observations, reports, model)`가 같은 tick의 실제 완료 관측을 먼저 반영하고, 도착한 보고들을 해석한 뒤 기존 `events.apply_deliveries`에 한 batch로 전달한다. 수리·재확인을 따로 적용해 중간 재계획하지 않는다. `READY`는 **사건 제안을 만들었다는 뜻**이며 근거 승인·제한 해제·실제 실행 성공과 다르다.

| 객체 | 필드와 소유자 |
| --- | --- |
| `ReportInput` | `schema_version=1.0`, `run_id`, `report_id`, `incident_id`, `source_id`, `observed_tick`, `received_tick`, `text`, `target_hint|null`. ID·출처·시각·회차·선택된 대상은 접수 프로그램이 부여하고 모델이 덮어쓸 수 없다. UTF-8 본문 1–4,000자, 공백만 있는 본문 거부, 관측≤수신≤현재 tick |
| `ReportDelivery` | `report`, `published_evidence[]`. 근거는 현재 도착한 외부 관측의 별도 입력이다. 모델 응답 필드가 아니며 보고 문장을 검사 사실로 승격하지 않는다 |
| 조회 요청 | `action=lookup_evidence`, `target`, `observation_kind`. 허용된 한 종류의 현재 근거와 같은 회차·대상의 활성 제한만 반환. 일반 파일·SQL·shell·코드 실행 도구 없음 |
| 사건 제안 | `action=propose`, `kind`, `target`, `evidence_ids[]`, `restriction_ids[]`. 모델이 사건 ID·출처·시각·버전·새 근거·수식·실행 관측을 생성하는 필드는 없음 |
| 확인/거절 | `action=clarify`와 `reason` enum 또는 `action=refuse`. 임의 도구명·추가 필드·잘린 JSON·중복 key·잘못된 enum/타입은 거부 |
| `AdapterResult` | `status`, `reason_codes`, `report_id`, `report_fingerprint`, `based_on_state_version`, `received_report`, `model_calls`, `lookup_count`, `delivery|null`, `trace[]`, `prompt_version=report-v1`. received_report는 접수 입력을 보존하며 모델 응답이 아니다. trace의 MODEL은 요청/응답·응답 hash, LOOKUP은 조회와 반환 ID를 보존 |

조회는 `visible_evidence_refs`에 있고 현재까지 공개된 근거와 이 보고와 함께 **현재 도착한** 근거만 대상으로 한다. 전자는 `PUBLIC_STATE`, 후자는 `ARRIVED_UNCOMMITTED`로 구별한다. 새 근거는 출처·관측 종류·대상·시각을 검사한 뒤에도 미승인 상태이며 기존 사건 처리기가 개정·반증·제한 연결 등을 다시 검사한다. 서로 다른 출처/개정은 함께 반환해 모델이 임의로 진실을 선택하지 못하게 한다. `provenance` 자유 서술은 모델 조회 결과에서 제외한다. 제안에 인용할 수 있는 ID는 이 보고에서 실제 반환된 목록뿐이다. 빈 인용도 사실을 보증하지 않으며 기존 사건 처리기가 현재 근거를 직접 재조회한다.

`target_hint`는 본문에서 모델이 추측한 값이 아니라 접수자가 선택한 업무 대상이다. 있으면 모델의 대상은 정확히 같아야 한다. 없으면 제안 ID가 실제 엔터티이면서 본문에 명시돼 있어야 한다(현재 별칭 해석은 미구현). 실패 시 본문에서 임의로 대상을 추출하여 보류 범위를 정하지 않는다. 보고 source는 등록된 비실행 관측 source이며 `IDENTITY_MATCH` 권한이 필요하다. 실제 계정 인증은 외부 접수기의 후속 책임이며 이 개발 시뮬레이터는 등록부와 입력 계약을 검증한다.

### 오류, 중복, 적용 경계

| 결과 | 다음 행동 |
| --- | --- |
| `READY` | 기존 사건 검증으로 제출. 그 검증의 `APPLIED`, `INSUFFICIENT_EVIDENCE`, `EVIDENCE_CONFLICT`, `INVALID_INPUT` 등을 따로 기록 |
| `NEEDS_CLARIFICATION`, `MODEL_REFUSAL`, `MODEL_TIMEOUT`, `MODEL_ERROR` | 신뢰할 수 있는 접수 대상이 있으면 `IDENTITY_UNCERTAIN` + `REPORT_INTERPRETATION` unknown 필드로 공통 처리해 그 대상에 `EVIDENCE_PENDING`. 대상이 없으면 `UNRESOLVED`로 식별 요청. 기존 제한은 해제하지 않음 |
| `INVALID_MODEL_RESPONSE`, `INVALID_PROPOSAL`, `INVALID_EVIDENCE`, `RESPONSE_LIMIT`, `CONTEXT_LIMIT`, `TOOL_LIMIT`, `MODEL_CALL_LIMIT` | 같은 확인 필요 경로. 모델 출력/잘못된 도착 근거로 기준 사실을 쓰지 않음 |
| `INVALID_REPORT` | host run/시각/출처/대상 권한 오류. 모델을 호출하지 않고 사건도 만들지 않으며 기록과 추가 확인 요청만 남김 |
| `STALE_CONTEXT` | 해석 중 상태 버전이 바뀌면 제안을 버리고 현재 상태에서 위 확인 필요 경로로 전환. 모델 자동 재호출 없음. 이후 사건 검사와 계획 CAS도 별도로 수행 |
| 같은 보고 ID·같은 내용 | 기존 `REPORT_RESULT` 확인, 수신 기록만 추가. 모델·사건·계획을 중복 실행하지 않음. 같은 batch의 같은 보고도 한 번 해석 |
| 같은 보고 ID·다른 내용 | `ID_CONFLICT`, 추가 확인 요청, 새 제안 적용 안 함. 같은 batch에서 최초 충돌한 ID도 격리 결과를 저장해 이후 단독 재전달로 승인되지 않음 |

보고 fingerprint는 접수 보고(재수신 tick 제외)와 도착 근거의 불변 내용을 정규화한 SHA-256이다. `report_id`를 사건 ID로 사용하므로 같은 실행의 정형 사건 ID와도 충돌하지 않게 접수 프로그램이 관리한다. DB의 `REPORT_RESULT`와 변환 사건·근거·상태 변경은 `commit_event_batch(..., report_results=...)`의 **한 트랜잭션**으로 저장한다. 조회·모델 실행은 트랜잭션 밖에서 한다. 중복 수신은 `REPORT_RECEIPT`, 원래 해석은 `REPORT_RESULT`에서 확인한다. 해석 실패도 같은 ID로 자동 재시도하지 않으며 정정/새 관측은 새 보고 ID로 제출한다. 기록 저장 이전의 프로세스 crash 자동 재개는 구현하지 않았다.

해석 실패가 만든 `EVIDENCE_PENDING:IDENTITY_MATCH`는 정상 FITUP/수리 보고만으로 해제되지 않는다. 같은 대상·회차의 명시적 `IDENTITY_MATCH=PASS` 확인 근거와 새 `IDENTITY_UNCERTAIN` 보고가 필요하며, 해당 정보 보류만 해제한다. 기존 FITUP_HOLD 등 물리적 시작 제한은 각각의 복구 근거를 계속 요구한다. 새 보고의 제안은 조회에서 받은 해당 제한 ID를 인용한다. 이는 보수적으로 확인을 요청하는 개발 정책이며 본문 자동 재해석으로 보류를 해제하는 기능은 없다.

보고당 모델 2회·읽기 전용 조회 1회, 응답 16,384 UTF-8 bytes·요청 문맥 32,768 bytes를 개발 상한으로 둔다. 자동 재시도는 없다. 가짜 모델의 `TimeoutError` 주입은 오류 분기 검사이며 벽시계 deadline이나 실제 API 취소가 구현됐다는 뜻이 아니다. 과금 토큰·실제 금액·SDK timeout·전체 deadline은 유료 연결 전에 별도로 정한다([DEC-07](decisions.md)).

`simulation/reports/D01–D04.jsonl`은 보고 본문·접수 대상·가짜 응답 대본을 가진 드라이버 전용 replay fixture다. 미래 대본을 모델 문맥으로 넘기지 않고 현재 도착한 보고의 대본만 `FakeReportModel`에 준다. 가짜 모델은 대본을 반환할 뿐 문장을 이해하거나 정답을 추론하지 않는다. 모델에 전달된 요청을 관측하는 sentinel 테스트로 미래 보고·복구 근거·oracle·truth 누출을 확인한다. 이 경계는 단일 프로세스 데이터 흐름이며 OS 격리가 아니다.

## Responses API 공급자 계약 (미승인 파일럿)

`responses.ResponsesReportModel.respond(request)`는 기존 `ReportModel` 인터페이스를 구현한다. SDK는 이 공급자 경계에서만 지연 import하며 운영 상태 저장소를 받지 않는다. 기존 `interpret_report`가 조회 결과를 만들고 최종 사건을 검사한다. 기준 상태·제약·솔버·checker·evaluator는 그대로 사용한다. 새로운 `ModelCallFailure(status, reason, metadata)`는 공급자별 실패를 기존 대상별 확인 대기/식별 요청 경로에 연결하며 원문 HTTP 오류를 복사하지 않는다.

### 실제 전송과 응답

첫 요청에는 고정 system instruction, 보고 본문과 접수 대상, 현재 엔터티 ID, 사건 종류 목록·현재 tick만 보낸다. run/report/source ID·전체 snapshot·파일 경로·미래 대본·정답·API 키를 프롬프트에 합치지 않는다. 조회 후에는 해당 보고의 첫 function call과 `call_id`를 가진 `function_call_output`으로 현재 조회 결과를 보낸다. SDK의 서버 대화 저장이나 `previous_response_id`를 사용하지 않는다. 첫 응답의 유효한 output items를 그대로 연결하고 새 보고는 새 대화로 시작한다.

허용 function은 `lookup_evidence`, `propose`, `clarify`이며 모두 기존 Pydantic 계약으로 만든 strict schema를 사용한다. 각 객체는 모든 필드를 required로 표시하고 nullable 필드로 선택값을 나타내며 extra 필드를 금지한다. `parallel_tool_calls=false`, 한 응답의 허용 function call 정확히 1개를 요구한다. 조회 이후에는 lookup tool을 제거한다. `propose`도 실행 도구가 아니라 사건 제안의 전달 형식이다. 알 수 없는 도구·복수 호출·함수명/action 불일치·추가 코드 필드는 거부한다.

생성 요청은 `store=false`, `background=false`, `stream=false`, Standard `service_tier=default`, `reasoning.effort=none`, `truncation=disabled`, explicit cache 모드와 breakpoint 없음으로 고정한다. SDK 기본 재시도 대신 `max_retries=0`을 쓴다. 토큰 count endpoint에는 SDK가 지원하는 동일 model/input/instructions/tools/choice/reasoning 형식을 전달하며, count 규격에 없는 저장 옵션을 넣었다고 주장하지 않는다. count도 외부 데이터 전송이므로 같은 사전 동의 뒤에만 실행한다. 모델·데이터 보관·count 청구의 공식 확인 범위는 [DEC-07](decisions.md)에 있다.

### 승인·범위·시간과 비용

`config/llm-policy.json`은 검토용 제안이며 `approved_for_paid_calls=false`, `project_id=null`이다. 생성과 count를 시작하려면 승인된 로컬 설정과 `--allow-paid`가 모두 있어야 한다. `OPENAI_API_KEY`는 실행 환경에서만 읽고 파일/프롬프트/trace에 기록하지 않는다. SDK API URL은 `https://api.openai.com/v1`로 명시하며 환경 프록시·redirect로 다른 곳에 전송하지 않는다. 조직·프로젝트·모델의 실제 권한은 계정 설정의 책임이며 모델 metadata 조회만으로 생성 권한·잔액·쿼터를 확인했다고 쓰지 않는다.

첫 구현의 외부 전송은 **합성 D01, 최대 3개 보고**로 제한한다. 승인 설정에는 공개 초기값·근거·드라이버 입력·보고 대본·개발 정책의 SHA-256을 고정한다. 파일 내용이 달라지거나 다른 사례를 지정하면 네트워크 전에 거부한다. 이것은 승인한 fixture의 바이트 경계이며 민감정보를 자동 탐지하는 기능이 아니다. 원본 보고서와 실제 현장 자료는 전송 대상에 포함하지 않는다.

제안 기본값은 입력 12,000/출력 1,000토큰(요청당), 보고당 생성 2회·조회 1회, HTTP 작업 15초·보고 전체 45초·실행 전체 180초다. count는 생성 전 최대 1회다. 출력 상한은 reasoning/formatting을 포함하는 API max_output_tokens로 전달하고, 입력은 count 결과가 상한 이내일 때만 생성한다. UTF-8 바이트/본문 길이의 기존 제한도 유지한다. AsyncOpenAI 요청은 `asyncio.timeout`으로 감싸고 남은 보고/실행 시간을 다시 계산하며 deadline 이후 결과는 적용하지 않는다. 로컬 취소는 서버 생성·과금 취소 보장이 아니다.

`llm_budget.BudgetLedger`는 운영 DB와 분리된 `artifacts/llm-budget.sqlite3`를 사용한다. 보고($0.005)·실행($0.05)·프로젝트 누적($0.10)의 제안 한도를 정수 micro-USD로 저장한다. 네트워크 전에 최대 입력·출력에 대한 generation 비용을 `BEGIN IMMEDIATE`로 예약한다. Luna의 현재 Standard text 단가와 캐시 비사용 조건에서 최대 한 생성 예약액은 **1,700 micro-USD($0.0017)**다. usage가 유효하면 generation 추정 비용만 정산하고, timeout/연결 실패/usage 누락/과다 usage/프로세스 종료 시 미정산 예약을 유지한다. 같은 run/report의 예약 횟수도 2회로 제한하므로 재시작으로 생성 상한을 초기화할 수 없다. count에서 실패한 예약도 횟수에 포함한다. 보고·실행·프로젝트 한도는 동시에 검사하며 다른 프로세스·새 실행에서도 같은 프로젝트 원장을 계속 사용한다. 다른 DB·스키마를 기존 원장으로 오인하지 않는다.

원장의 `settled_micro_usd`는 공급자 청구서가 아니라 usage와 고정 단가에서 계산한 generation 추정액이다. count의 별도 청구 조건은 확인되지 않았으므로 계정 전체 청구액의 절대 상한을 보장하지 않는다. 플랫폼의 별도 지출 한도도 설정·전파 조건을 확인해야 한다. 원장을 삭제하거나 새 경로로 바꿔 한도를 초기화하면 안 된다. 이 버전은 저장된 프로젝트 한도를 조용히 변경하지 않으며, 변경에는 사용자의 별도 결정과 명시적 후속 작업이 필요하다.

### 오류와 추적

| 결과 | 동작 |
| --- | --- |
| `MODEL_AUTH_ERROR`, `MODEL_PERMISSION_ERROR`, `MODEL_NOT_FOUND` | 인증/권한/모델 endpoint 오류를 구별. 자동 모델 변경 없이 현재 공급자 세션 중단 |
| `MODEL_QUOTA_ERROR`, `MODEL_RATE_LIMIT` | 429의 quota와 rate 원인을 구별. 숨은 재시도 없이 중단 |
| `MODEL_TIMEOUT`, `MODEL_CONNECTION_ERROR`, `MODEL_SERVICE_ERROR`, `MODEL_REQUEST_ERROR` | 지연·연결·서비스·요청 오류. 미확정 비용 예약 유지, 사건은 기존 확인 필요 경로 |
| `MODEL_REFUSAL`, `MODEL_INCOMPLETE` | 거절 또는 불완전 출력. 유효해 보이는 함수 인자가 있어도 제안으로 적용하지 않음 |
| `MODEL_INPUT_LIMIT`, `MODEL_BUDGET_EXCEEDED`, `MODEL_CALL_LIMIT` | 입력/count·금액·횟수 상한 위반. 다음 generation 금지 |
| `MODEL_USAGE_UNKNOWN`, `MODEL_USAGE_MISMATCH` | usage 누락/이상·출력/입력 초과·모델 불일치. 예약 유지하고 공급자 세션 중단 |
| `MODEL_SESSION_STOPPED` | 앞선 전송/정산 오류 뒤 남은 보고에서 추가 요청하지 않음 |

`REPORT_RESULT.trace[].provider`에는 모델/SDK/wire 버전, 호출 단계, response/request ID, 토큰 사용량, 예약 ID·예약액·generation 추정액·정산 상태를 기록한다. `manifest.llm`에는 키/프로젝트 ID를 제외한 정책·정책 hash·SDK 버전·원장의 실행/프로젝트 집계를 남긴다. 보고의 `model_calls`는 기존 어댑터 호출 횟수이며 실제 성공한 generation 수와 같다고 가정하지 않는다. 사전 차단과 token-count 단계 실패는 provider metadata의 단계로 구별한다. local trace는 기존처럼 Git 제외 실행 폴더에 보존하며 자동 삭제 기간은 아직 정하지 않았다.

## 사건과 활성 제한

### 외부 근거의 도착과 공개

시뮬레이터 전용 `events.jsonl`의 각 레코드는 `{delivery_tick, event, published_evidence[]}`다. 미래 사건과 새 수리/재확인 근거를 같이 보관하되 드라이버는 해당 tick에 도달한 레코드만 `ArrivalBatch{tick, observations[], deliveries[]}`로 넘긴다. 초기 DB에 미래 근거를 미리 넣고 쿼리에서 숨기는 방식을 사용하지 않는다.

controller는 공개할 근거의 source·대상·시각·ID/개정과 event 참조를 검사한다. 검증된 새 근거·사건 처리·제한 갱신을 한 트랜잭션으로 기록한다. 원문 수신 로그는 별도 보존하되 미검증 근거를 공개 사실 테이블로 승격하지 않는다. 근거 ID는 불변이며 다른 내용의 재사용은 충돌이다. 같은 tick의 deliveries를 모두 대조한 뒤 충돌 근거를 표시하고 한 번 계획한다. 수신 fingerprint는 event 도메인 payload와 published evidence의 불변 내용 hash를 연결한다.

### EventEnvelope

필수 필드:

```text
schema_version, run_id, event_id, incident_id, source_id,
kind, target, observed_tick, received_tick, observed_state_version|null,
evidence_ids[], restriction_ids[], unknown_fields[],
raw_report|null, supersedes_event_id|null
```

`kind`의 허용값은 `FITUP_FAILED`, `PAIR_UNREACHABLE`, `PAIR_REACHABILITY_CHANGED`, `REPAIR_REPORTED`, `RECHECK_REPORTED`, `MATERIAL_READINESS_CHANGED`, `ROBOT_AVAILABILITY_CHANGED`, `ZONE_AVAILABILITY_CHANGED`, `IDENTITY_UNCERTAIN`이다. `PAIR_UNREACHABLE`은 FAIL 보고이며, 구현 과정에서 추가한 `PAIR_REACHABILITY_CHANGED`는 같은 조합의 FAIL/PASS 상태 변화와 명시적 복구를 표현한다. 불가 보고에 PASS를 억지로 넣지 않고 R §2.3 표4와 B §3의 조합별 제한·복구 요구를 이어가기 위한 구체화다. 구조화된 실행 관측은 아래 별도 계약을 쓰고 외부 source가 가짜 완료 관측을 만들지 못하게 한다. 정형 사건은 LLM 없이 이 계약으로 들어온다. 문장형 어댑터는 동일 필드의 제안을 만들지만 출처 권한과 도메인 검사를 우회할 수 없다.

- `target.scope`는 JOB/JOB_ROBOT/ROBOT/ZONE/UNRESOLVED. FITUP은 JOB, PAIR는 JOB_ROBOT이며 필요한 ID가 반드시 있어야 한다. 범위가 미확정이면 자동으로 R1전체나 구역전체를 정지시키지 않는다.
- 일치한 대상에서 실행 여부가 미확정이면 그 대상의 새로운 착수를 `EVIDENCE_PENDING` 제한으로 차단하고 조회/확인을 진행한다. 대상 자체를 특정할 수 없으면 사건을 격리하고 식별 질문을 남기며 알려진 무관한 작업은 유지한다.
- 수신 tick은 드라이버가 부여한다. `received_tick>=observed_tick`이어야 하며 현재 tick보다 미래인 사건은 입력에서 거부한다. 늦게 도착한 외부 관측은 현재 상태와 대조하여 반영하고 과거를 되감지 않는다.
- `observed_state_version`이 오래됐으면 최신 정보로 재검사한다. 오래됐다는 이유만으로 실제 불가 보고를 버리지 않는다. 이미 진행 용접을 침해하는 사건은 `OUT_OF_SCOPE_RUNNING`으로 기록하고 시나리오 오류로 처리하며 용접을 소급 취소하지 않는다.

### 중복과 충돌

`(run_id,event_id)`를 UNIQUE로 둔다. canonical payload는 도메인 필드로 만든 UTF-8 JSON의 SHA-256이며 key 정렬·공백 제거·배열 중 set 의미 필드의 정렬을 고정한다. `received_tick`, `received_at`, 재전달 순번 등 수신 메타데이터는 fingerprint에서 제외한다. 같은 ID/같은 fingerprint는 기존 처리 결과와 처리 버전을 반환한다. 같은 ID/다른 fingerprint는 `ID_CONFLICT`; 새 제한·해제를 적용하지 않고 충돌 로그와 확인 대상을 남긴다. 기존 제한은 유지한다.

사건 자체가 잘못된 경우와 알려진 대상의 근거 부족을 구별한다. 데이터 파싱 실패가 임의의 전역 보류가 되지 않는다. 같은 tick의 상반된 근거는 모두 접수한 뒤 충돌 판정을 먼저 하고 최종 상태에서 한 번 계획한다. arrival order로 PASS가 FAIL을 덮지 않는다. 동일 batch에서 처음 보는 같은 사건 ID가 충돌해도 격리 결과를 저장하여 나중의 단독 재전달을 새 사건으로 승인하지 않는다. 중복 수신은 수신 이력과 EVENT에만 남기며 제한 생성/해제·추가 사람 요청을 다시 발생시키지 않는다.

### Restriction

필수: `restriction_id`, `incident_id`, `kind`, `target`, `status: ACTIVE/RELEASED`, `opened_event_id`, `opened_evidence_ids[]`, `opened_tick`, `rule_id`, `release_rule_id`, `released_event_id|null`, `released_evidence_ids[]`, `released_tick|null`.

| 제한/파생 조건 | 생성과 범위 | 해제 규칙 |
| --- | --- | --- |
| `FITUP_HOLD` | 유효한 W17 FAIL, JOB W17 모든 로봇 | 같은 회차·대상의 조치 완료 + 그 조치에 연결된 재확인 PASS |
| `PAIR_BAN` | 근거로 확인된 R1–W17만 | 해당 제한을 지목한 더 최신 PAIR_REACHABILITY PASS |
| `ROBOT_BAN` / `ZONE_BAN` | 확인된 자원·구역의 신규 착수 | 같은 대상·제한을 지목하는 유효 AVAILABLE 근거 |
| `EVIDENCE_PENDING` | 특정된 대상에 필요한 근거 누락/충돌 | 그 누락/충돌을 해결한 근거와 검증 결과. 해결 중 물리 FAIL 확인 시 별도 물리 제한 생성 |
| `PREDECESSOR_INCOMPLETE` | W20의 W17 미완료로 계산하는 **파생 차단** | W17 실제 완료 기록으로만 착수 차단 해소. 저장된 FITUP_HOLD를 W20에 복제하지 않음 |
| 자재 미준비/UNKNOWN | Job readiness의 현재 공개 값으로 계산 | 유효한 readiness TRUE 근거. 복구 신호가 자재 상태를 임의로 바꾸지 않음 |

한 제한의 해제는 다른 제한을 지우지 않는다. 해제 근거는 `restriction_id`, `incident_id`, 대상이 모두 맞고 제한 활성화 이후 관측이어야 한다. 재확인은 `related_evidence_ids`에 같은 대상·회차·제한의 조치 완료 근거를 명시하고 `recheck.observed_tick >= repair.observed_tick`이어야 한다. 현재 조치 완료 근거가 FALSE/UNKNOWN으로 바뀌거나 출처 간 충돌한 경우에는 옛 TRUE를 연결해 해제할 수 없다. 같은 tick은 연결된 근거의 순서로 구별한다. 오래된 회차의 PASS, 대상이 다른 PASS, 재확인 없는 수리 보고는 해제 근거가 아니다. 동일 해제 사건의 재전달은 no-op이다. 다른 사건 ID로 비활성 제한을 지정하면 새로 해제하지 않고 확인 필요 사유를 남긴다. 해결되지 않은 추가 제한이 있으면 W17은 계속 제외된다.

`EVIDENCE_PENDING`의 `rule_id`에는 확인해야 할 관측 종류를 포함한다. 같은 회차의 다른 종류 근거가 도착했다고 정보 보류를 일괄 해제하지 않는다. 해당 종류의 현재 근거가 확인되면 정보 보류를 해제하고, 물리 FAIL이면 별도 FITUP_HOLD/PAIR_BAN 등을 남긴다. 해제한 제한의 상세 객체는 RESTRICTION 로그에 보존하고 공개 snapshot의 `active_restrictions`에는 ACTIVE만 둔다.

## 상태 전이와 phase

| 대상 | 허용 전이와 조건 |
| --- | --- |
| 작업 | `NOT_STARTED -> COMMITTED`: 첫 MOVE/SETUP phase를 실제로 시작할 때. 계획 수락만으로 바꾸지 않음 |
| 작업 | `COMMITTED -> RUNNING`: 유효한 WELD_START 관측, 실제 선행 완료·근거·자원 재검사 |
| 작업 | `RUNNING -> COMPLETED`: 등록된 execution의 실제 WELD_END 관측. 예정 시각 경과나 핏업 복구로 만들지 않음 |
| 작업 | `COMMITTED -> NOT_STARTED`: 용접 전 취소, 현재 MOVE/SETUP phase 종료와 위치·점유 반영 후. 취소 이유와 실행 이력 보존 |
| 로봇 | `IDLE -> MOVING -> WAITING_FOR_ZONE(필요시) -> SETTING_UP -> WELDING -> IDLE`. 각 phase 경계에서 새 제한과 현재 계획을 검사. 취소 시 현재 phase 종료 뒤 IDLE |

이동이 0이면 같은 tick에서 완료 관측을 반영한다. 용접 준비 상태를 임의로 재활용하지 않도록 취소 뒤 새 작업을 배정하면 setup을 다시 수행한다. 준비 완료 시 구역을 다음 WELD 시작까지 같은 tick에서 보유하되, 취소되면 release한다. 진행 phase 종료 관측은 계획 교체로 무효화하지 않는다. W18 WELD는 비선점으로 `end=2` 고정이다.

미착수 계획의 W20을 미래에 배치하는 것은 가능하다. 모델에서는 W17의 예정 종료 뒤 W20의 **MOVE 시작**을 제한한다. 실제 실행에서는 MOVE·SETUP·WELD 각 시작 전에 W17 `COMPLETED`와 실제 종료 tick을 다시 확인한다. 이 개발 정책은 선행 완료 전 이동도 허용하지 않는 보수적 가정이다.

이동/준비 중 새 제한이 생기면 현재 phase는 끝내고 다음 phase를 시작하지 않는다. 이미 이동한 위치를 되돌리지 않는다. 재계획은 진행 phase를 고정 입력으로 받고 그 완료 뒤만 재배정한다. 같은 작업을 계속 진행하는 경우 이미 끝낸 이동/준비를 중복 실행하지 않는다. 새로운 제한 때문에 취소된 경우에는 위 취소 규칙을 따른다. 용접 도중 중단은 범위 밖이다.

## 제약과 계획

`PlanningSnapshot`은 `schema_version`, `run_id`, `state_version`, `active_plan_version`, `now_tick`, `jobs`, `robots`, `zones`, `travel_times`, `active_restrictions`, `visible_evidence_refs`, `evidence`, `sources`, `baseline_plan`, `policy_version`, `rule_version`, `current_instructions`, `execution_progress`만 allowlist로 새로 구성한다. `evidence`와 `sources`는 현재 공개 근거의 내용·출처 권한을 검사하기 위해, `baseline_plan`은 일정 변경량 비교를 위해 포함한다. 모두 공개 초기값과 실제 도착한 자료에서 구성하며 DB 경로·scenario 원본·private future queue·oracle·truth를 포함하지 않는다.

최소 구현의 `current_instructions`에는 완료·취소된 지시도 실행 ID 연결을 위해 보존한다. 목록에 존재한다는 것만으로 활성 지시가 되지 않으며 상태·적용 계획 버전·최신 전제조건으로 착수 가능성을 판단한다.

`execution_progress[]`는 `{job_id,attempt_id,robot_id,completed_phases:[{phase,execution_id,start_tick,end_tick,from_zone_id,to_zone_id}],current_phase|null,next_phase|null,setup_valid}`다. 관측에서만 갱신한다. SETUP 종료와 WELD 시작 사이에도 setup_valid와 완료 prefix를 보존하여 중복 준비를 막는다. 용접 전 취소 시 setup_valid=false, 새 attempt에서 준비를 다시 한다. 현재 지시 목록만으로 과거 phase 완료를 추측하지 않는다.

`ConstraintSet` 필수: `snapshot_ref`, `required_job_ids`, `deferred_jobs[{job_id,reason_codes,restriction_ids,evidence_ids,recheck_triggers}]`, `allowed_assignments`, `precedence_edges`, `fixed_executions`, `travel_setup`, `zone_capacities`, `immediate_start_pairs`, `horizon_end`, `rule_version`, `constraint_hash`.

계획 대상은 미완료 작업 중 준비가 확인되고 활성 제한이 없으며 능력·사용 가능 자원 조합이 있는 작업이다. 후속의 선행이 완료 또는 같은 계획의 필수 대상이면 후속도 포함할 수 있다. 선행이 보류/미준비/미확정으로 제외되면 후속도 이유와 함께 제외한다. ROBOT_BAN은 다른 가능한 로봇까지 배제하지 않는다. 다른 로봇/진행 작업의 종료를 기다리는 것은 제외 이유가 아니라 시간 제약이다. 진행 phase·용접은 고정 의무로 포함한다. DAG 순서로 필수/제외를 계산하고 모든 미완료 작업에 포함 또는 제외 이유가 있어야 한다.

| 제약 | 모델 표현과 독립 검사 기준 |
| --- | --- |
| 배정 | required의 미착수 작업별 허용 로봇에 정확히 한 번 배정. deferred는 신규 지시 없음. held 작업에 exactly-one을 남겨 모순을 만들지 않음 |
| 시간 | 신규 `move_start>=now_tick`. 고정 진행 이력만 과거 시작 허용. `move_end=move_start+travel`, `setup_start>=move_end`, `setup_end=setup_start+setup`, `weld_start=setup_end`, `weld_end=weld_start+duration`. 이동 뒤 구역을 기다리는 동안 로봇 점유 유지 |
| 로봇 | `[move_start,weld_end)` 배타. 현재 실행 phase와 WELD를 고정. 다음 작업 이동시간은 **바로 앞 작업의 종료 위치** 기준 |
| 구역 | `[setup_start,weld_end)` capacity=1. 진행 준비/용접도 반영. 로봇이 구역에 이동해 있다는 이유로 무한 점유하지 않음 |
| 선행 | `successor.move_start >= predecessor.weld_end`; 완료된 선행은 실제 종료값. 실제 시작 gate는 COMPLETED 필수 |
| 전환 | 각 로봇의 dummy 시작 위치와 인접 작업 arc/순서를 모델링. 비인접 모든 쌍에 전환시간을 중복 추가하지 않음. 첫 이동은 로봇의 현재/고정 phase 종료 위치에서 계산 |
| 즉시 착수 | DEC-04의 즉시 가능한 조합이 있으면 `z[j,r] => assigned[j,r] AND move_start[j,r]==now`, `sum(z)>=1`. 미선택 optional interval로 통과하지 못함. 후보가 없으면 추가하지 않음 |

`setup_start>move_end`인 대기에도 로봇은 해당 작업 COMMITTED 상태다. 대기는 `phase=WAITING_FOR_ZONE`, `phase_job_id=job_id`로 표현하고 COMMITTED 예약을 유지한다. 실제 이동은 완료됐으므로 current phase execution ID는 null이고 다음 SETUP 지시가 예약을 연결한다. 이 대기는 재계획 때 취소 가능하며 위치를 보존한다. 대기 상태를 유휴 자원으로 보아 다른 작업을 중복 배정하지 않는다.

Plan 필수: `plan_id`, `based_on_state_version`, `based_on_tick`, `based_on_active_plan_version`, `policy_version`, `rule_version`, `constraint_hash`, `required_job_ids`, `deferred_jobs`, `fixed_segments[]`, `assignments[]`, `solver_result`, `created_at`, `payload_hash`.

각 assignment는 `mode: NEW/CONTINUATION`, `attempt_id`, `resume_phase`, `job_id`, `robot_id`, `zone_id`, `move_start`, `move_end`, `setup_start`, `setup_end`, `weld_start`, `weld_end`, `predecessor_ids`, `source_position`, `is_fixed`, `execution_id|null`을 갖는다. `NEW`는 모든 이동/준비/용접 시각을 가진다. `CONTINUATION`은 같은 로봇·attempt의 끝난 prefix를 execution_progress로 참조하고 남은 phase 시각만 계획한다. 이미 완료된 phase의 계획 시각은 null을 허용하며 위 시간식은 실제로 남은 phase에만 적용한다.

`fixed_segments`는 `{execution_id,attempt_id,job_id,robot_id,phase,zone_id|null,start_tick,end_tick}`다. 현재 MOVE/SETUP/WELD의 구간은 여기에만 한 번 표현하고 로봇/구역 NoOverlap에 넣는다. CONTINUATION은 고정 segment 종료 이후의 잔여 suffix만 자원 interval로 넣어 **자기 구간 중복을 금지**한다. 이미 끝난 과거 구간은 재계획 자원 interval에 다시 넣지 않는다. W18은 fixed WELD [-2,2)만 있고 새로운 assignment가 없다. 알 수 없는 W18의 과거 이동/준비 시각은 생성하지 않는다.

COMMITTED 작업을 유지하면 현재 로봇/attempt와 prefix를 보존한다. 취소가 결정되면 현재 phase만 fixed로 남기고 이후 suffix를 제거한다. 현재 phase 종료·상태 반영 뒤 새 attempt에서 재배정할 수 있으며 곧바로 같은 작업을 다른 로봇에 중복 배정하지 않는다. 모든 required 작업은 새/잔여 assignment 또는 fixed WELD로 정확히 한 번 완료 경로를 갖는다. 취소 중 현재 phase만 남은 작업은 deferred 이유를 기록한다.

`solver_result`의 실제 필드는 `raw_status`, 정규화한 `status`, `objective_values[]`, `proven_stages`, `elapsed_ms`, `reason_codes[]`, `random_seed`, `num_search_workers`, `time_limit_seconds`, `solver_version`, `termination_reason`, `stages[]`다. 각 stage에는 `name`, `status`, `objective|null`, `best_bound`, `wall_time_ms`를 기록한다. 목적과 자동 적용 규칙은 DEC-04·05를 따른다.

### 독립 검사와 원자적 적용

CheckResult 필수: `check_id`, `plan_id`, `plan_payload_hash`, `checked_state_version`, `checked_tick`, `checked_active_plan_version`, `policy_version`, `checker_version`, `passed`, `violations[]`.

최소 구현의 `violations`는 `"위반코드"` 또는 `"위반코드:대상/상세"` 문자열 목록이다. 대상·기대값·관측값을 별도 중첩 객체로 중복 저장하기보다 계획·snapshot·근거 ID가 연결된 추적 기록에서 확인하도록 단순화했다. 위반 코드와 원인 추적을 생략한 것은 아니며, 후속 UI나 분석에서 필드별 질의가 필요하면 구조화 객체로 확장한다.

검사기는 원 공개 상태에서 배정·제외·선행·시간·능력·점유·이동/준비·현재 실행 고정·정책을 다시 검사한다. `constraints.py`의 모델 생성 함수나 솔버의 PASS를 호출하여 검사를 대신하지 않는다. 단순 동일 코드 두 번 실행은 독립 검사로 보지 않는다.

적용 절차는 다음과 같다.

1. 솔버/검사는 DB 쓰기 트랜잭션 밖에서 실행한다.
2. `BEGIN IMMEDIATE` 후 현재 run row를 읽고 기대 `state_version`, `now_tick`, `active_plan_version`과 일치하는지 확인한다. check의 plan hash/버전/정책도 대조한다.
3. 일치하면 이전 pending 지시를 supersede하고 고정된 현재 실행은 보존한다. 새 accepted plan version, 지시, 작업 예약, 감사 로그를 같은 트랜잭션으로 기록한다. `UPDATE runs ... WHERE state_version=? AND now_tick=? AND active_plan_version=?`의 rowcount=1도 확인한다.
4. 불일치·오류면 전부 rollback. `STALE_PLAN`은 최신 상태에서 다시 검사·계산한다. 예전 PASS를 재사용하지 않는다. controller가 인메모리 DB 사본을 기준으로 쓰지 않게 한다.
5. SQLite commit 후 드라이버는 새 지시를 ID로 조회한다. 재전달해도 instruction/execution의 UNIQUE 키와 start gate가 중복 착수를 막는다. 프로세스 crash 후 자동 재개는 첫 버전의 필수 기능이 아니며 기존 출력 보존 후 새 run으로 재실행한다.

최종 plan 적용 자체도 state_version을 올린다. 지시 실행 때는 ‘후보 계산 버전 그대로인가’만 비교하지 않고 **현재 활성 지시/실행 ID와 최신 전제조건**을 확인한다. 완료 관측 때문에 버전이 올랐다는 이유만으로 이미 진행 중인 실행을 폐기하지 않는다.

사전식 solve는 단계마다 마지막 유효 incumbent와 증명한 상위 목적 수 `proven_stages`를 보존한다. 하위 목적 단계에서 UNKNOWN/예산 소진이어도 이전 incumbent를 `FEASIBLE_NOT_PROVEN`으로 반환할 수 있다. 네 목적 모두 증명한 경우에만 전체 OPTIMAL이다. 앞 단계의 해와 정확히 고정한 목적값이 있는데 다음 단계가 INFEASIBLE이면 모델 구현 오류로 진단하며 정상 운영의 해 없음으로 숨기지 않는다.

## 지시와 실행 관측

Instruction 필드: `instruction_id`, `plan_id`, `accepted_state_version`, `accepted_plan_version`, `job_id|null`, `robot_id`, `action: MOVE/SETUP/WELD/WAIT`, `not_before_tick`, `expected_end_tick|null`, `precondition_refs[]`, `status: PENDING/RUNNING/COMPLETED/CANCELLED`, `superseded_by|null`, `reason_codes[]`, `recheck_triggers[]`, `execution_id|null`, `attempt_id|null`, `from_zone_id|null`, `to_zone_id|null`.

`accepted_state_version`은 해당 지시가 저장된 상태 버전이고 `accepted_plan_version`은 적용된 계획 순번이다. repository가 성공한 적용 시 둘을 부여한다. 작업 지시의 `attempt_id`는 이미 수행한 phase의 연속성을, from/to는 실제 이동·위치 검사를 연결한다. WAIT의 작업·attempt·위치·종료 시각은 null일 수 있으나 대상 robot ID는 필수다. 초기 진행 W18의 지시는 버전 0으로 등록하고 기존 실행 ID를 보존한다.

WAIT는 이유와 재검토 trigger가 필수이며 물리 실행 구간을 만들지 않는다. trigger는 새 유효 근거·자원/phase 완료·외부 사건 도착이지 비공개 복구시각이 아니다. WELD 지시가 끝났다는 사실은 실제 WELD_END 관측으로 저장한다. 로그상 승인만으로 실행했다고 채점하지 않는다.

실행 관측 DTO `Observation`의 필수 필드: `observation_id`, `execution_id`, `instruction_id`, `job_id`, `robot_id`, `phase`, `kind: PHASE_STARTED/PHASE_ENDED`, `tick`, `source_id`, `from_zone_id`, `to_zone_id`, `expected_state_version|null`.

controller는 현재 활성 지시·대상·예정 phase·실제 자원과 대조한 후 저장한다. `PHASE_STARTED`는 시작 허용 검사와 예약을 같은 업무 트랜잭션으로 수행한 결과만 수용한다. 동일 observation ID의 재전송은 no-op, 같은 ID 내용 충돌은 오류다. 지시 취소 후라도 이미 시작한 execution의 종료는 받아 위치·점유를 갱신한다.

## 실패와 후속 행동

| 결과 | 신규 지시와 후속 행동 | 잘못된 해석 |
| --- | --- | --- |
| `OPTIMAL` | 독립 검사와 정책 통과 후 자동 적용 | 솔버 최적=근거와 실행도 자동 정답 |
| `FEASIBLE_NOT_PROVEN` | 개발 정책상 검사 통과 시 적용. 증명 여부·bound 보존 | OPTIMAL로 기록하거나 미실행 목적의 최적값 주장 |
| `INFEASIBLE` | 현재 후보·제약·horizon에서 모순 진단. 진행 구간 유지, 새 계획 적용 없음. 원인과 재검토 조건 기록 | 물리 제약 완화 또는 영구 작업 불가능 단정 |
| `NO_SOLUTION_WITHIN_LIMIT` | 모든 목적 단계를 통틀어 유효 incumbent가 없는 UNKNOWN/예산 소진. 자동 예산 확대 없이 진단·일시 대기 | 해 없음이나 ‘가능한 대체가 없음’으로 채점 |
| `MODEL_INVALID` / `INTERNAL_ERROR` | 신규 계획 적용 중지, 구현 오류 기록, 기존 유효 진행만 유지 | 정상적인 운영 대기/자동화 성공으로 집계 |
| `NO_ELIGIBLE_WORK` | 모든 제외 이유와 wake trigger를 기록한 정상 WAIT. 진행 작업은 계속 | 솔버 INFEASIBLE 또는 전체 완료 |
| `INSUFFICIENT_EVIDENCE` / `EVIDENCE_CONFLICT` | 공개 근거 1회 자동 조회, 미해결 대상만 차단, 필요한 새 관측 요청. 무관 작업 재계획 가능 | UNKNOWN을 PASS로 바꾸거나 확인 요청을 무개입 완결로 채점 |
| `STALE_PLAN` | candidate/PASS 폐기, 최신 상태 재계획 최대2회. 소진 시 `RETRY_EXHAUSTED` | old plan 일부만 적용 |
| `ID_CONFLICT` / `INVALID_INPUT` | 사건 격리, 이유 기록. 기존 제한 유지. 확인 가능한 대상 영향만 다룸 | 동일 ID라는 이유로 새 payload를 조용히 수용 |
| `POLICY_REJECTED` / `CHECK_REJECTED` | 신규 적용 없음, 위반별 기록과 원인 진단 | 검사 거부를 성공한 실제 실행으로 계산 |

실패 시 이전 pending 지시 전체를 그대로 신뢰하지 않는다. 최신 snapshot에서 아직 유효한 지시는 phase 시작 gate를 거쳐 실행할 수 있고, 근거가 사라진 지시는 취소한다. 진행 WELD는 유지한다. 실패 재시도로 원인 확인을 숨기지 않는다. 조회로도 없는 새로운 검사 근거는 만들어내지 않는다.

## 저장·로그·독립 평가 경계

실제 SQL 테이블은 `runs`, `events`, `event_receipts`, `evidence`, `plans`, `checks`, `instructions`, `observations`, `audit_log`다. `runs.snapshot_json`의 공개 snapshot을 기준 상태로 저장하고 상태 버전·tick·활성 계획 버전을 별도 열에 둔다. 사건·근거·계획·검사·지시·관측은 run과 객체 ID를 키로 하는 JSON payload 행에도 저장하여 중복 검사와 추적에 사용한다. SQL PK/UNIQUE·run FK·check→plan FK와 업무 객체의 도메인 검사를 함께 적용한다.

초기 설계의 엔터티별 다수 테이블을 snapshot JSON과 조회용 키 테이블로 줄였다. 작은 사례에서 같은 값을 여러 표에 나누어 동기화하는 코드를 줄이되, 기준 상태·새 근거·사건 결과·지시·감사 기록은 여전히 한 트랜잭션으로 갱신한다. 해제한 제한은 RESTRICTION 로그에 남긴다. 동적 테이블 생성·임의 SQL 도구는 없다. 실제 DDL은 `src/shipyard/schema.sql`, `PRAGMA user_version=1`이며 버전이 다른 실행 DB는 거부한다. 자동 migration·프로세스 장애 후 재개는 이번 범위 밖이다.

공개 감사 레코드는 `{seq, run_id, tick, received_at, stage, actor, outcome, state_version_before, state_version_after, data}`다. DB의 자동 증가 `log_seq`를 export에서 `seq`로 표시한다. 단계마다 필요 없는 최상위 필드를 null로 반복하지 않고 관련 원문과 참조를 `data`에 둔다.

| stage | `data`의 핵심 내용 |
| --- | --- |
| `EVENT` | `event` 원문, `published_evidence[]` 수신 근거, `result` 처리 결과. result에 근거/제한/해제 ID·reason_codes·intervention_kind 포함 |
| `QUERY` | `event_id`, `query_kind=PUBLIC_SNAPSHOT`, `lookup_passes`, 반환한 `evidence_ids` |
| `RESTRICTION` | 생성 또는 해제된 `restriction` 상세 객체와 원인·해제 근거 |
| `PLAN`, `CHECK` | 각각 `plan`, `check` 전체 객체 |
| `APPLY` | `plan_id`, 적용한 `instructions[]`; 오래된 후보 거부는 같은 stage의 STALE_PLAN outcome |
| `WAIT`, `HUMAN_REQUEST` | 대기 지시의 robot·reason_codes·recheck_triggers, 또는 `event_id`와 확인 사유 |
| `PHASE_START`, `PHASE_END` | 실제 시작/종료 `observation`과 instruction/execution ID |
| `CLOCK`, `CANCEL`, `FAILURE` 등 | 시간 진행·준비 취소·오류의 사유와 관련 ID |

EVENT의 `published_evidence`는 수신 원문이며 공개 근거로 승인됐다는 뜻은 아니다. `result`의 결과와 연결해 판단한다. QUERY는 현재 공개 snapshot에 이미 로드된 근거를 한 번 조회한 기록이다. 별도 외부 검색이나 추가 DB 왕복을 수행했다고 표시하지 않는다. 동일 사건 재전달에는 QUERY/RESTRICTION/HUMAN_REQUEST를 재발생시키지 않는다. `intervention_kind`는 사건 result의 PREDEFINED_EXTERNAL/ADDITIONAL_HUMAN_REQUEST로 외부 신호와 추가 확인 요청을 구별한다.

로그는 append-only API로 작성하고 업데이트/삭제하지 않는다. 암호학적 변조 방지 저장소를 구현한 것은 아니다. `manifest.json`에는 코드·빌드 설정·lockfile의 합성 source hash, Python/OS/CPU/SQLite/패키지 버전, 정책·입력 hash, seed, 실행 명령, 시작/종료 시각을 기록한다.

실행 출력은 `run.sqlite3`가 기준, `trace.jsonl`, `execution.csv`, `summary.json`이 재생성 가능한 export다. 독립 채점 출력은 `evaluation.json`/`evaluation.csv`로 구분하고 operational summary와 섞지 않는다. 출력 디렉터리를 덮어쓰지 않는다.

- **실행 전 checker**: 현재 공개 근거로 지시가 허용되는지 판단한다. 미래 사실을 볼 수 없다.
- **독립 evaluator**: 실행 후 oracle의 허용/금지 행동과 평가자 truth, 실제 phase 로그로 사건을 채점한다. checker가 놓친 오류도 찾아야 한다. oracle은 시스템 출력에서 역생성하지 않는다.
- 원문·정답표·미래 외부 큐를 controller 생성자나 solver DTO에 넘기지 않는다. root case 로더는 공개 입력만 읽고 simulator 전용 로더와 평가 로더를 구분한다. 후속 에이전트는 공개 조회 도구만 가진다.
- `truth.jsonl`은 평가자만 아는 환경 사실이며 future signal 파일의 허용 범위/시점과 별도로 검토한다. simulator가 만든 actual execution을 truth와 혼동하지 않는다.
- 실제 검증에는 금지 import/경로 접근 검사, DTO의 extra-field 거부, 미래/정답에 넣은 sentinel이 solver input·조회 응답·프롬프트에 나오지 않는 검사를 포함한다. 폴더 분리나 스키마 선언만으로 정보 차단을 완료했다고 쓰지 않는다. 단일 프로세스 설계는 악성 코드에 대한 OS 보안 경계가 아니다.

## 계약 예시와 첫 검증

다음은 사건 payload의 **설명 예시**다. 참조하는 근거와 ID가 저장소에 이미 등록돼야 하며 이 블록 자체가 실행 데이터가 아니다.

```json
{
  "schema_version": "1.0",
  "run_id": "demo-run",
  "event_id": "E-fitup-W17-1",
  "incident_id": "INC-W17-1",
  "source_id": "sim-inspection",
  "kind": "FITUP_FAILED",
  "target": {"scope": "JOB", "job_id": "W17", "robot_id": null, "zone_id": null},
  "observed_tick": 0,
  "received_tick": 0,
  "observed_state_version": 0,
  "evidence_ids": ["EV-fitup-W17-1"],
  "restriction_ids": [],
  "unknown_fields": [],
  "raw_report": null,
  "supersedes_event_id": null
}
```

`run_id`는 실데이터에서 실행 UUID로 치환한다. 동일 사건 재전달은 기존 결과, `kind`만 바꾼 동일 ID는 충돌, `received_tick` 변경만 있는 재전달은 중복이다. tick 문자열·미정의 `future_recovery_tick` 필드·등록되지 않은 source는 거부한다. W17 수리 근거만 추가하면 제한 유지, 연결된 재확인 PASS까지 도착하면 해당 제한만 해제, W17 실제 WELD_END 전에는 W20 착수 거부가 첫 계약 테스트의 핵심이다.
