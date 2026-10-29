"""꾸기 backend 호출. 공통 응답 포장 해석, 토큰 관리, Locust 통계 이름을 맡는다.

통계 이름에는 식별자를 넣지 않고 경로 템플릿을 쓴다(예: /daily-summaries/{date}).
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

from kkugi_load.accounts.identity import Account
from kkugi_load.accounts.state import AccountState, StateStore
from kkugi_load.config import Config


class ApiError(Exception):
    def __init__(self, name: str, status: int, code: str | None, message: str):
        super().__init__(f"{name}: HTTP {status} {code or ''} {message}".strip())
        self.status = status
        self.code = code


@dataclass
class Result:
    status: int
    data: Any
    # 허용한 오류 응답이면 그 오류 코드
    error_code: str | None = None


@dataclass
class Expect:
    """성공으로 셀 응답. ok는 정상 상태 코드, allowed는 {상태 코드: 허용하는 오류 코드 집합}이다."""

    ok: tuple[int, ...] = (200,)
    allowed: dict[int, set[str]] = field(default_factory=dict)


def jwt_expires_at(token: str) -> float | None:
    """서명은 검증하지 않고 exp만 읽는다. 재발급 시점을 정하는 용도다."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, KeyError, ValueError):
        return None


def _parse(response) -> tuple[Any, str | None, str]:
    try:
        body = response.json()
    except ValueError:
        return None, None, (response.text or "")[:200]
    if not isinstance(body, dict):
        return None, None, "공통 응답 포장이 아님"
    error = body.get("error") or {}
    return body.get("data"), error.get("code"), error.get("message") or ""


class KkugiApi:
    def __init__(self, client, config: Config, account: Account, state: AccountState, store: StateStore):
        self.client = client
        self.config = config
        self.account = account
        self.state = state
        self.store = store

    # --- 요청 ---

    def call(
        self, method: str, path: str, *, name: str, expect: Expect | None = None, auth: bool = True,
        json_body: Any = None, params: dict | None = None,
    ) -> Result:
        expect = expect or Expect()
        if auth:
            self.ensure_token()
        result = self._send(method, path, name, expect, auth, json_body, params)
        if result is not None:
            return result
        # 액세스 토큰이 서버에서 거절되었다. 한 번만 재발급하고 다시 보낸다
        self._renew()
        result = self._send(method, path, name, expect, auth, json_body, params, retry=False)
        assert result is not None
        return result

    def _send(self, method, path, name, expect, auth, json_body, params, retry=True) -> Result | None:
        headers = {}
        if self.config.synthetic_header:
            headers[self.config.synthetic_header[0]] = self.config.synthetic_header[1]
        if auth:
            headers["Authorization"] = f"Bearer {self.state.access_token}"
        with self.client.request(
            method, self.config.api_prefix + path, name=name, headers=headers, json=json_body, params=params,
            catch_response=True,
        ) as response:
            status = response.status_code
            data, code, message = _parse(response)
            if status in expect.ok:
                response.success()
                return Result(status, data)
            if code is not None and code in expect.allowed.get(status, set()):
                response.success()
                return Result(status, None, code)
            response.failure(f"HTTP {status} {code or ''} {message}".strip())
            if auth and retry and status == 401 and code == "UNAUTHORIZED":
                return None
            raise ApiError(name, status, code, message)

    # --- 인증 ---

    def ensure_token(self) -> None:
        if not self.state.access_token:
            self.login()
            return
        expires_at = jwt_expires_at(self.state.access_token)
        if expires_at is None or expires_at - time.time() < self.config.refresh_margin_seconds:
            self._renew()

    def login(self) -> None:
        result = self.call(
            "POST", "/auth/login/dev", name="[auth] POST /auth/login/dev", auth=False,
            json_body={"email": self.account.email},
        )
        data = result.data
        self.state.member_id = data["memberId"]
        self.state.access_token = data["accessToken"]
        self.state.refresh_token = data["refreshToken"]
        self.state.onboarded = self.state.onboarded or bool(data["onboarded"])
        self.store.save(self.state)

    def _renew(self) -> None:
        """리프레시 토큰으로 재발급한다. 리프레시 토큰이 없거나 거절되면 다시 로그인한다."""
        if self.state.refresh_token:
            result = self.call(
                "POST", "/auth/refresh", name="[auth] POST /auth/refresh", auth=False,
                json_body={"refreshToken": self.state.refresh_token},
                expect=Expect(allowed={401: {"AUTH_REFRESH_INVALID"}}),
            )
            if result.error_code is None:
                # 재발급하면 이전 리프레시 토큰이 폐기되므로 바로 저장한다
                self.state.access_token = result.data["accessToken"]
                self.state.refresh_token = result.data["refreshToken"]
                self.store.save(self.state)
                return
        self.login()
