# 독립 사후 평가

`score.py`와 D01–D04의 `oracle.json`, `truth.jsonl`을 구현했다. 원문·개발 가정에서 평가 자료를 별도로 작성하고 실행 결과에서 정답을 역생성하지 않았다. 작성/코드 교차 검토는 별도 에이전트가 수행했으며 **사람 팀의 교차 검토는 미완료**다.

```sh
uv run --locked python evaluation/score.py --run-dir outputs/D01 --oracle evaluation/dev/D01/oracle.json --truth evaluation/dev/D01/truth.jsonl
```

평가기는 표준 라이브러리만 사용하고 `shipyard.checker`·제약 생성·솔버를 import하지 않는다. `summary.json`의 성공 여부나 검사기의 PASS를 정답으로 쓰지 않는다. 실제 `execution.csv`와 `trace.jsonl`을 oracle·평가자 환경 사실에 대조한다. 완료 CSV가 누락한 착수도 PHASE_START와 대조하여 검출한다.

- `checker.py`: 현재 공개 근거로 새 지시를 실행해도 되는지 판단한다.
- `evaluation/score.py`: 실행 종료 후 독립 규칙과 실제 이력으로 올바른 행동인지 채점한다.
- `oracle.json`: 보류 범위·필수 완료/미착수·대기/확인 요청·복구 후 재편입 조건이다.
- `truth.jsonl`: 평가자만 읽는 초기 실제 조건과 외부 복구 사실이다.

출력 `evaluation.json`, `evaluation.csv`에는 사건별 정답 행동, 무개입 완결, 보류·재편입, 금지 계획의 제안/차단/실행, 이유와 trace 참조가 포함된다. 평가자 코드·oracle·truth·실행 CSV·trace의 SHA256, 평가 시각·명령도 기록한다. 명령은 조건 충족 시 0, 평가 실패 시 1, 입력 오류 시 2로 종료한다.

개발 평가에서는 분모에 전체 접수 사건을 넣고, 사례의 복구·재편입 의무까지 충족해야 사건 완결로 본다. D04의 재확인 요청은 올바른 행동이므로 개발 수락은 통과하지만 사람 응답이 필요해 무개입 완결에는 포함하지 않는다. 본 평가의 시간 제한은 아직 적용하지 않았다(`deadline_applied=false`). 현재 값은 개발 수락 결과이며 연구 성능 추정값이 아니다.

D01–D04 실제 결과는 [검증 기록](../docs/next-steps.md), 평가 오류 주입은 [test_evaluation.py](../tests/test_evaluation.py), 입력 경계 검증은 [test_information_boundary.py](../tests/test_information_boundary.py)에 있다. 미래 사건은 driver/simulator만 읽고 도착한 근거만 controller에 전달한다. 폴더 분리만으로 정보 차단을 주장하지 않으며 악성 Python에 대한 OS 격리는 없다.

후속 주 비교는 단일 에이전트+솔버와 역할 분리 에이전트+동일 솔버다. 같은 모델·자료·규칙·검사·호출/토큰 상한을 쓰고 역할 분리의 우수성을 전제하지 않는다. 사례 수·반복·시드·제한시간·수치 통과 기준·계산시간 반영은 예비실험 이후 고정한다. 생산 지연과 유효 WELD_START까지의 재개시간은 보조 지표다. LLM과 본 비교실험은 이번에 실행하지 않았다.
