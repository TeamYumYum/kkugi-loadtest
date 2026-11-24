"""Read deployment metadata, Locust schedule totals, and governor changes over SSH."""
import argparse
import ast
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import subprocess

KST = timezone(timedelta(hours=9))


def run(*args):
    return subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace", timeout=25)


def checked(result):
    result.check_returncode()
    return result.stdout


def git_head(directory):
    result = run("git", "-C", directory, "rev-parse", "HEAD") if directory else None
    return result.stdout.strip() if result and result.returncode == 0 else None


def lines(name, start, end):
    result = run("docker", "logs", "--timestamps", "--since", start, "--until", end, name)
    return (result.stdout + result.stderr).splitlines() if result.returncode == 0 else None


def inspect(containers):
    if not containers:
        return []
    return json.loads(checked(run("docker", "inspect", *containers)))


def metadata(item):
    config, state = item["Config"], item["State"]
    labels = config["Labels"] or {}
    service = labels.get("com.docker.compose.service")
    env = dict(pair.split("=", 1) for pair in config["Env"] if "=" in pair)
    image_labels = run("docker", "image", "inspect", "--format", "{{json .Config.Labels}}", item["Image"])
    revision = (json.loads(image_labels.stdout) or {}).get("org.opencontainers.image.revision") if image_labels.returncode == 0 else None
    return {
        "name": item["Name"].lstrip("/"), "service": service, "status": state["Status"],
        "started_at": state["StartedAt"], "restart_count": item["RestartCount"],
        "image_id": item["Image"], "image_revision": revision,
        "checkout_head": git_head(labels.get("com.docker.compose.project.working_dir")),
        "pool": env.get("KKUGI_POOL"),
        "schedule_mode": env.get("KKUGI_SCHEDULE", "realtime") if service and service.startswith("worker-") else None,
    }


def locust_context(start, end):
    containers = checked(run("docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=kkugi-loadgen")).split()
    result = {"containers": [], "master_pools": [], "governor_changes": [], "workers_with_plan": []}
    for item in inspect(containers):
        row = metadata(item)
        result["containers"].append(row)
        service = row["service"] or ""
        if service == "master":
            started = min(start, row["started_at"])
            history = lines(row["name"], started, end)
            result["master_logs_available"] = history is not None
            for line in history or []:
                match = re.search(r"계정 풀 (\{[^}]+\}) 승인", line)
                if match:
                    pool = ast.literal_eval(match[1])
                    if pool not in result["master_pools"]:
                        result["master_pools"].append(pool)
                if "거버너가 배율을" in line or "사람이 배율을" in line:
                    result["governor_changes"].append(line)
        elif service.startswith("worker-"):
            worker_logs = lines(row["name"], start, end)
            plans = [line for line in worker_logs or []
                     if "일정: 세션" in line or "일정을 새 설정으로 다시 만듦" in line]
            result["workers_with_plan"].append({"name": row["name"], "logs_available": worker_logs is not None,
                                                "plans": plans})
    return result


def backend_context(start, end):
    containers = checked(run("docker", "ps", "-q", "--filter", "label=com.docker.compose.project=nyamnyam",
                             "--filter", "name=nyamnyam-backend")).split()
    result = {"containers": [], "batch_log_records": []}
    for item in inspect(containers):
        row = metadata(item)
        result["containers"].append(row)
        batch_logs = lines(row["name"], start, end)
        row["logs_available"] = batch_logs is not None
        for line in batch_logs or []:
            timestamp, _, content = line.partition(" ")
            if not content.startswith("{"):
                continue
            try:
                entry = json.loads(content)
            except json.JSONDecodeError:
                continue
            logger = entry.get("logger_name", "").split(".")[-1]
            if "Scheduler" in logger or "StartupJob" in logger or "LastMealBatch" in logger:
                result["batch_log_records"].append({"at": timestamp, "logger": logger, "level": entry.get("level")})
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date")
    parser.add_argument("--host-side", choices=("obs", "backend"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.host_side:
        start = datetime.fromisoformat(args.date).replace(tzinfo=KST)
        end = min(start + timedelta(days=1), datetime.now(KST))
        if end <= start:
            parser.error("No elapsed time in this KST day")
        utc_window = [t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") for t in (start, end)]
        gather = locust_context if args.host_side == "obs" else backend_context
        print(json.dumps(gather(*utc_window), ensure_ascii=True))
    else:
        if not args.out:
            parser.error("--out is required")
        source = Path(__file__).read_bytes()
        result = {}
        for host in ("obs", "backend"):
            response = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"kkugi-{host}",
                                       "python3", "-", args.date, "--host-side", host],
                                      input=source, capture_output=True, timeout=105)
            result[host] = json.loads(response.stdout) if response.returncode == 0 else {
                "error": response.stderr.decode("utf-8", errors="replace")}
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
