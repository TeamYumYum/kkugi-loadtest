"""Grafana 경보를 git의 파일(deploy/grafana/alerting.yaml)로 관리한다. Grafana API를 쓰므로 Grafana를 다시 시작하지 않는다.

    python tools/grafana_alerts.py diff   [--file deploy/grafana/alerting.yaml] [--grafana http://localhost:3000]
    python tools/grafana_alerts.py apply  [--prune] [--lock]
    python tools/grafana_alerts.py export > /tmp/alerting-live.yaml

diff    파일과 지금 Grafana의 규칙·받는 곳·기본 정책을 비교한다. 다르면 종료 코드 1이다.
apply   파일 내용을 Grafana에 반영한다. 같은 내용을 여러 번 반영해도 결과가 같다.
        --prune: 폴더 안에서 파일에 없는 규칙을 지운다. 없으면 지우지 않고 알리기만 한다.
        --lock:  반영한 규칙을 Grafana 화면에서 고칠 수 없게 한다. 없으면 화면에서도 고칠 수 있고, 고친 뒤에는 export로
                 파일에 옮긴다.
export  Grafana 폴더의 규칙을 파일의 rules 모양으로 출력한다. 화면에서 고친 내용을 파일에 옮길 때 쓴다.

Grafana 관리자 계정은 환경 변수 GRAFANA_USER, GRAFANA_PASSWORD에서 읽는다. apply는 파일의 ${이름} 자리표시를 같은 이름의
환경 변수로 채우고, 하나라도 비어 있으면 아무것도 바꾸지 않고 멈춘다. 운영 방법은 docs/alerts.md에 있다.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable

import yaml

DEFAULT_FILE = "deploy/grafana/alerting.yaml"
DEFAULT_GRAFANA = "http://localhost:3000"
DATASOURCE = "prometheus"
# 이 도구가 만든 규칙에 붙이는 라벨. 알림 정책에서 이 라벨로 받는 곳을 가를 수 있다
COMMON_LABELS = {"team": "kkugi", "source": "kkugi-loadtest"}
PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")
OPS = {"gt", "lt"}

# (메서드, 경로, 본문, 헤더) -> (상태 코드, 본문). 바꿔 끼우면 네트워크 없이 시험할 수 있다
Http = Callable[[str, str, object, dict], tuple[int, object]]


class Grafana:
    def __init__(self, base: str, user: str, password: str, http: Http | None = None):
        self.base = base.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.http = http or self._urllib

    def _urllib(self, method: str, path: str, body: object, headers: dict) -> tuple[int, object]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode("utf-8")
                return response.status, json.loads(text) if text else None
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8")
            try:
                return e.code, json.loads(text)
            except ValueError:
                return e.code, text

    def call(self, method: str, path: str, body: object = None, *, editable: bool = True,
             ok: tuple[int, ...] = (200, 201, 202, 204)) -> object:
        headers = {"Authorization": self.auth, "Content-Type": "application/json"}
        if editable:  # 없으면 API로 만든 것은 Grafana 화면에서 고칠 수 없다
            headers["X-Disable-Provenance"] = "true"
        status, data = self.http(method, path, body, headers)
        if status not in ok:
            raise SystemExit(f"Grafana {method} {path} 실패: HTTP {status} {data}")
        return data

    def get(self, path: str, missing_ok: bool = False) -> object:
        return self.call("GET", path, ok=(200, 404) if missing_ok else (200,))


# ---------------------------------------------------------------- 파일

def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    for key in ("folder", "group", "contactPoint", "policy", "rules"):
        if key not in spec:
            raise SystemExit(f"{path}에 {key}가 없다")
    uids = [r["uid"] for r in spec["rules"]]
    if len(uids) != len(set(uids)):
        raise SystemExit(f"{path}의 규칙 uid가 겹친다")
    for rule in spec["rules"]:
        if rule["threshold"]["op"] not in OPS:
            raise SystemExit(f"규칙 {rule['uid']}의 op는 {sorted(OPS)} 중 하나여야 한다")
    return spec


def fill(value: object, env: dict) -> object:
    """문자열 안의 ${이름}을 환경 변수로 채운다. 비어 있는 이름이 있으면 멈춘다."""
    if isinstance(value, dict):
        return {k: fill(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, env) for v in value]
    if isinstance(value, str):
        missing = [name for name in PLACEHOLDER.findall(value) if not env.get(name)]
        if missing:
            raise SystemExit(f"환경 변수가 비어 있다: {', '.join(missing)}. 아무것도 바꾸지 않았다")
        return PLACEHOLDER.sub(lambda m: env[m.group(1)], value)
    return value


def to_grafana(rule: dict, spec: dict) -> dict:
    """파일의 짧은 규칙을 Grafana 규칙 본문으로 바꾼다. 쿼리 A, 기준 C 두 단계다."""
    return {
        "uid": rule["uid"],
        "title": rule["title"],
        "folderUID": spec["folder"]["uid"],
        "ruleGroup": spec["group"]["name"],
        "orgID": 1,
        "condition": "C",
        "for": rule["for"],
        "noDataState": rule["noData"],
        "execErrState": "Alerting",
        "labels": dict(COMMON_LABELS),
        "annotations": {"summary": rule["summary"]},
        "data": [
            {"refId": "A", "relativeTimeRange": {"from": 600, "to": 0}, "datasourceUid": DATASOURCE,
             "model": {"refId": "A", "expr": rule["query"], "instant": True, "range": False}},
            {"refId": "C", "datasourceUid": "__expr__",
             "model": {"refId": "C", "type": "threshold", "expression": "A",
                       "conditions": [{"evaluator": {"type": rule["threshold"]["op"],
                                                     "params": [rule["threshold"]["value"]]}}]}},
        ],
    }


def from_grafana(live: dict) -> dict:
    """Grafana 규칙을 파일의 짧은 모양으로 바꾼다. 쿼리 하나와 기준 하나 모양이 아니면 멈춘다."""
    data = {d["refId"]: d for d in live.get("data", [])}
    if set(data) != {"A", "C"} or data["C"]["model"].get("type") != "threshold":
        raise SystemExit(f"규칙 {live.get('uid')}({live.get('title')})는 쿼리 A·기준 C 모양이 아니라 파일로 옮길 수 없다")
    evaluator = data["C"]["model"]["conditions"][0]["evaluator"]
    return {
        "uid": live["uid"],
        "title": live["title"],
        "query": data["A"]["model"]["expr"],
        "threshold": {"op": evaluator["type"], "value": evaluator["params"][0]},
        "for": live["for"],
        "noData": live["noDataState"],
        "summary": (live.get("annotations") or {}).get("summary", ""),
    }


def _duration(text: str) -> str:
    """Grafana가 5m을 5m0s처럼 돌려줄 수 있어 비교 전에 맞춘다."""
    text = str(text)
    for unit in ("h", "m"):
        text = re.sub(rf"(\d+){unit}0s$", rf"\g<1>{unit}", text)
    return "0s" if text in ("0", "0m", "0h") else text


def _comparable(rule: dict) -> dict:
    return {**rule, "for": _duration(rule["for"]), "query": " ".join(rule["query"].split()),
            "threshold": {"op": rule["threshold"]["op"], "value": float(rule["threshold"]["value"])},
            "summary": " ".join(str(rule["summary"]).split())}


# ---------------------------------------------------------------- 명령

def live_rules(g: Grafana, spec: dict) -> dict[str, dict]:
    rules = g.get("/api/v1/provisioning/alert-rules") or []
    return {r["uid"]: r for r in rules if r.get("folderUID") == spec["folder"]["uid"]}


def diff(g: Grafana, spec: dict, out: Callable[[str], None]) -> int:
    """다른 곳의 수. 0이면 파일과 Grafana가 같다."""
    problems = []
    live = live_rules(g, spec)
    for rule in spec["rules"]:
        if rule["uid"] not in live:
            problems.append(f"Grafana에 없음: 규칙 {rule['uid']} ({rule['title']})")
            continue
        want, got = _comparable(rule), _comparable(from_grafana(live[rule["uid"]]))
        for key in want:
            if want[key] != got.get(key):
                problems.append(f"다름: 규칙 {rule['uid']}의 {key}: 파일 {want[key]!r}, Grafana {got.get(key)!r}")
    for uid, rule in live.items():
        if uid not in {r["uid"] for r in spec["rules"]}:
            problems.append(f"파일에 없음: Grafana 규칙 {uid} ({rule.get('title')})")
    points = {p["name"]: p for p in g.get("/api/v1/provisioning/contact-points") or []}
    cp = spec["contactPoint"]
    if cp["name"] not in points:
        problems.append(f"Grafana에 없음: 받는 곳 {cp['name']}")
    elif points[cp["name"]].get("type") != cp["type"]:
        problems.append(f"다름: 받는 곳 {cp['name']}의 type")
    policy = g.get("/api/v1/provisioning/policies") or {}
    for key, value in spec["policy"].items():
        if policy.get(key) != value:
            problems.append(f"다름: 기본 정책의 {key}: 파일 {value!r}, Grafana {policy.get(key)!r}")
    for p in problems:
        out(p)
    out("파일과 Grafana가 같다" if not problems else f"다른 곳 {len(problems)}개")
    return len(problems)


def apply(g: Grafana, spec: dict, env: dict, out: Callable[[str], None], prune: bool = False,
          lock: bool = False) -> None:
    contact = fill(spec["contactPoint"], env)  # 비밀값이 비면 여기서 멈춘다. 그 전에는 아무것도 바꾸지 않는다
    editable = not lock
    folder = spec["folder"]
    found = g.get(f"/api/folders/{folder['uid']}", missing_ok=True)
    if not (isinstance(found, dict) and found.get("uid") == folder["uid"]):
        g.call("POST", "/api/folders", {"uid": folder["uid"], "title": folder["title"]})
        out(f"폴더 만듦: {folder['title']}")

    points = {p["name"]: p for p in g.get("/api/v1/provisioning/contact-points") or []}
    body = {"name": contact["name"], "type": contact["type"], "settings": contact["settings"],
            "disableResolveMessage": False}
    if contact["name"] in points:
        uid = points[contact["name"]]["uid"]
        g.call("PUT", f"/api/v1/provisioning/contact-points/{uid}", {**body, "uid": uid}, editable=editable)
        out(f"받는 곳 갱신: {contact['name']}")
    else:
        g.call("POST", "/api/v1/provisioning/contact-points", body, editable=editable)
        out(f"받는 곳 만듦: {contact['name']}")
    g.call("PUT", "/api/v1/provisioning/policies", spec["policy"], editable=editable)
    out(f"기본 정책: {spec['policy']['receiver']}")

    live = live_rules(g, spec)
    for rule in spec["rules"]:
        body = to_grafana(rule, spec)
        if rule["uid"] in live:
            g.call("PUT", f"/api/v1/provisioning/alert-rules/{rule['uid']}", body, editable=editable)
            out(f"규칙 갱신: {rule['title']}")
        else:
            g.call("POST", "/api/v1/provisioning/alert-rules", body, editable=editable)
            out(f"규칙 만듦: {rule['title']}")
    for uid, rule in live.items():
        if uid in {r["uid"] for r in spec["rules"]}:
            continue
        if prune:
            g.call("DELETE", f"/api/v1/provisioning/alert-rules/{uid}", editable=editable)
            out(f"규칙 지움: {rule.get('title')} ({uid})")
        else:
            out(f"파일에 없는 규칙이 남아 있다: {rule.get('title')} ({uid}). 지우려면 --prune")

    group = spec["group"]
    path = f"/api/v1/provisioning/folder/{folder['uid']}/rule-groups/{group['name']}"
    current = g.get(path)
    seconds = int(re.fullmatch(r"(\d+)m", group["interval"]).group(1)) * 60
    if isinstance(current, dict) and current.get("interval") != seconds:
        g.call("PUT", path, {**current, "interval": seconds}, editable=editable)
    out(f"평가 간격 {group['interval']}")


def export(g: Grafana, spec: dict) -> str:
    live = live_rules(g, spec)
    order = [r["uid"] for r in spec["rules"]] + sorted(set(live) - {r["uid"] for r in spec["rules"]})
    rules = [from_grafana(live[uid]) for uid in order if uid in live]
    return yaml.safe_dump({"rules": rules}, allow_unicode=True, sort_keys=False, width=120)


def main(argv: list[str] | None = None, *, env: dict | None = None, http: Http | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["diff", "apply", "export"])
    parser.add_argument("--file", default=DEFAULT_FILE)
    parser.add_argument("--grafana", default=DEFAULT_GRAFANA, help="예: 서버 안 http://localhost:3000, 터널 http://localhost:13000")
    parser.add_argument("--prune", action="store_true", help="apply: 폴더 안에서 파일에 없는 규칙을 지운다")
    parser.add_argument("--lock", action="store_true", help="apply: Grafana 화면에서 고칠 수 없게 한다")
    args = parser.parse_args(argv)
    env = dict(os.environ) if env is None else env
    if not env.get("GRAFANA_USER") or not env.get("GRAFANA_PASSWORD"):
        raise SystemExit("GRAFANA_USER와 GRAFANA_PASSWORD(Grafana 관리자)가 필요하다")
    spec = load(args.file)
    g = Grafana(args.grafana, env["GRAFANA_USER"], env["GRAFANA_PASSWORD"], http)
    if args.command == "diff":
        return 1 if diff(g, spec, print) else 0
    if args.command == "apply":
        apply(g, spec, env, print, prune=args.prune, lock=args.lock)
        return 0
    sys.stdout.write(export(g, spec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
