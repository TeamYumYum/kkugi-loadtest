"""설정 파일(TOML)과 환경 변수를 읽는다.

페르소나별 설정은 각 페르소나가 직접 해석하므로 여기서는 원본 그대로 넘긴다.

운영 중 설정 다시 읽기(kkugi_load/control.py)에서는 Master가 읽은 원문 TOML 텍스트를 Worker에 보낸다.
Worker가 각자 자기 파일을 읽으면 노트북마다 설정이 달라질 수 있어서다. 그래서 텍스트에서 읽는 loads를
따로 두고 load는 파일을 읽어 loads에 넘긴다. 다만 FROZEN_KEYS의 값은 다시 읽어도 바꿀 수 없다(frozen_changes).

[governor] 표(거버너, kkugi_load/governor.py)는 Master(또는 단독 실행)만 쓴다. Worker도 같은 파일을 읽으므로 함께
검증해 잘못된 값이면 어느 프로세스도 시작하지 않는다. 다시 읽기로 바꿀 수 있다.
"""

from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, fields
from datetime import time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "default.toml"
DEFAULT_STATE_DB = ROOT / "state" / "accounts.db"
# 시간대별 확률표의 칸 수. 인덱스가 한국 시간의 시(0~23)다
HOURS = 24
# 운영 중에 다시 읽어도 바꿀 수 없는 최상위 키. 바뀌면 계정의 이메일, 인증, 계정 범위가 달라져
# 이미 만든 회원이나 다른 Worker의 범위와 어긋난다
FROZEN_KEYS = ("timezone", "api_prefix", "accounts", "auth", "synthetic", "pools")
# 경로 표시(KKUGI_ROUTE)에 쓸 수 있는 글자. 통계 이름 앞에 [값]으로 붙으므로 공백이나 괄호가 섞이면
# 이름을 읽기 어렵고 경로별로 나눠 보기도 어렵다
ROUTE_PATTERN = re.compile(r"[a-z0-9-]+")


@dataclass(frozen=True)
class Pool:
    """Worker 하나가 맡는 계정 번호 범위 [start, start + count)."""

    name: str
    start: int
    count: int
    # 이 시간대(시작, 끝)에 계획된 세션만 실행한다. 비어 있으면 하루 전체
    only_between: tuple[tuple[time, time], ...]

    @property
    def end(self) -> int:
        return self.start + self.count

    def allows(self, at: time) -> bool:
        return not self.only_between or any(start <= at < end for start, end in self.only_between)


@dataclass(frozen=True)
class Config:
    tz: ZoneInfo
    api_prefix: str
    email_prefix: str
    email_domain: str
    seed: int
    refresh_margin_seconds: int
    synthetic_header: tuple[str, str] | None
    search_think: tuple[float, float]
    select_think: tuple[float, float]
    # realtime 또는 immediate(scheduler.py)
    schedule_mode: str
    max_lateness: timedelta
    # 페르소나 이름 → [personas.<이름>] 표. 표마다 activity(페르소나 활동 배수)가 있음을 검증했다
    personas: dict[str, dict[str, Any]]
    # 시간대별로 계정 하나가 앱을 열 확률. 24개, 인덱스가 한국 시간의 시
    open_probability: tuple[float, ...]
    # 계정 활동 배수와 비중 (배수, 비중). 비중 가중 평균 배수가 1이다
    activity_multipliers: tuple[tuple[float, float], ...]
    pool: Pool
    state_db: Path
    # 시작할 때의 배율(0~1). 계획된 세션을 이 확률로만 실행한다(scheduler.py). 운영 중에는 Master가 바꾼다
    scale: float
    governor: GovernorSettings
    # 이 Worker가 요청을 보내는 경로의 표시(KKUGI_ROUTE, 예: public). 비어 있으면 표시하지 않는다.
    # 경로 자체는 컨테이너의 이름 해석이 정하고(notes/decisions/2026-10-29-route-split.md), 이 값은 HTTP 요청의
    # 통계 이름을 경로별로 나눠 보기 위한 것이다
    route: str = ""

    @property
    def route_prefix(self) -> str:
        """HTTP 요청 통계 이름 앞에 붙일 경로 표시. 예: "[public] ". 경로 표시가 없으면 빈 문자열이다."""
        return f"[{self.route}] " if self.route else ""


@dataclass(frozen=True)
class GovernorSettings:
    """[governor] 표. 키 이름과 같다. 뜻은 config/default.toml의 주석과 kkugi_load/governor.py에 있다."""

    enabled: bool
    interval_seconds: float
    window_seconds: float
    max_failure_ratio: float
    max_p99_ms: float
    min_requests: int
    recovery_seconds: float
    recovery_step: float


def hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def read_traffic(
    traffic: dict[str, Any], personas: dict[str, dict[str, Any]]
) -> tuple[tuple[float, ...], tuple[tuple[float, float], ...]]:
    """[traffic] 표와 페르소나 activity를 검증한다. 어긋나면 이유를 담은 ValueError를 낸다."""
    for key in ("open_probability", "activity_multipliers"):
        if key not in traffic:
            raise ValueError(f"[traffic]에 {key}가 없다")
    table = tuple(float(p) for p in traffic["open_probability"])
    if len(table) != HOURS:
        raise ValueError(f"[traffic] open_probability는 {HOURS}개여야 한다: {len(table)}개")
    for hour, p in enumerate(table):
        if not 0 <= p <= 1:
            raise ValueError(f"[traffic] open_probability[{hour}]가 0~1 밖이다: {p}")

    if any(len(pair) != 2 for pair in traffic["activity_multipliers"]):
        raise ValueError("[traffic] activity_multipliers의 항목은 [배수, 비중] 두 값이어야 한다")
    multipliers = tuple((float(m), float(w)) for m, w in traffic["activity_multipliers"])
    if not multipliers:
        raise ValueError("[traffic] activity_multipliers가 비어 있다")
    for m, w in multipliers:
        if m <= 0 or w <= 0:
            raise ValueError(f"[traffic] activity_multipliers의 배수와 비중은 양수여야 한다: [{m}, {w}]")
    mean = sum(m * w for m, w in multipliers) / sum(w for _, w in multipliers)
    if not math.isclose(mean, 1.0, rel_tol=0, abs_tol=1e-9):
        raise ValueError(f"[traffic] activity_multipliers의 비중 가중 평균 배수가 1이 아니다: {mean}")

    for name, persona in personas.items():
        # 기본값으로 채우지 않는다. 빠지면 페르소나의 활동량을 모르는 채로 실행하게 된다
        if "activity" not in persona:
            raise ValueError(f"[personas.{name}]에 activity가 없다")
        if float(persona["activity"]) <= 0:
            raise ValueError(f"[personas.{name}] activity는 양수여야 한다: {persona['activity']}")

    top_persona = max(float(p["activity"]) for p in personas.values())
    top_account = max(m for m, _ in multipliers)
    peak = max(table) * top_persona * top_account
    if peak > 1:
        raise ValueError(
            f"확률 최댓값 {max(table)} × 페르소나 activity 최댓값 {top_persona} × 계정 배수 최댓값 {top_account}"
            f" = {peak}로 1을 넘는다"
        )
    return table, multipliers


def read_scale(value: Any) -> float:
    """배율을 검증해 돌려준다. 수가 아니거나 0~1 밖이면 이유를 담은 ValueError를 낸다."""
    if isinstance(value, bool):
        raise ValueError(f"배율은 0 이상 1 이하의 수여야 한다: {value!r}")
    try:
        scale = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"배율은 0 이상 1 이하의 수여야 한다: {value!r}") from None
    # NaN은 어떤 비교도 거짓이라 아래 범위 검사에 걸린다
    if not 0 <= scale <= 1:
        raise ValueError(f"배율은 0 이상 1 이하여야 한다: {value!r}")
    return scale


def _positive_number(table: dict[str, Any], key: str) -> float:
    value = table[key]
    # TOML의 true도 파이썬에서는 int라 따로 막는다. NaN과 무한대는 아래 비교에 걸린다
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < math.inf:
        raise ValueError(f"[governor] {key}는 양수여야 한다: {value!r}")
    return float(value)


def read_governor(table: Any) -> GovernorSettings:
    """[governor] 표를 검증한다. 어긋나면 이유를 담은 ValueError를 낸다.

    기본값으로 채우지 않는다. 빠지면 어떤 기준으로 배율을 낮추는지 모르는 채로 실행하게 된다.
    """
    if not isinstance(table, dict):
        raise ValueError("[governor] 표가 없다")
    missing = [f.name for f in fields(GovernorSettings) if f.name not in table]
    if missing:
        raise ValueError(f"[governor]에 {', '.join(missing)}가 없다")
    if not isinstance(table["enabled"], bool):
        raise ValueError(f"[governor] enabled는 true나 false여야 한다: {table['enabled']!r}")
    min_requests = table["min_requests"]
    if isinstance(min_requests, bool) or not isinstance(min_requests, int) or min_requests < 1:
        raise ValueError(f"[governor] min_requests는 1 이상의 정수여야 한다: {min_requests!r}")
    settings = GovernorSettings(
        enabled=table["enabled"],
        interval_seconds=_positive_number(table, "interval_seconds"),
        window_seconds=_positive_number(table, "window_seconds"),
        max_failure_ratio=_positive_number(table, "max_failure_ratio"),
        max_p99_ms=_positive_number(table, "max_p99_ms"),
        min_requests=min_requests,
        recovery_seconds=_positive_number(table, "recovery_seconds"),
        recovery_step=_positive_number(table, "recovery_step"),
    )
    if settings.max_failure_ratio >= 1:
        raise ValueError(f"[governor] max_failure_ratio는 1보다 작아야 한다: {settings.max_failure_ratio}")
    if settings.recovery_step > 1:
        raise ValueError(f"[governor] recovery_step은 1 이하여야 한다: {settings.recovery_step}")
    # 창은 확인 때마다 남긴 스냅샷의 차이로 만든다. 창이 주기보다 짧으면 실제로는 주기만큼의 창을 보게 된다
    if settings.window_seconds < settings.interval_seconds:
        raise ValueError(
            f"[governor] window_seconds({settings.window_seconds})는 interval_seconds"
            f"({settings.interval_seconds}) 이상이어야 한다"
        )
    return settings


def read_route(value: str) -> str:
    """KKUGI_ROUTE 값을 검증한다. 영문 소문자, 숫자, 하이픈만 허용하고 어긋나면 이유를 담은 ValueError를 낸다."""
    if value and not ROUTE_PATTERN.fullmatch(value):
        raise ValueError(f"KKUGI_ROUTE는 영문 소문자, 숫자, 하이픈만 쓸 수 있다: {value!r}")
    return value


def config_path(env: dict[str, str]) -> Path:
    return Path(env.get("KKUGI_CONFIG") or DEFAULT_CONFIG)


def read_text(env: dict[str, str] | None = None) -> str:
    """설정 파일의 원문. Master가 다시 읽은 설정을 Worker에 보낼 때 이 텍스트를 그대로 보낸다."""
    env = dict(os.environ) if env is None else env
    return config_path(env).read_text(encoding="utf-8")


def frozen_changes(old_text: str, new_text: str) -> list[str]:
    """두 설정 원문에서 FROZEN_KEYS 중 값이 다른 키 이름. 같으면 빈 목록.

    파싱한 값끼리 비교하므로 주석이나 줄 바꿈만 다른 것은 바뀐 것으로 보지 않는다.
    """
    old, new = tomllib.loads(old_text), tomllib.loads(new_text)
    return [key for key in FROZEN_KEYS if old.get(key) != new.get(key)]


def load(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    return loads(read_text(env), env)


def loads(text: str, env: dict[str, str] | None = None) -> Config:
    """설정 원문과 환경 변수로 설정을 만든다. 계정 풀과 일정 방식, 상태 DB, 경로 표시는 환경 변수가 정한다."""
    env = dict(os.environ) if env is None else env
    raw = tomllib.loads(text)

    accounts = raw["accounts"]
    auth = raw["auth"]
    synthetic = raw["synthetic"]
    think = raw["think_time"]

    if auth["method"] != "dev":
        raise ValueError(f"지원하지 않는 인증 방식: {auth['method']}")
    header_name = synthetic["header_name"].strip()

    personas = raw.get("personas") or {}
    if not personas or sum(float(p["weight"]) for p in personas.values()) <= 0:
        raise ValueError("[personas]에 weight가 양수인 페르소나가 하나 이상 있어야 한다")
    open_probability, activity_multipliers = read_traffic(raw.get("traffic") or {}, personas)

    pool_name = env.get("KKUGI_POOL", "base")
    pools = raw.get("pools") or {}
    if pool_name not in pools:
        raise ValueError(f"설정에 없는 계정 풀: {pool_name}")
    pool_raw = pools[pool_name]
    # 로컬 검증용으로 풀의 범위를 덮어쓸 수 있다
    pool = Pool(
        name=pool_name,
        start=int(env.get("KKUGI_ACCOUNT_START", pool_raw["start"])),
        count=int(env.get("KKUGI_ACCOUNT_COUNT", pool_raw["count"])),
        only_between=tuple((hhmm(a), hhmm(b)) for a, b in pool_raw.get("only_between", [])),
    )
    if pool.start < 0 or pool.count < 1:
        raise ValueError(f"계정 풀 {pool_name}의 범위가 잘못됨: start={pool.start}, count={pool.count}")
    # 기본값으로 채우지 않는다. 빠지면 시작 배율을 모르는 채로 실행하게 된다
    if "scale" not in (raw.get("control") or {}):
        raise ValueError("[control]에 scale이 없다")
    scale = read_scale(raw["control"]["scale"])
    governor = read_governor(raw.get("governor"))
    route = read_route(env.get("KKUGI_ROUTE", ""))

    return Config(
        tz=ZoneInfo(raw["timezone"]),
        api_prefix=raw["api_prefix"].rstrip("/"),
        email_prefix=accounts["email_prefix"],
        email_domain=accounts["email_domain"],
        seed=int(accounts["seed"]),
        refresh_margin_seconds=int(auth["refresh_margin_seconds"]),
        synthetic_header=(header_name, synthetic["header_value"]) if header_name else None,
        search_think=(float(think["search_min"]), float(think["search_max"])),
        select_think=(float(think["select_min"]), float(think["select_max"])),
        schedule_mode=env.get("KKUGI_SCHEDULE", "realtime"),
        max_lateness=timedelta(minutes=float(raw["schedule"]["max_lateness_minutes"])),
        personas=personas,
        open_probability=open_probability,
        activity_multipliers=activity_multipliers,
        pool=pool,
        state_db=Path(env.get("KKUGI_STATE_DB") or DEFAULT_STATE_DB),
        scale=scale,
        governor=governor,
        route=route,
    )
