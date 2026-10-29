# 구현 계획

마지막 갱신: 2026-10-29. 숫자와 근거는 결정 기록을 본다.

- 트래픽 모양: [시간대별 표](decisions/2026-10-29-hourly-table.md), [트래픽 모양과 규모](decisions/2026-10-29-traffic-shape.md)
- 규모와 서버: [시작 규모와 관측 서버 부담](decisions/2026-10-29-start-scale-and-load.md), [로컬 테스트](decisions/2026-10-29-local-tests.md)
- 운영 연결: [backend 준비 완료와 재분배 채택](decisions/2026-10-29-backend-ready.md)

## 진행 규칙

- 작업 하나가 커밋 하나다. 고칠 일이 생기면 새 커밋으로 고친다.
- 기준선: `pytest` 23 passed, skipped 0 (2026-10-29). 작업마다 통과 수가 늘고 실패·건너뜀은 0이어야 한다.
- 실제 backend가 필요한 확인은 작업 안에 "실제 확인"으로 따로 적는다. 로컬 backend(Docker)나 운영 backend 중 어디서 했는지 기록한다.

## 전체 순서

```text
단계 0  운영 연결 값        ─┐
단계 1  시간대별 표          ├─▶ 단계 3 배포 ─▶ 단계 4 실측 1,000 ─▶ 10,000 ─▶ 20,000
단계 2  운영 안전장치       ─┘
```

단계 0~2는 서로 독립적인 부분이 많지만, 같은 파일(`config/default.toml`, `scheduler.py`)을 여러 작업이 고치므로 순서대로 한다.

## 단계 0. 운영 연결 값

### 0-1. 구분 헤더와 이메일 확정 반영
- 고칠 곳: `config/default.toml`의 `[synthetic]`(`header_name = "X-Synthetic-Traffic"`), `[accounts]`의 "확인 필요" 주석, README의 "운영에서는 dev 로그인이 꺼져 있다" 문장
- 테스트: 설정을 읽으면 헤더가 `("X-Synthetic-Traffic", "1")`이다. 로그인 요청을 포함해 모든 요청에 헤더가 붙는다(가짜 HTTP 클라이언트로 확인)
- 완료: 테스트 통과

### 0-2. 재분배 옵션 강제
- 고칠 곳: `kkugi_load/distributed.py`의 `setup_master`, README 분산 실행 명령
- 내용: Master가 `--enable-rebalancing` 없이 뜨면 이유를 남기고 시작하지 않는다. 옵션을 빠뜨리면 실행기가 조용히 줄어드는 구조라서, 경고만 남기지 않고 실패시킨다
- 테스트: 옵션 확인 함수가 꺼짐을 거부하고 켜짐을 통과시킨다
- 완료: 테스트 통과, README 명령에 옵션 반영

### 0-3. 탈퇴된 계정 다시 만들기
- 고칠 곳: `kkugi_load/accounts/state.py`, `kkugi_load/accounts/identity.py`, `kkugi_load/client/api.py`, `kkugi_load/runner.py`
- 내용
  - 상태 DB에 `generation`(다시 만든 횟수)을 더하고, 기본 키를 이메일에서 계정 번호로 바꾼다
  - 이메일 번호 = `generation × 1,000,000 + 계정 번호`. 예: 계정 42를 처음 다시 만들면 `lt-1000042@synthetic.kkugi.test`. 페르소나와 프로필은 계정 번호로 정하므로 바뀌지 않고, 식사 UUID는 이메일로 정하므로 새 회원의 것과 겹치지 않는다
  - 어느 요청에서든 401 `MEMBER_WITHDRAWN`을 받으면 `generation`을 올리고 토큰·회원 ID·온보딩 여부를 지운 뒤 저장한다. 그 세션은 `ACCOUNT withdrawn`으로 기록하고 끝낸다. 다음 세션이 새 이메일로 가입한다
  - 운영 상태 DB가 아직 없으므로 마이그레이션은 만들지 않는다. 옛 형식의 DB를 만나면 지우라는 메시지와 함께 시작하지 않는다
- 테스트: 이메일 번호 계산, 탈퇴 응답을 받으면 상태가 바뀌는지(가짜 응답), 다시 읽은 상태에 `generation`이 남는지
- 실제 확인: 로컬 backend에서 계정 하나를 직접 탈퇴시키고 다음 세션이 새 이메일로 성공하는지

## 단계 1. 시간대별 표

### 1-1. 표와 활동 성향 설정
- 고칠 곳: `config/default.toml`(`[traffic]` 새 표), `kkugi_load/config.py`
- 내용
  - `open_probability`: 0~23시 각 시간대에 계정 하나가 앱을 열 확률, 24개. 시작 값은 결정 기록의 표
  - `activity_multipliers`와 비중: 계정마다 확률에 곱할 배수(예: 0.5, 1.0, 1.5). 비중을 곱한 평균이 1이어야 한다
  - 검증: 칸이 24개가 아니거나, 확률 × 가장 큰 배수가 1을 넘거나, 평균 배수가 1이 아니면 시작하지 않는다
- 테스트: 정상 설정 읽기, 각 검증이 실패하는지

### 1-2. 조회 세션
- 고칠 곳: `kkugi_load/sessions/view.py`(새 파일), `kkugi_load/runner.py`의 `SESSIONS`
- 내용: 앱을 열어 홈 화면만 본다(요약, 목표, 성장). 기존 `steps.open_home`을 쓴다. 요약의 날짜가 요청한 날짜인지 확인한다
- 통계 이름: `SESSION <페르소나>_view`
- 테스트: 가짜 API로 요청 순서와 검증

### 1-3. 다이어터 일정을 표 기반으로 바꾸기
- 고칠 곳: `kkugi_load/personas/dieter.py`, `kkugi_load/personas/base.py`, `config/default.toml`의 `[personas.dieter]`
- 내용
  - 시간대마다 `확률 × 계정 배수`로 한 번 뽑고, 열기로 나오면 그 시간 안의 무작위 분에 세션을 둔다
  - 끼니 시간대(아침·점심·저녁 창) 안에서 처음 연 세션은 그 끼니의 기록 확률로 식사 기록이 되고, 나머지는 조회 세션이다. 그래서 끼니마다 식사 기록은 하루 최대 한 번이다
  - `active_probability`(하루 활동 확률)는 없앤다. 하루 활동량은 표가 정한다
  - 같은 계정·날짜·시간대에서는 늘 같은 결과가 나와야 한다(재시작해도 같은 일정)
- 테스트
  - 결정성: 같은 입력이면 같은 일정
  - 끼니당 식사 기록은 하루 최대 한 번
  - **분포 확인: 계정 20,000개의 하루 일정을 만들어 시간대별 세션 수가 `계정 수 × 확률`의 ±5% 안인지**

### 1-4. 실행기 필요량 경고
- 고칠 곳: `kkugi_load/scheduler.py` 또는 새 모듈, `locustfile.py`
- 내용: Worker가 오늘 일정에서 가장 바쁜 시간의 동시 세션 수를 추정한다(시간당 세션 수 × 평균 세션 길이 ÷ 3600, 여유 2배). 받은 User 수가 이보다 적으면 시작할 때와 User 수가 바뀔 때 경고를 남긴다
- 테스트: 추정 계산

### 1-5. 문서
- README의 실행 모델, 설정 표, 통계 이름을 표 기반으로 고친다

## 단계 2. 운영 안전장치

### 2-1. 배율과 정지, 설정 다시 읽기
- 고칠 곳: `kkugi_load/distributed.py`, `kkugi_load/scheduler.py`, `locustfile.py`, `config/default.toml`(`scale`)
- 내용
  - 배율(0~1): 계획된 세션을 이 확률로만 실행한다. 계정·날짜·시간대로 정해지는 값과 비교하므로 배율을 바꿔도 같은 세션이 일관되게 남거나 빠진다
  - Master 웹 UI에 경로를 더한다: 배율 바꾸기, 정지(배율 0), 설정 파일 다시 읽기. 웹 UI는 서버 안(`127.0.0.1`)에서만 열리므로 SSH 터널로 쓴다
  - Master가 바뀐 값을 모든 Worker에 보내고, Worker는 지금 이후의 일정을 다시 만든다. 나중에 붙은 Worker는 계정 풀 승인 응답에서 현재 값을 받는다
- 테스트: 배율에 따른 선택이 결정적인지, 다시 만든 일정이 지난 세션을 다시 넣지 않는지, 승인 응답에 현재 값이 실리는지
- 실제 확인: 로컬 Master·Worker 두 개로 배율을 바꿔 두 Worker가 모두 따르는지

### 2-2. Master `/metrics`
- 고칠 곳: 새 모듈(예: `kkugi_load/metrics.py`), `locustfile.py`, `requirements.txt`(`prometheus_client`)
- 내용
  - Worker가 통계 보고에 지표 증가분을 실어 보내고(Locust의 Worker 보고 이벤트), Master가 합쳐 노출한다. 노트북 Worker의 값도 Master에 모인다
  - 지표: API별 응답 시간 히스토그램, 세션 결과(성공·실패·건너뜀·탈퇴)별 수, 놓친 세션 수, 시작 지연 히스토그램, 현재 배율, Worker별 계정 수와 User 수
  - 라벨에 계정 번호나 이메일을 넣지 않는다
- 테스트: 증가분 합치기, 라벨 값이 정해진 집합 안에 있는지

### 2-3. 거버너
- 고칠 곳: `kkugi_load/governor.py`(새 파일), `kkugi_load/distributed.py`
- 내용: Master가 1분마다 최근 5분의 HTTP 실패율과 p99를 본다. 기준을 넘으면 배율을 절반으로 낮추고, 10분 동안 정상이면 0.1씩 올린다. 사람이 정한 배율이 상한이다. 바뀔 때마다 로그와 지표를 남긴다
- 기준값(가정, 설정): 실패율 2%, p99 3초
- 테스트: 가짜 지표 입력에 대한 배율 변화

## 단계 3. 배포

### 3-1. 요청 경로 분할 (프라이빗 95%, 공인 5%)
- 방식은 구현할 때 비교해서 정한다
  - (가) 공인 경로 전용 Worker 컨테이너를 따로 두고 계정 5%를 맡긴다. 단순하지만 풀 범위를 계정 수에 맞춰 나눠야 한다
  - (나) 한 Worker 안에서 세션마다 경로를 고른다. 프라이빗 IP로 연결하면서 TLS 인증서 이름은 도메인으로 검사하는 연결 설정이 필요하다
- 지표에서 경로별 응답 시간을 나눠 본다

### 3-2. 컨테이너와 compose
- 고칠 곳: `Dockerfile`, `deploy/docker-compose.yml`(새 파일), `deploy/.env.example`
- 내용: Master와 상시 Worker. Worker는 CPU 1개·메모리 1GB 상한(가정). 상태 DB 볼륨. 앱 서버 프라이빗 IP는 `.env`로 받는다. 웹 UI와 `/metrics`는 `127.0.0.1`에만 연다
- 노트북 Worker는 Master 포트(5557)를 인터넷에 열지 않고 SSH 터널(`ssh -L 5557:localhost:5557 kkugi-obs`)로 붙는다

### 3-3. 운영 안내
- README에 시작·정지, 배율·다시 읽기, 노트북 붙이기, 되돌리기, Lightsail 크레딧 조회를 적는다

### 저장소 밖에 필요한 것
- 관측 서버 Prometheus가 발생기 `/metrics`를 수집하는 설정(서비스 저장소 `infra/observability/prometheus/prometheus.yml`)
- 있으면 좋은 것: 서버 CPU·메모리 수집, 인공 요청 추적·로그 샘플링. 없어도 계정 1,000개 실측은 Lightsail 지표(AWS CLI)와 `docker stats`로 시작할 수 있다

## 단계 4. 실측과 증량

- [ ] 계정 1,000개로 하루 이상 돌린다. 잴 것: Lightsail CPU·크레딧(두 서버), 발생기 컨테이너 CPU, Tempo·Loki 하루 증가량, API별 p95·p99, 놓친 세션 수
- [ ] 실측값으로 10,000개를 계산해 버틸 수 있으면 올린다
- [ ] 같은 방식으로 20,000개를 판단한다. 10,000개를 넘기기 전에 `peak-*` 풀 번호를 `base`와 겹치지 않게 옮긴다(지금 `peak-1`은 10,000번부터다)

버티지 못하면 원인에 따라 대응한다.

| 버티지 못한 곳 | 대응 |
|---|---|
| 발생기 CPU | 실측상 가능성은 낮다(로컬 테스트 결과 3). 순서대로 ① `FastHttpUser` ② 데이터 서버(SSAFY EC2 `t3.xlarge`)에 Worker ③ `base` 고정과 노트북 피크 |
| 수집(Tempo·Loki) | 서비스 저장소의 추적·로그 샘플링. 그 전까지 전체 트래픽을 낮춘다. 노트북으로 옮겨도 줄지 않는다 |
| backend·DB | 찾으려던 병목이다. 트래픽을 낮추고 원인을 분석한다 |

## 이후

- `FastHttpUser` 전환(지금은 여유가 충분해 보류)
- 다른 페르소나 4종(헬스 입문자, 꾸준러, 워치 유저, 휴면 직전). 필요한 API는 OpenAPI에 모두 있다
- 식사 수정(`PATCH /meals/{mealId}`), 운동·체중 세션, 가입 직후 루틴·주간 목표 설정
- 이탈·복귀 생애주기
- 노트북 Worker를 붙인 피크 실험
- 사진 시나리오: S3 전환 뒤
