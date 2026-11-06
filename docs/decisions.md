# 최소 구현을 위한 기술 결정

2026-11-06 갱신. 최신 사용자 요청 → 프로젝트 기준 문서 B v1.0 → 원본 보고서 R 순으로 적용한다. **DEC-01–06의 개발용 기본안을 정형 사건 최소 구현에 적용했다. 본 평가 조건의 확정을 뜻하지 않는다.** 실제 구현 범위·실행 결과는 [다음 단계와 검증 기록](next-steps.md)에 연결한다. 사용자가 결정을 위임한 범위의 선택이며 팀의 별도 승인·실행 검토를 받은 것으로 기록하지 않는다. 기존 DEC ID를 유지한다.

원문 식별: R=`references-local/midterm-report.docx`, B=`references-local/project-baseline-v1.txt`. 두 파일에 실제 접근해 다시 읽었고 SHA-256은 [출처 기록](sources.md)과 같다. 원본의 기술명을 무조건 유지하지 않고 아래 요구사항과 연결해 판단했다.

| ID | 상태 | 결정 |
| --- | --- | --- |
| DEC-01 | 채택 | 공통 솔버 하나: OR-Tools CP-SAT 9.15.6755 |
| DEC-02 | 채택 | CPython 3.13.18, uv 0.13.7, SQLite, argparse, heapq, pytest |
| DEC-03 | 채택 | JSON/JSONL + Pydantic 2.14.3, 명시적 사건·근거·제한·버전 계약 |
| DEC-04 | 개발 기본안 채택 | 필요한 후보 전부 배치, 무근거 유휴 방지, 가중 지연 → 변경량 → 완료시각 순. 검사 통과한 실행 가능 해 자동 적용 |
| DEC-05 | 개발 기본안 채택 | 해 없음·미발견·미증명·근거 부족·오래된 계획을 분리 |
| DEC-06 | 개발 가정 채택 | 기존 2대·5개 수치, 정수 tick, 준비와 용접의 구역 점유, 명시적 사건 순서 |
| DEC-07 | 후속 구성 추천 | 공식 SDK 직접 호출 + 구조화 출력. 단일/역할 분리 모두 같은 코드 경로. 모델·예산은 연결 단계에서 결정 |
| DEC-08 | 보류 | 본 평가 규모·반복·수치 통과 기준은 예비실험 후 동결 |
| DEC-09 | 보류 | OCR·웹 UI·3D·클라우드는 필요가 확인될 때만 추가 |

## DEC-01 솔버

**R §4.4 → 배정·선행·로봇/구역 배타·이동/준비·진행 작업 고정 → CP-SAT 유지.** B §6과 최신 사용자 지시에 따라 Gurobi 경험 및 학생 라이선스를 함께 검토했다. CP-SAT를 채택한 이유는 구간 일정 제약에 잘 맞고 팀마다 라이선스 자격·키·갱신을 준비하지 않아도 공통 실행 경로를 재현할 수 있기 때문이다. Gurobi가 느리거나 부적합하다는 결론은 아니다.

| 기준 | OR-Tools CP-SAT | Gurobi |
| --- | --- | --- |
| 실제 제약 표현 | Boolean 배정, optional interval, `NoOverlap`, 조건부 선행·전환 제약 | 이진 배정·순서, 선형/indicator 시간 제약. 모두 표현 가능 |
| 이동·준비 | 인접 작업 arc와 조건부 시간차를 별도 작성. `NoOverlap`만으로 이동이 생기지 않음 | 인접 순서와 indicator 또는 검증한 유한 big-M을 직접 구성 |
| 진행 중 고정 | 배정·시작·종료가 고정된 interval | 변수 bounds/equality 고정 |
| 다목적 | 상위 단계가 OPTIMAL인 경우 값을 고정하고 다음 solve | 우선순위를 가진 다목적 API 제공 |
| 구현·학습 | interval/presence/reification 학습 필요. 원본의 CP-SAT 구조 재사용 | 사용자의 경험은 장점. 구간 배타 및 전환 모델을 직접 정식화 |
| 팀 실행·비용 | Apache-2.0, 라이선스 키 불필요. 설치 뒤 로컬 실행 | 적격 학술 용도는 무료일 수 있으나 라이선스 형태와 사용자·환경 조건 확인 필요 |
| 재현 | 버전·정렬 입력·seed·worker·파라미터 기록 | 같은 정보와 라이선스/지원 환경 확보 필요 |
| 이번 결론 | **단일 기본 솔버** | 추가 솔버 구현은 보류. CP-SAT의 표현/성능 문제가 예비실험에서 확인되면 재검토 |

공식 근거: [CP-SAT와 해 상태](https://developers.google.com/optimization/cp/cp_solver), [job shop 예제](https://developers.google.com/optimization/scheduling/job_shop), [Python API](https://or-tools.github.io/docs/pdoc/ortools/sat/python/cp_model.html), [전환시간 예제](https://github.com/google/or-tools/blob/stable/examples/contrib/scheduling_with_transitions_sat.py), [Gurobi indicator](https://docs.gurobi.com/projects/optimizer/en/current/reference/python/model.html#Model.addGenConstrIndicator), [Gurobi 다목적](https://docs.gurobi.com/projects/optimizer/en/current/features/multiobjective.html).

Gurobi 13.0.3 공식 지원 표는 Python 3.10–3.14 및 운영체제별 조건을 명시한다. 사용자의 **실제 라이선스 종류는 확인하지 않았다.** Named-User와 WLS의 사용 조건은 다르다. WLS는 인터넷·갱신·동시 세션 조건이 있고, 구체 제한 문서는 등록자 사용과 키 공유 제한을 명시한다. 일반 안내표의 여러 사용자 표시만으로 한 학생의 키를 팀/CI/클라우드에서 공유할 수 있다고 해석하지 않는다. 팀원 각자의 적격 라이선스 또는 기관/명시 허용 조건을 확인해야 한다. [지원 플랫폼](https://docs.gurobi.com/projects/optimizer/en/current/reference/releasenotes/platforms.html), [학술 라이선스 종류](https://support.gurobi.com/hc/en-us/articles/360040541251-How-do-I-obtain-a-free-academic-license), [WLS 조건](https://support.gurobi.com/hc/en-us/articles/13210193318033-What-is-an-Academic-WLS-license), [WLS 공유 제한](https://support.gurobi.com/hc/en-us/articles/34672988479633-What-are-the-restrictions-on-using-an-academic-WLS-license), [클라우드 조건](https://support.gurobi.com/hc/en-us/articles/360037221732-What-are-the-options-for-using-an-academic-license-on-the-cloud).

## DEC-02 언어와 실행 기반

**R §4.1·4.2·4.6 → 하나의 관계형 기준 상태와 결정적 처리·경량 시뮬레이션 → 단일 Python 프로그램과 SQLite.** 원본의 논리 구성을 유지하면서 아직 없던 제품·패키지·실행 방식을 구체화한다. 별도 API 서버·서비스별 DB·메시지 브로커는 두 로봇 사례에 필요한 책임이 없다.

| 항목 | 현실적 후보와 부담 | 채택 이유와 역할 |
| --- | --- | --- |
| 언어/런타임 | Python: 솔버·검증 연결이 직접적, 타입/예외 학습 필요. TypeScript: 웹에는 편리하지만 Python 솔버와 경계 추가. Julia: 최적화에 적합하지만 별도 생태계 학습 | **CPython 3.13.18**. 한 언어로 상태·제약·시뮬레이션·평가를 구현. GPU 불필요 |
| 패키지 | pip+venv: 친숙하지만 Python/전이 의존성 고정 절차가 나뉨. Poetry: 프로젝트 관리 가능. uv: 새 명령을 배우지만 Python·lock·실행 관리 통합 | **uv 0.13.7**. `.python-version`, `pyproject.toml`, `uv.lock`으로 Python·의존성을 고정하고 `uv sync --locked` 사용 |
| 저장 | JSON만: 읽기 쉽지만 여러 상태/로그의 원자적 갱신을 직접 구현. PostgreSQL: 동시 접속에 유리하나 서버 운영 추가 | **SQLite + 표준 `sqlite3`**. 실행별 DB 하나, 한 writer, 트랜잭션/PK/FK/UNIQUE. ORM·WAL은 처음에는 불필요 |
| 시뮬레이션 | SimPy 4.1.2: process/resource에 유리, generator 학습과 자원 배정 중복 가능. 직접 큐: 업무 순서를 직접 써야 하지만 작음 | **표준 `heapq`**. `(tick, phase_priority, sequence)` 큐와 명시적 취소·재검사. 물리·용접 열모델 없음 |
| 테스트 | unittest: 표준이나 사례 표/fixture가 장황. pytest: fixture·parametrize·임시 DB에 적합 | **pytest 9.2.0 개발 의존성**. M01–M10 고정 사례부터. Hypothesis는 후속 순서조합 검증 후보 |
| 실행/출력 | notebook: 탐색에 편리하지만 숨은 상태 위험. Typer: 편의 기능과 의존성 추가. FastAPI/웹 UI: 서버·UI 상태까지 관리 | **argparse CLI**의 `run`, `inspect`. DB에서 JSONL 추적과 CSV 요약을 내보냄. 평가자는 별도 오프라인 명령 |

SQLite만 상태·근거·지시·추적의 기준 저장소다. JSONL/CSV는 내보낸 읽기 자료이며 수정해 기준 상태에 역반영하지 않는다. `PRAGMA foreign_keys=ON`, 명시적 `BEGIN IMMEDIATE/COMMIT/ROLLBACK`, 버전 조건부 갱신을 사용한다. 계산 중에는 쓰기 트랜잭션을 열어 두지 않는다. [SQLite 트랜잭션](https://www.sqlite.org/lang_transaction.html), [격리](https://www.sqlite.org/isolation.html), [Python sqlite3](https://docs.python.org/3.13/library/sqlite3.html).

최소 구현에서는 `runs.snapshot_json`에 공개 상태를 보관하고 사건·근거·계획·검사·지시·관측·로그를 ID로 조회하는 키 테이블을 함께 둔다. 작업·로봇·구역마다 별도 테이블과 동기화 코드를 먼저 만들지 않았다. R §4.2의 일관된 기준 상태와 R §4.5의 원자적 적용 요구를 유지하면서 저장 구현을 줄인 선택이다. 객체 관계는 도메인 검사, 저장 행의 유일성과 run/plan 관계는 SQL 제약, 상태 최신성은 트랜잭션 안의 버전 비교로 검사한다. 정확한 테이블·로그 형식은 [데이터 계약](data-contracts.md)에 기록한다.

### 버전과 실행 조건

아래는 2026-11-06 공식 문서/배포 정보에서 확인한 기준이다. 최신 버전이라는 일반 주장은 하지 않는다. 런타임·직접 의존성은 아래 버전을 기준으로 고정했고 전이 의존성은 `uv.lock`에 기록한다. 보안 패치나 호환성 때문에 변경하면 이유와 작은 재검증 결과를 기록한다.

| 구성 | 채택 버전/조건 | 환경·비용·공식 근거 |
| --- | --- | --- |
| CPython | 3.13.18, 일반 GIL 빌드 | 3.13 계열은 보안 수정 단계. PSF 라이선스. [릴리스](https://www.python.org/downloads/release/python-31318/), [지원 상태](https://devguide.python.org/versions/). 공식 소스 배포와 uv 관리 빌드를 구분 |
| uv | 0.13.7 | Apache-2.0 또는 MIT. macOS/Linux/Windows 지원; Tier 1·최소 OS는 [플랫폼 정책](https://docs.astral.sh/uv/reference/policies/platforms/) 참조. [배포](https://pypi.org/project/uv/), [lock/sync](https://docs.astral.sh/uv/concepts/projects/sync/), [라이선스](https://docs.astral.sh/uv/reference/policies/license/) |
| OR-Tools | 9.15.6755 | Apache-2.0. Python 3.13의 Windows x64/macOS ARM64·Intel/Linux wheel 확인. [공식 배포](https://pypi.org/project/ortools/). 전이 패키지의 OS 조건까지 실제 설치에서 확인 |
| Pydantic | 2.14.3 | MIT, Python 3.10+. Rust 기반 core는 wheel로 설치하며 Rust를 배울 필요 없음. [설치](https://docs.pydantic.dev/latest/install/), [배포](https://pypi.org/project/pydantic/) |
| SQLite/표준 모듈 | 선택한 Python 빌드에 포함된 버전을 manifest에 기록 | SQLite public domain, 서버/계정 없음. [사용 조건](https://www.sqlite.org/copyright.html). SQLite 별도 패키지를 설치하지 않음 |
| pytest | 9.2.0 | MIT, Python 3.10+, macOS/Windows/POSIX. [시작](https://docs.pytest.org/en/stable/getting-started.html), [라이선스](https://docs.pytest.org/en/stable/license.html), [배포](https://pypi.org/project/pytest/) |

팀 공통 재현 대상은 macOS ARM64/Intel, Windows 11 x64, Ubuntu 24.04 x64를 우선한다. 이는 프로젝트의 시험 대상이지 모두 실행 검증됐다는 뜻이 아니다. 이번 확인은 로컬 macOS ARM64만이며 팀원 OS·메모리·설치 권한은 아직 확인하지 않았다. Docker·클라우드는 필수 환경에서 제외한다. 같은 lockfile도 플랫폼별 wheel이 달라질 수 있으므로 OS/CPU/Python/SQLite/패키지 버전을 결과와 함께 남긴다. 단일 worker와 seed를 고정해도 wall-clock 제한과 장비 부하가 같지 않으면 동일 incumbent를 보장하지 않는다.

## DEC-03 데이터와 갱신 계약

**R §4.2·표7, §4.3 → 출처·현재성·JSON 사건과 공통 검증 → JSON/JSONL + Pydantic 2 + SQLite 원자 갱신.** YAML은 사람이 쓰기 편하지만 타입 해석·추가 parser가 생기고, dataclass 단독은 경계 검증을 직접 구현해야 한다. JSON Schema 단독도 가능하나 Python 도메인 객체와 이중 관리가 생긴다. Pydantic 모델에서 JSON Schema를 생성해 문서/후속 LLM 출력과 연결한다.

`strict=True`, `extra='forbid'`, 정수 tick, 명시적 nullable/UNKNOWN, enum을 사용한다. JSON 로더는 중복 key와 NaN/Infinity를 거부한다. 타입 검증이 대상 존재·권한·근거 유효성까지 보증하지 않으므로 도메인 검사를 추가한다. [strict mode](https://docs.pydantic.dev/latest/concepts/strict_mode/), [JSON Schema](https://docs.pydantic.dev/latest/concepts/json_schema/), [Python JSON](https://docs.python.org/3.13/library/json.html).

필수 필드·상태 전이·중복·복구·검사와 적용 사이 충돌은 [데이터 계약](data-contracts.md)이 기준이다. SQL이나 수식을 받아 실행하는 입력은 없다. 같은 사건은 한 번만 적용하고, 사건 기록·제한·상태 변경·로그를 한 트랜잭션에 기록한다. 임의 TTL로 핏업 제한을 해제하지 않는다.

구현 과정에서 다음 계약을 구체화했다. 새 기술이나 연구 범위를 추가한 변경은 아니다.

- R §2.3 표4·B §3의 조합별 제한/복구를 위해 `PAIR_REACHABILITY_CHANGED`를 추가했다. FAIL 전용 `PAIR_UNREACHABLE`과 구별하며 같은 조합의 최신 PASS와 제한 ID·회차가 맞을 때만 해제한다.
- R §4.2의 출처 확인과 R §4.4의 변경량 목적을 위해 공개 snapshot에 현재 `evidence`, `sources`, `baseline_plan`을 포함했다. 미래 사건·평가자 정보는 포함하지 않는다.
- 지시에 `accepted_state_version`, `accepted_plan_version`, `attempt_id`, 이동 from/to를 기록한다. 계획 교체와 실제 실행 회차·위치를 연결하고 진행 phase를 보존하기 위한 필드다.
- 검사 위반은 코드/대상 문자열 목록, 감사 로그는 공통 envelope와 단계별 `data` 객체로 구현했다. 모든 단계에 쓰이지 않는 중첩 필드를 반복하지 않고 원문 사건·근거·계획·검사·관측을 ID로 연결한다. 후속 분석에 필드별 질의가 필요하면 구조를 확장한다.

## DEC-04 목적함수와 자동 적용

**R §4.4 → 가중 납기 지연 우선, 변경 후순위 → 사전식 목적 유지.** R §4.5의 미정 예외 승인 한도는 B §2·3·6에 따라 개발용 정책으로 구체화한다. 납기 한도 초과만으로 사람 승인을 요구하는 기능은 첫 사례에 넣지 않는다.

1. 제약 변환기가 현재 근거로 `required_job_ids`와 사유별 `deferred_jobs`를 정한다. 솔버가 임의로 작업을 빼서 목적값을 줄이지 못하게 한다.
2. 개발 정책은 **지금 유효한 작업 구간을 시작할 수 있는 유휴 로봇이 있으면 적어도 하나를 지금 착수**하게 한다. ‘착수’는 이동부터다. 초기 위치·준비·구역과 실제 선행 완료를 검토한 즉시 가능 조합을 계산하고, 그 조합의 `assigned AND move_start == now` indicator 합을 1 이상으로 둔다. 이 집합이 비면 해당 규칙을 추가하지 않는다. 무조건 모든 로봇을 바쁘게 만드는 규칙은 아니다.
3. 이 운영 규칙과 하드 제약 아래 `T=sum(weight*max(0,weld_end-due))` → 기존 유효 계획 대비 로봇 변경 작업 수 → 공통 작업의 용접 시작시각 절대 변화 합 → 용접 완료시각 합을 순서대로 최소화한다. 최초 계획에 비교 시각이 없으면 해당 변경 항목은 0 및 비교 불가 사유를 기록한다. 진행 작업은 목적이 아니라 고정 제약이다.
4. 상위 목적이 OPTIMAL일 때만 해당 최적값을 등식으로 고정하고 다음 단계를 푼다. FEASIBLE이면 마지막 독립 검사 가능한 incumbent를 반환하고 하위 목적은 수행하지 않는다. 하위 단계 UNKNOWN/예산 소진에도 이전 incumbent를 보존해 FEASIBLE_NOT_PROVEN으로 반환하며, 네 목적 모두 증명해야 전체 OPTIMAL이다. 아무 단계에서도 해가 없을 때만 NO_SOLUTION_WITHIN_LIMIT이다. 하위 단계가 앞 단계의 해와 모순되면 모델 오류로 진단한다. 숫자 가중합의 거대한 가중치로 우선순위를 흉내 내지 않는다.
5. 실행 가능한 해는 근거·물리 제약·최신성·작업 포함·정책 검사를 모두 통과하면 자동 적용한다. FEASIBLE_NOT_PROVEN도 허용하고 최적성 미증명과 bound를 기록한다. 임의 코드/수식이나 근거 부족을 승인으로 우회하지 않는다.

2번은 빠른 후속 지시를 우선하기 위한 **개발 가정**이다. 전환 부담 때문에 의도적으로 기다리는 정책은 첫 데이터의 전환시간 1에서 적용하지 않는다. 알려지지 않은 복구 시각을 기대하며 기다리지 않는다. 실제 전환 비용이 있는 정책은 예비실험 전 별도 근거·비교 조건으로 재검토한다. 본 연구의 주 지표는 여전히 무개입 정답완결이며 위 목적값은 보조 운영 지표다.

## DEC-05 실패와 재시도

**R §4.4·4.5, B §6 → 솔버 결과와 실행 가능성을 혼동하지 않음 → 결과별 행동 계약 채택.** [데이터 계약](data-contracts.md)의 상태표를 사용한다. 빈 후보 집합의 정상 대기, INFEASIBLE, 시간 내 해 미발견, 최적성 미증명, 근거 부족, 오래된 계획을 각각 기록한다.

개발 기본은 전체 사전식 solve 벽시계 예산 5초, `num_search_workers=1`, `random_seed=0`이다. 각 목적 단계에 남은 예산만 준다. UNKNOWN이면 자동 시간 연장·같은 입력 무한 재시도를 하지 않는다. 앞 목적 단계의 유효 해가 있으면 보존한다. 최신성 충돌만 최신 스냅샷으로 최대 2회 다시 계획하고 계속 충돌하면 `RETRY_EXHAUSTED`로 신규 지시를 보류한다. DB busy는 1초 timeout 후 충돌/오류로 기록한다. 조회로 근거를 보완하는 결정적 pass는 1회이며 새 관측이 필요하면 대상 범위와 질문을 남긴다. 이는 개발 상한이며 연구 통과시간·최적 성능 수치가 아니다.

## DEC-06 수치와 시간

**R §2.2 표2·§4.6, B §2·4 → 같은 가상 사례를 끝까지 연결 → 기존 2대·5개 수치를 개발 fixture v1로 채택.** 시간·납기·가중치·능력·구역은 모두 [data/README](../data/README.md)의 실험용 가정이다. 1 tick은 추상 시뮬레이션 시간 1단위이며 현장 초/분 환산값이 없다. W18의 -2 시작은 초기 용접 이력만 허용하며 알 수 없는 과거 준비/이동 시각은 생성하지 않는다. 선행 완료 전 MOVE까지 금지하는 보수적 기본안을 추가하고 용접 재개시간은 실제 WELD_START로 측정한다.

로봇은 이동·준비·용접 동안 점유하고 구역은 **준비 시작부터 용접 종료까지** 점유한다. 이동은 구역을 점유하지 않는다. 이동 중 충돌·통로·기하학은 모델 밖이다. 동일 tick 순서는 실제 완료/phase 종료 → 도착 외부 사건 일괄 반영 → 재계획·적용 → 새 phase 시작이다. 진행 용접은 고정하며 이동/준비 도중 새 제한은 현재 phase 종료 후 다음 phase를 차단한다. 상세는 계약을 따른다.

현재 미착수 후보의 horizon은 `now + max(진행 구간의 잔여시간,0) + sum(미완료 작업의 처리시간 + 최대 이동시간 + 준비시간)`으로 보수적 직렬 상한을 잡는다. 현재 알려진 release가 있으면 그 최대값을 시작 기준에 포함한다. 알려지지 않은 복구 시각은 사용하지 않는다. 유한 horizon에서 INFEASIBLE이라는 사실을 공정의 영구 불가능으로 확대하지 않는다.

## DEC-07 이후 LLM 연결

**R §4.3, B §4·5 → 같은 모델의 해석·근거 확인·조율 비교 → 공식 SDK를 직접 호출하는 얇은 어댑터 추천.** 정형 경로에는 LLM 의존성을 설치하지 않는다. 후속 첫 연결 후보는 **OpenAI Python SDK + Responses API의 구조화 출력/명시적 조회 도구**다. `ReportAdapter -> EventProposal` 인터페이스를 두어 공급자를 교체할 수 있게 한다. 모델 ID/고정 snapshot·SDK 버전·가격·계정 사용 가능 여부·호출/토큰 상한은 연결 시 공식 자료와 개발 예비시험을 확인해 결정한다. 이번에 모델을 선택·호출하거나 API 계정 사용 가능성을 확인하지 않았다.

| 후보 | 판단 |
| --- | --- |
| 공식 SDK + 작은 호출 루프 | **추천**. 역할별 프롬프트와 조회/재검토 횟수를 명시적으로 기록하기 쉬움. provider별 오류·재시도 처리는 직접 작성 |
| LangGraph | 장기 실행·checkpoint·복잡한 분기가 늘 때 후보. 현재는 공통 상태 경계와 프레임워크 상태를 함께 관리할 이득이 작음 |
| 로컬 모델 | 데이터 외부전송 통제 장점. 하드웨어·모델 라이선스·출력 품질·실행환경 검증이 추가되어 첫 연결에서는 보류 |

단일 구조에는 같은 조회 도구와 자기 검토 기회를 주고 역할 분리에는 같은 총 호출/토큰/시간 상한을 준다. 역할별 3배 예산을 주지 않는다. 도구는 ID 기반 읽기 전용 조회와 `EventProposal` 반환뿐이며 파일 읽기·shell·SQL·상태 변경 권한을 주지 않는다. refusal·형식 실패·timeout은 근거 부족과 구별한다. 구조화 출력은 형식을 돕지만 사실 확인을 대신하지 않는다. API는 호출 비용과 계정/모델별 사용 조건이 있으므로 고정 모델을 선정할 때 당시 단가와 예산을 기록한다. [구조화 출력](https://developers.openai.com/api/docs/guides/structured-outputs), [function calling](https://developers.openai.com/api/docs/guides/function-calling), [공식 가격](https://developers.openai.com/api/docs/pricing), [LangGraph 범위](https://docs.langchain.com/oss/python/langgraph/overview).

## DEC-08 평가와 DEC-09 확장

본 평가의 사례 수·유형 비중·반복·제한시간·시드·수치 통과 기준과 모델 비용 예산은 예비실험 후 동결한다. D01–D04와 M01–M10은 개발 수락 기준이다. `pytest`는 코드 검증, `evaluation/score.py`는 독립 정답 및 환경 사실을 이용하는 오프라인 채점 역할이며 서로 대체하지 않는다. [평가 원칙](../evaluation/README.md).

OCR는 보고 입력의 근거가 실제로 필요할 때, UI는 추적 결과의 열람 문제가 생길 때, 3D는 로그로 보여주기 어려운 시연 요구가 있을 때 검토한다. 클라우드는 로컬 재현 후 공유 실행 요구가 생길 때 검토한다. 현재 기능의 필수 의존성이 아니다.

## 기술 선택 단계의 확인 기록

기술 선택 작업 당시 공식 자료로 지원 기능·배포·사용 조건을 조사하고 원본 두 파일·당시 원격 저장소의 기준 커밋 `4de3ce9`를 확인했다. 아래는 **애플리케이션 구현 이전의 선택 확인용 프로브 이력**이다. 당시 검증은 임시 가상환경의 작은 API 예제로 제한했으며 다음 결과를 실제로 관측했다.

| 확인 | 실제 결과 | 한계 |
| --- | --- | --- |
| 환경 | macOS 26.6.2 ARM64, Python 3.13.18, uv 0.13.7, OR-Tools 9.15.6755, Pydantic 2.14.3, SQLite 3.53.1 | Windows/Linux/팀원 환경 미실행 |
| 선택 배정·고정 구간 | W17 presence=0, R2의 W18 [-2,2) 고정, W19 선택 배정과 W21 겹침 금지 소형 모델이 OPTIMAL | 5개 전체 흐름·이동/준비/복구 모델 검증이 아님 |
| 명시적 모순 | W21 시작 >=2 및 <2를 함께 넣어 INFEASIBLE 확인 | UNKNOWN/FEASIBLE 시간 제한 분기는 실제로 만들지 않았음 |
| strict 입력 | 문자열 tick, bool tick, 미정의 미래시각 필드 3종 거부 | 전체 업무 스키마 구현 아님 |
| SQLite 버전 비교 | version=3 조건의 갱신 두 번에서 rowcount [1,0] | 순차 재현한 오래된 요청 검사. 병렬 경쟁·앱 원자적 적용 검증 아님 |

검증 스크립트와 출력은 로컬 Git 제외 경로 `artifacts/selection-probe.py`, `artifacts/selection-probe-result.json`에 보존했다. 실행 명령은 `/private/tmp/shipyard-probe313/bin/python artifacts/selection-probe.py`였고 임시 환경은 프로젝트 설치 환경이 아니다. 스크립트 SHA-256: `40ff399f3e6b5836048730d443f825409362d4c55e9d151fadbc2065941c8580`. 프로브용 설치는 PyPI와 uv 관리 Python을 사용했다. **그 기술 선택 작업에서는** 앱 의존성/lockfile을 생성하거나 전체 앱·전체 이동/준비/복구 흐름·M01–M10을 실행하지 않았다. 이 한계는 당시 프로브의 범위이며 현재 구현 상태와 구별한다.

이후 정형 사건 구현 작업에서 프로젝트 환경·lockfile·코드·개발 사례·검증 코드를 작성했다. 현재의 실제 명령·환경·사례 결과·M01–M10 증거는 [검증 기록](next-steps.md)을 따른다. 기술 선택 프로브의 결과를 통합 검증이나 성능 비교 결과로 확대하지 않는다. Gurobi 라이선스 파일·API 키는 읽지 않았으며 LLM 연결·본 비교실험·현장 성능 평가는 후속 범위다.

## 먼저 공부할 내용

각 연습은 현재 최소 구현의 해당 모듈을 이해하고 수정하는 학습으로 연결된다. 이미 익숙한 항목은 건너뛸 수 있다.

| 기술과 하는 일 | 먼저 배울 개념 3개 | 공식 학습 자료와 작은 연습 |
| --- | --- | --- |
| Python: 처리 모듈과 데이터 | 함수/모듈, type hint, 예외 처리 | [튜토리얼](https://docs.python.org/3.13/tutorial/). W17 제한을 받아 허용/거부 이유를 반환하는 순수 함수 |
| uv: 팀 환경 재현 | pyproject, lockfile, sync/run | [프로젝트 안내](https://docs.astral.sh/uv/guides/projects/). 임시 프로젝트에서 lock 기반 재설치 후 버전 일치 확인 |
| Pydantic: 입력 경계 검사 | BaseModel, strict/extra forbid, model validator | [모델](https://docs.pydantic.dev/latest/concepts/models/). 문자열 tick·추가 필드·대상 없는 사건을 거부 |
| SQLite: 일관된 기준 상태 | PK/FK/UNIQUE, 트랜잭션, 조건부 UPDATE | [sqlite3](https://docs.python.org/3.13/library/sqlite3.html). 버전 3을 본 두 요청 중 하나만 4로 갱신 |
| CP-SAT: 배정과 일정 | optional interval, NoOverlap, reification/전환 arc | [job shop](https://developers.google.com/optimization/scheduling/job_shop). W17 제외·W18 고정·W19 선택 배정을 구성 |
| heapq: 가상 시간 진행 | 우선순위 큐, tie-break, 취소/세대 번호 | [heapq](https://docs.python.org/3.13/library/heapq.html). 같은 t=2에서 완료와 제한을 처리한 뒤 오래된 착수 지시 건너뛰기 |
| pytest: 오류를 드러내는 개발 검증 | fixture, parametrize, tmp_path | [입문](https://docs.pytest.org/en/stable/getting-started.html). 동일 사건 재전달·같은 ID 충돌·재확인 없는 해제를 표로 테스트 |
| argparse/JSONL/CSV: 재현과 결과 확인 | subcommand, exit code, 직렬화 | [argparse](https://docs.python.org/3.13/library/argparse.html). inspect로 미완료 이유를 출력하고 추적을 파일로 내보내기 |
| 후속 LLM SDK: 보고 해석 | structured output, tool call, 예산/오류 처리 | [공식 가이드](https://developers.openai.com/api/docs/guides/function-calling). 먼저 가짜 응답 어댑터로 읽기 전용 조회→사건 제안 계약을 시험. 유료 실제 호출은 후속 단계 |

위 학습 연습을 사용자가 완료했다고 기록하지 않는다. 코드의 실제 검증과 학습 이수는 별개다. SimPy·Hypothesis·LangGraph는 채택 의존성이 아니다.
