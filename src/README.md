# 구현 코드 위치와 경계

**실행 가능한 앱 코드는 아직 없다.** 다음 구현은 Python 패키지 `src/shipyard/`를 사용한다. CP-SAT·Pydantic을 런타임 의존성으로, pytest를 개발 의존성으로 관리하고 SQLite·heapq·argparse는 표준 라이브러리를 쓴다. uv의 실제 의존성 파일과 lockfile은 I01에서 만든다.

모듈별 입출력·권한과 전체 예정 트리는 [아키텍처](../docs/architecture.md)에, 필드·상태 전이·실패 처리는 [데이터 계약](../docs/data-contracts.md)에 모았다. 이 파일에서 같은 명세를 중복 정의하지 않는다.

- `contracts`, `events`, `constraints`: 입력 계약, 근거 검사, 고정 제약 변환.
- `scheduler`, `checker`: CP-SAT 후보 계산과 별도 실행 검사. 검사기는 솔버 모델 생성 로직을 재사용하지 않는다.
- `repository`, `controller`: 유일한 기준 상태 쓰기 구현과 업무 흐름 조율. 최신 버전 확인·지시 교체·로그를 원자적으로 저장한다.
- `simulator`, `reporting`, `cli`: 가상 시간·실제 phase 관측·추적 내보내기·명령줄 실행.

정형 사건은 LLM 없이 처리한다. 미래 외부 큐는 드라이버/시뮬레이터에만 있고 controller에는 현재 도착 사건만 전달한다. 후속 에이전트는 읽기 전용 조회와 사건 제안만 수행한다. 구현 순서는 [I01~I09](../docs/next-steps.md)를 따른다. 이번에는 빈 모듈을 생성하지 않았다.
