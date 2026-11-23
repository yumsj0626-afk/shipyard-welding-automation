# 구현 코드 위치와 경계

`src/shipyard/`는 D01–D04 정형·fake 시뮬레이션, Responses 연동, 선급 조항 기반 리포트 분석을 구현한다. CP-SAT와 Pydantic을 사용하며 OpenAI SDK 3.46.0은 선택적 `llm` extra로 고정한다. 모의 D01 통합 검증 결과는 [검증 기록](../docs/next-steps.md)에 있다.

실제 모듈 API·입출력·권한과 파일 배치는 [아키텍처](../docs/architecture.md)에, 필드·상태 전이·실패 처리는 [데이터 계약](../docs/data-contracts.md)에 모았다. 이 파일에서 같은 명세를 중복 정의하지 않는다.

- `contracts`, `events.apply_deliveries`, `constraints.build_constraints`: 엄격한 공개 DTO, 근거 검사와 제한 변경 제안, 필수/보류 작업의 고정 제약 변환.
- `reports.interpret_report`, `ReportModel`, `FakeReportModel`: 도착한 보고를 읽기 전용 근거 조회와 사건 제안으로 변환한다. 모델은 호스트 메타데이터를 바꾸거나 기준 상태를 쓰지 못하며 반환된 제안도 기존 `events` 검사를 거친다. 현재 fake 응답은 고정 대본이므로 자연어 이해 성능을 측정하지 않는다.
- `responses.LLMPolicy`, `ResponsesReportModel`, `preflight`: SDK를 필요할 때 import하여 입력 토큰 count와 strict function calling을 연결한다. 조회 결과는 native `function_call_output`과 같은 `call_id`로 전달하며, 공급자 응답·거절·오류를 공통 보고 결과/`ModelCallFailure`로 바꾼다. 기준 상태를 직접 수정하지 않는다.
- `openai_settings.read_openai_settings`는 루트 `.env`에서 프로젝트 ID와 API 키를 읽는다. 환경 변수에 설정된 값이 우선한다.
- `history`는 실행이 끝난 뒤 리포트별 결과를 공용 결과 DB에 쌓고, 이후 실행에 앞선 결과만 임베딩으로 검색해 준다.
- `report_retrieval`은 OpenAI 임베딩 클라이언트와 GPT 단독 분석을 담당한다.
- `rule_knowledge`는 선급 추출본을 검색하며 보고 분류별 검색어는 `config/rule-topics.json`에서 읽는다.
- `rule_knowledge.RuleKnowledge`는 한국선급 발췌에서 적용 후보 조항을 조회하고 원본 페이지와 함께 반환한다.

- `llm_budget.BudgetLedger`: 운영 DB와 별도인 `artifacts/llm-budget.sqlite3`에 생성 비용을 사전 예약하고 SDK usage 기반 추정액으로 정산한다. 보고·실행·프로젝트 누적 상한을 확인하며 결과 미확정 예약은 남긴다. 실제 청구서나 계정 전체 비용 한도를 대체하지 않는다.
- `scheduler`, `checker`: CP-SAT 후보 계산과 별도 실행 검사. 검사기는 솔버 모델 생성 로직을 재사용하지 않는다.
- `Repository`, `Controller`: repository가 SQLite 쓰기·최신 버전 확인·지시 교체·로그의 원자적 저장을 구현하고 controller가 업무 호출 순서를 조율한다. `handle_report_batch`는 보고 해석 후 공통 처리 경로에 합류하며 보고 결과·사건·상태를 같은 트랜잭션으로 저장한다. 현재 상태는 `runs.snapshot_json`, 사건·근거·계획·검사·지시·관측·감사는 ID와 JSON payload를 가진 관계형 이력 테이블에 저장한다. 보고 수신 결과는 기존 감사 기록의 `REPORT_RESULT`에서 조회한다.
- `Simulator`, `reporting`, `cli`: simulator는 미래 외부 사건과 승인된 phase의 완료 큐를 소유한다. **시간 진행과 지시 시작 요청을 조율하는 드라이버는 `cli.run_case`**다. reporting은 DB에서 추적·실행 CSV·요약을 내보내며 `inspect`는 저장된 요약을 표시한다.

저장소 루트에서 `uv sync --locked` 후 `uv run --locked python -m shipyard run --case D01 --output outputs/D01`로 실행하고, `uv run --locked python -m shipyard inspect --run-dir outputs/D01`로 요약을 확인한다. D02–D04도 같은 CLI를 사용한다. 기존 출력 디렉터리는 덮어쓰지 않는다. 독립 개발 채점은 `evaluation/score.py`의 별도 명령이다.

문장형 경로는 `uv run --locked python -m shipyard run --case D01 --output outputs/D01-fake --report-mode fake`로 실행한다. 기본 모드는 `structured`다. `simulation/reports/D01.jsonl`부터 D04까지의 보고·응답 대본은 CLI 드라이버만 읽으며 도착한 보고의 응답만 fake 모델에 전달한다. 보고당 최대 모델 호출 2회·근거 조회 1회이며 중복 수신, 오래된 해석 문맥, 거절·timeout·형식 오류는 별도로 기록한다. 세부 정책은 [DEC-07](../docs/decisions.md#dec-07-문장형-보고-어댑터와-이후-llm-연결)을 따른다.

API 연결은 `.env`에 `OPENAI_PROJECT_ID`와 `OPENAI_API_KEY`를 설정하고 `llm` extra를 설치한 뒤 준비한다. 기본 `llm-preflight`는 로컬 설정만 확인한다. `--check-access`를 지정하면 모델 메타데이터를 조회한다.

`--report-mode responses`에는 사례별 승인 정책과 `--allow-paid`가 필요하다. 선택한 합성 사례의 고정 입력과 선급 규칙 발췌·주제 설정의 해시, 과거 결과 전송 승인을 검증한다. 첫 모델 요청에 공용 결과 DB의 앞선 실행 결과를, 조회 응답에 현재 관측과 선급 규칙을 담을 수 있다. `--history`를 주면 실행이 끝난 뒤 리포트별 결과를 그 DB에 쌓는다(fake 모드는 기록만 한다). [공통 실행 명령](../docs/report-rag.md#실행)

정형 사건은 LLM 없이 처리한다. 미래 외부 큐는 드라이버/시뮬레이터에만 있고 controller에는 현재 도착 사건·보고만 전달한다. 어댑터는 공개 snapshot과 현재 도착 근거에서 필요한 값만 모델 요청으로 구성한다. 공개 로더·금지 의존관계·누출과 모델 출력 경계를 테스트하며 단일 프로세스가 OS 보안 격리를 제공한다고 주장하지 않는다. 진행 단계·실제 증거는 [다음 작업과 검증 기록](../docs/next-steps.md)에서 확인한다.

D01의 과거 사례·선급 규칙 연동은 모의 검증을 수행했다. 두 에이전트 구성 비교와 본 평가는 후속 범위다. [리포트 RAG 사용법](../docs/report-rag.md)
