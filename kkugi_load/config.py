"""설정 파일(TOML)과 환경 변수를 읽는다.

페르소나별 설정은 각 페르소나가 직접 해석하므로 여기서는 원본 그대로 넘긴다.
"""

from __future__ import annotations

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
    # 페르소나 이름 → [personas.<이름>] 표
    personas: dict[str, dict[str, Any]]
    pool: Pool
    state_db: Path


def hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


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
        pool=pool,
        state_db=Path(env.get("KKUGI_STATE_DB") or DEFAULT_STATE_DB),
    )
