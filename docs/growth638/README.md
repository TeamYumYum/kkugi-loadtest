# Growth 638 성능 측정

성장 조회가 뱃지 지급 후보를 계산하고 조건에 따라 DB에 쓰던 경로를 분리했다. 조회 비용이 실제로 줄었는지, 저장 쪽으로 옮겨간 처리가 저장 응답이나 이벤트 적체에 어떤 영향을 주는지 2026년 11월 23일에 측정했다.

비교 대상은 꾸기 백엔드 `3d42e7b6`와 `fd45d8da`다. 같은 초기 데이터에서 두 버전을 차례로 실행했다. 새 버전에는 내구 이벤트 발행과 성장 조회의 지급 분리가 모두 포함된다.

- [측정 보고서](results-2026-11-23.md)에 비교 조건, 실측 결과와 운영 부분 관측을 기록했다. 이 기록의 운영 하루 판정은 11월 24일 전체 자료를 기준으로 한다.
- [실행별 집계](matrix-2026-11-23.json)와 [50건 몰림·최초 지급 재검증](arrival-2026-11-23.json)은 로컬 결과의 근거다. [그래프가 있는 HTML 보고서](results-2026-11-23.html)는 내려받아 브라우저에서 열 수 있다.
- [지급 분리 이후 조회 흐름](../../notes/figures/growth_me_sequence_current.png)은 운영 추적에서 선택한 표본으로 커넥션 획득과 SQL 시간을 보여 준다. 운영 추적의 집계 단위는 로컬 비교와 별개다. [편집용 Excalidraw 원본](../../notes/figures/growth_me_sequence_current.excalidraw)

## 로컬 비교

Windows PowerShell, Docker Compose, JDK 25, Python 3.11 이상이 필요하다. 아래 명령은 `kkugi-loadtest` 루트에서 실행한다. 경로는 측정 당시 워크스테이션 기준이다. 다른 장비에서는 서비스 저장소 경로를 바꾸고 Orca가 반환한 작업 트리 경로를 쓴다. 두 작업 트리가 이미 있으면 생성 줄을 건너뛰고 HEAD를 확인한다.

```powershell
orca worktree create --repo path:C:/Users/SSAFY/gyunho/kkugi --name growth638-before --base-branch 3d42e7b6 --no-parent --setup skip --json
orca worktree create --repo path:C:/Users/SSAFY/gyunho/kkugi --name growth638-after --base-branch fd45d8da --no-parent --setup skip --json

$root = 'C:/Users/SSAFY/orca/workspaces/kkugi'
foreach ($version in 'before','after') {
    Push-Location "$root/growth638-$version/backend"
    git rev-parse HEAD
    .\gradlew.bat bootJar --offline --console=plain
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw 'bootJar failed' }
    Pop-Location
}
$before = "$root/growth638-before/backend/build/libs/nyamnyam-backend-0.0.1-SNAPSHOT.jar"
$after = "$root/growth638-after/backend/build/libs/nyamnyam-backend-0.0.1-SNAPSHOT.jar"
python -m pip install PyMySQL==1.1.2
python -m tools.growth638.bench $before $after --mode seed
python -m tools.growth638.bench $before $after --mode smoke
python -m tools.growth638.bench $before $after --mode matrix
python -m tools.growth638.bench $before $after --mode arrival
python -m tools.growth638.summarize state/growth638 --out state/growth638/matrix-summary.json
```

Gradle 캐시가 비어 있다면 빌드에서 `--offline`을 생략한다. 기존 측정 원자료를 보존하려면 다시 실행하기 전에 `state/growth638`의 기존 결과를 다른 로컬 폴더에 복사한다. `seed`는 당일 KST 날짜에 맞춰 초기 상태를 만든다. 토큰 유효기간은 240분이다.

- `compose.yml`의 프로젝트는 `kkugi-growth638`다. 동일한 MySQL·Redis·JRE 이미지와 CPU·메모리 제한을 사용한다. 백엔드 Hikari 풀은 10개다. 애플리케이션, 프록시 API, SQL 확인용 포트는 모두 루프백에 바인딩한다.
- `seed`는 온보딩한 로컬 계정 64개를 만든다. 32개로 식사 3건, 운동 1건을 저장하고 성장 조회를 호출한다. 식사 저장은 API에서 허용하는 당일 시각으로 보내고, 초기 데이터에만 SQL로 1·3·8일 전 상대 날짜를 적용한다. 같은 32명의 성장 시작일과 판정 기록을 보충한다.
- 매 실행은 같은 SQL 덤프를 복원하고 Redis를 비운 뒤 백엔드를 재기동한다. 새 버전은 자체 Flyway 변경을 적용한다. 성장 백필의 시작 지연을 하루로 놓고 내구 이벤트의 즉시 전달과 3초 릴레이를 유지한다. 새벽 정기 배치와 겹치면 시각을 기록한다.
- `matrix`는 각 방향에 0·1·2ms를 더하고, 조건마다 실행 순서를 앞뒤로 바꾼다. 셀마다 `SELECT 1`의 왕복 시간을 별도로 60회 잰다. 이 값에는 Windows 호스트에서 Docker로 전달되는 시간도 포함된다.
- 셀마다 순차 성장 조회 100건, 동시 성장 조회 50건, 그리고 1분 동안 성장 조회 180건·식사 저장 105건·운동 저장 20건을 측정한다. 순차 조회 전에 80회 이상, 혼합 측정 전에 각 저장 API 12회를 실행한다. 새 버전에서는 예열에 유입된 이벤트가 해소된 후 혼합 측정을 시작한다.
- `arrival`은 0ms에서 50건 몰림을 두 버전으로 다시 실행한다. Tomcat이 기록한 요청 시작과 끝을 함께 저장한다. 이어서 로컬 초기 데이터에서 첫 기록 뱃지 지급 행을 지운 상태로 최초 성장 조회 10건을 보낸다. [Tomcat Access Log Valve](https://tomcat.apache.org/tomcat-11.0-doc/config/valve.html)
- `state/growth638`에 초기 SQL, 로컬 토큰, k6 원자료, 실행별 집계, 이벤트 스냅샷이 생긴다. 이 경로는 저장소의 `state/` 제외 규칙을 따른다. 백업, CSV 원본, 토큰은 이 경로에 보관한다.

측정이 끝난 로컬 서비스는 `docker compose -f tools/growth638/compose.yml stop`으로 정지할 수 있다.

## 과거 관측 자료 복원

11월 17일 백업에서 필요한 Prometheus 블록과 Tempo 블록을 별도 폴더에 푼다. 로컬 Windows 기본 `tar.exe`의 zstd 및 `--include` 지원을 사용한다. 복원 경로는 첫 실행에 비어 있어야 한다.

```powershell
python -m tools.growth638.collect_csv kkugi-20261109-145702_stats_history.csv
python -m tools.growth638.restore_archive C:/Users/SSAFY/kkugi-obs-backup/obs-20261117-0300.tar.zst state/growth638/archive-replay
$archive = (Resolve-Path state/growth638/archive-replay).Path
docker run -d --name kkugi-growth638-replay-prom --user 0 -p 127.0.0.1:16390:9090 -v "$archive/prometheus:/prometheus" prom/prometheus:v3.15.0 --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/prometheus --storage.tsdb.retention.time=90d
docker run -d --name kkugi-growth638-replay-tempo --user 0 -p 127.0.0.1:16393:3200 -v "$archive/tempo:/var/tempo" -v "$archive/tempo.yaml:/etc/tempo.yaml:ro" grafana/tempo:3.1.0 '-config.file=/etc/tempo.yaml'
python -m tools.growth638.history state/growth638/kkugi-20261109-145702_stats_history.csv --date 2026-11-10
python -m tools.growth638.history state/growth638/kkugi-20261109-145702_stats_history.csv --date 2026-11-12
python -m tools.growth638.archive_trace
```

기존 재생 컨테이너가 포트를 쓰고 있다면 먼저 해당 로컬 컨테이너를 정지한다. 복원한 블록은 로컬 도구가 압축하거나 보존 정책에 따라 변경할 수 있다. 원본 아카이브는 SHA-256을 확인하는 읽기에만 쓴다.

## 운영 하루 집계

11월 24일 00:00부터 25일 00:00까지 KST 자료를 쓴다. 이 구간이 끝나고 마지막 Locust 기록과 Prometheus 스크레이프가 들어온 뒤 실행한다. 관측 호스트 SSH, Prometheus와 Locust의 GET, 이미 있는 컨테이너 메타데이터·로그·CSV 조회를 사용한다.

별도 터미널에서 SSH 포워딩을 연다.

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:16391:127.0.0.1:9090 -L 127.0.0.1:16392:127.0.0.1:8089 kkugi-obs
```

기존 발생기 CSV가 자정을 넘겨 이어졌다면 아래 명령의 파일을 쓴다. 재시작이 있었다면 목록에서 하루를 덮는 CSV를 모두 내려받아 `--csv`로 각각 전달한다.

```powershell
python -m tools.growth638.collect_csv
python -m tools.growth638.collect_csv kkugi-20261123-115324_stats_history.csv
python -m tools.growth638.production 2026-11-24 --csv state/growth638/kkugi-20261123-115324_stats_history.csv --out state/growth638/production-2026-11-24.json
python -m tools.growth638.production_context 2026-11-24 --out state/growth638/production-context-2026-11-24.json
```

11월 23일 부분 구간은 `production 2026-11-23 --partial --start-at 11:53:24 --end-at 16:13:05`에 같은 CSV 및 출력 옵션을 붙여 집계한다.

- 종료 조건은 인공 요청이 들어온 모든 API의 하루 p95 ≤ 300ms, Master·backend 재시작 뒤 5분을 뺀 Hikari 대기 표본 최대 0, 홈 API 3개의 07·12·18시 p95가 01~05시의 2배 이내다. 새벽 p95가 25ms 미만이면 50ms까지 허용한다.
- 하루와 시간대 p95는 각각 해당 구간의 Prometheus 히스토그램에서 계산한다. 표본 수와 함께 읽는다. 특정 구간이나 API의 요청이 적으면 표본 수를 명시하고, 히스토그램이 비어 있으면 그 항목의 판정을 보류한다. Hikari는 15초 스크레이프 사이의 짧은 대기를 놓칠 수 있다.
- Worker 계정 구간 합계, 실행기 수, 스케줄에서 계획·지각·제외된 세션, 실제 요청 및 구간별 비율, 거버너 변경, 스크레이프 대상과 최대 관측 공백을 대조한다. CSV의 시각은 약 60초마다 기록된다. 첫 기록과 마지막 기록 경계에는 오차가 있다.
- CSV 내 카운터가 감소하면 그 시각과 항목 수를 남기고 해당 증가분을 제외한다. 작업자 재연결 같은 변화가 있을 때는 서버 카운터와 대조한다.
- backend 배치와 겹치는 시각을 확인한다. 코드 기준 자정은 일일 판정, 00:01은 주간 운동 판정, 04:00 이후는 탈퇴 정리, 04:50은 성장 백필이다. 마지막 끼니 배치는 17시를 기준으로 한다. 운영 배치의 실제 실행 여부는 해당 구간의 저장 로그로 확인한다.
- 운영 이벤트 Timer는 핸들러별 완료 건수·합계·최댓값을 제공한다. 발행 기록 생성부터 처리 표시 직전까지의 시간이다. 이 Timer에는 p95 버킷이 없다.

종료 조건 원문은 [backend 저부하 지연 해결과 처리 한계 확인](../../notes/decisions/2026-11-11-low-load-latency.md)에 있다.
