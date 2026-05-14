from typing import Any

import pytest

from skillbridge import Workspace
from skillbridge.client.channel import Channel, ChannelClosedError
from skillbridge.client.translator import DefaultTranslator
from skillbridge.client.workspace import _open_workspaces  # noqa: PLC2701


class DummyChannel(Channel):
    def __init__(self, max_transmission_length: int) -> None:
        super().__init__(max_transmission_length)
        self.connected = True

    def send(self, data: str) -> str:  # noqa: ARG002
        self._check_closed()
        if not self.connected:
            self.connected = True
        return 'nil'

    def flush(self) -> None:
        pass

    def try_repair(self) -> Any:
        pass

    def close(self):
        self._mark_closed()
        self.connected = False
        raise RuntimeError("no, i won't close")

    def send_exit(self) -> None:
        self._mark_closed()
        self.connected = False


class DeadChannel(Channel):
    """Simulates a channel whose server is already dead."""

    def __init__(self, max_transmission_length: int) -> None:
        super().__init__(max_transmission_length)
        self.connected = True

    def send(self, data: str) -> str:  # noqa: ARG002
        raise RuntimeError("The server unexpectedly died")

    def flush(self) -> None:
        raise BrokenPipeError("Broken pipe")

    def try_repair(self) -> Any:
        pass

    def close(self):
        self.connected = False
        raise BrokenPipeError("Broken pipe")

    def send_exit(self) -> None:
        raise BrokenPipeError("Broken pipe")


def test_a_crash_while_closing_still_clears_the_cache():
    dummy_channel = DummyChannel(1)
    ws = Workspace(channel=dummy_channel, id_=123, translator=DefaultTranslator())
    _open_workspaces[123] = ws

    ws.close()
    assert 123 not in _open_workspaces


def test_stale_remote_function_after_failed_close_raises():
    ws = Workspace(channel=DummyChannel(1), id_='close-stale', translator=DefaultTranslator())
    remote = ws.db.open_cell_view

    ws.close()

    with pytest.raises(ChannelClosedError):
        remote("lib", "cell", "view")


def test_exit_on_live_workspace_does_not_raise():
    ws = Workspace(channel=DummyChannel(1), id_='exit-live', translator=DefaultTranslator())
    _open_workspaces['exit-live'] = ws

    ws.exit()

    assert 'exit-live' not in _open_workspaces
    assert ws._closed is True


def test_exit_on_dead_server_does_not_raise():
    ws = Workspace(channel=DeadChannel(1), id_='exit-dead', translator=DefaultTranslator())
    _open_workspaces['exit-dead'] = ws

    ws.exit()

    assert 'exit-dead' not in _open_workspaces
    assert ws._closed is True


def test_exit_then_close_does_not_raise():
    ws = Workspace(channel=DummyChannel(1), id_='exit-close', translator=DefaultTranslator())
    _open_workspaces['exit-close'] = ws

    ws.exit()
    ws.close()

    assert 'exit-close' not in _open_workspaces


def test_workspace_usage_after_exit_raises():
    ws = Workspace(channel=DummyChannel(1), id_='exit-use', translator=DefaultTranslator())

    ws.exit()

    with pytest.raises(ChannelClosedError):
        ws['some_function']()

    with pytest.raises(ChannelClosedError):
        ws.flush()

    with pytest.raises(ChannelClosedError):
        ws.define('foo', ['x'], 'x')

    # FunctionCollection paths also go through the replaced channel
    with pytest.raises(ChannelClosedError):
        ws.db.open_cell_view("lib", "cell", "view")
