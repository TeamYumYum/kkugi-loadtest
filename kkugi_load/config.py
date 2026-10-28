"""설정 파일(TOML)과 환경 변수를 읽는다."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "default.toml"
DEFAULT_STATE_DB = ROOT / "state" / "accounts.db"


@dataclass(frozen=True)
class DietLunchConfig:
    items: int
    grams_min: int
    grams_max: int
    breakfast_from: time
    breakfast_to: time
    lunch_from: time
    lunch_to: time
    breakfast_keywords: tuple[str, ...]
    lunch_keywords: tuple[str, ...]
    preferred_lunch_keywords: int


@dataclass(frozen=True)
class Config:
    tz: ZoneInfo
    api_prefix: str
    email_prefix: str
    email_domain: str
    seed: int
    auth_method: str
    refresh_margin_seconds: int
    synthetic_header: tuple[str, str] | None
    search_think: tuple[float, float]
    select_think: tuple[float, float]
    diet_lunch: DietLunchConfig
    # 이 프로세스가 맡는 계정 번호 범위 [account_start, account_start + account_count)
    account_start: int
    account_count: int
    state_db: Path


def _hhmm(value: str) -> time:
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
    lunch = raw["diet_lunch"]

    if auth["method"] != "dev":
        raise ValueError(f"지원하지 않는 인증 방식: {auth['method']}")
    header_name = synthetic["header_name"].strip()

    diet_lunch = DietLunchConfig(
        items=int(lunch["items"]),
        grams_min=int(lunch["grams_min"]),
        grams_max=int(lunch["grams_max"]),
        breakfast_from=_hhmm(lunch["breakfast_from"]),
        breakfast_to=_hhmm(lunch["breakfast_to"]),
        lunch_from=_hhmm(lunch["lunch_from"]),
        lunch_to=_hhmm(lunch["lunch_to"]),
        breakfast_keywords=tuple(lunch["breakfast_keywords"]),
        lunch_keywords=tuple(lunch["lunch_keywords"]),
        preferred_lunch_keywords=int(lunch["preferred_lunch_keywords"]),
    )
    if diet_lunch.preferred_lunch_keywords < diet_lunch.items:
        raise ValueError("preferred_lunch_keywords는 items 이상이어야 한다")

    account_count = int(env.get("KKUGI_ACCOUNT_COUNT", "1"))
    if account_count < 1:
        raise ValueError("KKUGI_ACCOUNT_COUNT는 1 이상이어야 한다")

    return Config(
        tz=ZoneInfo(raw["timezone"]),
        api_prefix=raw["api_prefix"].rstrip("/"),
        email_prefix=accounts["email_prefix"],
        email_domain=accounts["email_domain"],
        seed=int(accounts["seed"]),
        auth_method=auth["method"],
        refresh_margin_seconds=int(auth["refresh_margin_seconds"]),
        synthetic_header=(header_name, synthetic["header_value"]) if header_name else None,
        search_think=(float(think["search_min"]), float(think["search_max"])),
        select_think=(float(think["select_min"]), float(think["select_max"])),
        diet_lunch=diet_lunch,
        account_start=int(env.get("KKUGI_ACCOUNT_START", "0")),
        account_count=account_count,
        state_db=Path(env.get("KKUGI_STATE_DB") or DEFAULT_STATE_DB),
    )
