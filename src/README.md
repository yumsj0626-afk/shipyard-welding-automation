# 구현 코드 위치와 경계

**정형 사건 기반 최소 애플리케이션과 가짜 모델을 사용하는 문장형 보고 어댑터가 `src/shipyard/`에 구현되어 있다.** Python 3.13.18에서 CP-SAT·Pydantic을 런타임 의존성으로, pytest를 개발 의존성으로 사용한다. SQLite·heapq·argparse는 표준 라이브러리다. `pyproject.toml`, `uv.lock`, `.python-version`으로 실행 환경을 고정한다. 유료 LLM SDK는 설치하지 않았다.

실제 모듈 API·입출력·권한과 파일 배치는 [아키텍처](../docs/architecture.md)에, 필드·상태 전이·실패 처리는 [데이터 계약](../docs/data-contracts.md)에 모았다. 이 파일에서 같은 명세를 중복 정의하지 않는다.

- `contracts`, `events.apply_deliveries`, `constraints.build_constraints`: 엄격한 공개 DTO, 근거 검사와 제한 변경 제안, 필수/보류 작업의 고정 제약 변환.
- `reports.interpret_report`, `ReportModel`, `FakeReportModel`: 도착한 보고를 읽기 전용 근거 조회와 사건 제안으로 변환한다. 모델은 호스트 메타데이터를 바꾸거나 기준 상태를 쓰지 못하며 반환된 제안도 기존 `events` 검사를 거친다. 현재 fake 응답은 고정 대본이므로 자연어 이해 성능을 측정하지 않는다.
- `scheduler`, `checker`: CP-SAT 후보 계산과 별도 실행 검사. 검사기는 솔버 모델 생성 로직을 재사용하지 않는다.
- `Repository`, `Controller`: repository가 SQLite 쓰기·최신 버전 확인·지시 교체·로그의 원자적 저장을 구현하고 controller가 업무 호출 순서를 조율한다. `handle_report_batch`는 보고 해석 후 공통 처리 경로에 합류하며 보고 결과·사건·상태를 같은 트랜잭션으로 저장한다. 현재 상태는 `runs.snapshot_json`, 사건·근거·계획·검사·지시·관측·감사는 ID와 JSON payload를 가진 관계형 이력 테이블에 저장한다. 보고 수신 결과는 기존 감사 기록의 `REPORT_RESULT`에서 조회한다.
- `Simulator`, `reporting`, `cli`: simulator는 미래 외부 사건과 승인된 phase의 완료 큐를 소유한다. **시간 진행과 지시 시작 요청을 조율하는 드라이버는 `cli.run_case`**다. reporting은 DB에서 추적·실행 CSV·요약을 내보내며 `inspect`는 저장된 요약을 표시한다.

저장소 루트에서 `uv sync --locked` 후 `uv run --locked python -m shipyard run --case D01 --output outputs/D01`로 실행하고, `uv run --locked python -m shipyard inspect --run-dir outputs/D01`로 요약을 확인한다. D02–D04도 같은 CLI를 사용한다. 기존 출력 디렉터리는 덮어쓰지 않는다. 독립 개발 채점은 `evaluation/score.py`의 별도 명령이다.

문장형 경로는 `uv run --locked python -m shipyard run --case D01 --output outputs/D01-fake --report-mode fake`로 실행한다. 기본 모드는 `structured`다. `simulation/reports/D01.jsonl`부터 D04까지의 보고·응답 대본은 CLI 드라이버만 읽으며 도착한 보고의 응답만 fake 모델에 전달한다. 보고당 최대 모델 호출 2회·근거 조회 1회이며 중복 수신, 오래된 해석 문맥, 거절·timeout·형식 오류는 별도로 기록한다. 세부 정책은 [DEC-07](../docs/decisions.md#dec-07-문장형-보고-어댑터와-이후-llm-연결)을 따른다.

정형 사건은 LLM 없이 처리한다. 미래 외부 큐는 드라이버/시뮬레이터에만 있고 controller에는 현재 도착 사건·보고만 전달한다. 어댑터는 공개 snapshot과 현재 도착 근거에서 필요한 값만 모델 요청으로 구성한다. 공개 로더·금지 의존관계·누출과 모델 출력 경계를 테스트하며 단일 프로세스가 OS 보안 격리를 제공한다고 주장하지 않는다. 진행 단계·실제 증거는 [다음 작업과 검증 기록](../docs/next-steps.md)에서 확인한다.

실제 LLM 연결, 단일/역할 분리 에이전트 비교, API 서버·웹 UI·OCR·3D·클라우드·실제 로봇 제어와 본 비교실험은 구현하지 않았다. 후속 공급자 어댑터도 `ReportModel.respond`와 현재의 공통 검증·처리 경로를 사용해야 한다.
