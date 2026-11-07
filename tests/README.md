# 개발 검증

정형 사건과 가짜 모델 문장형 보고의 단위·통합·오류 주입 검사를 구현했다. **2026-11-07 현재 186 passed(기존 117 + 추가 69), 실패·오류·skip 0**이며 [문장 어댑터 증거](evidence/2026-11-07-reports.json)에 기록했다. 2026-11-06 새 uv 환경에서 **117 passed, 실패 0, 오류 0, skip 0**을 확인했다. 실제 실행 기록과 환경/코드 해시는 [검증 증거 JSON](evidence/2026-11-06.json), M01–M10 연결은 [다음 작업](../docs/next-steps.md)에 있다.

```sh
uv sync --locked
uv run --locked pytest -q
```

| 파일 | 검증 대상 |
| --- | --- |
| `test_contracts.py` | strict 타입·ID·추가 필드·중복 JSON key·NaN·선행 DAG·이동 시간 행렬 |
| `test_repository.py`, `test_events.py` | 중복/충돌, immutable 근거, 현재 근거 조회, 제한별 복구, 과거/잘못된 회차·반증 근거 거부 |
| `test_constraints.py`, `test_scheduler.py` | 필수/제외, 진행 고정, continuation, CP-SAT 해, 단계별 incumbent·해 상태 |
| `test_checker.py` | 솔버 함수와 독립된 검사. 보류/능력/선행/로봇·구역 중복/이동·준비/진행 변경/구역 예약 위반 주입 |
| `test_application.py` | 검사–적용 사이 갱신, 실제 두 연결 동시 CAS, 저장 중 실패 rollback, 제한된 재시도, 중복 착수·실패 정책 |
| `test_simulator.py`, `test_review_regressions.py` | 완료→외부 사건→착수 순서, 이동/준비 중 취소, 위치·진행 W18 보존, 초기 실제 상태 일관성 |
| `test_information_boundary.py` | 공개 로더의 실제 파일 접근, 금지 의존관계, extra-field 거부와 미래/oracle/truth sentinel이 솔버 입력에 없는지 검사 |
| `test_integration.py` | D01–D04 전체 실행·별도 평가·CLI 조회·출력 덮어쓰기 거부 |
| `test_reports.py` | 보고 strict 계약·현재 근거 조회·권한/대상/인용 검증·오류 fallback·호출/조회/크기 상한·수리와 실제 완료 구분 |
| `test_reports_integration.py` | 정형/fake D01–D04 실행과 독립 채점 동등성, 모델/솔버 미래정보 경계, 보고 중복·충돌·stale·원자 rollback, CLI와 manifest |
| `test_evaluation.py` | 수작업 허용 실행과 잘못된 실행·복구·검사기 판정 등을 주입하여 독립 채점·추적·해시 검사 |

`pytest` console entry와 `python -m pytest`의 경로 차이를 해결하기 위해 pytest 설정에서 프로젝트 루트를 추가했다. 평가 모듈을 테스트가 import하기 위한 설정이며 운영 패키지가 평가 자료를 읽도록 만든 것이 아니다.

실제 CP-SAT의 정상 해와 작은 모순 모델의 INFEASIBLE을 실행했다. UNKNOWN 및 이전 유효해를 보존한 FEASIBLE_NOT_PROVEN 분기는 명시적으로 오류를 주입했다. 실제 시간 초과나 성능 한계를 측정했다고 해석하지 않는다.

`outputs/verification-20261106-final/pytest.xml` 및 D01–D04 DB/CSV/JSONL은 로컬 생성물이며 Git에서 제외한다. 저장소에 남는 `evidence/2026-11-06.json`에는 테스트별 이름, 실제 결과, 실행 요약, 모든 생성물 해시와 재현 명령이 있다. 문장형 어댑터의 추가 검증은 [2026-11-07 증거](evidence/2026-11-07-reports.json)와 다음 작업 문서에 별도로 남긴다. 가짜 모델은 미리 정한 응답만 돌려주므로 실제 자연어 정확도·API 성능 검증이 아니다. 다른 OS·실물 로봇·본 비교실험은 검증하지 않았다. 사람 팀의 개발 정답표 교차 검토도 남아 있다.
