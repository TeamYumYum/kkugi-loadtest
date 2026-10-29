"""[traffic] 표, 페르소나 activity, 계정 활동 배수의 설정 읽기와 검증."""

import re
from collections import Counter

import pytest

from kkugi_load import config as config_module
from kkugi_load.accounts.identity import activity_multiplier

DEFAULT_TEXT = config_module.DEFAULT_CONFIG.read_text(encoding="utf-8")

NIGHT = (1, 2, 3, 4, 5)
PEAK = (7, 12, 18)
BUFFER = (6, 8, 11, 13, 17, 19)
NORMAL = (0, 9, 10, 14, 15, 16, 20, 21, 22, 23)


def _load_with(tmp_path, text: str):
    """기본 설정을 고친 임시 TOML 파일로 설정을 읽는다."""
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return config_module.load({"KKUGI_CONFIG": str(path)})


def _with_table(values: list[float]) -> str:
    """open_probability 배열을 통째로 바꾼 설정 본문."""
    text, n = re.subn(r"open_probability = \[.*?\n\]", f"open_probability = {values!r}", DEFAULT_TEXT,
                      count=1, flags=re.S)
    assert n == 1
    return text


def _with_multipliers(value: str) -> str:
    text, n = re.subn(r"^activity_multipliers = .*$", f"activity_multipliers = {value}", DEFAULT_TEXT,
                      count=1, flags=re.M)
    assert n == 1
    return text


def _with_activity(line: str) -> str:
    text, n = re.subn(r"^activity = 1\.0\n", line, DEFAULT_TEXT, count=1, flags=re.M)
    assert n == 1
    return text


def _expected_table() -> list[float]:
    table = [0.0] * 24
    for hours, p in ((NIGHT, 0.01), (PEAK, 0.5), (BUFFER, 0.25), (NORMAL, 0.1)):
        for hour in hours:
            table[hour] = p
    return table


def test_default_config_reads_table_and_multipliers():
    config = config_module.load({})
    assert len(config.open_probability) == 24
    assert list(config.open_probability) == _expected_table()
    assert config.activity_multipliers == ((0.5, 1.0), (1.0, 2.0), (1.5, 1.0))
    assert config.personas["dieter"]["activity"] == 1.0
    # 불변이어야 설정을 여러 곳에서 나눠 써도 안전하다
    assert isinstance(config.open_probability, tuple)
    assert all(isinstance(pair, tuple) for pair in config.activity_multipliers)


def test_hour_groups_cover_every_hour_once():
    assert sorted(NIGHT + PEAK + BUFFER + NORMAL) == list(range(24))


def test_table_must_have_24_hours(tmp_path):
    with pytest.raises(ValueError, match="24개"):
        _load_with(tmp_path, _with_table([0.1] * 23))
    with pytest.raises(ValueError, match="24개"):
        _load_with(tmp_path, _with_table([0.1] * 25))


@pytest.mark.parametrize("bad", [-0.01, 1.01])
def test_probability_must_be_between_0_and_1(tmp_path, bad):
    table = [0.1] * 24
    table[9] = bad
    with pytest.raises(ValueError, match=r"open_probability\[9\]"):
        _load_with(tmp_path, _with_table(table))


@pytest.mark.parametrize("value", ["[[0.0, 1], [1.0, 2], [2.0, 1]]", "[[0.5, 0], [1.0, 2], [1.5, 1]]",
                                   "[[0.5, -1], [1.0, 2], [1.5, 1]]"])
def test_multiplier_and_weight_must_be_positive(tmp_path, value):
    with pytest.raises(ValueError, match="양수"):
        _load_with(tmp_path, _with_multipliers(value))


def test_weighted_mean_multiplier_must_be_one(tmp_path):
    with pytest.raises(ValueError, match="평균"):
        _load_with(tmp_path, _with_multipliers("[[0.5, 1], [1.0, 1], [1.5, 2]]"))
    # 비중이 달라도 가중 평균이 1이면 통과한다
    config = _load_with(tmp_path, _with_multipliers("[[0.5, 2], [2.0, 1]]"))
    assert config.activity_multipliers == ((0.5, 2.0), (2.0, 1.0))


def test_multiplier_entry_must_be_a_pair(tmp_path):
    with pytest.raises(ValueError, match="두 값"):
        _load_with(tmp_path, _with_multipliers("[[1.0, 1, 1]]"))


def test_missing_traffic_table_stops_start(tmp_path):
    text, n = re.subn(r"^activity_multipliers = .*\n", "", DEFAULT_TEXT, count=1, flags=re.M)
    assert n == 1
    with pytest.raises(ValueError, match="activity_multipliers가 없다"):
        _load_with(tmp_path, text)


def test_persona_activity_is_required(tmp_path):
    with pytest.raises(ValueError, match=r"\[personas\.dieter\]에 activity가 없다"):
        _load_with(tmp_path, _with_activity(""))


@pytest.mark.parametrize("bad", ["0", "-1.0"])
def test_persona_activity_must_be_positive(tmp_path, bad):
    with pytest.raises(ValueError, match="activity는 양수"):
        _load_with(tmp_path, _with_activity(f"activity = {bad}\n"))


def test_peak_probability_times_multipliers_must_not_exceed_one(tmp_path):
    # 0.5 × 1.5 × 1.5 = 1.125
    with pytest.raises(ValueError, match="1을 넘는다"):
        _load_with(tmp_path, _with_activity("activity = 1.5\n"))
    # 0.5 × 1.0 × 2.0 = 1.0은 허용한다
    config = _load_with(tmp_path, _with_multipliers("[[0.5, 2], [2.0, 1]]"))
    assert max(config.open_probability) * max(m for m, _ in config.activity_multipliers) == 1.0


def test_account_multiplier_is_deterministic():
    config = config_module.load({})
    assert activity_multiplier(config, 7) == activity_multiplier(config, 7)
    assert activity_multiplier(config, 7) == activity_multiplier(config_module.load({}), 7)
    assert {activity_multiplier(config, i) for i in range(100)} == {0.5, 1.0, 1.5}


def test_account_multiplier_follows_weights_and_mean_is_one():
    config = config_module.load({})
    n = 20_000
    values = [activity_multiplier(config, i) for i in range(n)]
    assert abs(sum(values) / n - 1.0) <= 0.02
    counts = Counter(values)
    # 비중 1:2:1 → 25%, 50%, 25%
    assert abs(counts[0.5] / n - 0.25) < 0.02
    assert abs(counts[1.0] / n - 0.50) < 0.02
    assert abs(counts[1.5] / n - 0.25) < 0.02
