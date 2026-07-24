# EviWatch Laptop MVP

Windows 노트북에서 안전하게 적응형 관측을 시험하기 위한 작은 실험 세트다.

- Kubernetes, Docker, 방화벽, 실제 디스크 용량을 건드리지 않는다.
- 루프백 주소 `127.0.0.1:18080`의 테스트 HTTP 서비스만 실행한다.
- 모든 결과는 `runs/` 아래 새 폴더에 저장된다.

## 첫 실험

PowerShell에서 저장소 루트로 이동한 뒤 실행한다.

```powershell
python experiments\eviwatch_lab\run_trial.py `
  --policy fixed-high `
  --fault service-stop
```

동일 장애를 적응형 정책으로 실행한다.

```powershell
python experiments\eviwatch_lab\run_trial.py `
  --policy adaptive `
  --fault service-stop
```

두 실행은 기본 설정에서 각각 약 32초 걸린다.

## CPU 포화 실험

`cpu-pressure`는 지정 시간 동안 CPU 연산 프로세스를 실행한 뒤 자동 종료한다.
기본값은 논리 CPU 수의 절반만 사용한다.

```powershell
python experiments\eviwatch_lab\run_trial.py `
  --policy adaptive `
  --fault cpu-pressure `
  --cpu-workers 4
```

## 결과 읽기

각 run 폴더에는 다음 파일이 생긴다.

```text
ground_truth.json      장애 정답과 필수 증거
metrics.csv            시간별 CPU/RAM/디스크/health 관측값
policy-events.jsonl    장애 주입과 정책 상태 전이
retained-events.jsonl  실제 보존된 서비스 로그
summary.json           파일별 저장량
```

첫 비교에서 볼 것은 두 가지다.

1. `adaptive`의 `summary.json`이 `fixed-high`보다 작아졌는가?
2. `adaptive`에도 health 실패와 서비스 종료 관련 증거가 남았는가?

현재 `adaptive` 정책은 아주 단순하다.

```text
NORMAL: 5초 간격, WARN/ERROR 로그만 보존
INCIDENT: health 실패 또는 CPU 70% 이상이면 1초 간격, 모든 로그 보존
```

이 정책이 효과가 있는지 확인하는 것이 첫 MVP의 목적이다. 수치가 좋다고
바로 논문이 되는 것은 아니며, 이후 디스크 예산·증거 우선순위·여러 장애를
추가해 연구 설계로 확장한다.
