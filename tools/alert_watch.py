"""발생기 경보. 1분마다 발생기 Master를 보고, 상태가 바뀔 때만 Mattermost 웹훅으로 알린다.

    python tools/alert_watch.py --master http://master:8089 \\
        [--prometheus http://host.docker.internal:9090 --stats-dir /app/stats --reports-dir /app/reports]

backend 경보(5xx, 응답 느림, DB 커밋 급증, 커넥션 대기, 재시작, 수집 중단)는 관측 서버 Grafana의 경보 규칙이 맡는다
(폴더 "꾸기 경보", README의 "경보와 하루 요약"). 이 도구는 Prometheus에 없는 발생기 상태만 본다: 발생기 HTTP 실패,
거버너의 배율 낮춤, 응답 없는 Worker, Master 조회 실패.

관측 서버에서는 deploy/docker-compose.yml의 alerts 서비스로 돈다. 웹훅 주소는 환경 변수 KKUGI_MM_WEBHOOK_URL이고,
비어 있으면 시작하지 않는다. 주소는 비밀값이라 저장소에 두지 않는다.

규칙마다 조건이 for_minutes 동안 이어지면 [경보]를 한 번 보내고, 조건이 풀리면 [해소]를 한 번 보낸다. 같은 경보를 매분
다시 보내지 않는다. 상태는 메모리에만 있으므로 감시가 다시 시작되면 아직 이어지는 경보를 한 번 더 보낸다.
감시 자체가 죽었는지 알 수 있게 시작할 때와 매일 HEARTBEAT_HOUR시에 "동작 중"을 보낸다.
--stats-dir를 주면 "동작 중"을 보낼 때 전날의 하루 요약(tools/daily_report.py)도 보낸다. 통계 파일은 --stats-dir에서 가장
최근의 *_stats_history.csv를 쓰고, 요약 JSON을 --reports-dir에 날짜별로 남겨 다음 날 비교에 쓴다.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
WEBHOOK_ENV = "KKUGI_MM_WEBHOOK_URL"
INTERVAL_SECONDS = 60
HEARTBEAT_HOUR = 9           # 매일 이 시각(한국 시간)에 "동작 중"을 보낸다
FETCH_FAILURES_TO_ALERT = 3  # 가정: Master 조회가 연달아 이만큼 실패하면 경보
Fetch = Callable[[str], bytes]
Post = Callable[[str, bytes], None]


def _urlopen(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=20) as response:
        return response.read()


def _post(url: str, body: bytes) -> None:
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


@dataclass
class Rule:
    name: str
    # Master의 /kkugi/control 응답을 받아, 조건이 참이면 현재 값을 설명하는 문장, 거짓이면 None
    check: Callable[[dict], str | None]
    for_minutes: int = 0


def query_master(base: str, fetch: Fetch) -> dict:
    return json.loads(fetch(f"{base.rstrip('/')}/kkugi/control"))


# ---------------------------------------------------------------- 규칙

def _generator_failures(control: dict) -> str | None:
    window = (control.get("governor") or {}).get("last_window") or {}
    failures = window.get("failures") or 0
    if failures > 0:
        return f"발생기 HTTP 실패 {failures}건(최근 {window.get('seconds', 300):.0f}초, 요청 {window.get('requests')}건)"
    return None


def _governor(control: dict) -> str | None:
    requested, scale = control.get("requested_scale"), control.get("scale")
    if requested is not None and scale is not None and scale < requested:
        last = (control.get("governor") or {}).get("last_change")
        return f"거버너가 배율을 {requested} → {scale}로 낮췄다" + (f": {last}" if last else "")
    return None


def _workers(control: dict) -> str | None:
    dead = [node for node, w in (control.get("workers") or {}).items() if not w.get("alive")]
    return f"응답 없는 Worker {len(dead)}개: {', '.join(n.split('_')[0] for n in dead)}" if dead else None


RULES = [
    Rule("발생기 실패", _generator_failures),
    Rule("거버너 배율 낮춤", _governor),
    Rule("Worker 응답 없음", _workers, for_minutes=2),
]


# ---------------------------------------------------------------- 상태와 전송

@dataclass
class Watch:
    post: Callable[[str], None]
    rules: list[Rule] = field(default_factory=lambda: list(RULES))
    # 규칙 이름 -> 조건이 처음 참이 된 시각
    pending_since: dict[str, datetime] = field(default_factory=dict)
    firing: dict[str, datetime] = field(default_factory=dict)
    fetch_failures: int = 0
    fetch_firing: bool = False
    last_heartbeat: str | None = None

    def evaluate(self, control: dict | None, now: datetime) -> None:
        """control이 None이면 Master 조회에 실패한 것이다. 그때는 규칙을 판정하지 않고 조회 실패만 센다."""
        if control is None:
            self.fetch_failures += 1
            if self.fetch_failures >= FETCH_FAILURES_TO_ALERT and not self.fetch_firing:
                self.fetch_firing = True
                self.post(f"[경보] 발생기 Master 조회 실패 {self.fetch_failures}회 연속. 발생기 경보를 판정하지 못한다")
            return
        self.fetch_failures = 0
        if self.fetch_firing:
            self.fetch_firing = False
            self.post("[해소] 발생기 Master 조회가 다시 된다")
        for rule in self.rules:
            detail = rule.check(control)
            if detail is None:
                self.pending_since.pop(rule.name, None)
                if rule.name in self.firing:
                    started = self.firing.pop(rule.name)
                    minutes = max(1, round((now - started).total_seconds() / 60))
                    self.post(f"[해소] {rule.name} ({minutes}분 이어짐)")
                continue
            since = self.pending_since.setdefault(rule.name, now)
            if rule.name not in self.firing and now - since >= timedelta(minutes=rule.for_minutes):
                self.firing[rule.name] = since
                self.post(f"[경보] {rule.name}: {detail}")

    def heartbeat(self, now: datetime) -> bool:
        """하루 한 번 "동작 중"을 보낸다. 보냈으면 True."""
        day = now.date().isoformat()
        if now.hour == HEARTBEAT_HOUR and self.last_heartbeat != day:
            self.last_heartbeat = day
            firing = ", ".join(sorted(self.firing)) or "없음"
            self.post(f"발생기 경보 감시 동작 중 ({now:%m-%d %H:%M}). 이어지는 경보: {firing}")
            return True
        return False


def post_daily_report(day: str, stats_dir: str, reports_dir: str, prometheus: str, fetch: Fetch,
                      send: Callable[[str], None]) -> None:
    """day(YYYY-mm-dd)의 하루 요약을 보낸다. 만들지 못하면 조용히 넘어가지 않고 그 이유를 경보로 보낸다."""
    try:
        try:
            from tools import daily_report as dr
        except ImportError:  # python tools/alert_watch.py로 실행하면 tools/가 경로의 맨 앞이다
            import daily_report as dr
        histories = sorted(glob.glob(os.path.join(stats_dir, "*_stats_history.csv")), key=os.path.getmtime)
        if not histories:
            raise FileNotFoundError(f"{stats_dir}에 *_stats_history.csv가 없다")
        previous_day = (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()
        previous_path = os.path.join(reports_dir, f"{previous_day}.json")
        previous = None
        if os.path.exists(previous_path):
            with open(previous_path, encoding="utf-8") as f:
                previous = json.load(f)
        report = dr.build(day, histories[-1], dr.Prometheus(prometheus, fetch), None, previous)
        with open(os.path.join(reports_dir, f"{day}.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        send(dr.mattermost_text(report))
    except (Exception, SystemExit) as e:  # daily_report는 Prometheus 접속 실패에 SystemExit을 쓴다
        send(f"[경보] {day} 하루 요약을 만들지 못했다: {e}")


def webhook_poster(url: str, post: Post, log: Callable[[str], None]) -> Callable[[str], None]:
    def send(text: str) -> None:
        log(text)
        try:
            post(url, json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"))
        except Exception as e:  # 전송이 실패해도 감시는 계속한다. 로그에는 남는다
            log(f"Mattermost 전송 실패: {e}")
    return send


def main(argv: list[str] | None = None, *, env: dict | None = None, fetch: Fetch = _urlopen, post: Post = _post,
         sleep: Callable[[float], None] = time.sleep, now: Callable[[], datetime] = lambda: datetime.now(KST),
         rounds: int | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--master", required=True, help="발생기 Master 웹 UI. 예: http://master:8089")
    parser.add_argument("--prometheus", help="하루 요약용. 예: http://host.docker.internal:9090")
    parser.add_argument("--stats-dir", help="Master의 통계 CSV 폴더. 주면 매일 전날의 하루 요약을 보낸다")
    parser.add_argument("--reports-dir", help="하루 요약 JSON을 남길 폴더. --stats-dir와 함께 준다")
    args = parser.parse_args(argv)
    if args.stats_dir and not args.prometheus:
        parser.error("--stats-dir를 주면 하루 요약에 쓸 --prometheus도 주어야 한다")
    url = (os.environ if env is None else env).get(WEBHOOK_ENV, "").strip()
    if not url:
        raise SystemExit(f"{WEBHOOK_ENV}가 비어 있다. 경보를 보낼 곳이 없어 시작하지 않는다")

    def log(text: str) -> None:
        print(f"[{now():%Y-%m-%d %H:%M:%S}] {text}", flush=True)

    watch = Watch(webhook_poster(url, post, log))
    watch.post(f"발생기 경보 감시 시작 ({now():%m-%d %H:%M}). 규칙 {len(watch.rules)}개, {INTERVAL_SECONDS}초마다 본다")
    done = 0
    while rounds is None or done < rounds:
        current = now()
        try:
            control = query_master(args.master, fetch)
        except Exception as e:  # 접속 실패, 잘못된 응답 모두 조회 실패로 센다
            log(f"발생기 Master 조회 실패: {e}")
            control = None
        watch.evaluate(control, current)
        if watch.heartbeat(current) and args.stats_dir:
            yesterday = (current - timedelta(days=1)).date().isoformat()
            post_daily_report(yesterday, args.stats_dir, args.reports_dir or args.stats_dir, args.prometheus, fetch,
                              watch.post)
        done += 1
        if rounds is None or done < rounds:
            sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
