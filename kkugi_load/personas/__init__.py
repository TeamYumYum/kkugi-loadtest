"""페르소나 목록. 새 페르소나는 여기에 등록하고 설정 파일에 [personas.<이름>]을 더한다."""

from __future__ import annotations

from kkugi_load.accounts.identity import Account, email_of, persona_of
from kkugi_load.config import Config
from kkugi_load.personas.base import Persona
from kkugi_load.personas.dieter import Dieter

PERSONAS: dict[str, Persona] = {persona.name: persona for persona in [Dieter()]}


def build_account(config: Config, index: int) -> Account:
    name = persona_of(config, index)
    if name not in PERSONAS:
        raise ValueError(f"설정에 있지만 구현하지 않은 페르소나: {name}")
    return Account(
        index=index, email=email_of(config, index), persona=name,
        profile=PERSONAS[name].build_profile(config, index),
    )
