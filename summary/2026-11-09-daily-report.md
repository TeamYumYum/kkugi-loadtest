# 하루 요약: 2026-11-09 (한국 시간 00:00-24:00)

## 주의

- HTTP 실패 7건
- `GET /api/v1/daily-summaries/{date}` p95 384.8ms (기준 300ms)
- `GET /api/v1/growth/me` p95 441.0ms (기준 300ms)
- `POST /api/v1/auth/login/dev` p95 372.0ms (기준 300ms)
- `POST /api/v1/members/me/onboarding` p95 424.3ms (기준 300ms)
- `POST /api/v1/workout-sessions` p95 318.7ms (기준 300ms)
- 커넥션 대기 최대 70
- backend 재시작: 09:49:51, 10:14:44, 10:45:59, 11:18:53, 14:21:04, 14:35:04, 15:36:59, 17:55:44

## 트래픽

Locust 기록 23.9시간 분량의 누적값 증가분이다.

| 항목 | 값 |
|---|---:|
| HTTP 요청 | 158,196 |
| 평균 TPS | 1.836 |
| 1시간 최대 TPS | 9.438 (23시) |
| 1분 최대 TPS | 21.48 (23:15) |
| 세션 | 21,062 |
| HTTP 실패 | 7 |
| 세션 실패 | 7 |
| 놓친 세션 (`SCHEDULE missed`) | 0 |

## 시간대별

DB TPS는 그 시간대(예: 17시는 17:00-18:00)의 평균 커밋/s다.

| 시 | TPS | 세션/s | DB TPS |
|---|---:|---:|---:|
| 00 | 0.426 | 0.107 | 8.4 |
| 01 | 0.046 | 0.012 | 1.0 |
| 02 | 0.043 | 0.011 | 0.1 |
| 03 | 0.032 | 0.008 | 0.1 |
| 04 | 0.030 | 0.007 | 0.1 |
| 05 | 0.039 | 0.010 | 0.1 |
| 06 | 1.625 | 0.247 | 1.7 |
| 07 | 2.879 | 0.481 | 2.9 |
| 08 | 1.227 | 0.246 | 1.2 |
| 09 | 0.427 | 0.100 | 0.5 |
| 10 | 0.386 | 0.097 | 0.5 |
| 11 | 1.771 | 0.250 | 2.5 |
| 12 | 3.001 | 0.499 | 2.8 |
| 13 | 1.163 | 0.239 | 1.1 |
| 14 | 0.542 | 0.104 | 1.8 |
| 15 | 1.149 | 0.200 | 2.5 |
| 16 | 1.205 | 0.220 | 2.4 |
| 17 | 4.161 | 0.498 | 37.4 |
| 18 | 6.101 | 0.808 | 62.1 |
| 19 | 3.565 | 0.567 | 63.7 |
| 20 | 1.917 | 0.253 | 51.0 |
| 21 | 1.832 | 0.248 | 49.1 |
| 22 | 1.036 | 0.189 | 62.6 |
| 23 | 9.438 | 0.463 | 72.1 |

## API별 응답 시간 (서버 기준)

인공 요청(`synthetic="true"`)만 센다. 백분위는 히스토그램 구간으로 추정한 값이고 단위는 ms다.

| API | 요청 수 | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| `GET /api/v1/daily-summaries/{date}` | 34,923 | 42.6 | 384.8 | 734.3 |
| `GET /api/v1/daily-goals/{goalDate}` | 25,064 | 21.1 | 265.9 | 522.0 |
| `GET /api/v1/growth/me` | 25,058 | 110.3 | 441.0 | 785.9 |
| `POST /api/v1/auth/refresh` | 17,858 | 23.0 | 115.1 | 264.8 |
| `GET /api/v1/foods` | 17,536 | 27.9 | 63.7 | 88.0 |
| `GET /api/v1/meals` | 9,188 | 45.8 | 299.2 | 602.1 |
| `POST /api/v1/meals` | 8,765 | 30.6 | 73.7 | 88.8 |
| `POST /api/v1/auth/login/dev` | 3,026 | 76.9 | 372.0 | 878.8 |
| `POST /api/v1/members/me/onboarding` | 3,018 | 91.9 | 424.3 | 867.2 |
| `GET /api/v1/routines` | 1,992 | 35.3 | 213.6 | 382.3 |
| `GET /api/v1/workout-schedules` | 1,992 | 29.4 | 228.9 | 430.7 |
| `GET /api/v1/workout-sessions` | 1,992 | 22.2 | 149.4 | 315.6 |
| `GET /api/v1/workout-sessions/recent-sets` | 1,992 | 27.0 | 127.9 | 263.3 |
| `POST /api/v1/workout-sessions` | 1,991 | 152.6 | 318.7 | 443.3 |
| `GET /api/v1/weekly-workout-goal/me` | 1,985 | 25.8 | 60.8 | 87.6 |
| `GET /api/v1/weight-records/latest` | 1,178 | 20.8 | 57.4 | 79.8 |
| `GET /api/v1/weight-records` | 1,171 | 17.9 | 27.5 | 59.4 |
| `PUT /api/v1/weight-records/{recordedOn}` | 1,171 | 20.3 | 32.6 | 62.9 |
| `POST /api/v1/workout-schedules` | 1,104 | 21.5 | 29.6 | 62.1 |
| `PATCH /api/v1/meals/{mealId}` | 1,078 | 27.9 | 64.1 | 86.9 |
| `GET /api/v1/exercises` | 498 | 23.4 | 46.0 | 89.7 |
| `PUT /api/v1/weekly-workout-goal/me` | 409 | 97.5 | 144.8 | 175.9 |
| `POST /api/v1/routines` | 204 | 76.5 | 128.1 | 222.8 |

## 서버 자원

| 지표 | 값 | 보는 곳 |
|---|---|---|
| Hikari 커넥션 최대 사용 | 10 | `hikaricp_connections_active` |
| Hikari 커넥션 대기 최대 | 70 | `hikaricp_connections_pending` |
| MySQL 실행 중 스레드 최대 | 5 | `mysql_global_status_threads_running` |
| MySQL 느린 쿼리 증가 | 0건 | `mysql_global_status_slow_queries` |
| DB 커밋 | 1,538,970건, 1분 최대 87.6/s | `mysql_global_status_commands_total{command="commit"}` |
| JVM CPU | 평균 3.4%, 최대 71.5% | `process_cpu_usage` |
| 힙 최대 | 436MB | `jvm_memory_used_bytes{area="heap"}` |
| GC 정지 | 합계 67.1초, 1회 최대 159ms | `jvm_gc_pause_seconds` |
| backend 재시작 | 09:49:51, 10:14:44, 10:45:59, 11:18:53, 14:21:04, 14:35:04, 15:36:59, 17:55:44 | `process_start_time_seconds` |

## 집계 방법

이 날 실측 구간이 있는 Locust 기록 CSV 4개를 모두 합쳤다. 재시작을 걸친 구간과 180초가 넘는 공백은 뺐다. 비교할 전날 JSON은 없다. 서버 지표는 하루 전체를 덮는다.

> 이 절은 원래 글자가 깨져 있어서, 같은 날 JSON의 `collection_note`를 근거로 다시 썼다.

사용한 CSV:

- `kkugi-20261104-090514_stats_history.csv`
- `kkugi-20261109-111459_stats_history.csv`
- `kkugi-20261109-113822_stats_history.csv`
- `kkugi-20261109-145702_stats_history.csv`
