# 첫 노트북 실험 기록

## 실험

- 장애: 테스트 HTTP 서비스 강제 종료 후 자동 복구
- 정상 구간: 8초
- 장애 구간: 10초
- 복구 구간: 8초
- 비교 정책: `fixed-high`와 `adaptive`

## 관찰

`fixed-high`는 1초마다 관측해 장애 직후부터 `health_ok=False`와
`URLError`를 기록했다.

`adaptive`는 정상 상태에서 5초 간격으로 수집하다가 health 실패를 처음
관찰한 뒤 `INCIDENT` 상태로 전환했고, 이후 1초 간격 수집으로 바뀌었다.
이번 실행에서 장애 주입 뒤 첫 전환까지는 약 2초였다.

## 중요한 발견

테스트 서비스를 Windows `terminate()`로 강제 종료했기 때문에 서비스는
자기 자신의 `service_stopped` 로그를 남기지 못했다. 이는 오류가 아니라
강제 종료·프로세스 crash 상황에서 실제로 생길 수 있는 현상이다.

따라서 이 종류의 장애에서 Ground Truth 증거는 다음처럼 잡아야 한다.

```text
- 관측기의 fault_injected 이벤트
- health_ok=False
- health_error=URLError
- 복구 뒤 health_ok=True
```

서비스 자체의 마지막 로그만으로 장애 원인을 판단하게 설계하면, abrupt crash를
놓칠 수 있다. 시스템 상태·health probe·수집기 이벤트를 함께 보존해야 한다는
근거가 된다.

## 아직 결론 내리면 안 되는 것

두 실행은 단 한 번씩만 돌렸고, 수집 간격도 짧게 설정한 MVP다. 저장량 차이나
정책의 우수성을 결론 내릴 단계는 아니다. 다음에는 동일 실험을 여러 번 반복하고
CPU 포화와 안전한 저장공간 압박 시나리오를 추가한다.
