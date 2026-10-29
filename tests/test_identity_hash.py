"""해시로 정하는 균등 값(seeded_uniforms)이 재현되고, 용도끼리 상관되지 않는지 확인한다."""

import math
import subprocess
import sys
from collections import Counter
from datetime import date

import pytest

from kkugi_load import config as config_module
from kkugi_load.accounts.identity import activity_multiplier, seeded_uniforms
from kkugi_load.personas.dieter import _sample

CONFIG = config_module.load({})
DAY = date(2026, 10, 28)
N = 20_000


def _correlation(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))


def test_same_input_gives_same_values_and_values_are_in_unit_interval():
    a = seeded_uniforms(1, "open", 7, "2026-10-28", count=48)
    assert a == seeded_uniforms(1, "open", 7, "2026-10-28", count=48)
    assert len(a) == 48 and all(0.0 <= x < 1.0 for x in a)
    assert a != seeded_uniforms(1, "open", 8, "2026-10-28", count=48)
    assert a != seeded_uniforms(2, "open", 7, "2026-10-28", count=48)


def test_same_values_in_a_new_process():
    """hash()처럼 프로세스마다 달라지지 않는다. 재시작한 Worker도 같은 일정을 만들어야 한다."""
    code = "from kkugi_load.accounts.identity import seeded_uniforms; print(repr(seeded_uniforms(1, 'x', 3, count=4)))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == repr(seeded_uniforms(1, "x", 3, count=4))


def test_earlier_values_do_not_depend_on_count():
    """시간대 수나 끼니 음식 수가 늘어도 앞쪽 값은 그대로다."""
    assert seeded_uniforms(1, "a", count=48)[:2] == seeded_uniforms(1, "a", count=2)


def test_count_must_be_positive():
    with pytest.raises(ValueError):
        seeded_uniforms(1, "a", count=0)


def test_values_are_uniform():
    values = [seeded_uniforms(CONFIG.seed, "t", i)[0] for i in range(N)]
    counts = Counter(int(v * 10) for v in values)
    # 10칸 각각 기대값 2,000. 표준편차는 약 42이므로 넉넉히 200을 둔다
    assert all(abs(counts[b] - N / 10) < 200 for b in range(10)), counts


def test_open_decision_minute_and_activity_are_uncorrelated():
    """같은 시간대의 열기 판정 값과 분 값, 그리고 계정 활동 배수는 서로 상관되지 않는다."""
    hour = 12
    opens, minutes, multipliers = [], [], []
    for index in range(N):
        u = seeded_uniforms(CONFIG.seed, "open", index, DAY.isoformat(), count=48)
        opens.append(u[2 * hour])
        minutes.append(u[2 * hour + 1])
        multipliers.append(activity_multiplier(CONFIG, index))
    # 독립이면 상관계수의 표준편차는 1/√N ≈ 0.007이다
    assert abs(_correlation(opens, minutes)) < 0.03
    assert abs(_correlation(opens, multipliers)) < 0.03
    assert abs(_correlation(minutes, multipliers)) < 0.03


def test_sample_picks_distinct_words_uniformly():
    pool = [f"w{i}" for i in range(5)]
    counts = Counter()
    for index in range(N):
        picked = _sample(pool, seeded_uniforms(CONFIG.seed, "sample", index, count=2))
        assert len(set(picked)) == 2 and set(picked) <= set(pool)
        counts.update(picked)
    # 단어마다 2/5의 확률로 뽑힌다
    assert all(abs(counts[w] - N * 2 / 5) < 0.05 * N * 2 / 5 for w in pool), counts
    with pytest.raises(ValueError):
        _sample(pool, (0.1,) * 6)
