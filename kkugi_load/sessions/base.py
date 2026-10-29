"""세션 공통 타입."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from kkugi_load.client.api import KkugiApi
from kkugi_load.config import Config


class SessionSkipped(Exception):
    """세션을 실행할 필요가 없다. 실패가 아니다."""


class ValidationFailed(Exception):
    """HTTP 응답은 정상이지만 업무 결과가 기대와 다르다."""


@dataclass
class SessionContext:
    api: KkugiApi
    config: Config
    now: Callable[[], datetime]
    sleep: Callable[[float], None]
    rng: random.Random

    def think(self, bounds: tuple[float, float]) -> None:
        """사용자가 입력하거나 고르는 시간. 화면이 자동으로 부르는 요청 사이에는 쓰지 않는다."""
        self.sleep(self.rng.uniform(*bounds))
