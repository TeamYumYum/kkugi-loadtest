"""설정 파일(TOML)과 환경 변수를 읽는다.

페르소나별 설정은 각 페르소나가 직접 해석하므로 여기서는 원본 그대로 넘긴다.
"""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass
from datetime import time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "default.toml"
DEFAULT_STATE_DB = ROOT / "state" / "accounts.db"
# 시간대별 확률표의 칸 수. 인덱스가 한국 시간의 시(0~23)다
HOURS = 24


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


def load(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    path = Path(env.get("KKUGI_CONFIG") or DEFAULT_CONFIG)
    with path.open("rb") as f:
        raw = tomllib.load(f)

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
    )
