"""계정별 실행 상태를 발생기 로컬 SQLite에 저장한다.

Worker마다 자기 파일을 쓴다. Worker 사이에 상태를 옮기지 않는다. 실행 중인 두 Worker가 같은 계정 번호를
맡지 않게 하는 것은 Master의 계정 범위 검사다(kkugi_load/distributed.py).

계정 번호가 기본 키다. 이메일은 계정 번호와 generation에서 계산하므로 저장하지 않는다.
옛 형식의 파일은 옮기지 않고 시작을 멈춘다. 지우고 다시 시작하면 된다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS account_state (
    account_index     INTEGER PRIMARY KEY,
    persona           TEXT NOT NULL,
    generation        INTEGER NOT NULL DEFAULT 0,
    member_id         INTEGER,
    access_token      TEXT,
    refresh_token     TEXT,
    onboarded         INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT NOT NULL
)
"""
COLUMNS = {
    "account_index", "persona", "generation", "member_id", "access_token", "refresh_token", "onboarded",
    "updated_at",
}


class StaleStateDb(Exception):
    """상태 DB가 지금 형식과 다르다."""


@dataclass
class AccountState:
    account_index: int
    persona: str
    # 탈퇴되어 다시 만든 횟수. 이메일 번호를 정한다(identity.email_of)
    generation: int = 0
    member_id: int | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    onboarded: bool = False


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # gevent greenlet은 같은 OS 스레드에서 돈다. 호출마다 바로 커밋하므로 연결 하나를 같이 쓴다
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(SCHEMA)
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(account_state)")}
        if columns != COLUMNS:
            self._conn.close()
            raise StaleStateDb(
                f"상태 DB {path}의 account_state 형식이 옛 형식이다(칼럼 {sorted(columns)}). "
                "변환하지 않으므로 이 파일을 지우고 다시 시작하라"
            )

    def load(self, account_index: int, persona: str) -> AccountState:
        row = self._conn.execute(
            "SELECT * FROM account_state WHERE account_index = ?", (account_index,),
        ).fetchone()
        if row is None:
            state = AccountState(account_index=account_index, persona=persona)
            self.save(state)
            return state
        if row["persona"] != persona:
            raise ValueError(f"계정 {account_index}의 저장된 페르소나 {row['persona']}가 설정 {persona}와 다르다")
        return AccountState(
            account_index=row["account_index"],
            persona=row["persona"],
            generation=row["generation"],
            member_id=row["member_id"],
            access_token=row["access_token"],
            refresh_token=row["refresh_token"],
            onboarded=bool(row["onboarded"]),
        )

    def save(self, state: AccountState) -> None:
        self._conn.execute(
            """
            INSERT INTO account_state (account_index, persona, generation, member_id, access_token, refresh_token,
                                       onboarded, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_index) DO UPDATE SET
                generation = excluded.generation,
                member_id = excluded.member_id,
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                onboarded = excluded.onboarded,
                updated_at = excluded.updated_at
            """,
            (
                state.account_index, state.persona, state.generation, state.member_id, state.access_token,
                state.refresh_token, int(state.onboarded), datetime.now(timezone.utc).isoformat(),
            ),
        )

    def close(self) -> None:
        self._conn.close()
