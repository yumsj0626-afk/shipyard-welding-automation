# 구현 상태·검증 증거와 다음 작업

**현재 단계: 정형 사건 I01–I09와 가짜 모델 문장형 보고 어댑터 구현·개발 검증 완료.** 기존 117개 테스트를 수정하지 않고 유지했으며 2026-11-07 추가한 69개를 포함해 186개가 통과했다. 2026-11-06의 정형 사건 검증 기록은 아래에 그대로 보존한다. [DEC-01–06](decisions.md), [아키텍처](architecture.md), [데이터 계약](data-contracts.md)을 구현에 맞게 갱신했다. 아래는 실제 구현 순서와 증거이며 본 비교실험의 결과가 아니다.

이번 구현은 **하나의 프로그램에서 로봇 2대·작업 5개의 정형 사건을 근거 확인 → 보류 → 대체 또는 대기 → 재계획 → 독립 검사 → 적용 → 시뮬레이션 → 복구와 재편입까지 연결**했다. 정형 경로에는 LLM·API 서버·웹 UI가 필요하지 않다.

## 구현 순서

아래 I01–I09는 모두 **구현·검증 완료**다. 표는 작업의 입력·변경 파일·산출물·수락 기준을 유지한다. 실행 증거는 아래 M01–M10 표와 검증 기록에 연결했다. 기능 없는 빈 모듈은 추가하지 않았다. `D01` 경로를 기본으로 하며 D02–D04는 같은 계약을 사용하는 개발 변형이다.

| 작업·의존관계 | 입력 | 작성·수정한 파일 | 산출물 | 완료 확인 방법 |
| --- | --- | --- | --- | --- |
| **I01 환경·계약·개발 사례**. 문서 결정 이후 | DEC-01–06, 데이터 계약, [2대·5개 사례](../data/README.md), 원문 규칙 | `pyproject.toml`, `uv.lock`, `.python-version`; `src/shipyard/__init__.py`, `src/shipyard/contracts.py`; `config/dev-policy.json`; `data/dev/D01/initial.json`, `data/dev/D01/evidence.json`; `simulation/dev/D01/events.jsonl`; `evaluation/dev/D01/oracle.json`, `evaluation/dev/D01/truth.jsonl`; `tests/test_contracts.py` | Python 3.13.18·uv 환경, CP-SAT·Pydantic 의존성 및 pytest 개발 의존성 고정. 공통 DTO와 D01–D04 입력·독립 기대 행동. 원문 근거와 개발 가정을 표시한 사례 메타데이터 | 새 가상환경에서 lockfile로 설치하고 실제 버전 기록. 정상 입력 수용, 누락·알 수 없는 필드·잘못된 타입/ID·중복 JSON key 거부. 정답표를 솔버 출력에서 만들지 않고 원문 규칙에서 별도로 작성. 미래 사건·정답이 시스템용 초기 상태에 없음을 검사. 교차 검토의 수행 여부와 남은 검토를 사실대로 기록. **M01·M09·M10 준비** |
| **I02 기준 상태·근거 저장**. I01 이후 | 검증된 초기 상태·근거, 버전 및 저장 계약 | `src/shipyard/repository.py`, `src/shipyard/schema.sql`; `tests/test_repository.py` | 실행별 SQLite DB 초기화, 읽기 전용 snapshot, 근거 조회, 트랜잭션 갱신·이력. 상태/계획 버전과 ID 유일성 | 임시 DB에서 초기화·재조회·롤백 검증. 성공한 도메인 변경만 상태 버전을 올리고 로그만 추가한 경우와 구별. 같은 버전을 읽은 두 갱신 중 오래된 갱신 거부. 원본 파일·다른 실행 DB를 변경하지 않음. **M01·M08** |
| **I03 사건 검사·활성 제한**. I02 이후 | 정형 사건, 현재 snapshot, 근거 조회 결과 | `src/shipyard/events.py`, `src/shipyard/repository.py`; `tests/test_events.py` | 사건 승인/중복/충돌/근거 부족 결과, 원인별 제한 생성·유지·해제, 추가 확인 기록 | W17 핏업은 모든 로봇에 적용; R1–W17 조합 불가는 해당 쌍만 제한. 같은 사건 재전달은 재적용하지 않고 같은 ID의 다른 내용은 충돌 처리. 조치 완료만으로 해제하지 않음. 복구가 지정한 제한과 발생 회차·대상·근거가 맞는지 검사. 미해결 제한은 남김. **M02·M05·M06·M08·M09** |
| **I04 고정 제약 변환**. I03 이후 | snapshot, 활성 제한, 정책 | `src/shipyard/constraints.py`; `tests/test_constraints.py` | 솔버에 전달할 고정 형식의 배정 가능성·시간·자원·선행 제약과 제외 이유 | W17 보류 중 W20의 실행이 허용되지 않음. W18의 진행 중 배정·시간 유지. 수행 능력·자재 준비·구역·이동·준비 조건 반영. 누락 정보를 임의 수치로 채우지 않음. 에이전트 코드·수식·평가 정답을 입력으로 받지 않음. **M01·M02·M04·M06** |
| **I05 CP-SAT 재계획**. I04 이후 | 고정 제약, 현재 시각과 버전, 개발용 목적·시간 제한 | `src/shipyard/scheduler.py`; `tests/test_scheduler.py` | 후보 계획, 해 상태·목적값·최적성 증명 여부·설정·계산시간·제외 이유 | 작은 수작업 검산 사례로 배정·선행·로봇/구역 중복·이동/준비·진행 중 고정을 확인. 가능한 작업을 목적값 개선을 위해 임의 누락하지 않음. 최적/실행 가능·최적성 미증명/해 없음/시간 내 해 미발견/모델 오류를 구별. 상태별 분기는 오류 주입으로도 검사하고 실제 시간 초과 측정과 구별. 특정 최적 일정 하나만 정답으로 강제하지 않음. **M03·M04·M07·M09** |
| **I06 독립 실행 검사·원자적 적용**. I05 이후 | 후보 계획, 최신 snapshot, 정책, 검사 대상 버전 | `src/shipyard/checker.py`, `src/shipyard/controller.py`, `src/shipyard/repository.py`; `tests/test_checker.py`, `tests/test_application.py` | 솔버 모델 생성과 별개인 검사기, 통과/거부 사유, 버전 비교와 지시 저장의 원자적 처리 | 각 제약을 하나씩 깨뜨린 계획을 주입해 거부. 솔버 제약 생성 함수를 그대로 재사용해 검사하지 않음. 검사 후 적용 전에 상태가 바뀌면 전체 적용을 거부하고 최신 snapshot으로 재계산. 부분 적용·중복 착수 없음. 오래된 미착수 지시는 무효화하되 진행 작업 유지. **M03·M07·M08·M09** |
| **I07 가상 시간·외부 신호 연결**. I06 이후 | 적용 지시, simulator 전용 사건 흐름, 개발용 시간/동시각 순서 규칙 | `src/shipyard/simulator.py`, `src/shipyard/controller.py`; `tests/test_simulator.py` | heapq 사건 큐, 실제 시작/완료 관측, 현재까지 도착한 외부 사건 전달, 복구 후 재계획 | 같은 tick의 처리 순서를 계약대로 확인. 취소된 계획의 미착수 이벤트는 건너뛰고 W18 완료는 보존. t=6 복구 시각을 사전에 controller·solver로 넘기지 않음. W17 복구는 완료로 기록하지 않으며 W20 착수 직전 W17의 실제 완료 확인. 큐가 비면 완료/근거 있는 대기/실패 이유를 구별. **M01·M03·M04·M05·M08·M10** |
| **I08 추적 기록·독립 채점**. I07 이후; oracle 초안은 I01에서 준비 | DB 기록, simulator 실제 실행 기록, 평가자 전용 oracle·truth | `src/shipyard/reporting.py`; `evaluation/score.py`; `tests/test_integration.py`, `tests/test_evaluation.py`, `tests/test_information_boundary.py` | 입력→근거→제한→계획→검사→지시→실행 추적, JSONL·CSV·사람이 읽는 요약, 별도 채점 결과 | 검사기 PASS를 정답으로 복사하지 않고 독립 기대 행동·실제 상태로 채점. 금지 계획의 제안/차단/실행을 분리. 추가 사람 요청과 사전 외부 신호를 구별. 미래 시각·정답용 표식 값을 넣고 controller/solver/후속 agent 입력에 유입되지 않는지 검사. 폴더 분리만으로 차단 완료라 하지 않음. **M06·M09·M10** |
| **I09 CLI·통합·재현 안내**. I01–I08 이후 | D01–D04, M01–M10, 단계별 테스트 | `src/shipyard/__main__.py`, `src/shipyard/cli.py`; `tests/test_integration.py`; `README.md`, `data/README.md`, `src/README.md`, `tests/README.md`, `evaluation/README.md`, 본 문서 | argparse 실행 진입점, 설치/실행/조회/채점 명령, 실행별 환경·설정·입력 해시·결과·실패 기록 | lockfile 기반 새 환경에서 D01 전체 흐름과 D02 대기 변형, D03 조합 제한, D04 불충분한 복구를 실행. M01–M10마다 실제 증거 경로 제시. 성공한 항목만 완료로 바꾸고 환경 제약·남은 실패 명시. **M01–M10** |

솔버나 검사기 구현에서 계약의 모순을 발견하면 해당 DEC와 데이터 계약을 먼저 수정하고 이유를 남긴다. 기술을 새로 늘리거나 연구 범위를 바꾸는 근거로 사용하지 않는다. 실제 담당·마감은 아직 배정하지 않았다.

## 최소 흐름의 수락 기준

기존 **M01–M10 ID를 유지**한다. 이는 개발 기능의 수락 기준이며 본 평가의 표본 수·성능 목표·통계적 통과 기준이 아니다. 모든 항목의 현재 상태는 **개발 검증 통과**다. 이는 아래 명시한 시험 범위에 한정한다.

| ID | 사건/검증 | 관측 가능한 완료 기준 | 주 구현 작업 |
| --- | --- | --- | --- |
| M01 | 정상 초기화 | R1·R2와 W17–W21을 읽고 미착수·진행·완료를 구별. 이미 진행 중인 W18의 배정·완료 예정은 재계획으로 바뀌지 않음 | I01·I02·I04·I07 |
| M02 | 핏업 불가 | W17이 모든 로봇에서 보류되고 W20 착수가 막힘. 무관한 가능 작업을 일괄 보류하지 않음 | I03·I04 |
| M03 | 대체 후보 있음 | 능력·준비·선행·구역·이동/준비시간을 반영한 계획이 별도 검사 후 승인 요청 없이 적용되고 대체 작업이 실제 시작·완료됨. 가능한 대체가 있는데 이유 없이 전부 정지하면 실패 | I05·I06·I07 |
| M04 | 대체 후보 없음 | 가능한 작업이 없다는 이유와 재검토 조건을 기록하고 대기. 금지/미준비 작업을 실행하거나 필수 확인 없이 제한을 해제하면 실패 | I04·I05·I07 |
| M05 | 복구와 선행조건 | 조치 완료만으로 해제하지 않음. 정해진 재확인 근거까지 도착하면 W17 재편입, W17 실제 완료 후에만 W20 시작. 복구가 있는 사례는 재편입까지 채점 | I03·I07 |
| M06 | 제한별 해제 | 동일 작업에 미해결 제한이 여러 개 있으면 하나의 복구 신호가 다른 제한까지 제거하지 않음. 오래되거나 대상이 다른 복구 신호는 실행 근거가 되지 않음 | I03·I04·I08 |
| M07 | 잘못된 계획 차단 | 보류 포함·능력 위반·선행 위반·로봇/구역 중복·이동/준비 누락·진행 중 변경을 각각 주입했을 때 적용 전 거부. 제안/차단/실행을 별도 기록 | I05·I06 |
| M08 | 최신성·중복 | 오래된 상태/계획의 적용과 같은 사건/지시의 중복 실행이 없음. 현재 상태에 맞는 재조회·재계산으로 이어짐. 검사와 적용 사이 갱신도 차단하고 동일 ID의 충돌 내용은 단순 중복과 구별 | I02·I03·I06·I07 |
| M09 | 불충분한 근거와 실패 | DB 조회로 해결 가능한 누락은 자동 보완. 새 관측이 필요하면 이유를 기록해 요청하고 무개입 완결로 채점하지 않음. 솔버 시간 초과를 곧바로 해 없음으로 기록하지 않음 | I01·I03·I05·I06·I08 |
| M10 | 결과 추적·독립 검증 | 입력→근거→활성 제한→계획→검사→지시→실제 실행이 사건 ID와 버전으로 연결. 시스템 검사와 별개로 정답표 및 실행 상태를 대조하며 미래 사건/정답 누출이 없음 | I01·I07·I08·I09 |

M01–M05가 핵심 시연이다. M06–M10은 이를 신뢰할 수 있게 만드는 개발 검증이다. 대체 사례 D01에서는 유효한 외부 복구 뒤 5개 작업의 실제 완료를 확인한다. 대기 사례 D02에서 준비되지 않은 W19·W21은 미완료 이유를 남기고 임의로 완료 처리하지 않는다. 유효한 일정은 여러 개일 수 있으므로 W19를 반드시 특정 로봇에 배정하도록 강제하지 않는다.

수락 증거에는 실행 명령, 코드/환경/정책/입력 버전, 사례 ID, 테스트 결과와 해당 추적 기록을 연결한다. 실패를 숨기거나 아직 실행하지 않은 항목을 통과로 표시하지 않는다. 선택 확인용 작은 예제의 성공을 전체 흐름 성공으로 옮겨 적지 않는다.

## 실제 검증 기록 — 2026-11-06

저장소에 남긴 [기계 판독 증거](../tests/evidence/2026-11-06.json)에 테스트 117개의 이름, 실행 환경, 코드/입력/출력 SHA256, 사례별 trace 위치와 명령을 기록했다. 로컬 상세 생성물은 `outputs/verification-20261106-final/`에 보존하며 Git 제외 경로다. 다른 환경에서는 새 출력 경로로 재생성한다.

- 환경: macOS 26.6.2 ARM64, CPython 3.13.18, uv 0.13.7, SQLite 3.53.1, OR-Tools 9.15.6755, Pydantic 2.14.3, pytest 9.2.0.
- 새 가상환경을 `uv sync --locked --offline`으로 캐시에서 설치했다. 전체 pytest: **117 passed / failures 0 / errors 0 / skipped 0**. 실행 시간은 1.42초로 출력됐으며 성능 벤치마크가 아니다.
- 별도 새 환경에서 `--no-dev` 설치 후 D01도 완료했다. 실행 프로그램은 pytest 설치를 요구하지 않는다.
- 검증 코드 SHA256: `a1a50ca19e738fef4f35b4493f1c895173ac59f7dc7fb0f2a46d3be01fe52c0c`. 산식은 `cli.source_hash`이며 Python/SQL/pyproject/lockfile을 포함한다. 평가자 코드는 별도 해시로 기록한다.

| 사례 | 실제 최종 상태 | 독립 평가 | 해석 |
| --- | --- | --- | --- |
| D01 | t=15, W17–W21 모두 완료, 추가 확인 0 | 수락 통과, 실행 위반 0 | W18 [-2,2) 유지. W19 [2,5), W21 [3,5) 용접. W17 복구 t=6 → 용접 [8,12) → W20 이동/준비 t=12, 용접 [13,15) |
| D02 | t=14, W17·W18·W20 완료; W19·W21 미준비 대기 | 수락 통과, 실행 위반 0 | t=0 R1에 이유·trigger가 있는 대기. 미준비 작업을 임의 실행/완료하지 않음 |
| D03 | t=15, 5개 완료 | 수락 통과, 실행 위반 0 | R1–W17 제한만 유지, W17은 R2 실행 |
| D04 | t=6, W18·W19·W21 완료; W17·W20 보류, 추가 확인 1 | 수락 통과, **무개입 완결 아님** | 수리만으로 해제하지 않고 재확인 요청. 사람의 추가 관측이 남음 |

유효 일정은 여러 개일 수 있으며 위 시각/배정은 이번 실제 실행 결과다. 모든 미래 실행이 반드시 이 한 일정과 같아야 한다는 정답 조건은 아니다.

| 기준 | 실제 검증 증거 | 범위/확인 내용 |
| --- | --- | --- |
| M01 | `test_contracts.py`, `test_review_regressions.py`, `test_integration.py`; D01 execution.csv | 2대·5개, 잘못된 진행 이력/FK 거부, W18 실제 [-2,2) 고정 |
| M02 | `test_events.py`, `test_constraints.py`; D01 trace EVENT/RESTRICTION/PLAN | W17 모든 로봇 보류와 W20 파생 차단, W19·W21 유지 |
| M03 | `test_scheduler.py`, `test_integration.py`; D01 APPLY/PHASE_START/END | 독립 검사 후 자동 적용, 대체 작업 실제 용접·완료 |
| M04 | `test_integration.py`; D02 t=0 WAIT 및 최종 deferred_jobs | 대체가 없을 때 이유와 재검토 trigger, W19·W21 실행 금지 |
| M05 | `test_events.py`, `test_simulator.py`, `test_integration.py`; D01·D04 평가 | 복구≠완료, repair+linked recheck만 해제, W20 실제 선행 gate, 동일 tick 외부 제한 우선 |
| M06 | `test_events.py`의 다중 제한·새 PASS·잘못된 회차/대상/개정·supersedes 검사 | 한 제한 해제가 다른 제한에 영향을 주지 않음. 최신 반증된 수리 근거 재사용 거부 |
| M07 | `test_checker.py`, `test_evaluation.py` | 각 물리/선행/예약 위반을 독립적으로 주입해 차단; 실제 실행 위반은 평가자가 별도 검출 |
| M08 | `test_repository.py`, `test_application.py` | 중복/ID 충돌, 검사 후 상태 변경, 두 연결 동시 CAS에서 하나만 commit, 중간 쓰기 오류 rollback, 최대2회 재계산, 중복 착수 차단 |
| M09 | `test_events.py`, `test_scheduler.py`, `test_application.py`, `test_review_regressions.py` | 공개 snapshot 1회 조회로 보완/없으면 요청. 실제 작은 INFEASIBLE 모델과 오류 주입 UNKNOWN·FEASIBLE_NOT_PROVEN·MODEL_INVALID·INTERNAL_ERROR 분기 |
| M10 | `test_information_boundary.py`, `test_evaluation.py`, `test_integration.py`; trace·평가 해시 | 공개 파일 접근 allowlist, 금지 import, 미정의 필드, 미래/oracle/truth sentinel 차단; 실제 관측·독립 정답·근거 연결 비교 |

M06–M10 오류 주입은 정상 4개 사례만으로 결론내리지 않도록 추가한 검증이다. UNKNOWN/FEASIBLE 분기 테스트는 실제 시간 초과 측정과 구별한다. 현재 알려진 미해결 테스트 실패는 없다.

### 재현 명령

저장소 루트에서 실행한다. 이미 존재하는 출력 경로는 새로운 이름으로 바꾼다.

```sh
uv sync --locked
uv run --locked pytest -q
uv run --locked python -m shipyard run --case D01 --output outputs/D01
uv run --locked python -m shipyard inspect --run-dir outputs/D01
uv run --locked python evaluation/score.py --run-dir outputs/D01 --oracle evaluation/dev/D01/oracle.json --truth evaluation/dev/D01/truth.jsonl
```

D02–D04도 case·output·oracle·truth 경로를 함께 바꿔 실행한다. 이번 실제 실행의 임시 환경과 상세 명령은 증거 JSON에 기록했다. 운영 summary와 평가자의 결과를 혼동하지 않는다.

### 수정한 문제와 남은 한계

초기 이력의 작업 시간/FK 불일치, 준비 완료 후 구역 예약, pytest가 없는 런타임의 manifest 생성, pytest console entry의 평가 모듈 경로 문제를 발견해 수정하고 회귀 검사했다. 원본 두 파일은 기존 SHA256 그대로다.

Windows/Linux 실행, 사람 팀의 정답표 교차 검토, 본 평가 시간 제한·규모·반복·성능 비교, crash 자동 재개, 악성 코드에 대한 OS 격리, 실물 설비 검증은 수행하지 않았다. 프로세스 중단 시 기존 출력은 보존하고 새 run으로 실행한다. 입력 한계/재시도/실패 정책은 데이터 계약을 따른다.

2026-11-06 검증 당시에는 로컬 main에 커밋이 없었고 HTTPS 접근이 `could not read Username`으로 실패하여 원격에 push하지 못했다. 이는 당시 상태 기록이다.

2026-11-07 업로드 작업에서 기존 SSH 인증이 `yumsj0626-afk` 계정으로 성공함을 확인하고 origin을 동일 저장소의 `git@github.com:yumsj0626-afk/shipyard-welding-automation.git`으로 변경했다. 원격 main의 `9efbc92da87f6d4f7900374baef847d37c1fcf84` 이력을 가져와 로컬 작업 파일 64개의 바이트를 보존한 채 기준 이력으로 연결했다. 게시 전 같은 소스 해시에서 `uv run --locked --offline pytest -q`를 재실행하여 **117 passed, 실패 0**을 확인했다(로컬 JUnit: `outputs/publish-20261107/pytest.xml`). 기존 원본·출력·가상환경은 Git 제외를 유지하며, 구현은 기존 원격 이력 위에 이어지는 커밋으로 관리한다. 강제 push는 사용하지 않는다.

## 문장형 보고 어댑터 검증 — 2026-11-07

`reports.py`의 strict 입력/응답 계약, `FakeReportModel`, 읽기 전용 조회와 사건 제안, controller의 batch 합류, repository의 원자적 보고 receipt 저장, CLI `--report-mode fake`를 추가했다. 패키지 의존성·lockfile·기존 솔버/검사기/평가자·정형 사건 규칙은 변경하지 않았다. 미래 보고를 읽는 드라이버용 대본 4개를 `simulation/reports/`에 추가했다. 기존 파일을 이동하거나 원본 자료를 변경하지 않았다. 실제 LLM 공급자 연결, 에이전트 비교, 본 비교실험은 수행하지 않았다.

[새 검증 증거 JSON](../tests/evidence/2026-11-07-reports.json)에 테스트명·실제 명령·환경·소스/입력/출력 해시·사례 결과를 기록했다. 상세 생성물은 `outputs/reports-verification-20261107/`이며 Git에서 제외한다. 기존 증거 JSON은 당시 정형 구현의 기록으로 유지한다.

- 기존 테스트 파일의 바이트가 기준 커밋 `2e2742f`와 같음을 확인했다. **117개 회귀 + 어댑터 단위 57개 + 통합/정보 경계 12개 = 186 passed**, 실패·오류·skip 0.
- 실제 전체 테스트 명령: `.venv/bin/python -m pytest -q --junitxml=outputs/reports-verification-20261107/pytest.xml`. 이 명령은 현재 프로젝트 환경에서 실행했다. D01–D04의 두 모드는 기존 별도 uv 환경에서 `uv run --locked --offline`으로 실행하고 별도 평가 CLI로 채점했다.
- 두 모드의 실제 작업·로봇·phase 시작/완료 기록과 독립 평가 결과가 일치했다. 같은 CP-SAT·checker 호출을 관측했다. 무작위 ID가 다르므로 원시 JSON 전체 바이트 동등성을 요구하지 않는다.
- timeout/예외/거부/잘못된 JSON/추가 필드/UTF-8 오류/중첩 JSON/위조 ID/도구 남용/크기 상한을 주입했다. 실제 네트워크 취소·과금·언어 정확도·처리시간 성능을 검증한 것은 아니다.
- 보고/모델 문맥·솔버 입력을 관측하고 미래 문장·복구 근거·oracle·truth에 sentinel을 넣었다. 평가 파일을 runtime에서 읽지 못하게 한 실행도 통과했다. `reports`에서 repository/controller/cli/simulator/evaluation import 금지를 검사했다.
- 발견해 수정한 문제: 기존 보고 ID 충돌을 DUPLICATE로 반환하던 분기를 REPORT_REJECTED로 구별했고, 유효하지 않은 UTF-8/과도한 JSON 중첩을 해석 오류로 처리했다. 첫 fixture 작성 프로브의 잘못된 종류 이름을 실제 계약 `PAIR_UNREACHABLE`로 고쳤다. 최종 실행에 남은 실패는 없다.

| 사례 | fake 보고 / 모델 호출 / 조회 | 실제 결과와 독립 평가 |
| --- | --- | --- |
| D01 | 3 / 6 / 3 | t=15, 5개 완료, 추가 확인 0, 수락 통과. W18 유지, W17 복구와 실제 완료 구분, 완료 후 W20 착수 |
| D02 | 3 / 6 / 3 | t=14, W17·W18·W20 완료, W19·W21 미준비 대기, 수락 통과 |
| D03 | 1 / 2 / 1 | t=15, 5개 완료, R1–W17 제한만 유지하고 R2로 W17 실행, 수락 통과 |
| D04 | 2 / 4 / 2 | t=6, W17·W20 보류, 추가 재확인 요청 1, 수락 통과지만 **무개입 완결 아님** |

기존 M01–M10의 의미와 증거는 유지하며 추가 검증을 다음처럼 연결한다.

| 기준 | 이번 추가 증거 |
| --- | --- |
| M01–M05 | D01–D04의 정형/fake 실행 기록·독립 채점 대조, 같은 tick의 repair+recheck 일괄 처리, 실제 완료 gate 유지 |
| M06 | 단위 검사에서 해석 실패의 정보 보류를 명시적 식별 확인으로만 해제하고 다른 FITUP_HOLD는 유지; 잘못된 제한 인용 거부 |
| M07 | 기존 독립 검사 회귀 유지, fake 모드가 같은 checker를 호출함을 관측. 모델에 계획·수식·실행 코드 필드 없음 |
| M08 | 같은 보고 ID 중복의 모델/계획 재실행 없음, ID 충돌 격리, 해석 중 상태 변경의 STALE_CONTEXT, 보고 결과 저장 중 오류의 사건/근거/상태/receipt 전체 rollback |
| M09 | 근거 없음을 사실로 만들지 않음, 잘못된 응답·모델 오류·조회 예산 초과에서 대상별 확인 대기 또는 식별 요청. 실패를 실제 해 없음으로 바꾸지 않음 |
| M10 | REPORT_RESULT의 접수 입력·요청/응답·조회 ID·상태 버전과 EVENT 연결, 요약 집계, fixture/코드 해시, model/solver 미래·정답 누출 검사 |

다른 환경에서는 uv 설치 후 아래 명령으로 새 출력 경로에 재현한다. 환경 변수/실제 uv 절대 경로가 포함된 당시 명령은 증거 JSON에 보존한다.

```sh
uv sync --locked
uv run --locked pytest -q
uv run --locked python -m shipyard run --case D01 --report-mode fake --output outputs/reports-D01
uv run --locked python -m shipyard inspect --run-dir outputs/reports-D01
uv run --locked python evaluation/score.py --run-dir outputs/reports-D01 --oracle evaluation/dev/D01/oracle.json --truth evaluation/dev/D01/truth.jsonl
```

LLM 연결 전 결정: [DEC-07](decisions.md)의 모델 후보·계정 권한, SDK 버전, 보고/실행/프로젝트 금액 상한, 토큰·timeout·전체 deadline, 입력 외부 전송/보관 정책을 정한다. 먼저 Pydantic의 discriminated union/extra 금지, 읽기 전용 tool call, receipt·state version·트랜잭션을 학습하면 다음 구현과 바로 연결된다. 현재 가짜 모델에는 API 키·네트워크·유료 호출이 없다.

## 최소 흐름 이후

정형 흐름·가짜 보고 어댑터 통과 → 예산·데이터 전송 정책 확정 → 실제 단일 모델의 문장형 보고 처리 → 같은 모델의 해석·근거 검증·조율 구성 → 단일 사건 비교 → 복수 사건 → 연속 사건과 복구 → 예비실험 → 설정 동결과 본 평가 순서다. 두 에이전트 구성은 같은 솔버·근거 조회·상태 갱신·검사·적용 경로를 사용한다. 역할 분리의 우수성을 전제하지 않는다.

본 평가의 사례 수·반복 수·시간 제한·수치 통과 기준은 예비실험 후 결정한다. 개발용 시간 제한이나 D01–D04의 값으로 본 평가 조건을 확정하지 않는다. 주 성과는 전체 평가 사건의 무개입 정답완결이며 생산 지연은 보조 지표다. 납기 우선 규칙 비교는 필요할 때 보조 실험으로 수행한다. OCR·3D·클라우드는 핵심 흐름에서 필요한 이유가 생겼을 때 검토한다.

## 다음 Codex 작업에 그대로 넣을 요청

```text
현재 정형 사건·가짜 모델 보고 어댑터와 186개 회귀 테스트를 유지하며,
DEC-07의 미결정 사항을 확인해 실제 LLM 연결을 준비해 주세요.
먼저 AGENTS.md, git status, 실제 코드·계약·검증 기록을 읽고 기존 변경을 보존하세요.
OpenAI 공식 문서에서 후보/가격/SDK 지원을 다시 확인하고, 모델 계정 권한과
보고·실행·프로젝트별 금액/토큰/시간 상한, 보고 외부 전송·보관 정책을 정하세요.
아직 승인되지 않은 유료 호출은 실행하지 말고, 필요한 결정만 구체적으로 요청하세요.
ReportModel 뒤에 얇은 공급자 어댑터를 연결하며 SDK 숨은 재시도와 병렬 도구 호출을
차단하고 refusal/incomplete/timeout/권한/속도 제한/과금 오류를 구별하세요.
모델은 현재 공개 근거 조회와 사건 제안만 수행하고 기존 controller·solver·checker·
evaluator를 공유하게 하세요. 정답·truth·미래 보고·복구 시각을 전달하지 마세요.
가짜 모델 회귀를 먼저 통과시키고, 명시적으로 합의한 소규모 유료 예비시험만 실행하세요.
언어 해석 정확도와 무개입 정답완결, 실제 사용량·비용을 별도로 기록하세요.
역할 분리 비교·본 비교실험·OCR·3D·클라우드·실제 로봇 제어는 확대하지 마세요.
```
