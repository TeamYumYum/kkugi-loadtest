# 로컬 테스트: User 재분배와 발생기 CPU

| 항목 | 값 |
|---|---|
| 날짜 | 2026-10-29 |
| 상태 | 일부 미결: `--enable-rebalancing` 채택(제안) |
| 환경 | 노트북 Intel Core Ultra 9 185H(논리 CPU 22), Windows, Python 3.12, Locust 2.47.0 |

## 배경

- 노트북 Worker가 피크 직전에 붙고 끝나면 떨어질 때 Locust가 User(실행기)를 어떻게 나누는지 몰랐다. 앞선 논의에서는 "새로 붙은 Worker에 User를 다시 나누지 않는다"고 설명했는데, 확인하지 않은 설명이었다.
- 관측 서버가 발생기를 감당할 수 있는지 판단하려면 요청 1건당 발생기 CPU가 필요했다.

## 결과 1: User 재분배

Master(User 10개)와 `base` Worker로 시작하고, 20초에 `peak-1` Worker를 붙였다가 45초에 강제 종료했다(노트북 연결이 끊긴 상황).

| 시점 | 재분배 옵션 끔(기본) | `--enable-rebalancing` |
|---|---|---|
| 처음 | base 10 | base 10 |
| peak-1이 붙은 뒤 | base 5, peak-1 5 | base 5, peak-1 5 |
| peak-1이 끊긴 뒤 | **base 5, 전체 5** | base 10, 전체 10 |

- **앞선 설명은 틀렸다.** 기본 설정에서도 새로 붙은 Worker에는 User를 다시 나눈다.
- 문제는 반대쪽이다. 기본 설정에서는 Worker가 끊기면 그 Worker의 User가 사라지고 남은 Worker로 돌아가지 않는다. 노트북이 떨어진 뒤 서버 Worker의 실행기가 절반으로 줄어든 채 남는다.
- `--enable-rebalancing`을 켜면 끊긴 뒤 5초 안에 서버 Worker가 다시 10개를 받았다.
- 제안: Master를 `--enable-rebalancing`으로 띄운다. Locust는 이 옵션을 "실험 기능"으로 표시한다.
- 확인하지 않은 것: 노트북 Worker를 정상 종료(Ctrl+C)할 때, 같은 노트북이 다시 붙을 때, 계정 풀이 겹쳐 거부된 Worker가 종료될 때.

## 결과 2: 요청 1건당 발생기 CPU

가짜 backend(약 2KB JSON, HTTP keep-alive)에 정해진 RPS로 요청하고, 안정 구간 30초 동안 Locust 프로세스의 CPU 시간을 쟀다.

| User 클래스 | 목표 RPS | 요청당 CPU | 코어 하나 대비 사용률 |
|---|---|---|---|
| `HttpUser` (지금 발생기) | 20 | 1.88ms | 3.7% |
| `HttpUser` | 100 | 1.88ms | 18.8% |
| `HttpUser` | 300 | 1.83ms | 54.9% |
| `FastHttpUser` | 20 | 0.99ms | 2.0% |
| `FastHttpUser` | 100 | 0.96ms | 9.6% |
| `FastHttpUser` | 300 | 0.93ms | 28.4% |

- 요청당 CPU는 RPS와 관계없이 거의 일정하다. `FastHttpUser`는 `HttpUser`의 절반이다.
- 같은 노트북에서 단일 스레드 점수가 두 번의 실행에서 44,613과 28,616으로 1.5배 차이 났다. 노트북은 전원 상태와 코어 종류(성능 코어, 효율 코어)에 따라 속도가 크게 흔들린다. 노트북 숫자를 서버에 그대로 옮기면 안 된다.
- 빠진 것: HTTPS 암호화, 우리 세션 코드(응답 검증, 일정표), 실제 응답 크기.

### 관측 서버에 대입한 추정

가정: 실제 발생기는 HTTPS와 세션 코드 때문에 위 값의 2~3배, 관측 서버의 코어는 이 노트북보다 최대 2배 느리다. 그러면 `HttpUser` 기준 요청당 약 6~12ms다.

| 계정 수 | 하루 평균 | 피크 |
|---|---|---|
| 20,000 (평균 약 6 RPS, 피크 약 22 RPS) | 코어 하나의 4~7% | 코어 하나의 13~26% |

관측 서버가 크레딧을 깎지 않고 계속 쓸 수 있는 CPU는 코어 하나의 40%다(vCPU 2개 × 기준선 20%). 보수적으로 잡아도 발생기는 하루 평균으로 이 예산의 20% 아래를 쓴다.

## 결과 3: 관측 서버에서 같은 측정

같은 스크립트를 관측 서버(Lightsail Medium, Intel Xeon Platinum 8175M 2.5GHz, vCPU 2개)의 Docker 컨테이너(`python:3.12-slim`, `--cpus=1`)에서 돌렸다.

| User 클래스 | 목표 RPS | 실제 RPS | 요청당 CPU | 코어 하나 대비 사용률 |
|---|---|---|---|---|
| `HttpUser` | 20 | 20.0 | 1.17ms | 2.3% |
| `HttpUser` | 100 | 99.9 | 1.06ms | 10.6% |
| `HttpUser` | 300 | 252.6 | 1.10ms | 27.9% |
| `FastHttpUser` | 20 | 20.0 | 0.55ms | 1.1% |
| `FastHttpUser` | 100 | 99.9 | 0.49ms | 4.9% |
| `FastHttpUser` | 300 | 299.9 | 0.47ms | 14.1% |

- **관측 서버가 노트북보다 요청당 CPU가 작았다.** 단일 스레드 점수도 53,547로 노트북(28,616~44,613)보다 높았다. 코어 성능 차이보다 운영체제 차이(Linux와 Windows)가 크게 작용한 것으로 보인다. 원인은 확인하지 않았다.
- `HttpUser` 300 RPS는 252.6 RPS에 그쳤다. 코어 사용률이 28%라 발생기 CPU 때문은 아니다. 같은 컨테이너(CPU 1개)에서 돈 가짜 backend가 원인일 수 있다. 원인은 확인하지 않았다.
- 측정하는 동안 관측 서버의 Lightsail CPU 사용률(5분 평균)은 3%에서 7~11%로 올랐고, 버스트 용량은 100% 근처를 유지했다.

관측 서버 값으로 다시 계산한다. 실제 발생기는 HTTPS와 세션 코드 때문에 2~3배라고 가정하면 `HttpUser` 기준 요청당 약 2.2~3.3ms다.

| 계정 수 | 하루 평균(약 6 RPS) | 피크(약 22 RPS) |
|---|---|---|
| 20,000 | 코어 하나의 1~2% | 코어 하나의 5~7% |

하루 평균은 지속 가능 예산(코어 하나의 40%)의 약 5%다. **계정 20,000개까지 발생기 CPU는 관측 서버의 병목이 아니다.** 남은 위험은 수집(Tempo·Loki)과 backend 쪽이고, 계정 1,000개 실측에서 확인한다.

## Lightsail 크레딧 조회 방법

AWS CLI `kkugi` 프로필(IAM 사용자 `kkugi-member`)로 두 서버의 지표가 조회된다.

```bash
aws lightsail get-instance-metric-data --profile kkugi --region ap-northeast-2 \
  --instance-name kkugi-observability-lightsail --metric-name BurstCapacityPercentage \
  --period 300 --start-time <UTC> --end-time <UTC> --unit Percent --statistics Minimum Average Maximum
```

- 지표: `BurstCapacityPercentage`(남은 크레딧 비율), `BurstCapacityTime`(남은 버스트 시간, 초), `CPUUtilization`
- 응답 필드 이름은 소문자(`average`, `minimum`, `maximum`)다
- 인스턴스: `kkugi-observability-lightsail`(medium_3_0), `kkugi-backend-lightsail`(large_3_0)
- 2026-10-29 기준 최근 24시간: 두 서버 모두 버스트 용량 99% 이상, 평균 CPU 3~8%. 앱 서버는 최대 94~99%까지 치솟는 구간이 여러 번 있었다. 배포 빌드로 보이며 확인 필요다. 실측 중에 겹치면 걸러 봐야 한다
