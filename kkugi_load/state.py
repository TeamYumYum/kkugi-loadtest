"""계정별 실행 상태를 발생기 로컬 SQLite에 저장한다.

Worker마다 자기 파일을 쓴다. Worker 사이에 상태를 옮기지 않으므로, 같은 계정 번호를 두 Worker에
배정하지 않는 것은 실행하는 사람의 책임이다(KKUGI_ACCOUNT_START, KKUGI_ACCOUNT_COUNT).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS account_state (
    email             TEXT PRIMARY KEY,
    account_index     INTEGER NOT NULL,
    persona           TEXT NOT NULL,
    member_id         INTEGER,
    access_token      TEXT,
    refresh_token     TEXT,
    onboarded         INTEGER NOT NULL DEFAULT 0,
    breakfast_ready_on TEXT,
    last_session_on   TEXT,
    updated_at        TEXT NOT NULL
)
"""


@dataclass
class AccountState:
    email: str
    account_index: int
    persona: str
    member_id: int | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    onboarded: bool = False
    # 이 날짜(YYYY-MM-DD)의 아침 기록을 확인했다
    breakfast_ready_on: str | None = None
    last_session_on: str | None = None


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # gevent greenlet은 같은 OS 스레드에서 돈다. 호출마다 바로 커밋하므로 연결 하나를 같이 쓴다
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(SCHEMA)

    def load(self, email: str, account_index: int, persona: str) -> AccountState:
        row = self._conn.execute("SELECT * FROM account_state WHERE email = ?", (email,)).fetchone()
        if row is None:
            state = AccountState(email=email, account_index=account_index, persona=persona)
            self.save(state)
            return state
        if row["account_index"] != account_index or row["persona"] != persona:
            raise ValueError(f"{email}의 저장된 계정 번호나 페르소나가 설정과 다르다")
        return AccountState(
            email=row["email"],
            account_index=row["account_index"],
            persona=row["persona"],
            member_id=row["member_id"],
            access_token=row["access_token"],
            refresh_token=row["refresh_token"],
            onboarded=bool(row["onboarded"]),
            breakfast_ready_on=row["breakfast_ready_on"],
            last_session_on=row["last_session_on"],
        )

    def save(self, state: AccountState) -> None:
        self._conn.execute(
            """
            INSERT INTO account_state (email, account_index, persona, member_id, access_token, refresh_token,
                                       onboarded, breakfast_ready_on, last_session_on, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                member_id = excluded.member_id,
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                onboarded = excluded.onboarded,
                breakfast_ready_on = excluded.breakfast_ready_on,
                last_session_on = excluded.last_session_on,
                updated_at = excluded.updated_at
            """,
            (
                state.email, state.account_index, state.persona, state.member_id, state.access_token,
                state.refresh_token, int(state.onboarded), state.breakfast_ready_on, state.last_session_on,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def close(self) -> None:
        self._conn.close()
