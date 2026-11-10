# 경보 운영 가이드

경보가 무엇을 언제 보내는지는 README의 "경보와 하루 요약"에 있다. 이 문서는 경보를 **고치고 반영하는 방법**을 적는다.

## 구조

```text
deploy/grafana/alerting.yaml   backend 경보의 기준. 규칙, 받는 곳, 기본 알림 정책
        │ tools/grafana_alerts.py apply  (Grafana API, 재시작 없음)
        ▼
관측 서버 Grafana (폴더 "꾸기 경보")  ──→  Mattermost (KKUGI_MM_WEBHOOK_URL)

tools/alert_watch.py           발생기 경보. deploy/docker-compose.yml의 alerts 컨테이너로 돈다
```

- **git이 기준이다.** 규칙을 바꾸면 파일을 고쳐 PR로 올리고, 머지한 뒤 `apply`로 반영한다.
- **Grafana를 다시 시작하지 않는다.** 파일 프로비저닝과 달리 API로 반영하므로 바로 적용된다.
- **화면에서도 고칠 수 있다.** 급할 때는 Grafana 화면에서 고치고, 나중에 `export`로 파일에 옮긴다. 화면 수정을 막고 싶으면 `apply --lock`으로 반영한다.

## 실행 위치

Grafana는 관측 서버의 `127.0.0.1:3000`에만 열려 있다. 둘 중 한 곳에서 돌린다.

| 위치 | `--grafana` | 준비 |
| --- | --- | --- |
| 관측 서버 (`ssh kkugi-obs`, `~/kkugi-loadtest`) | 기본값 `http://localhost:3000` | 서버의 `python3`에 PyYAML이 있다 |
| 노트북 | `http://localhost:13000` | `ssh kkugi-obs-tunnel`로 터널을 연다. `pip install -r requirements.txt` |

Grafana 관리자 계정은 환경 변수로 준다. 관측 서버에서는 Grafana 컨테이너의 값을 그 자리에서 읽는다.

```bash
export GRAFANA_USER=$(docker exec kkugi-observability-grafana-1 printenv GF_SECURITY_ADMIN_USER)
export GRAFANA_PASSWORD=$(docker exec kkugi-observability-grafana-1 printenv GF_SECURITY_ADMIN_PASSWORD)
# apply할 때만 필요하다. 받는 곳의 웹훅 주소를 채운다
export $(grep '^KKUGI_MM_WEBHOOK_URL=' deploy/.env)
```

## 명령

| 명령 | 하는 일 | 바꾸는 것 |
| --- | --- | --- |
| `python3 tools/grafana_alerts.py diff` | 파일과 Grafana를 비교한다. 같으면 종료 코드 0, 다르면 1 | 없음 |
| `python3 tools/grafana_alerts.py apply` | 파일을 Grafana에 반영한다. 여러 번 해도 결과가 같다 | 규칙, 받는 곳, 기본 정책 |
| `python3 tools/grafana_alerts.py apply --prune` | 위와 같고, 폴더 안에서 파일에 없는 규칙을 지운다 | 위 + 규칙 삭제 |
| `python3 tools/grafana_alerts.py apply --lock` | 반영한 것을 Grafana 화면에서 고칠 수 없게 한다 | 위와 같음 |
| `python3 tools/grafana_alerts.py export` | Grafana 폴더의 규칙을 파일의 `rules` 모양으로 출력한다 | 없음 |

- `apply`는 웹훅 환경 변수가 비어 있으면 아무것도 바꾸지 않고 멈춘다.
- `--prune`이 없으면 파일에 없는 규칙은 지우지 않고 알리기만 한다. 실수로 파일에서 빠진 규칙이 사라지지 않게 하기 위해서다.
- `export`는 파일 전체가 아니라 `rules`만 출력한다. 파일의 주석을 지키기 위해 출력을 보고 손으로 옮긴다.

## 작업 흐름

**규칙을 더하거나 고칠 때**

1. `deploy/grafana/alerting.yaml`의 `rules`를 고친다. 규칙 하나는 쿼리(`query`) 하나와 기준(`threshold`) 하나다.
2. PR을 올리고 머지한다.
3. 관측 서버에서 `git pull` 뒤 `diff`로 바뀔 곳을 보고 `apply`한다. 규칙을 뺐으면 `apply --prune`이다.
4. 다시 `diff`가 "같다"인지 본다.

**화면에서 먼저 고쳤을 때**

1. `export` 출력을 보고 파일의 해당 규칙을 고친다.
2. `diff`가 "같다"인지 보고 PR로 올린다.

**웹훅을 바꿀 때**

1. 관측 서버 `deploy/.env`의 `KKUGI_MM_WEBHOOK_URL`을 고친다.
2. `apply`로 Grafana 받는 곳을 갱신하고, `docker compose -f deploy/docker-compose.yml up -d --no-deps alerts`로 발생기 경보도 새 값을 읽게 한다.

## 규칙을 더할 때의 기준

- **사람이 바로 움직여야 하는가.** 받아도 할 일이 없으면 하루 요약(`tools/daily_report.py`)의 주의 항목으로 보낸다.
- **원인이 아니라 증상인가.** 트래픽 급증은 원인이다. 서버가 견디지 못하면 응답 느림, 5xx, 커넥션 대기가 잡는다.
- **다른 경보와 겹치는가.** 같은 장애로 두 번 울리면 하나를 뺀다.
- **결과가 비는 것이 정상인가.** 그렇다면 `noData: OK`다. 대상이 사라진 것이 장애이면 `noData: Alerting`이다.
- **얼마나 급한가.** 서비스가 멈췄거나 오류를 내면 `severity: critical`(`:usagi_chainsaw:`), 곧 장애가 될 수 있는 신호면 `severity: warning`(`:hachiware_angry:`)이다. 해소는 `:dancing_usagi:`다. 이모지는 받는 곳의 메시지 틀(`contactPoint.settings.text`)이 고른다.

## 한계

- Grafana의 기본 알림 정책은 서버 전체에 하나다. `apply`는 기본 정책을 이 파일 값으로 바꾼다. 다른 곳(예: 서비스 저장소의 파일 프로비저닝)에서 정책을 정하면 서로 덮어쓴다.
- 관측 서버나 Grafana가 멈추면 backend 경보도 멈춘다. 발생기 경보의 매일 9시 메시지는 관측 서버에서 오므로 이것도 함께 멈춘다.
- 파일의 `query`는 Grafana에 그대로 들어간다. PromQL 문법 오류는 `apply` 뒤 Grafana 규칙 상태(health)에서 보인다.
