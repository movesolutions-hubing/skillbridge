from __future__ import annotations

import contextlib
import logging
from argparse import ArgumentParser
from logging import WARNING, basicConfig, getLogger
from os import getenv
from pathlib import Path
from select import select
from socketserver import (
    BaseRequestHandler,
    BaseServer,
    StreamRequestHandler,
    ThreadingMixIn,
)
from sys import argv, platform, stderr, stdin, stdout
from sys import exit as sys_exit
from typing import Iterable

LOG_DIRECTORY = Path(getenv("SKILLBRIDGE_LOG_DIRECTORY", "."))
LOG_FILE = LOG_DIRECTORY / "skillbridge_server.log"
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
LOG_DATE_FORMAT = "%d.%m.%Y %H:%M:%S"
LOG_LEVEL = WARNING

basicConfig(filename=LOG_FILE, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
logger = getLogger("python-server")


def send_to_skill(data: str) -> None:
    try:
        stdout.write(data)
        stdout.write("\n")
        stdout.flush()
        logger.debug(f"Successfully sent {len(data)} chars to skill")
    except Exception as e:
        logger.error(f"Failed to send to skill: {type(e).__name__}: {e}")
        raise


def read_from_skill(timeout: float | None) -> str:
    try:
        readable = data_ready(timeout)

        if readable:
            result = stdin.readline()
            logger.debug(f"Read {len(result)} chars from skill")
            return result

        logger.warning("Skill timeout - no response received")
        return "failure <timeout>"
    except Exception as e:
        logger.error(f"Failed to read from skill: {type(e).__name__}: {e}")
        raise


def create_windows_server_class(single: bool) -> type[BaseServer]:
    from socketserver import TCPServer  # noqa: PLC0415

    class SingleWindowsServer(TCPServer):
        request_queue_size = 0
        allow_reuse_address = True

        def __init__(self, port: int, handler: type[BaseRequestHandler]) -> None:
            super().__init__(("localhost", port), handler)

        def server_bind(self) -> None:
            try:
                from socket import (  # type: ignore[attr-defined]  # noqa: PLC0415
                    SIO_LOOPBACK_FAST_PATH,
                )

                self.socket.ioctl(  # type: ignore[attr-defined]
                    SIO_LOOPBACK_FAST_PATH,
                    True,  # noqa: FBT003
                )
            except ImportError:
                pass
            super().server_bind()

    class ThreadingWindowsServer(ThreadingMixIn, SingleWindowsServer):
        pass

    return SingleWindowsServer if single else ThreadingWindowsServer


def data_windows_ready(timeout: float | None) -> bool:
    _ = timeout
    return True


def create_unix_server_class(single: bool) -> type[BaseServer]:
    from socketserver import UnixStreamServer  # noqa: PLC0415

    class SingleUnixServer(UnixStreamServer):
        request_queue_size = 0
        allow_reuse_address = True

        def __init__(self, file: str, handler: type[BaseRequestHandler]) -> None:
            self.path = f"/tmp/skill-server-{file}.sock"
            with contextlib.suppress(FileNotFoundError):
                Path(self.path).unlink()

            super().__init__(self.path, handler)

    class ThreadingUnixServer(ThreadingMixIn, SingleUnixServer):
        pass

    return SingleUnixServer if single else ThreadingUnixServer


def data_unix_ready(timeout: float | None) -> bool:
    readable, _, _ = select([stdin], [], [], timeout)

    return bool(readable)


if platform == "win32":
    data_ready = data_windows_ready
    create_server_class = create_windows_server_class
else:
    create_server_class = create_unix_server_class
    data_ready = data_unix_ready


class Handler(StreamRequestHandler):
    def receive_all(self, remaining: int) -> Iterable[bytes]:
        while remaining:
            data = self.request.recv(remaining)
            remaining -= len(data)
            yield data

    def handle_one_request(self) -> bool:
        client = self.client_address

        try:
            length = self.request.recv(10)
        except Exception as e:
            logger.error(f"Failed to recv length from {client}: {type(e).__name__}: {e}")
            return False

        if not length:
            logger.warning(f"Client {client} lost connection - empty length")
            return False

        logger.debug(f"Got length {length} from {client}")

        try:
            length = int(length)
        except ValueError:
            logger.error(f"Invalid length from {client}: {length!r}")
            return False

        try:
            command = b"".join(self.receive_all(length))
            logger.debug(f"Received {len(command)} bytes from {client}")
        except Exception as e:
            logger.error(f"Failed to recv command from {client}: {type(e).__name__}: {e}")
            return False

        if command.startswith(b"$close"):
            logger.debug(f"Client {client} disconnected normally")
            return False

        logger.debug(f"Got data from {client}: {command[:1000].decode()}")

        try:
            send_to_skill(command.decode())
            logger.debug(f"Sent data to skill for {client}")
        except Exception as e:
            logger.error(f"Failed to send to skill for {client}: {e}")
            return False

        try:
            timeout = self.server.skill_timeout  # type: ignore[attr-defined]
            result = read_from_skill(timeout).encode()
            logger.debug(f"Got skill response for {client}: {result[:1000]!r}")
        except Exception as e:
            logger.error(f"Failed to read from skill for {client}: {e}")
            return False

        try:
            self.request.send(f"{len(result):10}".encode())
            logger.debug(f"Sent length header {len(result)} to {client}")
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.error(f"Broken pipe sending length to {client}: {e}")
            return False
        except Exception as e:
            logger.error(f"Error sending length to {client}: {e}")
            return False

        try:
            self.request.send(result)
            logger.debug(f"Sent response data to {client}")
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.error(f"Broken pipe sending data to {client}: {e}")
            return False
        except Exception as e:
            logger.error(f"Error sending data to {client}: {e}")
            return False

        return True

    def try_handle_one_request(self) -> bool:
        try:
            return self.handle_one_request()
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.error(
                f"Connection broken with client {self.client_address}: " f"{type(e).__name__}: {e}"
            )
            return False
        except OSError as e:
            logger.error(
                f"OS error with client {self.client_address}: "
                f"{e.errno if hasattr(e, 'errno') else 'unknown'}: {e}"
            )
            return False
        except Exception:
            logger.exception(f"Failed to handle request from {self.client_address}")
            return False

    def handle(self) -> None:
        logger.info(f"client {self.client_address} connected")
        client_is_connected = True
        while client_is_connected:
            client_is_connected = self.try_handle_one_request()


def main(id_: str, log_level: str, notify: bool, single: bool, timeout: float | None) -> None:
    logger.setLevel(getattr(logging, log_level))

    server_class = create_server_class(single)

    with server_class(id_, Handler) as server:
        server.skill_timeout = timeout  # type: ignore[attr-defined]
        logger.info(
            f"starting server id={id_} log={log_level} notify={notify} "
            f"single={single} timeout={timeout}",
        )
        if notify:
            send_to_skill("running")
        server.serve_forever()


if __name__ == "__main__":
    log_levels = ["DEBUG", "WARNING", "INFO", "ERROR", "CRITICAL", "FATAL"]
    argument_parser = ArgumentParser(argv[0])
    if platform == "win32":
        argument_parser.add_argument("id", type=int)
    else:
        argument_parser.add_argument("id")
    argument_parser.add_argument("log_level", choices=log_levels)
    argument_parser.add_argument("--notify", action="store_true")
    argument_parser.add_argument("--single", action="store_true")
    argument_parser.add_argument("--timeout", type=float, default=None)

    ns = argument_parser.parse_args()

    if platform == "win32" and ns.timeout is not None:
        print("Timeout is not possible on Windows", file=stderr)
        sys_exit(1)

    with contextlib.suppress(KeyboardInterrupt):
        main(ns.id, ns.log_level, ns.notify, ns.single, ns.timeout)
