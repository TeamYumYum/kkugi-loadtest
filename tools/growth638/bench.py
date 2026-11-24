"""Build a local fixture, restore it for every run, and measure both backend jars."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
import http.client
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import threading
import time as timer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "state/growth638"
COMPOSE = ["docker", "compose", "-f", str(Path(__file__).with_name("compose.yml"))]
APP = "127.0.0.1:16380"
METRICS = "http://127.0.0.1:16381/actuator/prometheus"
PROXY = "http://127.0.0.1:16385/proxies"
KST = timezone(timedelta(hours=9))
RATES = {"growth": 180, "meal": 105, "workout": 20}


def compose(*args, jar=None, access_log=False, **kwargs):
    env = os.environ.copy()
    if jar:
        env["GROWTH638_JAR"] = str(jar.resolve())
    if access_log:
        env["GROWTH638_ACCESSLOG"] = "true"
    return subprocess.run([*COMPOSE, *args], cwd=ROOT, env=env, check=True, **kwargs)


def mysql(sql, *args):
    result = compose("exec", "-T", "mysql", "mysql", "-uroot", "-plocal-root",
                     "-NBr", *args, input=sql.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.decode("utf-8").splitlines()


def request(url, data=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    with urlopen(Request(url, data=body, method=method,
                         headers={"Content-Type": "application/json"}), timeout=5) as response:
        return response.read()


def proxy(latency):
    existing = json.loads(request(PROXY))
    if "mysql" not in existing:
        request(PROXY, {"name": "mysql", "listen": "0.0.0.0:13306", "upstream": "mysql:3306"})
    for name in ("upstream", "downstream"):
        path = f"{PROXY}/mysql/toxics"
        try:
            request(f"{path}/{name}", method="DELETE")
        except HTTPError as error:
            if error.code != 404:
                raise
        if latency:
            request(path, {"name": name, "type": "latency", "stream": name,
                           "attributes": {"latency": latency, "jitter": 0}})


def start(jar, access_log=False):
    compose("up", "-d", "--no-deps", "--force-recreate", "app", jar=jar, access_log=access_log)
    for _ in range(90):
        try:
            health = json.loads(request("http://127.0.0.1:16381/actuator/health"))
            if health["status"] == "UP":
                return
        except (URLError, TimeoutError, OSError):
            pass
        timer.sleep(1)
    compose("logs", "--tail", "25", "app")
    raise RuntimeError("Backend did not become healthy")


def api(conn, path, body=None, token=None):
    headers = {"Content-Type": "application/json", "X-Synthetic-Traffic": "1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request("POST" if body is not None else "GET", f"/api/v1{path}",
                 json.dumps(body).encode() if body is not None else None, headers)
    response = conn.getresponse()
    content = json.loads(response.read())
    if response.status not in (200, 201):
        raise RuntimeError(f"{path}: {response.status} {content}")
    return content.get("data")


def fixture_templates(day):
    food = int(mysql("SELECT MIN(id) FROM food WHERE is_active", "nyamnyam")[0])
    exercise = int(mysql("SELECT MIN(id) FROM exercise WHERE is_active AND equipment='BARBELL'", "nyamnyam")[0])
    when = datetime.combine(day, time(10), KST)
    now = datetime.now(KST).replace(microsecond=0)
    meal = {"clientRevision": 1, "eatenAt": now.isoformat(), "mealType": "BREAKFAST",
            "items": [{"foodId": food, "grams": 125}]}
    values = {"weightKg": 30, "reps": 8}
    workout = {
        "startedAt": (when - timedelta(minutes=25)).isoformat(), "endedAt": when.isoformat(),
        "startExercises": [], "performedExercises": [{
            "sortOrder": 1, "exerciseId": exercise, "exerciseName": "Benchmark Barbell",
            "recordType": "WEIGHT_REPS", "weightType": "EXTERNAL", "restSeconds": 60,
            "sets": [{"setNo": i, "target": values, "actual": values, "status": "COMPLETED",
                      "completedAt": (when - timedelta(minutes=4 - i)).isoformat()} for i in range(1, 4)],
        }],
    }
    return meal, workout


def fixed_body(template, name, index, offset=0):
    body = dict(template, clientUuid=str(uuid5(NAMESPACE_URL, f"growth638/{name}/{index}/{offset}")))
    if offset:
        def shift(value):
            return (datetime.fromisoformat(value) - timedelta(days=offset)).isoformat()
        if name == "workout":
            body["startedAt"], body["endedAt"] = shift(body["startedAt"]), shift(body["endedAt"])
            body["performedExercises"] = [{
                **exercise, "sets": [{**s, "completedAt": shift(s["completedAt"])} for s in exercise["sets"]],
            } for exercise in body["performedExercises"]]
    return body


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def fixture(jar, accounts):
    compose("up", "-d", "--wait", "mysql", "redis", "proxy")
    proxy(0)
    reset()
    start(jar)
    day = datetime.now(KST).date()
    meal, workout = fixture_templates(day - timedelta(days=1))
    tokens = []
    shifts = []
    historical_members = []
    with closing(http.client.HTTPConnection(APP, timeout=15)) as conn:
        for i in range(accounts):
            login = api(conn, "/auth/login/dev", {"email": f"growth638-{i:04d}@synthetic.kkugi.test"})
            token = login["accessToken"]
            api(conn, "/members/me/onboarding", {
                "nickname": f"Bench{i:04d}", "healthGoal": "LOSE", "gender": "MALE",
                "birthDate": "1997-01-01", "heightCm": 173, "weightKg": 76, "activityLevel": "LIGHT",
            }, token)
            tokens.append(token)
            if i < accounts // 2:
                historical_members.append(int(login["memberId"]))
                for offset in (8, 3, 1):
                    body = fixed_body(meal, "meal", i, offset)
                    api(conn, "/meals", body, token)
                    shifts.append(f"UPDATE meal SET eaten_at=eaten_at-INTERVAL {offset} DAY "
                                  f"WHERE client_uuid='{body['clientUuid']}'")
                api(conn, "/workout-sessions", fixed_body(workout, "workout", i, 2), token)
                api(conn, "/growth/me", token=token)
    compose("stop", "app")
    if shifts:
        mysql(";".join(shifts), "nyamnyam")
    if historical_members:
        ids = ",".join(map(str, historical_members))
        mysql(f"UPDATE growth_record SET started_on=CURRENT_DATE-INTERVAL 20 DAY WHERE member_id IN ({ids})", "nyamnyam")
        daily = ",".join(f"({member},CURRENT_DATE-INTERVAL {offset} DAY,{int(offset % 3 != 0)})"
                         for member in historical_members for offset in range(1, 15))
        mysql(f"INSERT INTO growth_daily_judgment(member_id,goal_date,is_achieved) VALUES {daily}", "nyamnyam")
        monday = f"CURRENT_DATE-INTERVAL {day.weekday()} DAY"
        weekly = ",".join(f"({member},{monday}-INTERVAL {offset} DAY,{int(offset == 7)})"
                          for member in historical_members for offset in (7, 14))
        mysql(f"INSERT INTO growth_weekly_judgment(member_id,week_start_date,is_achieved) VALUES {weekly}", "nyamnyam")
    with (STATE / "seed.sql").open("wb") as output:
        compose("exec", "-T", "mysql", "mysqldump", "-uroot", "-plocal-root", "--single-transaction",
                "--set-gtid-purged=OFF", "--no-tablespaces", "nyamnyam", stdout=output)
    write_json(STATE / "fixture.json", {"date": day.isoformat(), "tokens": tokens, "meal": meal, "workout": workout})
    tables = ("member", "growth_record", "meal", "workout_session", "member_badge",
              "growth_daily_judgment", "growth_weekly_judgment")
    counts = {table: int(mysql(f"SELECT COUNT(*) FROM {table}", "nyamnyam")[0]) for table in tables}
    write_json(STATE / "fixture-counts.json", {"date": day.isoformat(), "counts": counts})


def reset():
    compose("stop", "app")
    mysql("DROP DATABASE IF EXISTS nyamnyam; CREATE DATABASE nyamnyam CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;")
    compose("exec", "-T", "redis", "redis-cli", "FLUSHALL", stdout=subprocess.DEVNULL)


def metrics(prefix):
    values = defaultdict(float)
    for line in request(METRICS).decode().splitlines():
        match = re.match(r'^([^\s{]+)(?:\{[^}]*\})?\s+(\S+)', line)
        if match and match.group(1).startswith(prefix):
            values[match.group(1)] += float(match.group(2))
    return dict(values)


def digest():
    rows = mysql("SELECT DIGEST_TEXT,COUNT_STAR,SUM_TIMER_WAIT FROM "
                 "performance_schema.events_statements_summary_by_digest "
                 "WHERE SCHEMA_NAME='nyamnyam' AND DIGEST_TEXT IS NOT NULL")
    return {part[0]: (int(part[1]), int(part[2])) for row in rows if len(part := row.split("\t")) == 3}


def percentile(values, q):
    return sorted(values)[max(0, math.ceil(len(values) * q) - 1)] if values else None


def distribution(values):
    return {"n": len(values), "p50_ms": percentile(values, .5), "p95_ms": percentile(values, .95),
            "max_ms": max(values, default=None)}


def statement_delta(initial, final):
    result = {}
    for stmt, (count, picoseconds) in final.items():
        before_count, before_time = initial.get(stmt, (0, 0))
        if count > before_count:
            result[stmt] = {"count": count - before_count, "server_ms": (picoseconds - before_time) / 1e9}
    return result


def sql_rtt():
    import pymysql

    with pymysql.connect(host="127.0.0.1", port=16386, user="bench",
                         password="local-bench", autocommit=True, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            elapsed = []
            for i in range(65):
                before = timer.perf_counter()
                cur.execute("SELECT 1")
                cur.fetchone()
                if i >= 5:
                    elapsed.append((timer.perf_counter() - before) * 1000)
    return distribution(elapsed)


def single(tokens, sample_count):
    def query(conn, index):
        before = timer.perf_counter()
        api(conn, "/growth/me", token=tokens[index % len(tokens)])
        return (timer.perf_counter() - before) * 1000

    elapsed, structural = [], []
    with closing(http.client.HTTPConnection(APP, timeout=20)) as conn:
        for i in range(max(80, len(tokens))):
            query(conn, i)
        while len(elapsed) < sample_count:
            n = min(10, sample_count - len(elapsed))
            initial_sql = digest()
            initial = metrics("hikaricp_connections")
            chunk = [query(conn, len(elapsed) + i) for i in range(n)]
            final = metrics("hikaricp_connections")
            final_sql = digest()
            sql = statement_delta(initial_sql, final_sql)
            delta = {k: final[k] - initial.get(k, 0) for k in final if k.endswith(("_count", "_sum"))}
            structural.append({"requests": n, "sql": sql, "hikari": delta,
                               "background_detected": any("shedlock" in s.lower() or
                                                          "event_publication" in s.lower() for s in sql)})
            elapsed.extend(chunk)
    return {"http": distribution(elapsed), "samples_ms": elapsed, "structure": structural}


def event_sample():
    rows = mysql("SELECT handler_id,COUNT(*),COUNT(processed_at),"
                 "SUM(processed_at IS NULL),COALESCE(TIMESTAMPDIFF(MICROSECOND,"
                 "MIN(IF(processed_at IS NULL,created_at,NULL)),NOW(6))/1000,0) "
                 "FROM event_publication GROUP BY handler_id", "nyamnyam")
    return {"at": datetime.now(KST).isoformat(), "handlers": [
        {"id": r[0], "published": int(r[1]), "processed": int(r[2]), "pending": int(r[3]), "oldest_ms": float(r[4])}
        for row in rows if (r := row.split("\t"))]}


def summarize_load(path, mode, duration):
    grouped = defaultdict(lambda: {"started": [], "complete": [], "samples": [], "status": Counter(),
                                   "timeout": 0, "dropped": 0})
    with path.open(encoding="utf-8") as raw:
        for line in raw:
            point = json.loads(line)
            if point["type"] != "Point":
                continue
            data, metric = point["data"], point["metric"]
            api_name = data["tags"].get("api") or data["tags"].get("scenario")
            if api_name not in RATES:
                continue
            row = grouped[api_name]
            if metric in ("bench_started", "bench_completed"):
                stamp = datetime.fromisoformat(data["time"]).timestamp()
                row["started" if metric == "bench_started" else "complete"].append(stamp)
                if metric == "bench_completed":
                    row["status"][data["tags"]["status"]] += 1
            elif metric == "http_req_duration":
                row["samples"].append(data["value"])
            elif metric in ("bench_timeout", "dropped_iterations"):
                row[{"bench_timeout": "timeout", "dropped_iterations": "dropped"}[metric]] += int(data["value"])
    results = {}
    for name, row in grouped.items():
        start, finish = row["started"], row["complete"]
        span = max(finish) - min(start) if start and finish else None
        arrival = max(start) - min(start) if start else None
        status_failures = sum(n for status, n in row["status"].items() if status not in {"200", "201"})
        results[name] = {**distribution(row["samples"]), "configured_per_min": RATES[name] if mode == "mixed" else None,
                         "started": len(start), "completed": len(finish), "status": row["status"],
                         "failures": status_failures, "failure_rate": status_failures / len(finish) if finish else None,
                         "timeouts": row["timeout"], "dropped": row["dropped"],
                         "start_rps": len(start) / duration if mode == "mixed" else None,
                         "completed_rps": len(finish) / duration if mode == "mixed" else None,
                         "arrival_span_ms": arrival * 1000 if arrival is not None else None,
                         "completion_span_ms": span * 1000 if span is not None else None}
    return results


def load(tag, mode, duration, probe_events):
    relative = f"{tag}-{mode}.jsonl"
    event_samples = [event_sample()] if probe_events else None
    stop = threading.Event()
    errors = []

    def collect():
        try:
            while not stop.is_set():
                event_samples.append(event_sample())
                stop.wait(.5)
        except subprocess.CalledProcessError as error:
            errors.append(error.returncode)

    poller = threading.Thread(target=collect) if probe_events else None
    if poller:
        poller.start()
    try:
        with (STATE / f"{tag}-{mode}.log").open("wb") as log:
            compose("run", "--rm", "-e", f"MODE={mode}", "-e", f"DURATION={duration}s",
                    "-e", f"RATES={json.dumps(RATES, separators=(',', ':'))}", "k6", "run",
                    "--quiet", "--out", f"json=/work/{relative}", "/scenario/load.js",
                    stdout=log, stderr=subprocess.STDOUT)
        if poller:
            load_stopped = event_sample()
            timer.sleep(5)
    finally:
        stop.set()
        if poller:
            poller.join()
    return {"http": summarize_load(STATE / relative, mode, duration),
            "event_samples": event_samples, "event_at_stop": load_stopped if poller else None,
            "event_after_drain": event_sample() if poller else None, "event_sampling_errors": errors}


def restore(jar, latency, access_log=False):
    reset()
    proxy(0)
    with (STATE / "seed.sql").open("rb") as source:
        compose("exec", "-T", "mysql", "mysql", "-uroot", "-plocal-root", "nyamnyam", stdin=source)
    start(jar, access_log)
    proxy(latency)
    fixture = json.loads((STATE / "fixture.json").read_text(encoding="utf-8"))
    if fixture["date"] != datetime.now(KST).date().isoformat():
        raise RuntimeError("Regenerate the fixture for the current KST date")
    return fixture


def run_cell(tag, jar, latency, duration, samples, new_version):
    fixture = restore(jar, latency)
    result = {"tag": tag, "fixture_date": fixture["date"], "latency_per_direction_ms": latency,
              "sql_rtt": sql_rtt(), "single": single(fixture["tokens"], samples)}
    result["burst"] = load(tag, "burst", duration, False)
    write_json(STATE / f"{tag}.json", result)
    with closing(http.client.HTTPConnection(APP, timeout=20)) as conn:
        for i in range(12):
            for name, path in (("meal", "/meals"), ("workout", "/workout-sessions")):
                token = fixture["tokens"][i]
                api(conn, path, fixed_body(fixture[name], name, i + len(fixture["tokens"])), token)
    if new_version:
        deadline = timer.monotonic() + 15
        while any(r["pending"] for r in event_sample()["handlers"]):
            if timer.monotonic() >= deadline:
                raise RuntimeError("Warmup event backlog did not drain")
            timer.sleep(.3)
    result["mixed"] = load(tag, "mixed", duration, new_version)
    result["event_timer"] = metrics("kkugi_event_publication_latency_seconds") if new_version else None
    write_json(STATE / f"{tag}.json", result)
    print(f"{tag} finished: {result['single']['http']} {result['mixed']['http']}", flush=True)


def pending_badge_probe(fixture):
    removed = int(mysql("DELETE mb FROM member_badge mb JOIN badge b ON b.id=mb.badge_id "
                        "WHERE b.condition_kind='FIRST_RECORD'; SELECT ROW_COUNT()", "nyamnyam")[0])
    if removed < 20:
        raise RuntimeError(f"Expected at least 20 earned first-record badges, removed {removed}")
    start = digest()
    elapsed = []
    with closing(http.client.HTTPConnection(APP, timeout=20)) as conn:
        for token in fixture["tokens"][:10]:
            before = timer.perf_counter()
            api(conn, "/growth/me", token=token)
            elapsed.append((timer.perf_counter() - before) * 1000)
    delta = statement_delta(start, digest())
    written = int(mysql("SELECT COUNT(*) FROM member_badge mb JOIN badge b ON b.id=mb.badge_id "
                         "WHERE b.condition_kind='FIRST_RECORD'", "nyamnyam")[0])
    return {"http": distribution(elapsed), "badges_removed_to_prepare": removed, "badges_after_get": written,
            "badge_inserts": sum(v["count"] for s, v in delta.items()
                                 if s.startswith("INSERT") and "member_badge" in s)}


def burst_arrival(jar, version):
    fixture = restore(jar, 0, access_log=True)
    with closing(http.client.HTTPConnection(APP, timeout=20)) as conn:
        for i in range(80):
            api(conn, "/growth/me", token=fixture["tokens"][i % len(fixture["tokens"])])
    tag = f"arrival-{version}"
    data = load(tag, "burst", 1, False)
    raw = compose("exec", "-T", "app", "cat", "/tmp/growth638-access/access_log.log",
                  stdout=subprocess.PIPE).stdout.decode("utf-8")
    lines = [row.split() for row in raw.splitlines() if row.endswith(" /api/v1/growth/me")]
    if len(lines) != 130:
        raise RuntimeError(f"Expected 80 warmup and 50 measured access-log entries, got {len(lines)}")
    measured = lines[-50:]
    started = [int(row[0]) for row in measured]
    completed = [int(row[1]) for row in measured]
    result = {"http": data["http"]["growth"], "server_start_span_ms": max(started) - min(started),
              "server_completion_span_ms": max(completed) - min(started),
              "server_status": Counter(row[2] for row in measured),
              "starts_ms_from_first": sorted(t - min(started) for t in started),
              "pending_badge_probe": pending_badge_probe(fixture)}
    write_json(STATE / f"{tag}.json", result)
    print(f"{tag}: {result}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--accounts", type=int, default=64)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--mode", choices=("seed", "smoke", "matrix", "arrival"), default="matrix")
    args = parser.parse_args()
    if args.accounts < 12 or args.samples < 1 or args.duration < 1:
        parser.error("Use at least 12 accounts and positive sample count and duration")
    STATE.mkdir(parents=True, exist_ok=True)
    if args.mode == "seed":
        fixture(args.before, args.accounts)
    elif args.mode == "arrival":
        burst_arrival(args.before, "before")
        burst_arrival(args.after, "after")
    elif args.mode == "smoke":
        run_cell("smoke-before", args.before, 0, 10, 20, False)
        run_cell("smoke-after", args.after, 0, 10, 20, True)
    else:
        for latency in (0, 1, 2):
            for rep, order in ((1, ("before", "after")), (2, ("after", "before"))):
                for version in order:
                    run_cell(f"r{rep}-{version}-{latency}ms", getattr(args, version), latency,
                             args.duration, args.samples, version == "after")
    if args.mode != "seed":
        compose("stop", "app")


if __name__ == "__main__":
    main()
