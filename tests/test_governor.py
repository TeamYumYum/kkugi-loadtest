"""거버너: 가짜 시계와 가짜 통계로 배율 변화를 확인한다."""

from dataclasses import replace

import pytest
from locust.stats import RequestStats

from kkugi_load import config as config_module
from kkugi_load.control import Control
from kkugi_load.governor import Governor, HttpTotals, http_totals

DEFAULT_TEXT = config_module.DEFAULT_CONFIG.read_text(encoding="utf-8")
# 기본값: 주기 60초, 창 300초, 실패율 2%, p99 3000ms, 최소 200건, 회복 600초마다 0.1
SETTINGS = config_module.loads(DEFAULT_TEXT, {}).governor
MINUTE = 60.0


class FakeStats:
    """분당 요청 수와 실패 수, 응답 시간을 정해 누적 값을 쌓는다."""

    def __init__(self):
        self.epoch = 1
        self.requests = 0
        self.failures = 0
        self.response_times: dict[int, int] = {}

    def add(self, requests: int, failures: int = 0, ms: int = 100) -> None:
        self.requests += requests
        self.failures += failures
        self.response_times[ms] = self.response_times.get(ms, 0) + requests

    def reset(self) -> None:
        self.requests = self.failures = 0
        self.response_times = {}

    def __call__(self) -> HttpTotals:
        return HttpTotals(self.epoch, self.requests, self.failures, dict(self.response_times))


class Rig:
    def __init__(self, settings=SETTINGS, requested: float = 1.0):
        self.now = 0.0
        self.settings = settings
        self.stats = FakeStats()
        self.control = Control(requested, DEFAULT_TEXT, {})
        self.governor = Governor(self.control, lambda: self.settings, self.stats, lambda: self.now,
                                 wall=lambda: f"t={self.now:.0f}")
        self.changes: list[float] = []

    def minute(self, requests: int = 1000, failures: int = 0, ms: int = 100) -> bool:
        """1분 동안 요청을 쌓고 확인한다. 배율을 바꿨으면 True."""
        self.now += MINUTE
        self.stats.add(requests, failures, ms)
        changed = self.governor.tick()
        if changed:
            self.changes.append(self.control.scale)
        return changed

    def start(self) -> None:
        # 시작하자마자 첫 스냅샷을 남긴다(governor.start와 같다)
        self.governor.tick()


def _filled(**kwargs) -> Rig:
    """창이 찰 때까지 정상 요청으로 채운 상태."""
    rig = Rig(**kwargs)
    rig.start()
    for _ in range(5):
        rig.minute()
    return rig


def test_does_not_judge_until_the_window_is_filled():
    rig = Rig()
    rig.start()
    for _ in range(4):
        assert not rig.minute(failures=1000)
        assert "아직 차지 않음" in rig.governor.last_result
    # 5분째: 0초 스냅샷과의 차이가 창이다. 실패율 100%라 낮춘다
    assert rig.minute(failures=1000)
    assert rig.control.scale == 0.5


@pytest.mark.parametrize("failures, ms, reason", [(200, 100, "실패율 4.00%"), (0, 3400, "p99 3400ms")])
def test_halves_scale_when_failure_ratio_or_p99_exceeds_threshold(failures, ms, reason):
    rig = _filled()
    # 창 5000건 중 나쁜 1분(1000건)이 실패 200건이면 실패율 4%, 3400ms면 p99가 3400ms다
    assert rig.minute(failures=failures, ms=ms)
    assert rig.changes == [0.5]
    change = rig.governor.last_change
    assert change["from"] == 1.0 and change["to"] == 0.5
    assert reason in change["reason"] and "HTTP 요청 5000건" in change["reason"]


def test_threshold_is_exclusive():
    # 실패율이 정확히 2%면 넘은 것이 아니다
    rig = _filled()
    for _ in range(5):
        rig.minute(failures=20, ms=3000)
    assert rig.changes == [] and rig.governor.last_window.failure_ratio == 0.02
    assert rig.governor.last_window.p99_ms == 3000


def test_does_not_lower_again_with_the_same_bad_window():
    rig = _filled()
    rig.minute(failures=1000)
    assert rig.changes == [0.5]
    # 낮춘 뒤 4분 동안은 창이 낮추기 전의 나쁜 값을 품고 있어 판단하지 않는다
    for _ in range(4):
        assert not rig.minute(failures=1000)
        assert "새 창이 아직 차지 않음" in rig.governor.last_result
    # 5분이 지나 새 창이 차면 그 창도 나쁘므로 다시 낮춘다
    assert rig.minute(failures=1000)
    assert rig.changes == [0.5, 0.25]


def test_recovers_by_step_after_healthy_period_up_to_requested_scale():
    rig = _filled(requested=0.8)
    rig.minute(failures=1000)
    assert rig.control.scale == 0.4
    minutes = 0
    while rig.control.scale < 0.8 and minutes < 100:
        rig.minute()
        minutes += 1
    # 낮춘 뒤 창 5분이 지나야 첫 정상 판단이고, 그 창의 시작부터 10분 정상이면 올린다. 이후는 10분마다
    assert rig.changes == [0.4, 0.5, 0.6, 0.7, 0.8]
    assert minutes == 10 + 10 * 3
    # 사람이 정한 배율을 넘지 않는다
    for _ in range(30):
        rig.minute()
    assert rig.control.scale == 0.8 and rig.changes[-1] == 0.8


def test_recovery_step_is_capped_at_requested_scale():
    settings = replace(SETTINGS, recovery_step=0.3)
    rig = _filled(settings=settings, requested=0.6)
    rig.minute(failures=1000)
    assert rig.control.scale == 0.3
    for _ in range(10):
        rig.minute()
    assert rig.control.scale == 0.6


def test_bad_window_breaks_the_healthy_streak():
    rig = _filled()
    rig.minute(failures=1000)
    # 낮춘 뒤 정상 9분. 10분째에 올릴 차례였지만 나쁜 창이 오면 올리지 않고 다시 낮춘다
    for _ in range(9):
        rig.minute()
    rig.minute(failures=1000)
    assert rig.changes == [0.5, 0.25]
    # 정상 구간은 처음부터 다시 센다. 새 창(5분)을 기다리고 그 창의 시작부터 10분이 지나야 올린다
    for _ in range(9):
        rig.minute()
    assert rig.control.scale == 0.25
    rig.minute()
    assert rig.control.scale == 0.35


def test_does_not_judge_below_min_requests():
    rig = Rig()
    rig.start()
    # 5분 동안 199건이고 모두 실패해도 판단하지 않는다
    for requests in (40, 40, 40, 40, 39):
        assert not rig.minute(requests=requests, failures=requests, ms=9000)
    assert rig.control.scale == 1.0
    assert "200건보다 적어" in rig.governor.last_result
    assert rig.governor.last_window.requests == 199
    # 200건이면 판단한다
    assert rig.minute(requests=41, failures=41)


def test_low_traffic_does_not_break_or_confirm_the_healthy_streak():
    rig = _filled()
    rig.minute(failures=1000)
    for _ in range(5):
        rig.minute()
    # 첫 정상 판단 뒤 요청이 적은 시간이 이어진다. 그동안은 올리지 않는다
    for _ in range(10):
        assert not rig.minute(requests=10)
    assert rig.control.scale == 0.5
    # 다시 요청이 차서 정상으로 판단하면 그 사이의 시간도 정상 구간으로 센다
    for _ in range(5):
        rig.minute()
    assert rig.control.scale == 0.6


def test_ignores_session_schedule_and_account_entries():
    stats = RequestStats()
    for _ in range(98):
        stats.log_request("GET", "/api/v1/foods", 120, 10)
    stats.log_request("POST", "[auth] POST /auth/login/dev", 2500, 10)
    stats.log_request("PATCH", "/api/v1/users/me", 80, 10)
    stats.log_error("PATCH", "/api/v1/users/me", "500")
    # 우리가 직접 남기는 항목. 실패와 긴 시간이 있어도 세지 않는다
    for _ in range(50):
        stats.log_request("SCHEDULE", "missed", 0, 0)
        stats.log_error("SCHEDULE", "missed", "late")
        stats.log_request("SESSION", "meal_log", 60_000, 0)
        stats.log_request("ACCOUNT", "withdrawn", 9_000, 0)
    totals = http_totals(stats)
    assert totals.requests == 100 and totals.failures == 1
    assert sum(totals.response_times.values()) == 100
    assert totals.response_times[120] == 98 and totals.response_times[2500] == 1


def test_window_from_real_stats_uses_only_http_entries():
    stats = RequestStats()
    now = [0.0]
    control = Control(1.0, DEFAULT_TEXT, {})
    governor = Governor(control, lambda: replace(SETTINGS, min_requests=10), lambda: http_totals(stats),
                        lambda: now[0])
    governor.tick()
    for _ in range(5):
        now[0] += MINUTE
        for _ in range(10):
            stats.log_request("GET", "/api/v1/foods", 100, 10)
            stats.log_request("SCHEDULE", "missed", 0, 0)
            stats.log_error("SCHEDULE", "missed", "late")
            stats.log_request("SESSION", "view", 20_000, 0)
        governor.tick()
    assert control.scale == 1.0
    assert governor.last_window.as_dict() == {
        "seconds": 300.0, "requests": 50, "failures": 0, "failure_ratio": 0.0, "p99_ms": 100,
    }


def test_skips_and_restarts_snapshots_when_stats_are_reset():
    rig = _filled()
    rig.stats.reset()
    rig.stats.add(500, 500)
    rig.now += MINUTE
    assert not rig.governor.tick()
    assert "초기화" in rig.governor.last_result and rig.governor.last_window is None
    # 다시 쌓은 첫 스냅샷부터 창이 차야 판단한다
    for _ in range(4):
        assert not rig.minute(failures=1000)
        assert "아직 차지 않음" in rig.governor.last_result
    assert rig.minute(failures=1000)


def test_reset_event_and_new_stats_object_are_treated_as_reset():
    rig = _filled()
    # 웹 UI의 reset 직후 이미 초기화 전보다 많이 쌓였어도 이벤트로 알아본다
    rig.governor.mark_reset()
    assert not rig.minute(failures=1000)
    assert "초기화" in rig.governor.last_result
    # 테스트를 새로 시작하면 통계 객체가 바뀐다
    rig = _filled()
    rig.stats.epoch = 2
    assert not rig.minute(failures=1000)
    assert "초기화" in rig.governor.last_result


def test_human_scale_change_restarts_from_that_value():
    rig = _filled()
    rig.minute(failures=1000)
    for _ in range(9):
        rig.minute()
    assert rig.control.scale == 0.5
    # 올릴 차례 직전에 사람이 0.9로 정한다. 정상 구간을 지우고 그 값으로 흐른 새 창을 기다린다
    rig.control.set_requested_scale(0.9)
    assert rig.control.scale == 0.9
    for _ in range(5):
        assert not rig.minute(failures=1000)
        assert "새 창이 아직 차지 않음" in rig.governor.last_result
    # 사람이 정한 값으로 흐른 창이 나쁘면 그 값의 절반으로 낮춘다. 상한은 그대로다
    assert rig.minute(failures=1000)
    assert rig.control.scale == 0.45 and rig.control.requested_scale == 0.9


def test_same_value_set_by_human_also_restarts_healthy_streak():
    rig = _filled(requested=0.5)
    rig.minute(failures=1000)
    assert rig.control.scale == 0.25
    for _ in range(9):
        rig.minute()
    # 같은 값을 다시 정해도 사람이 정한 것이다. 실제 배율이 그 값이 되어 거버너가 낮춘 값이 풀린다
    rig.control.set_requested_scale(0.5)
    assert not rig.minute()
    assert rig.control.scale == 0.5 and rig.governor.status()["last_result"].startswith("배율이 바뀐 뒤")


def test_disabling_restores_requested_scale_and_enabling_starts_over():
    rig = _filled(requested=0.8)
    rig.minute(failures=1000)
    assert rig.control.scale == 0.4
    rig.settings = replace(SETTINGS, enabled=False)
    assert rig.minute(failures=1000)
    assert rig.control.scale == 0.8 and rig.governor.status()["enabled"] is False
    assert not rig.minute(failures=1000)
    rig.settings = SETTINGS
    for _ in range(5):
        assert not rig.minute(failures=1000)
    assert rig.minute(failures=1000)
    assert rig.control.scale == 0.4


def test_reloaded_settings_apply_from_the_next_check(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(DEFAULT_TEXT, encoding="utf-8")
    control = Control(1.0, DEFAULT_TEXT, {"KKUGI_CONFIG": str(path)})
    assert control.governor_settings == SETTINGS
    path.write_text(DEFAULT_TEXT.replace("max_p99_ms = 3000", "max_p99_ms = 500"), encoding="utf-8")
    control.reload()
    assert control.governor_settings.max_p99_ms == 500
    now = [0.0]
    stats = FakeStats()
    governor = Governor(control, lambda: control.governor_settings, stats, lambda: now[0])
    governor.tick()
    for _ in range(5):
        now[0] += MINUTE
        stats.add(1000, ms=700)
        governor.tick()
    assert control.scale == 0.5


def test_status_is_shown_in_control_snapshot():
    rig = _filled()
    assert rig.control.snapshot()["governor"] is None
    rig.control.governor = rig.governor
    rig.minute(failures=1000)
    status = rig.control.snapshot()["governor"]
    assert status["enabled"] is True and status["checked_at"] == "t=360"
    assert status["last_window"] == {
        "seconds": 300.0, "requests": 5000, "failures": 1000, "failure_ratio": 0.2, "p99_ms": 100,
    }
    assert status["last_change"]["from"] == 1.0 and status["last_change"]["to"] == 0.5
    assert "실패율 20.00%" in status["last_change"]["reason"]
