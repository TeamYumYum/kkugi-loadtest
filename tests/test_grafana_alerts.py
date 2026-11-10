"""git의 경보 파일과 Grafana API 사이의 diff·apply·export. 가짜 Grafana로 네트워크 없이 본다."""

import copy

import pytest
import yaml

from tools import grafana_alerts as ga

SPEC = ga.load(ga.DEFAULT_FILE)
ENV = {"GRAFANA_USER": "admin", "GRAFANA_PASSWORD": "pw", "KKUGI_MM_WEBHOOK_URL": "https://mm.example/hooks/x"}


class FakeGrafana:
    """규칙, 받는 곳, 정책, 폴더, 그룹 간격을 메모리에 든 가짜. 요청과 헤더를 기록한다."""

    def __init__(self):
        self.rules, self.points, self.policy, self.folders = {}, {}, {}, set()
        self.interval = 60
        self.calls = []

    def __call__(self, method, path, body, headers):
        self.calls.append((method, path, headers.get("X-Disable-Provenance")))
        if path.startswith("/api/folders/"):
            uid = path.rsplit("/", 1)[1]
            return (200, {"uid": uid}) if uid in self.folders else (404, {"message": "not found"})
        if path == "/api/folders":
            self.folders.add(body["uid"])
            return 200, body
        if path == "/api/v1/provisioning/alert-rules":
            if method == "GET":
                return 200, list(self.rules.values())
            self.rules[body["uid"]] = copy.deepcopy(body)
            return 201, body
        if path.startswith("/api/v1/provisioning/alert-rules/"):
            uid = path.rsplit("/", 1)[1]
            if method == "DELETE":
                self.rules.pop(uid)
                return 204, None
            self.rules[uid] = copy.deepcopy(body)
            return 200, body
        if path == "/api/v1/provisioning/contact-points":
            if method == "GET":
                return 200, list(self.points.values())
            self.points[body["name"]] = {**body, "uid": "cp1"}
            return 202, body
        if path.startswith("/api/v1/provisioning/contact-points/"):
            self.points[body["name"]] = body
            return 202, body
        if path == "/api/v1/provisioning/policies":
            if method == "GET":
                return 200, self.policy
            self.policy = dict(body)
            return 202, {"message": "policies updated"}
        if "/rule-groups/" in path:
            if method == "GET":
                return 200, {"title": "backend", "interval": self.interval, "rules": []}
            self.interval = body["interval"]
            return 200, body
        raise AssertionError(f"모르는 요청 {method} {path}")


def _g(fake):
    return ga.Grafana("http://g", "admin", "pw", fake)


def test_apply_then_diff_is_clean_and_apply_is_repeatable():
    fake, out = FakeGrafana(), []
    ga.apply(_g(fake), SPEC, ENV, out.append)
    assert set(fake.rules) == {r["uid"] for r in SPEC["rules"]}
    assert fake.points["mattermost-kkugi"]["settings"]["url"] == "https://mm.example/hooks/x"
    assert fake.policy["receiver"] == "mattermost-kkugi"
    assert ga.diff(_g(fake), SPEC, out.append) == 0
    before = copy.deepcopy(fake.rules)
    ga.apply(_g(fake), SPEC, ENV, out.append)
    assert fake.rules == before


def test_rules_are_editable_unless_locked():
    fake = FakeGrafana()
    ga.apply(_g(fake), SPEC, ENV, lambda _t: None)
    writes = [c for c in fake.calls if c[0] in ("POST", "PUT", "DELETE") and "provisioning" in c[1]]
    assert writes and all(c[2] == "true" for c in writes)
    fake = FakeGrafana()
    ga.apply(_g(fake), SPEC, ENV, lambda _t: None, lock=True)
    writes = [c for c in fake.calls if c[0] in ("POST", "PUT", "DELETE") and "provisioning" in c[1]]
    assert writes and all(c[2] is None for c in writes)


def test_missing_webhook_changes_nothing():
    fake = FakeGrafana()
    with pytest.raises(SystemExit, match="KKUGI_MM_WEBHOOK_URL"):
        ga.apply(_g(fake), SPEC, {**ENV, "KKUGI_MM_WEBHOOK_URL": ""}, lambda _t: None)
    assert [c for c in fake.calls if c[0] != "GET"] == []


def test_diff_reports_changed_extra_and_missing_rules():
    fake = FakeGrafana()
    ga.apply(_g(fake), SPEC, ENV, lambda _t: None)
    fake.rules["kkugi-slow"]["for"] = "10m"
    fake.rules["kkugi-slow"]["data"][1]["model"]["conditions"][0]["evaluator"]["params"] = [2]
    fake.rules.pop("kkugi-pool-pending")
    fake.rules["old-random-uid"] = {**fake.rules["kkugi-5xx"], "uid": "old-random-uid", "title": "옛 규칙"}
    out = []
    assert ga.diff(_g(fake), SPEC, out.append) == 4
    text = "\n".join(out)
    assert "규칙 kkugi-slow의 for" in text and "규칙 kkugi-slow의 threshold" in text
    assert "Grafana에 없음: 규칙 kkugi-pool-pending" in text
    assert "파일에 없음: Grafana 규칙 old-random-uid" in text


def test_diff_ignores_grafana_duration_and_whitespace_forms():
    fake = FakeGrafana()
    ga.apply(_g(fake), SPEC, ENV, lambda _t: None)
    fake.rules["kkugi-slow"]["for"] = "5m0s"
    fake.rules["kkugi-5xx"]["data"][0]["model"]["expr"] = "  " + fake.rules["kkugi-5xx"]["data"][0]["model"]["expr"]
    assert ga.diff(_g(fake), SPEC, lambda _t: None) == 0


def test_prune_deletes_only_with_flag():
    fake = FakeGrafana()
    ga.apply(_g(fake), SPEC, ENV, lambda _t: None)
    fake.rules["old"] = {**fake.rules["kkugi-5xx"], "uid": "old", "title": "옛 규칙"}
    out = []
    ga.apply(_g(fake), SPEC, ENV, out.append)
    assert "old" in fake.rules and any("--prune" in line for line in out)
    ga.apply(_g(fake), SPEC, ENV, out.append, prune=True)
    assert "old" not in fake.rules


def test_export_round_trips_to_file_shape():
    fake = FakeGrafana()
    ga.apply(_g(fake), SPEC, ENV, lambda _t: None)
    exported = yaml.safe_load(ga.export(_g(fake), SPEC))["rules"]
    assert [ga._comparable(r) for r in exported] == [ga._comparable(r) for r in SPEC["rules"]]


def test_export_refuses_rules_it_cannot_express():
    fake = FakeGrafana()
    ga.apply(_g(fake), SPEC, ENV, lambda _t: None)
    fake.rules["kkugi-5xx"]["data"].append({"refId": "B", "model": {"type": "reduce"}})
    with pytest.raises(SystemExit, match="kkugi-5xx"):
        ga.export(_g(fake), SPEC)


def test_file_rules_are_valid_and_unique():
    assert len({r["uid"] for r in SPEC["rules"]}) == len(SPEC["rules"]) == 5
    assert "${KKUGI_MM_WEBHOOK_URL}" in SPEC["contactPoint"]["settings"]["url"]


def test_main_needs_grafana_admin():
    with pytest.raises(SystemExit, match="GRAFANA_USER"):
        ga.main(["diff"], env={})
