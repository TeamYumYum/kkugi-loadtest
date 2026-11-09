# kkugi-loadtest

꾸기의 사용자 행동을 재현하고 서비스의 안정성과 처리 성능을 검증하는 Locust 기반 인공 트래픽 발생기입니다.

계정마다 페르소나와 개인 특성을 배정하고, 식사 기록·음식 검색·요약 조회 등의 행동을 수행하도록 설계합니다. 일상적인 사용 패턴을 재현하는 상시 시뮬레이션과, 사용량을 높여 병목을 확인하는 부하 테스트를 함께 지원하는 것이 목표입니다.

> 현재 구성은 8,000명입니다. 다이어터 4,800명(기존 1,000 + 추가분 3,800), 운동 초보자·하루 몰아 기록형·운동 숙련자 1,000명씩, 휴면 직전 200명입니다. 기존 운동 세션을 공유하며, 최종 2만 명의 목표 비율과 이번 실행 인원은 구분합니다. 구성과 행동은 [페르소나 문서](docs/personas/)에 있습니다.

문서와 통계에서 쓰는 용어(Worker, 실행기, 풀, 배율, 거버너, TPS 등)는 [용어집](docs/glossary.md)에 모아 두었습니다.

## 목표

- 사용자별 페르소나, 인증 정보, 이전 행동과 개인 상태를 유지합니다.
- 식사 시간대와 사용자별 이용 빈도에 따라 자연스러운 트래픽을 발생시킵니다.
- API 응답뿐 아니라 식사 저장 등 실제 업무 결과를 검증합니다.
- 장시간 실행으로 안정성을 확인하고, 단계적으로 부하를 높여 성능 병목을 찾습니다.
- 동일한 시나리오와 데이터 조건에서 개선 전후의 성능을 비교합니다.

## 사용자와 부하 모델

`n`은 시뮬레이션 대상이 되는 **전체 고유 계정 수**입니다. 이번 구성은 `n=8,000`이며 다이어터 4,800명, 운동 초보자·하루 몰아 기록형·운동 숙련자 1,000명씩, 휴면 직전 200명을 배정합니다.

| 항목 | 의미 |
| --- | --- |
| 전체 계정 수 `n` | 페르소나와 개인 상태를 유지하는 고유 계정 수입니다. |
| DAU | 하루 동안 실제로 서비스를 이용한 고유 계정 수입니다. |
| 동시 활동 사용자 수 | 같은 시점에 업무 세션을 수행하는 사용자 수입니다. |
| 업무 세션 | 앱을 열고 점심 식사를 기록하는 등 한 번의 서비스 이용 흐름입니다. |
| TPS (= RPS) | 초당 처리한 HTTP 요청 수입니다. 로그인·토큰 재발급·온보딩 요청도 포함합니다. nGrinder에서 요청 하나를 테스트 하나로 둔 경우, JMeter의 샘플러 기준 처리량과 같은 뜻입니다. Locust 통계의 HTTP 요청 줄로 셉니다. |
| 세션/s | 초당 끝난 업무 세션 수입니다. 업무 처리량을 이 값으로 봅니다. 세션 하나는 요청 여러 개와 생각 시간을 포함하며, JMeter의 Transaction Controller로 묶은 단위에 해당합니다. Locust 통계의 `SESSION` 줄로 세고, 이미 기록한 끼니라 건너뛴 `(skipped)` 세션은 뺍니다. |
| DB TPS | 초당 DB 커밋 수입니다. MySQL 지표 `mysql_global_status_commands_total{command="commit"}`의 증가율로 보며, backend 전체 기준이라 인공 요청이 아닌 것도 포함됩니다. |

TPS라고만 쓰면 HTTP 요청 기준입니다. 세션 기준과 DB 기준은 각각 세션/s, DB TPS로 구분해 씁니다.

전체 계정 수와 Locust에서 실행 중인 User 수, 동시 활동 사용자 수는 구분합니다. 대기 중인 User가 존재할 수 있으며, 업무 세션은 로그인 세션이나 TCP 연결과도 다릅니다.

상시 시뮬레이션의 RPS는 계정 수, 시간대별 확률표, 페르소나와 계정의 활동 배수, 세션별 요청 수에 따라 달라집니다. 개별 요청의 시작 시각을 고정하기보다 사용자 행동을 모델링합니다. 시간대별 세션 수의 기대값은 `계정 수 × 그 시간대의 확률`입니다(실행 모델 참고).

### 계정별 정보

- **페르소나:** 시나리오 선택과 행동 경향을 정의합니다.
- **개인 프로필:** 목표 칼로리, 선호 음식, 평소 식사 시간, 접속 빈도 등을 유지합니다.
- **실행 상태:** 토큰, 기록한 식사, 응답에서 얻은 ID, 다음 활동 시점 등을 관리합니다.

## 분산 실행 구조

```text
관측 서버(deploy/docker-compose.yml)
 ├─ master
 ├─ worker-private (base 풀, 다이어터 950명) ─ 프라이빗 ──┐
 ├─ worker-public (public 풀, 다이어터 50명) ── 공인 ─────┤
 ├─ worker-beginner-private (운동 초보자 950명) ─ 프라이빗 ┤
 └─ worker-beginner-public (운동 초보자 50명) ── 공인 ────┤
                                                          ├─ nginx ─ backend
팀원 노트북(SSH 터널로 Master에 연결)                       │
 └─ 추가 Worker들 (peak-* 풀) ── 공인 주소 ────────────────┘

모든 Worker → 같은 Master에 연결하여 통계와 상태 보고
```

Master는 실행을 제어하고 통계를 집계합니다. 실제 가상 사용자와 HTTP 요청은 Worker에서 실행합니다. 팀원 노트북의 Worker는 계획한 피크 부하 실험에 활용합니다.

- Worker 추가는 트래픽 생성 자원을 늘리는 작업입니다. 실제 부하를 높이려면 활동 사용자 수나 세션 발생량도 함께 조절해야 합니다.
- 계정은 Worker가 설정의 계정 풀로 직접 맡습니다. Master는 Worker들의 계정 범위가 겹치는지만 확인하고, 겹치면 나중에 붙은 Worker를 거부합니다. Worker 재시작이나 이탈 시 계정 상태는 다른 Worker로 옮겨지지 않습니다.
- 상시 Worker는 열둘입니다. 8,000명 단계에서 다이어터 추가분은 `dieter-extra-private`(10,000-13,609)·`dieter-extra-public`(13,610-13,799), 휴면 직전은 `dormant-private`(50,000-50,189)·`dormant-public`(50,190-50,199) 풀을 씁니다. 하루 몰아 기록형은 `batch-private`(30,000-30,949)·`batch-public`(30,950-30,999), 운동 숙련자는 `expert-private`(40,000-40,949)·`expert-public`(40,950-40,999) 풀을 씁니다. 나머지는 2,000명 구성과 같습니다. 기존 `base` 풀 0-949번과 `public` 풀 950-999번은 다이어터로 유지합니다. 운동 초보자는 `beginner-private` 풀 1,000-1,949번, `beginner-public` 풀 1,950-1,999번을 사용합니다. 각 페르소나의 계정을 프라이빗·공인 경로에 95:5로 나눕니다. 실제 요청 비율은 계정별 일정에 따라 흔들립니다.
- 노트북 Worker는 `peak-*` 풀(`peak-1` 90,000-90,999, `peak-2` 91,000-91,999)을 씁니다. `peak-*` 풀의 계정은 피크 시간대에 계획된 세션만 실행합니다.
- 성능 측정 시 발생기를 대상 앱 서버와 분리하는 것을 원칙으로 합니다. 관측 서버에 함께 배치한다면 지표 수집에 영향을 주지 않도록 자원 여유를 확인합니다.

## 페르소나와 세션

다이어터는 끼니마다 식사를 기록하고 일부는 체중과 운동도 기록합니다. 운동 초보자는 루틴을 만들고 주 2-3회 운동을 기록하며, 나머지 접속에서는 홈을 조회합니다. 식사·체중·운동·조회 세션은 공통으로 두고 페르소나가 선택합니다. 자세한 규칙과 최종 2만 명의 목표 구성은 [docs/personas/](docs/personas/)에 있습니다. 하루 몰아 기록형과 운동 숙련자는 4,000명 단계에서, 휴면 직전과 다이어터 추가분은 8,000명 단계에서 더했습니다. 워치 유저는 없앴습니다.

## 관측과 검증

다음 지표를 함께 수집할 계획입니다.

- 전체 및 API별 요청 수, RPS, 응답 시간 p50/p95/p99, 실패 원인
- 예정·시작·완료·실패·누락 세션 수와 시작 지연
- 실제 DAU, 동시 활동 사용자 수, Worker별 실행 상태
- 앱 서버·DB·발생기의 CPU, 메모리, DB 연결과 쿼리 지연
- 네트워크 송수신량과 장시간 실행 중 자원 사용 추이

인공 요청의 양·응답 시간·오류는 backend 지표의 `synthetic` 태그로 Grafana에서 봅니다. 클라이언트 응답 시간과 세션 결과는 Locust 통계(웹 UI, CSV 기록)로 봅니다. 서버 CPU와 크레딧은 Lightsail 콘솔이나 AWS CLI로 봅니다. 발생기 통계를 Prometheus로 보내는 연동은 보류했습니다.

처음에는 소수 계정으로 기능과 상태 격리를 검증하고, `n=1,000` 규모로 하루 또는 이틀 실행합니다. 안정성과 실제 활동량을 확인한 뒤 계정 수를 늘립니다. 응답 시간과 오류율의 합격 기준은 초기 측정과 서비스 요구에 맞춰 명시합니다.

장시간 실행의 성공과 최대 처리량은 별도로 평가합니다. 처리 성능은 같은 시나리오·데이터·서버 조건에서 응답 시간과 오류율 기준을 만족하는 처리량으로 비교합니다.

## 구현 순서

구현 계획은 `notes/plan.md`, 설계 결정은 `notes/decisions/`에 있습니다.

## 실행 안내

### 준비

Python 3.12 이상이 필요합니다.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # macOS·Linux는 .venv/bin/python
.venv/Scripts/python -m pytest
```

대상 backend는 개발용 이메일 로그인(`AUTH_DEV_LOGIN_ENABLED=true`)이 켜져 있어야 합니다. 운영에서도 인공 트래픽용으로 켜져 있습니다(S15P31E101-342). 로컬 backend는 서비스 저장소의 `infra/docker-compose.yml`로 띄웁니다.

발생기는 모든 요청(로그인 포함)에 `X-Synthetic-Traffic: 1` 헤더를 붙입니다. backend는 이 헤더로 지표(`http.server.requests`의 `synthetic` 태그), 로그(`synthetic` 필드), 추적(span 속성)에서 인공 요청을 구분합니다. 인공 회원은 이메일이 `lt-<번호>@synthetic.kkugi.test`인 `EMAIL_DEV` 회원입니다.

`<번호>`는 `generation × 1,000,000 + 계정 번호`입니다. generation은 계정이 바깥에서 탈퇴되어 다시 만든 횟수이고 처음은 0입니다. 예를 들어 계정 42는 `lt-00042@...`이고, 탈퇴된 뒤에는 `lt-1000042@...`로 새 회원이 됩니다. 페르소나와 프로필은 계정 번호로 정하므로 바뀌지 않습니다. 발생기는 탈퇴 API를 부르지 않습니다.

### 구조

| 위치 | 역할 |
| --- | --- |
| `kkugi_load/personas/` | 페르소나. 계정의 프로필과 하루 세션 일정을 정합니다. 페르소나마다 파일 하나입니다 |
| `kkugi_load/sessions/` | 세션. 요청 흐름과 결과 검증입니다. 여러 페르소나가 함께 씁니다 |
| `kkugi_load/scheduler.py` | 한 프로세스가 맡은 계정들의 세션을 시각순으로 내줍니다 |
| `kkugi_load/accounts/` | 계정 번호에서 페르소나를, 계정 번호와 generation에서 이메일과 식사 UUID를 정하고, 토큰 등 상태를 SQLite에 저장합니다 |
| `kkugi_load/client/api.py` | backend 호출, 응답 해석, 토큰 재발급 |
| `kkugi_load/distributed.py` | Master가 Worker들의 계정 범위가 겹치지 않는지 확인합니다 |
| `kkugi_load/capacity.py` | 하루 일정에서 필요한 실행기(User) 수를 추정합니다 |
| `tools/stats_summary.py` | Master가 남긴 통계 CSV를 시간대별 TPS·세션/s 표와 API별 응답 시간 표로 요약합니다. 측정 기록(`notes/decisions/`의 기준선)을 같은 방법으로 다시 잴 때 씁니다 |
| `tools/daily_report.py` | 하루(한국 시간 00:00-24:00)의 트래픽(Locust history CSV)과 서버 응답 시간·자원(Prometheus)을 마크다운 한 장으로 요약하고, 기준을 넘은 항목을 맨 위 "주의"에 모읍니다. `--json`으로 저장한 파일을 다음 날 `--compare`에 주면 API별 p95가 1.5배 넘게 늘어난 것도 알려 줍니다. `--mattermost`는 주의 목록을 웹훅(`KKUGI_MM_WEBHOOK_URL`)으로 보냅니다. 노트북에서는 `ssh -L 19090:localhost:9090 kkugi-obs` 터널로 Prometheus를 엽니다 |
| `config/default.toml` | 시간대별 확률표, 활동 배수, 계정 풀, 페르소나 행동, 생각 시간 등 설정. "가정"으로 표시한 값은 확정 사양이 아닙니다 |

새 페르소나는 `personas/`에 파일을 만들고 `personas/__init__.py`에 등록한 뒤, 설정에 `[personas.<이름>]`을 더합니다. 새 세션 종류는 `sessions/`에 만들고 `runner.py`의 `SESSIONS`에 등록합니다.

### 실행 모델

Locust User는 계정이 아니라 **실행기**입니다. 각 프로세스는 맡은 계정들의 하루 일정을 만들고, 실행기는 시각이 된 세션을 꺼내 실행합니다. 따라서 User 수(`-u`)는 동시에 진행할 수 있는 세션 수이며 계정 수와 다릅니다. 전체 RPS는 정하지 않으며, 트래픽은 계정 수와 페르소나의 일정에서 나옵니다.

- `realtime`(기본): 계획 시각에 실행합니다. 발생기를 켜기 전에 지난 세션은 버리고, 계획보다 30분(설정값) 넘게 늦으면 놓친 세션으로 셉니다.
- `immediate`: 오늘의 세션을 시각과 관계없이 바로 실행하고, 단독 실행에서는 모두 끝나면 종료합니다. 로컬 검증용입니다.

**하루 일정.** 일반 접속은 시간대마다 `open_probability[h] × 페르소나 activity × 계정 활동 배수`의 확률로 정하고 무작위 분에 둡니다. 운동 초보자는 주간 목표 2-3회에 맞춰 운동 요일과 18-22시 완료 시각을 별도로 정하고, 그 시간대의 홈 조회를 운동 세션으로 대체합니다. 한 계정의 세션은 시간대마다 최대 하나이고 같은 계정·날짜면 재시작해도 같은 일정입니다.

| 설정 | 뜻 |
| --- | --- |
| `[traffic].open_probability` | 시간대마다 계정 하나가 앱을 열 확률, 24개. 계정 수 × 확률이 그 시간의 세션 수 기대값입니다 |
| `[traffic].activity_multipliers` | 계정 활동 배수 `[배수, 비중]` 목록. 계정마다 시드와 계정 번호로 하나를 고정합니다. 비중 가중 평균은 1이어야 합니다 |
| `[personas.<이름>].activity` | 페르소나 활동 배수(필수). 다이어터 1.0 |

칸 수, 확률 범위, 평균 배수, 곱한 확률의 최댓값이 1 이하인지는 설정을 읽을 때 검사하고, 어긋나면 시작하지 않습니다.

페르소나가 연 시각마다 어떤 세션을 고르는지는 [docs/personas/](docs/personas/)에 있습니다.

**실행기 수.** 각 Worker는 시간대별 `Σ(세션 수 × 평균 길이) ÷ 3600`의 최댓값에 여유 2배를 곱해 올림하고 필요량을 일정 로그에 남깁니다. 평균 길이는 추정값으로 식사 기록은 기본 약 14초, 조회는 1초입니다. 실행기가 모자라면 경고합니다. Master는 Worker에 고르게 나누므로 가장 바쁜 Worker의 필요량에 Worker 수를 곱한 값 이상으로 둡니다. 8,000명 구성(Worker 12개)은 총 540개입니다. 하루 몰아 기록형처럼 세션이 한 시간보다 짧은 창에 몰리면 이 추정이 실제보다 작게 나오므로 따로 계산합니다(`deploy/.env.example`의 주석).

한 계정의 세션은 동시에 둘 이상 실행하지 않습니다. 식사 UUID는 계정·날짜·끼니로 정해지므로, 이미 기록한 끼니는 저장하지 않고 `(skipped)`로 끝납니다.

### 단독 실행

```bash
KKUGI_SCHEDULE=immediate KKUGI_ACCOUNT_START=0 KKUGI_ACCOUNT_COUNT=10   .venv/Scripts/python -m locust -f locustfile.py --headless -u 3 -r 3 -t 10m   --host http://127.0.0.1:8080
```

### 분산 실행

```bash
# 서버에서는 아래 "서버 운영"의 compose를 씁니다. 같은 구성을 직접 띄우면 다음과 같습니다
locust -f locustfile.py --master --enable-rebalancing --web-host 127.0.0.1   --csv state/stats/kkugi --csv-full-history --host https://<대상>
KKUGI_POOL=base locust -f locustfile.py --worker --master-host 127.0.0.1
KKUGI_POOL=public KKUGI_ROUTE=public KKUGI_STATE_DB=state/public.db locust -f locustfile.py --worker --master-host 127.0.0.1

# 팀원 노트북: 풀마다 하나씩, 다른 노트북과 겹치지 않게
KKUGI_POOL=peak-1 locust -f locustfile.py --worker --master-host <Master 주소>
```

- Master의 User 수는 연결된 Worker들에 고르게 나뉩니다. 노트북 Worker를 붙일 때는 Worker마다 충분한 User가 가도록 Master의 User 수도 함께 늘립니다.
- Worker가 새로 붙으면 Locust가 User를 다시 나눕니다. 하지만 Worker가 응답 없이 끊기면, 기본 설정에서는 그 Worker의 User가 남은 Worker로 돌아가지 않아 전체 User 수가 줄어듭니다. Master를 `--enable-rebalancing`으로 띄우면 끊길 때도 다시 나눕니다(Locust 2.47.0, 로컬에서 확인). 이 옵션 없이 띄운 Master는 이유를 로그에 남기고 종료 코드 1로 끝납니다.
- Worker는 Master가 계정 범위를 승인하기 전에는 세션을 실행하지 않습니다. 겹친다고 거부된 Worker는 종료 코드 1로 끝납니다.
- 통계는 Master에 모입니다. `--csv <이름>`과 `--csv-full-history`를 주면 `<이름>_stats.csv`(누적), `<이름>_stats_history.csv`(API별 기록), `<이름>_failures.csv`, `<이름>_exceptions.csv`를 남깁니다. 기록 간격은 60초이고, 종료할 때 마지막 값을 한 번 더 씁니다(`kkugi_load/csv_stats.py`). Locust는 시작할 때 같은 이름의 파일을 비우므로, 서버 compose는 이름에 시작 시각을 붙입니다. 실측 기간의 클라이언트 응답 시간, 세션 결과, 놓친 세션은 이 파일로 보고, 서버 쪽 요청 양·응답 시간·오류는 backend 지표의 `synthetic` 태그로 봅니다.
- Master·Worker 연결 포트는 TCP 5557입니다. Windows에서는 `--processes`를 쓸 수 없으므로 Worker마다 프로세스를 따로 띄웁니다.

| 환경 변수 | 기본값 | 의미 |
| --- | --- | --- |
| `KKUGI_POOL` | `base` | 이 Worker가 맡을 계정 풀 이름(`config/default.toml`의 `[pools.*]`) |
| `KKUGI_ACCOUNT_START`, `KKUGI_ACCOUNT_COUNT` | 풀의 값 | 풀의 범위를 덮어씁니다. 로컬 검증용입니다 |
| `KKUGI_SCHEDULE` | `realtime` | `realtime` 또는 `immediate` |
| `KKUGI_ROUTE` | 없음 | 경로 표시. 값이 있으면 이 Worker의 HTTP 요청 통계 이름 앞에 `[값]`을 붙입니다(예: `[public] GET /foods`). 영문 소문자·숫자·하이픈만 씁니다. 경로 자체는 컨테이너의 이름 해석이 정하고, 이 값은 통계를 나눠 보는 용도입니다. 세션·일정·계정 통계에는 붙이지 않습니다 |
| `KKUGI_STATE_DB` | `state/accounts.db` | 계정 상태(generation, 토큰, 회원 ID, 온보딩 여부) SQLite 파일. 계정 번호가 키입니다. 옛 형식 파일이면 시작하지 않으니 지우고 다시 시작합니다. 저장소에 올리지 않습니다 |
| `KKUGI_CONFIG` | `config/default.toml` | 설정 파일 |

### 운영 중 조절

Master(단독 실행이면 그 프로세스)의 웹 UI에 조절 경로가 있습니다. 운영에서는 웹 UI를 `--web-host 127.0.0.1`로 띄우고 SSH 터널로만 접근합니다. 잘못된 값은 400과 이유(`{"error": ...}`)를 돌려줍니다.

```bash
curl http://localhost:8089/kkugi/control                  # 현재 배율, 설정 버전, Worker별 적용 상태
curl -X POST -d scale=0.5 http://localhost:8089/kkugi/scale  # 배율 바꾸기(0~1)
curl -X POST http://localhost:8089/kkugi/stop             # 정지(배율 0)
curl -X POST http://localhost:8089/kkugi/reload           # 설정 파일 다시 읽기
```

- **배율.** 계획된 세션을 이 확률로만 실행합니다. 세션마다 계정·날짜·시간대로 정해지는 값과 비교하므로, 배율을 바꿔도 같은 세션이 일관되게 남거나 빠지고 낮은 배율에서 남는 세션은 높은 배율에서도 남습니다. 걸러진 세션은 `SCHEDULE scaled_out`으로 세며 실패가 아닙니다. 배율을 다시 올리면 아직 시각이 되지 않은 세션만 실행됩니다. 시작 배율은 `[control] scale`이고, 설정을 다시 읽어도 배율은 바뀌지 않습니다.
- **설정 다시 읽기.** Master가 자기 설정 파일을 다시 읽어 검증한 뒤 원문을 모든 Worker에 보냅니다. Worker 쪽 설정 파일은 쓰지 않으므로 노트북마다 설정이 달라지지 않습니다.
  - `timezone`, `api_prefix`, `[accounts]`, `[auth]`, `[synthetic]`, `[pools]`가 시작할 때와 다르면 Master가 거부합니다. 바뀌면 계정의 이메일이나 범위가 달라지기 때문입니다. Worker도 자기 시작 설정과 다르면 적용하지 않고 오류를 남깁니다.
  - Worker는 오늘 일정을 다시 만들되, 지난 세션과 이미 실행한 세션은 넣지 않고 실행 중인 세션은 그대로 둡니다.
  - 계정의 페르소나는 시작할 때 정한 것을 유지합니다. 쓰이고 있는 페르소나를 설정에서 빼면 Worker가 적용에 실패하고 지금 일정을 유지합니다.
- **거버너.** Master(단독 실행이면 그 프로세스)가 `[governor] interval_seconds`(기본 60초)마다 최근 `window_seconds`(기본 300초)의 HTTP 요청 실패율과 p99를 봅니다. 실패율이 `max_failure_ratio`(기본 2%, 가정)를 넘거나 p99가 `max_p99_ms`(기본 3,000ms, 가정)를 넘으면 실제 배율을 절반으로 낮춥니다. `recovery_seconds`(기본 600초) 동안 정상이 이어지면 `recovery_step`(기본 0.1)씩 올리고, 상한은 사람이 `/kkugi/scale`로 정한 배율입니다.
  - 세션·일정·계정 통계(`SESSION`, `SCHEDULE`, `ACCOUNT`)는 보지 않습니다. 놓친 세션은 서버 문제가 아니고, 세션 시간에는 생각 시간이 들어 있기 때문입니다.
  - 창 안의 요청이 `min_requests`(기본 200건)보다 적거나, 배율을 바꾼 뒤 새 창이 아직 차지 않았거나, 통계를 초기화한 직후에는 판단하지 않습니다. 그래서 같은 나쁜 창으로 연달아 낮추지 않고, 요청이 적은 밤에 한두 건의 실패로 낮추지 않습니다.
  - 사람이 배율을 정하면 거버너는 그 값에서 다시 시작합니다. 상태와 마지막으로 바꾼 이유는 `GET /kkugi/control`의 `governor`에서 보고, 배율을 바꿀 때마다 이유를 로그에 남깁니다.
  - `[governor]`는 `/kkugi/reload`로 바꿀 수 있습니다. `enabled = false`로 끄면 사람이 정한 배율로 되돌립니다.
- 나중에 붙은 Worker는 계정 범위를 승인받을 때 현재 배율과 다시 읽은 설정을 함께 받고, 그것을 적용한 뒤에 세션을 실행합니다.

### 서버 운영

관측 서버(`ssh kkugi-obs`)에 `deploy/docker-compose.yml`로 Master 1개와 Worker 12개를 띄웁니다. 계정 블록 여섯(다이어터 기존·추가분, 운동 초보자, 하루 몰아 기록형, 운동 숙련자, 휴면 직전)마다 프라이빗·공인 경로 Worker 둘이며 상태 DB는 각각 다릅니다. 기존 `base.db`·`public.db`와 계정 범위는 유지하므로 상태 이동이나 초기화가 필요 없습니다. 명령은 모두 서버의 저장소 루트에서 실행합니다.

**처음 준비.** `deploy/.env.example`을 `deploy/.env`로 복사하고 값을 채웁니다. `.env`에는 앱 서버 프라이빗 IP가 들어가므로 커밋하지 않습니다.

| 값 | 뜻 |
| --- | --- |
| `APP_DOMAIN` | 대상 도메인. 두 Worker 모두 `https://<도메인>`으로 보냅니다 |
| `APP_PRIVATE_IP` | 앱 서버 프라이빗 IP. `worker-private`만 도메인을 이 주소로 해석합니다 |
| `KKUGI_USERS`, `KKUGI_SPAWN_RATE` | Master가 띄우는 전체 User 수와 초당 생성 수 |

**시작과 갱신.** 코드나 설정을 바꾼 뒤에도 같은 명령입니다. Master는 `--autostart`로 Worker가 붙으면 바로 User를 띄웁니다.

이번 구성에서는 `deploy/.env`의 `KKUGI_USERS=540`을 사용합니다. Worker 12개가 45개씩 받습니다. 가장 바쁜 `batch-private`가 23:00-23:30에 몰아 기록 세션을 몰아서 실행하기 때문이며(`deploy/.env.example`의 주석),
운영 실측으로 조정해야 합니다. 새로 더한 4,000명(다이어터 추가분 3,800, 휴면 직전 200)은 처음 사용하는 시점에 로그인·온보딩하고, 운동하는 다이어터는 첫 운동 때 루틴을 준비합니다.

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml ps
docker stats --no-stream
```

**멈추기.**

- 트래픽만 멈출 때: `curl -X POST http://localhost:8089/kkugi/stop`. 배율이 0이 되고 컨테이너는 그대로입니다. 다시 보내려면 `/kkugi/scale`로 배율을 올립니다.
- 모두 내릴 때: `docker compose -f deploy/docker-compose.yml down`. `down -v`는 상태 DB와 통계 CSV를 지우므로 쓰지 않습니다.
- Locust 웹 UI의 정지 버튼은 쓰지 않습니다. Master는 Worker가 모두 빠져 멈췄다가 Worker가 다시 붙으면 같은 User 수로 다시 시작하는데, 정지 버튼으로 멈춘 경우도 같은 방식으로 다시 시작될 수 있습니다.

**조절과 설정 다시 읽기.** 노트북에서 터널을 연 뒤 "운영 중 조절"의 명령을 그대로 씁니다. 설정을 바꿀 때는 서버의 `config/default.toml`을 고치고 `/kkugi/reload`를 보냅니다. 모든 컨테이너가 같은 `config/`를 읽기 전용으로 마운트합니다. 풀별 페르소나를 포함한 계정 풀 변경은 다시 읽기로 적용할 수 없고 재시작해야 합니다.

```bash
ssh -L 8089:localhost:8089 -L 5557:localhost:5557 kkugi-obs
```

**노트북 Worker 붙이기.** 위 터널을 연 상태에서, 노트북마다 다른 `peak-*` 풀로 띄웁니다. 붙이기 전에 웹 UI나 `/swarm`으로 Master의 User 수를 늘립니다. 노트북 한 대를 붙이면 Worker가 총 13개이므로 가장 바쁜 Worker 필요량의 열세 배 이상으로 둡니다.

```bash
KKUGI_POOL=peak-1 KKUGI_STATE_DB=state/peak-1.db .venv/Scripts/python -m locust -f locustfile.py --worker --master-host 127.0.0.1
```

**로그와 통계.** 로그는 컨테이너마다 최대 50MB까지 순환합니다. 통계 CSV는 `stats` 볼륨에 Master가 시작할 때마다 `kkugi-<YYYYmmdd-HHMMSS>_*.csv`로 새로 생깁니다.

```bash
docker compose -f deploy/docker-compose.yml logs -f --since 10m master
docker compose -f deploy/docker-compose.yml exec master ls -l /app/stats
docker compose -f deploy/docker-compose.yml cp master:/app/stats ./stats-backup
```

**서버 자원.** 컨테이너별 CPU와 메모리는 `docker stats`로, 서버 CPU와 버스트 크레딧은 Lightsail 콘솔이나 노트북의 AWS CLI로 봅니다. 앱 서버는 `--instance-name kkugi-backend-lightsail`로 봅니다.

```bash
aws lightsail get-instance-metric-data --profile kkugi --region ap-northeast-2   --instance-name kkugi-observability-lightsail --metric-name BurstCapacityPercentage   --period 300 --statistics Average --unit Percent   --start-time "$(date -u -d '-1 hour' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)"
```

**되돌리기.** 발생기를 내리려면 `down`만 하면 됩니다. 인공 계정이 쓰는 dev 로그인은 앱 서버 `infra/.env`의 `AUTH_DEV_LOGIN_ENABLED`로 끕니다(서비스 저장소 담당).

**로컬에서 같은 구성 확인.** `deploy/docker-compose.local.yml`을 겹치면 대상이 로컬 backend(`http://host.docker.internal:18080`)로 바뀝니다. 사용법은 파일 머리 주석에 있습니다.

### 경보와 하루 요약

경보는 사람이 바로 움직여야 하는 것만 보냅니다. 자주 울리는 경보는 무시되어 중요한 경보를 묻기 때문입니다. 용량 지표(DB 커밋률), 배포 때마다 생기는 backend 재시작, 발생기의 HTTP 실패(대부분 backend 오류라 5xx 경보와 겹침)는 경보로 보내지 않고 매일 9시 하루 요약의 주의 항목에 남깁니다.

경보는 두 곳에서 Mattermost로 보냅니다. 둘 다 상태가 바뀔 때만 보내고, 조건이 풀리면 해소를 한 번 보냅니다. 기준값은 모두 가정입니다.

**backend 경보: 관측 서버 Grafana** (폴더 "꾸기 경보", 그룹 `backend`, 받는 곳 `mattermost-kkugi`, 1분마다 평가)

| 경보 | 쿼리 요약 | 조건 | 지속 |
| --- | --- | --- | --- |
| backend 지표 수집 안 됨 | `min(up{job="backend"})` (값 없음도 경보) | < 1 | 2분 |
| 5xx 응답 | 최근 5분 5xx 증가분 + 새로 생긴 5xx 시계열 수 | > 0 | 즉시 |
| 응답 느림 | 인공 요청 p95(5분), 초당 0.2건 이상일 때만 | > 1초 | 5분 |
| 커넥션 풀 대기 | `max(hikaricp_connections_pending)` | > 0 | 1분 |

- 2026-11-09에 Grafana API로 만들었고, 서비스 저장소의 provisioning 파일에는 아직 없습니다. 규칙은 Grafana 데이터 볼륨에 있고, 내보낸 사본을 `deploy/grafana/alert-rules.yaml`에 둡니다. 화면에서 고치면 다시 내보내 이 파일을 갱신합니다. 나중에 서비스 저장소 `infra/observability/grafana/provisioning/alerting/`로 옮깁니다.
- 받는 곳은 Slack 형식(Mattermost 수신 웹훅이 받는 형식)이고, 기본 알림 정책이 이 받는 곳으로 보냅니다.

**발생기 경보: `alerts` 컨테이너** (`tools/alert_watch.py`, 1분마다 Master의 `/kkugi/control`을 봄)

| 경보 | 조건 |
| --- | --- |
| 거버너 배율 낮춤 | 실제 배율이 사람이 정한 배율보다 낮음 |
| Worker 응답 없음 | 살아 있지 않은 Worker, 2분 |
| Master 조회 실패 | 3번 연달아 실패 |

- Prometheus에 없는 발생기 상태라 Grafana로 옮기지 않습니다.
- 감시가 죽었는지 알 수 있게 매일 9시에 "동작 중"을 보냅니다. 이 메시지가 오지 않으면 감시가 죽은 것입니다. 시작 알림은 배포할 때마다 오고 할 일이 없어서 컨테이너 로그에만 남깁니다. 9시 메시지에는 전날의 하루 요약(`tools/daily_report.py`)을 함께 담아 하루 한 번만 보내고, 요약 JSON을 `reports` 볼륨에 남겨 다음 날 p95 비교에 씁니다.
- 웹훅 주소는 서버의 `deploy/.env`에 `KKUGI_MM_WEBHOOK_URL`로 넣습니다. 비어 있으면 compose 전체가 멈춥니다. 경보 장치가 빠진 채 발생기만 도는 일을 막기 위해서입니다.
- 감시를 다시 시작하면 아직 이어지는 경보를 한 번 더 보냅니다. 상태를 메모리에만 두기 때문입니다.

**웹훅 만들기.** Mattermost에서 받을 채널의 웹훅을 만듭니다.

1. 왼쪽 위 팀 이름 옆 메뉴(또는 제품 메뉴)에서 **통합(Integrations)**을 엽니다.
2. **Incoming Webhooks(수신 웹훅)** → **추가(Add Incoming Webhook)**를 누릅니다.
3. 제목(예: 꾸기 인공 트래픽 경보)을 적고, 받을 채널을 고르고, 저장합니다.
4. 화면에 나온 `https://<Mattermost 주소>/hooks/<키>`가 웹훅 주소입니다. 비밀값이므로 채팅과 저장소에 붙이지 않고 서버의 `deploy/.env`에만 넣습니다.
5. 메뉴에 통합이 보이지 않으면 시스템 관리자가 수신 웹훅을 꺼 두었거나 통합 관리 권한을 막은 것입니다. 관리자에게 요청합니다.

웹훅을 넣은 뒤 시험으로 하나 보내 볼 수 있습니다.

```bash
curl -s -X POST -H 'Content-Type: application/json' -d '{"text":"경보 시험"}' "$KKUGI_MM_WEBHOOK_URL"
```

### 통계 읽는 법

| 이름 | 의미 |
| --- | --- |
| `GET /...`, `POST /...` | 세션 안의 요청. 경로는 식별자 없이 템플릿으로 남깁니다 |
| `[auth] ...`, `[setup] ...` | 토큰 발급과 계정 준비(온보딩). 세션 요청과 나눠 봅니다 |
| `SESSION dieter_breakfast`, `dieter_lunch`, `dieter_dinner` | 식사 기록 세션 하나의 성공·실패와 걸린 시간(생각 시간 포함). 업무 결과 검증 실패도 여기서 실패로 셉니다 |
| `SESSION dieter_view` | 조회 세션(요약·목표·성장) 하나의 성공·실패와 걸린 시간 |
| `SESSION beginner_workout`, `beginner_view` | 운동 초보자의 공통 운동 기록과 홈 조회 성공·실패 |
| `SESSION batch_night`, `batch_view` | 하루 몰아 기록형의 몰아 기록(끼니·체중·운동을 이어서 기록)과 홈 조회 |
| `SESSION expert_workout`, `expert_<끼니>`, `expert_weight`, `expert_view` | 운동 숙련자의 운동·식사·체중 기록과 홈 조회 |
| `SESSION ... (skipped)` | 이미 기록한 끼니라 저장하지 않은 세션 |
| `SCHEDULE start_delay` | 계획 시각보다 늦게 시작한 정도. 실행기가 모자라면 늘어납니다 |
| `SCHEDULE missed` | 너무 늦어 실행하지 않은 세션(실패로 셉니다) |
| `SCHEDULE scaled_out` | 배율로 걸러 실행하지 않은 세션(실패가 아닙니다) |
| `ACCOUNT withdrawn` | 계정이 바깥에서 탈퇴되어(401 `MEMBER_WITHDRAWN`) 세션을 끝낸 경우. 서버는 정상 동작했으므로 실패로 세지 않습니다. 다음 세션이 새 이메일로 가입하고 온보딩합니다 |

식사 세션은 저장한 식사의 UUID·끼니·음식·양·날짜, 다른 데이터가 섞이지 않았는지, 요약의 섭취 칼로리가 저장한 식사만큼 늘었는지를 확인합니다.

인증 토큰과 비밀값은 저장소에 포함하지 않습니다.

## 참고

- [Locust 공식 문서](https://docs.locust.io/en/stable/)
- [시나리오 작성](https://docs.locust.io/en/stable/writing-a-locustfile.html)
- [분산 실행](https://docs.locust.io/en/stable/running-distributed.html)
