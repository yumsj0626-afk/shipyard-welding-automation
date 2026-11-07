# 구현 코드 위치와 경계

**정형 사건 기반 최소 애플리케이션, 가짜 모델 보고 어댑터와 OpenAI Responses 공급자 어댑터가 `src/shipyard/`에 구현되어 있다.** Python 3.13.18에서 CP-SAT·Pydantic을 런타임 의존성으로, pytest를 개발 의존성으로 사용한다. SQLite·heapq·argparse는 표준 라이브러리다. `pyproject.toml`, `uv.lock`, `.python-version`으로 실행 환경을 고정한다. OpenAI SDK 3.46.0은 선택적 `llm` extra로 설치·고정했으며 정형/fake 실행에서는 필요하지 않다. **실제 계정 API·토큰 count·모델 생성은 호출하지 않았고 유료 파일럿은 미승인이다.**

실제 모듈 API·입출력·권한과 파일 배치는 [아키텍처](../docs/architecture.md)에, 필드·상태 전이·실패 처리는 [데이터 계약](../docs/data-contracts.md)에 모았다. 이 파일에서 같은 명세를 중복 정의하지 않는다.

- `contracts`, `events.apply_deliveries`, `constraints.build_constraints`: 엄격한 공개 DTO, 근거 검사와 제한 변경 제안, 필수/보류 작업의 고정 제약 변환.
- `reports.interpret_report`, `ReportModel`, `FakeReportModel`: 도착한 보고를 읽기 전용 근거 조회와 사건 제안으로 변환한다. 모델은 호스트 메타데이터를 바꾸거나 기준 상태를 쓰지 못하며 반환된 제안도 기존 `events` 검사를 거친다. 현재 fake 응답은 고정 대본이므로 자연어 이해 성능을 측정하지 않는다.
- `responses.LLMPolicy`, `ResponsesReportModel`, `preflight`: SDK를 필요할 때 import하여 입력 토큰 count와 strict function calling을 연결한다. 조회 결과는 native `function_call_output`과 같은 `call_id`로 전달하며, 공급자 응답·거절·오류를 공통 보고 결과/`ModelCallFailure`로 바꾼다. 기준 상태를 직접 수정하지 않는다.
- `llm_budget.BudgetLedger`: 운영 DB와 별도인 `artifacts/llm-budget.sqlite3`에 생성 비용을 사전 예약하고 SDK usage 기반 추정액으로 정산한다. 보고·실행·프로젝트 누적 상한을 확인하며 결과 미확정 예약은 남긴다. 실제 청구서나 계정 전체 비용 한도를 대체하지 않는다.
- `scheduler`, `checker`: CP-SAT 후보 계산과 별도 실행 검사. 검사기는 솔버 모델 생성 로직을 재사용하지 않는다.
- `Repository`, `Controller`: repository가 SQLite 쓰기·최신 버전 확인·지시 교체·로그의 원자적 저장을 구현하고 controller가 업무 호출 순서를 조율한다. `handle_report_batch`는 보고 해석 후 공통 처리 경로에 합류하며 보고 결과·사건·상태를 같은 트랜잭션으로 저장한다. 현재 상태는 `runs.snapshot_json`, 사건·근거·계획·검사·지시·관측·감사는 ID와 JSON payload를 가진 관계형 이력 테이블에 저장한다. 보고 수신 결과는 기존 감사 기록의 `REPORT_RESULT`에서 조회한다.
- `Simulator`, `reporting`, `cli`: simulator는 미래 외부 사건과 승인된 phase의 완료 큐를 소유한다. **시간 진행과 지시 시작 요청을 조율하는 드라이버는 `cli.run_case`**다. reporting은 DB에서 추적·실행 CSV·요약을 내보내며 `inspect`는 저장된 요약을 표시한다.

저장소 루트에서 `uv sync --locked` 후 `uv run --locked python -m shipyard run --case D01 --output outputs/D01`로 실행하고, `uv run --locked python -m shipyard inspect --run-dir outputs/D01`로 요약을 확인한다. D02–D04도 같은 CLI를 사용한다. 기존 출력 디렉터리는 덮어쓰지 않는다. 독립 개발 채점은 `evaluation/score.py`의 별도 명령이다.

문장형 경로는 `uv run --locked python -m shipyard run --case D01 --output outputs/D01-fake --report-mode fake`로 실행한다. 기본 모드는 `structured`다. `simulation/reports/D01.jsonl`부터 D04까지의 보고·응답 대본은 CLI 드라이버만 읽으며 도착한 보고의 응답만 fake 모델에 전달한다. 보고당 최대 모델 호출 2회·근거 조회 1회이며 중복 수신, 오래된 해석 문맥, 거절·timeout·형식 오류는 별도로 기록한다. 세부 정책은 [DEC-07](../docs/decisions.md#dec-07-문장형-보고-어댑터와-이후-llm-연결)을 따른다.

선택적 연결 준비는 `uv sync --locked --extra llm` 후 `uv run --locked --extra llm python -m shipyard llm-preflight --llm-policy config/llm-policy.json`로 확인한다. 기본 preflight는 네트워크 없이 설정·SDK·키 존재 여부만 표시한다. `--check-access`를 명시하면 모델 metadata GET을 시도하지만 생성 권한·잔액·쿼터는 입증하지 않는다. 현재 정책은 `approved_for_paid_calls=false`, `project_id=null`이며 실제 계정 접근을 확인하지 않았다.

승인 후의 `--report-mode responses` 실행에는 `--llm-policy <승인된 정책 파일>`과 `--allow-paid`가 모두 필요하다. 합성 D01의 다섯 입력 파일 해시를 동결하고 최대 3개 보고로 제한한다. 실제 공급자는 가짜 응답 대본을 받지 않으며 보고 본문·대상 힌트·현재 ID/tick·허용 사건과 실제 조회 결과만 전송한다. 생성 전 count도 외부 전송이며, 생성의 `store=false`·`background=false`·`stream=false` 설정이 count에도 적용되거나 count가 무료라고 가정하지 않는다. 구체 금액·시간·보관 조건과 남은 결정은 DEC-07을 따른다.

정형 사건은 LLM 없이 처리한다. 미래 외부 큐는 드라이버/시뮬레이터에만 있고 controller에는 현재 도착 사건·보고만 전달한다. 어댑터는 공개 snapshot과 현재 도착 근거에서 필요한 값만 모델 요청으로 구성한다. 공개 로더·금지 의존관계·누출과 모델 출력 경계를 테스트하며 단일 프로세스가 OS 보안 격리를 제공한다고 주장하지 않는다. 진행 단계·실제 증거는 [다음 작업과 검증 기록](../docs/next-steps.md)에서 확인한다.

실제 LLM 호출·한국어 품질 검증, 단일/역할 분리 에이전트 비교, API 서버·웹 UI·OCR·3D·클라우드·실제 로봇 제어와 본 비교실험은 수행하지 않았다. 공급자 모의 검사는 실제 서비스 동작이나 과금/요청 취소의 증거가 아니다. 후속 모델·에이전트도 `ReportModel.respond`와 현재의 공통 검증·처리 경로를 사용해야 한다.
