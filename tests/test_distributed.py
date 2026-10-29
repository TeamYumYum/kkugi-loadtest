from types import SimpleNamespace

from kkugi_load import config as config_module
from kkugi_load.config import Pool
from kkugi_load.control import Control
from kkugi_load.distributed import (
    MSG_ACCEPTED, MSG_APPLIED, MSG_CONTROL, MSG_REGISTER, PoolRegistry, rebalancing_problem, setup_master, setup_worker,
)

DEFAULT_TEXT = config_module.DEFAULT_CONFIG.read_text(encoding="utf-8")


def _claim(start, end, pool="p"):
    return {"pool": pool, "start": start, "end": end}


def test_overlapping_range_of_running_worker_is_rejected():
    registry = PoolRegistry()
    assert registry.register("w1", _claim(0, 1000), lambda _: True) is None
    assert registry.register("w2", _claim(1000, 2000), lambda _: True) is None
    reason = registry.register("w3", _claim(999, 1001), lambda _: True)
    assert reason and "w1" in reason
    assert "w3" not in registry.claims


def test_range_of_missing_worker_can_be_reused():
    registry = PoolRegistry()
    registry.register("w1", _claim(0, 1000), lambda _: True)
    assert registry.register("w2", _claim(0, 1000), lambda node: node != "w1") is None


def test_same_worker_can_register_again():
    registry = PoolRegistry()
    registry.register("w1", _claim(0, 1000), lambda _: True)
    assert registry.register("w1", _claim(0, 1000), lambda _: True) is None


def test_master_without_rebalancing_is_rejected():
    reason = rebalancing_problem(SimpleNamespace(enable_rebalancing=False))
    assert reason and "--enable-rebalancing" in reason


def test_master_with_rebalancing_passes():
    assert rebalancing_problem(SimpleNamespace(enable_rebalancing=True)) is None


def test_unknown_options_are_rejected():
    assert rebalancing_problem(None)
    assert rebalancing_problem(SimpleNamespace())


# --- 승인 메시지와 조절 메시지 ---

class FakeRunner:
    def __init__(self):
        self.handlers = {}
        self.sent = []
        self.clients = {}

    def register_message(self, msg_type, listener, concurrent=False):
        self.handlers[msg_type] = listener

    def send_message(self, msg_type, data=None, client_id=None):
        self.sent.append((msg_type, data, client_id))


def _environment(runner):
    return SimpleNamespace(runner=runner, parsed_options=SimpleNamespace(enable_rebalancing=True), web_ui=None)


def _master():
    runner = FakeRunner()
    control = Control(1.0, DEFAULT_TEXT, {})
    setup_master(_environment(runner), control)
    return runner, control


def test_accepted_message_carries_current_scale_and_reloaded_config():
    runner, control = _master()
    control.set_requested_scale(0.3)
    control.config_text, control.config_version = "reloaded", 2
    runner.handlers[MSG_REGISTER](environment=None, msg=SimpleNamespace(node_id="w1", data=_claim(0, 10)))
    assert runner.sent == [(MSG_ACCEPTED, {"scale": 0.3, "version": 2, "config": "reloaded"}, "w1")]


def test_master_records_worker_applied_status():
    runner, control = _master()
    status = {"scale": 0.3, "version": 0, "error": None}
    runner.handlers[MSG_APPLIED](environment=None, msg=SimpleNamespace(node_id="w1", data=status))
    assert control.workers["w1"]["scale"] == 0.3


class FakeApplier:
    def __init__(self, gate_holder):
        self.received = []
        self.gate_holder = gate_holder

    def apply(self, payload):
        # 적용하는 동안에는 아직 세션을 실행하지 않아야 한다
        self.received.append((payload, self.gate_holder[0].accepted))
        return {"scale": payload["scale"], "version": payload["version"], "error": None}


def test_worker_applies_accepted_payload_before_opening_gate_and_reports():
    runner = FakeRunner()
    holder = []
    applier = FakeApplier(holder)
    gate = setup_worker(_environment(runner), Pool("base", 0, 10, ()), applier)
    holder.append(gate)
    payload = {"scale": 0.3, "version": 1, "config": "reloaded"}
    runner.handlers[MSG_ACCEPTED](environment=None, msg=SimpleNamespace(data=payload))
    assert applier.received == [(payload, False)] and gate.accepted
    runner.handlers[MSG_CONTROL](environment=None, msg=SimpleNamespace(data={**payload, "scale": 0.0}))
    applied = [data for msg_type, data, _ in runner.sent if msg_type == MSG_APPLIED]
    assert applied == [{"scale": 0.3, "version": 1, "error": None}, {"scale": 0.0, "version": 1, "error": None}]
