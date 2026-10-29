# 꾸기 인공 트래픽 발생기 이미지. Master와 Worker가 같은 이미지를 쓰고, 역할은 compose의 command가 정한다.
# 배포 구성은 deploy/docker-compose.yml에 있다.
FROM python:3.12-slim

# 로그를 버퍼 없이 바로 내보내야 docker logs에서 지연 없이 보인다.
# 코드 폴더는 비루트 사용자가 쓸 수 없으므로 .pyc를 만들지 않는다
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 코드는 Asia/Seoul을 파이썬 tzdata 패키지로 다루지만, 로그 시각(TZ)은 C 라이브러리가 시스템 시간대 DB로 계산한다.
# slim 이미지에는 시스템 시간대 DB가 없어 TZ를 줘도 UTC로 찍히므로 따로 설치한다
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성을 먼저 설치해 코드만 바뀌었을 때는 이 층을 다시 쓰게 한다
COPY requirements.txt .
RUN pip install -r requirements.txt

# 실행 사용자. 코드와 설정은 root 소유로 두어 실행 중에 바뀌지 않게 하고,
# 상태 DB(state/)와 통계(stats/)만 쓸 수 있게 한다. 이름 있는 볼륨은 처음 만들 때 이 폴더의 소유자를 물려받는다
RUN useradd --system --uid 10001 --user-group --home-dir /app --shell /usr/sbin/nologin kkugi
COPY . .
RUN mkdir -p state stats && chown kkugi:kkugi state stats

USER kkugi

# 8089: 웹 UI와 조절 경로(/kkugi/*), 5557: Master와 Worker 연결
EXPOSE 8089 5557

ENTRYPOINT ["locust", "-f", "locustfile.py"]
