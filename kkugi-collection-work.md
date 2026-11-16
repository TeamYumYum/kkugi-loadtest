# 서비스 저장소 수집 작업: 인공 트래픽의 관측 부담 줄이기

| 항목 | 값 |
|---|---|
| 작성일 | 2026-10-29 |
| 대상 저장소 | 서비스 모노레포 `kkugi`의 `infra/` |
| 확인 기준 | `origin/main` `cc7a96c4` |
| 관련 | `kkugi-loadtest` 저장소 `notes/decisions/2026-10-29-start-scale-and-load.md`, 운영 문서 `docs/operations/synthetic-traffic.md` 6.2·7장 |

> **모두 선택 작업이다(2026-10-29 결정).** 서버 CPU와 크레딧은 Lightsail 콘솔이나 AWS CLI로 본다. 추적·로그 샘플링(2.1, 2.2)은 계정 1,000개 실측에서 수집이 버티지 못할 때만 한다. 발생기 지표 수집(2.5)은 발생기 쪽 `/metrics`가 보류되어 지금은 할 일이 없다.

인공 트래픽이 늘면 backend가 남기는 추적·로그도 같은 비율로 늘고, 이것을 관측 서버(Lightsail Medium 4GB)가 받아 저장한다. 발생기를 어디서 돌리든 이 부담은 줄지 않는다. 이 문서는 그 부담을 줄이고 잴 수 있게 하는 서비스 저장소 쪽 작업을 정리한다.

## 1. 지금 구성

```text
앱 서버                                         관측 서버
 backend ──추적(OTLP)──▶ Alloy ──추적─────────▶ Tempo
         ──로그(Docker)─▶  │   ──로그─────────▶ Loki
         ◀──지표 수집────  │   ──지표(remote write)▶ Prometheus
```

| 항목 | 지금 상태 | 위치 |
|---|---|---|
| 추적 샘플링 | 설정이 없다. OTel Java Agent 기본값대로 모든 요청을 추적한다 | `infra/docker-compose.app-server.yml`의 backend 환경 변수에 `OTEL_TRACES_SAMPLER`가 없다 |
| 추적 처리 | actuator 경로 제외, 민감 정보 가림, 묶음 전송만 한다 | `infra/observability/alloy/config.alloy`의 `otelcol.processor.filter`, `transform`, `batch` |
| 로그 처리 | JSON을 풀어 라벨과 구조화 메타데이터를 단다. 걸러내는 단계는 없다 | `config.alloy`의 `loki.process "backend"` |
| 서버 CPU·메모리 수집 | 찾지 못했다. Alloy에 `prometheus.exporter.unix`가 없고, compose에 node-exporter도 없다 | `config.alloy`, 두 compose 파일 |
| 관측 컨테이너 자원 상한 | 없다 | `infra/docker-compose.observability.yml` |

## 2. 작업

### 2.1 인공 요청의 추적을 일부만 남긴다

운영 문서 6.2절에서 관측 데이터 중 가장 큰 몫이 추적이다. 인공 요청의 추적은 일부만 남기고, 오류와 느린 요청은 모두 남긴다.

- 앱 서버 Alloy에 tail sampling(추적이 끝난 뒤 내용을 보고 남길지 정하는 방식)을 더한다. 후보 구성 요소는 `otelcol.processor.tail_sampling`이다. 정책 예시는 다음과 같다.
  - 오류가 난 추적은 모두 남긴다
  - 느린 추적(예: 1초 이상)은 모두 남긴다
  - 인공 요청(구분 헤더가 있는 요청)은 10%만 남긴다
  - 그 밖의 요청(실사용자)은 모두 남긴다
- 앱 서버에서 거르므로 관측 서버로 가는 전송량과 Tempo 저장량이 함께 줄어든다.
- `OTEL_TRACES_SAMPLER`로 backend에서 미리 거르는 방식(head sampling)은 쓰지 않는다. 요청 시작 시점에 정하므로 오류가 날 추적인지 알 수 없고, 실사용자 요청까지 같은 비율로 줄어든다.
- 확인 필요: Alloy v1.20.1에서 이 구성 요소의 정책 이름과 설정 형식, 앱 서버 메모리 사용량(추적이 끝날 때까지 잠시 들고 있어야 한다).

### 2.2 성공한 인공 요청의 로그를 일부만 남긴다

- `loki.process "backend"`에 인공 요청이면서 오류가 아닌 로그만 일정 비율로 남기는 단계를 더한다. 후보는 `stage.match`와 `stage.sampling`이다.
- 오류 로그와 느린 요청 로그는 모두 남긴다.
- 확인 필요: 로그 줄에서 인공 요청인지 알 수 있어야 한다. 지금 요청 로그에는 그런 필드가 없다. backend가 구분 헤더 값을 MDC(로그 문맥 값)에 넣어 JSON 필드로 남겨야 한다.

### 2.3 두 서버의 CPU·메모리를 수집한다

실측(계정 1,000개)에서 발생기와 수집이 관측 서버를 얼마나 쓰는지 봐야 하고, 거버너(발생기가 실패율·지연을 보고 스스로 배율을 낮추는 장치)도 이 값을 볼 수 있으면 좋다.

- 관측 서버와 앱 서버의 Alloy에 `prometheus.exporter.unix`를 더하거나, node-exporter를 둔다.
- 컨테이너별 사용량이 필요하면 cAdvisor 같은 수집기를 검토한다. 발생기, Tempo, Loki가 각자 얼마나 쓰는지 나눠 봐야 하기 때문이다.
- Lightsail CPU 크레딧 잔량은 서버 안에서 잴 수 없고 Lightsail 지표로만 보인다. AWS CLI `kkugi` 프로필로 조회된다(2026-10-29 확인): `aws lightsail get-instance-metric-data --profile kkugi --region ap-northeast-2 --instance-name kkugi-observability-lightsail --metric-name BurstCapacityPercentage --period 300 --unit Percent --statistics Average --start-time … --end-time …`. 응답 필드 이름은 소문자(`average`)다. 남은 시간은 `BurstCapacityTime`(초)이다. 대시보드로 가져올지는 정한다.

### 2.4 관측 컨테이너 자원 상한 (검토)

관측 서버에서 발생기와 수집 스택이 CPU를 나눠 쓴다. 발생기에는 이 저장소에서 상한을 건다. 수집 스택에 상한을 걸지는 2.3절로 실측한 뒤 정한다. 근거 없이 걸면 Loki나 Tempo가 메모리 부족으로 재시작될 수 있다.

### 2.5 발생기 지표 수집

보류. 발생기 쪽 `/metrics`를 만들기로 하면 그때 한다. 발생기 Master가 관측 서버에서 Prometheus 형식의 `/metrics`를 연다. 관측 서버 Prometheus의 수집 대상에 이것을 더한다.

- 위치: `infra/observability/prometheus/prometheus.yml`
- 확인 필요: 발생기 컨테이너와 Prometheus 컨테이너가 닿는 방법(같은 Docker 네트워크에 붙일지, 호스트 포트로 볼지). 발생기의 compose 구성이 정해진 뒤에 정한다

## 3. 선행 조건

- 2.1과 2.2에 필요한 구분 헤더는 끝났다(S15P31E101-342, MR !249). 헤더 `X-Synthetic-Traffic: 1`이 지표 태그 `synthetic`, 로그 필드 `synthetic`, span 속성 `http.request.header.x-synthetic-traffic`으로 남는다.
- 발생기는 `config/default.toml`의 `[synthetic]`에 헤더 이름을 넣으면 모든 요청에 헤더를 붙인다(발생기 구현 계획 0-1).

## 4. 순서

1. 2.5 발생기 지표 수집. 발생기 `/metrics`가 생긴 뒤에 한다
2. 2.3 서버 CPU·메모리 수집. 있으면 계정 1,000개 실측이 정확해진다. 없으면 Lightsail 지표와 `docker stats`로 대신한다
3. 2.1 추적 샘플링
4. 2.2 로그 샘플링
5. 실측값을 보고 2.4를 정한다

## 5. 하지 않는 것

- 실사용자 요청의 추적·로그는 줄이지 않는다
- 지표(Prometheus)는 샘플링하지 않는다. 저장량이 요청 수가 아니라 시계열 수로 정해지므로 인공 트래픽이 늘어도 거의 같다
