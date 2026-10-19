# ruff: noqa: D102, D103, D107, FBT002, RET503, RUF005, SIM105
# SPDX-License-Identifier: GPL-3.0-or-later
"""Private, loopback-only launcher for the pinned QQMusicApi web application."""

import ctypes
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import select
import shutil
import signal
import socket
import stat
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

VERSION = "0.8.2-iwebplus.10"
NONCE = re.compile(r"[0-9a-f]{32}")
TOKEN = re.compile(r"[0-9a-f]{64}")


class Refused(Exception):
    """An unsafe or incompatible invocation."""


def require(condition, message):
    if not condition:
        raise Refused(message)


def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def private_dir(path, create=False, parent=False):
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & (0o022 if parent else 0o077),
        "unsafe runtime directory",
    )


def runtime_root(create=False):
    root = Path.cwd() / "runtime"
    private_dir(root, create, parent=True)
    root /= "qq"
    private_dir(root, create)
    return root


def run_dir(value):
    path = Path(value)
    require(".." not in path.parts, "unsafe runtime path")
    if not path.is_absolute():
        path = Path.cwd() / path
    require(path.parent == runtime_root() and NONCE.fullmatch(path.name), "unsafe runtime path")
    private_dir(path)
    return path


def open_private(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    info = os.fstat(fd)
    if not (
        stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1 and not info.st_mode & 0o077
    ):
        os.close(fd)
        raise Refused("unsafe runtime file")
    return fd


def read_json(path):
    with os.fdopen(open_private(path, os.O_RDONLY)) as stream:
        return json.loads(stream.read(16384))


def write_json(path, value):
    temporary = path.with_name("." + path.name + "-" + secrets.token_hex(8))
    with os.fdopen(open_private(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "w") as stream:
        json.dump(value, stream, separators=(",", ":"))
    os.replace(temporary, path)


def session(path):
    value = read_json(path / "session.json")
    require(
        value.get("protocol") == 1 and value.get("nonce") == path.name and value.get("version") == VERSION,
        "invalid runtime session",
    )
    return value


def lock_run(path):
    fd = open_private(path / "run.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise Refused("runtime is active") from None
    return fd


def identity(pid):
    require(type(pid) is int and pid > 1, "invalid pid")
    proc = Path("/proc") / str(pid)
    try:
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        exe = (proc / "exe").stat()
        return {
            "pid": pid,
            "startTicks": fields[19],
            "exeDev": exe.st_dev,
            "exeIno": exe.st_ino,
            "uid": proc.stat().st_uid,
        }
    except (FileNotFoundError, ProcessLookupError):
        return None


def owned(pid, path):
    current = identity(pid)
    if current is None:
        return False
    executable = Path(sys.executable).stat()
    if (current["uid"], current["exeDev"], current["exeIno"]) != (os.getuid(), executable.st_dev, executable.st_ino):
        return False
    proc = Path("/proc") / str(pid)
    try:
        args = (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        expected = (
            [b"serve"] if getattr(sys, "frozen", False) else [os.fsencode(str(Path(__file__).resolve())), b"serve"]
        )
        if args[1:] != expected:
            return False
        env = dict(item.split(b"=", 1) for item in (proc / "environ").read_bytes().split(b"\0") if b"=" in item)
        return (
            env.get(b"IWEBPLUS_RUN_DIR") == os.fsencode(path)
            and env.get(b"IWEBPLUS_MODULE_NONCE") == path.name.encode()
            and env.get(b"IWEBPLUS_MODULE_VERSION") == VERSION.encode()
        )
    except (FileNotFoundError, ProcessLookupError):
        return False


def process_record(path):
    value = read_json(path / "process.json")
    require(
        value.get("nonce") == path.name
        and value.get("version") == VERSION
        and value.get("bootId") == Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "invalid process record",
    )
    require(isinstance(value.get("processes"), list) and 1 <= len(value["processes"]) <= 2, "invalid process record")
    require(
        [item["pid"] for item in value["processes"]] == list(dict.fromkeys([value["pid"], value["supervisorPid"]])),
        "invalid process record",
    )
    return value


def verified_handles(record, path):
    handles = []
    try:
        for expected in record["processes"]:
            pid = expected["pid"]
            try:
                fd = os.pidfd_open(pid)
            except ProcessLookupError:
                continue
            handles.append((pid, fd))
            current = identity(pid)
            if current is not None:
                require(current == expected and owned(pid, path), "process ownership mismatch")
        return handles
    except BaseException:
        for _, fd in handles:
            os.close(fd)
        raise


def exited(fd, timeout=0):
    return bool(select.select([fd], [], [], timeout)[0])


def stop_handle(fd, grace):
    if not exited(fd):
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        if not exited(fd, grace):
            signal.pidfd_send_signal(fd, signal.SIGKILL)
            require(exited(fd, 2), "process did not exit")


def prepare():
    # Host storage may create session files with 0644. Protect their parent
    # before any account operation, without reading or rewriting credentials.
    data = Path.cwd() / "data"
    data.mkdir(mode=0o700, exist_ok=True)
    info = data.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid(), "unsafe storage directory")
    fd = os.open(data, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(fd)
        require(
            (opened.st_dev, opened.st_ino, opened.st_uid) == (info.st_dev, info.st_ino, info.st_uid),
            "storage directory changed",
        )
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    root = runtime_root(create=True)
    nonce, token = secrets.token_hex(16), secrets.token_hex(32)
    path = root / nonce
    path.mkdir(mode=0o700)
    (path / "tmp").mkdir(mode=0o700)
    os.close(open_private(path / "run.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL))
    write_json(
        path / "session.json",
        {
            "protocol": 1,
            "nonce": nonce,
            "version": VERSION,
            "tokenSha256": hashlib.sha256(token.encode()).hexdigest(),
        },
    )
    emit({"nonce": nonce, "token": token, "runDir": str(path)})


def wait_ready(value):
    path = run_dir(value)
    session(path)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            ready = read_json(path / "ready.json")
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        record = process_record(path)
        require(
            all(ready.get(key) == record.get(key) for key in ("nonce", "pid", "supervisorPid", "version"))
            and ready.get("protocol") == 1
            and type(ready.get("port")) is int
            and 0 < ready["port"] < 65536,
            "invalid ready record",
        )
        handles = verified_handles(record, path)
        try:
            require(
                len(handles) == len(record["processes"]) and not any(exited(fd) for _, fd in handles),
                "runtime has exited",
            )
        finally:
            for _, fd in handles:
                os.close(fd)
        emit(ready)
        return
    raise Refused("ready timeout")


def stop(pid, value, nonce):
    path = run_dir(value)
    session(path)
    require(nonce == path.name, "nonce mismatch")
    require(pid.isdecimal() and int(pid) > 1, "invalid pid")
    pid = int(pid)
    # Cancellation can arrive while the bootloader is still extracting files.
    # Give the launcher a chance to publish its identity before falling back.
    deadline = time.monotonic() + 3
    while not (path / "process.json").exists() and identity(pid) is not None:
        require(owned(pid, path), "process ownership mismatch")
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    if (path / "process.json").exists():
        record = process_record(path)
        require(pid in (record["pid"], record["supervisorPid"]), "pid mismatch")
    else:
        current = identity(pid)
        if current is None:
            emit({"ok": True})
            return
        require(owned(pid, path), "process ownership mismatch")
        children = (Path("/proc") / str(pid) / "task" / str(pid) / "children").read_text().split()
        processes = []
        for child in children:
            child_id = identity(int(child))
            if child_id is not None:
                require(owned(int(child), path), "process ownership mismatch")
                processes.append(child_id)
        require(len(processes) <= 1, "unexpected process tree")
        record = {"processes": processes + [current]}
    handles = verified_handles(record, path)
    try:
        for index, (_, fd) in enumerate(handles):
            if index:
                # The bootloader normally exits itself after removing _MEI*.
                exited(fd, 3)
            stop_handle(fd, 6 if index == 0 else 2)
    finally:
        for _, fd in handles:
            os.close(fd)
    emit({"ok": True})


def clean(value):
    path = run_dir(value)
    session(path)
    fd = lock_run(path)
    try:
        if (path / "process.json").exists():
            handles = verified_handles(process_record(path), path)
            try:
                require(all(exited(handle) for _, handle in handles), "runtime is active")
            finally:
                for _, handle in handles:
                    os.close(handle)
        # Also catches a bootloader not yet holding run.lock / process.json.
        for proc in Path("/proc").iterdir():
            if proc.name.isdecimal() and int(proc.name) > 1:
                try:
                    require(not owned(int(proc.name), path), "runtime is active")
                except PermissionError:
                    continue
        require(shutil.rmtree.avoids_symlink_attacks, "safe cleanup unavailable")
        shutil.rmtree(path)
    finally:
        os.close(fd)
    emit({"ok": True})


class PrivateApp:
    """Authenticate before routing, including errors, documentation and health."""

    def __init__(self, app, token, ready, path):
        self.app, self.authorization, self.ready = app, b"Bearer " + token.encode(), ready
        self.path = path

    def record_error(self, scope, error):
        # Host command.start discards stdout/stderr. Keep one private diagnostic
        # without exception messages, locals, request URLs, headers or bodies.
        frames, trace = [], error.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            frames.append({"file": Path(code.co_filename).name, "function": code.co_name, "line": trace.tb_lineno})
            trace = trace.tb_next
        stage = {
            "/login/qrcode/qq": "qr-create",
            "/login/qrcode/qq/status": "qr-status",
            "/user/get_vip_info": "account-check",
        }.get(scope["path"], "business")
        try:
            write_json(
                self.path / "last-error.json",
                {
                    "time": int(time.time()),
                    "stage": stage,
                    "type": type(error).__name__,
                    "frames": frames[-8:],
                },
            )
        except OSError:
            pass

    async def reply(self, send, status, value):
        data = json.dumps(value, separators=(",", ":")).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(data)).encode()),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": data})

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return await send({"type": "websocket.close", "code": 1008})
        headers = scope["headers"]
        auth = [value for key, value in headers if key == b"authorization"]
        if len(auth) != 1 or not hmac.compare_digest(auth[0], self.authorization):
            return await self.reply(send, 401, {"error": "unauthorized"})
        if any(key == b"origin" for key, _ in headers):
            return await self.reply(send, 403, {"error": "cross-origin requests disabled"})
        hosts = [value for key, value in headers if key == b"host"]
        if hosts != [f"127.0.0.1:{self.ready['port']}".encode()]:
            return await self.reply(send, 400, {"error": "invalid host"})
        if scope["path"] == "/_iwebplus/health":
            if scope["method"] != "GET":
                return await self.reply(send, 405, {"error": "method not allowed"})
            return await self.reply(send, 200, {**self.ready, "ok": True})
        blocked = False

        async def guarded_send(message):
            nonlocal blocked
            if message["type"] == "http.response.start":
                if 300 <= message["status"] < 400:
                    blocked = True
                    return await self.reply(send, 502, {"error": "redirects disabled"})
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if not key.lower().startswith(b"access-control-")
                    and key.lower() not in (b"location", b"cache-control")
                ]
                message["headers"].append((b"cache-control", b"no-store"))
            if not blocked:
                await send(message)

        try:
            return await self.app(scope, receive, guarded_send)
        except Exception as error:
            self.record_error(scope, error)
            raise


def serve():
    path = run_dir(os.environ.get("IWEBPLUS_RUN_DIR", ""))
    metadata = session(path)
    token = os.environ.get("IWEBPLUS_MODULE_TOKEN", "")
    require(
        TOKEN.fullmatch(token)
        and hmac.compare_digest(metadata["tokenSha256"], hashlib.sha256(token.encode()).hexdigest()),
        "invalid module token",
    )
    require(
        os.environ.get("IWEBPLUS_MODULE_NONCE") == path.name and os.environ.get("IWEBPLUS_MODULE_VERSION") == VERSION,
        "module identity mismatch",
    )
    fd = lock_run(path)
    try:
        require(not (path / "process.json").exists(), "runtime session already used")
        pid = os.getpid()
        supervisor = os.getppid() if getattr(sys, "frozen", False) else pid
        require(owned(pid, path) and owned(supervisor, path), "invalid supervisor")
        if supervisor != pid:
            # A host killing the bootloader must not leave an orphan server.
            require(
                ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) == 0 and os.getppid() == supervisor,
                "supervisor exited",
            )
        record = {
            "nonce": path.name,
            "version": VERSION,
            "pid": pid,
            "supervisorPid": supervisor,
            "bootId": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "processes": [identity(item) for item in dict.fromkeys([pid, supervisor])],
        }
        write_json(path / "process.json", record)
        # No inherited QQMusic configuration, account seeds, proxy credentials,
        # or application logging may turn this private module into a service.
        for key in list(os.environ):
            if key.startswith("QQMUSIC_"):
                del os.environ[key]
        os.environ["PYTHON_ZSTANDARD_IMPORT_POLICY"] = "cext"
        logging.disable(logging.CRITICAL)
        from web.src.core import config

        config.settings = config.Settings(
            _env_file=None,
            credential=config.CredentialConfig(
                enabled=False, store=config.CredentialStoreConfig(path=str(path / "credentials.sqlite3"))
            ),
            client=config.ClientConfig(device_path=str(path / "device.json")),
            security=config.SecurityConfig(
                cors_enabled=False,
                cors_allow_credentials=False,
                trusted_proxy_ips=[],
                client_ip_header=None,
                rate_limit_enabled=False,
            ),
        )
        import uvicorn

        from web.src import app as upstream

        upstream.load_account_configs = lambda path: []
        app = upstream.create_app()
        app.router.redirect_slashes = False
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def private_lifespan(application):
            async with original_lifespan(application):
                # Never import ~/.netrc credentials or environment proxy auth.
                application.state.services.engine.transport._client.trust_env = False
                yield

        app.router.lifespan_context = private_lifespan
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            ready = {
                "protocol": 1,
                "nonce": path.name,
                "pid": pid,
                "supervisorPid": supervisor,
                "port": sock.getsockname()[1],
                "version": VERSION,
            }

            class Server(uvicorn.Server):
                async def startup(self, sockets=None):
                    await super().startup(sockets)
                    if self.started:
                        write_json(path / "ready.json", ready)

            server = Server(
                uvicorn.Config(
                    PrivateApp(app, token, ready, path),
                    host="127.0.0.1",
                    port=ready["port"],
                    workers=1,
                    loop="asyncio",
                    http="h11",
                    ws="none",
                    lifespan="on",
                    proxy_headers=False,
                    forwarded_allow_ips="",
                    access_log=False,
                    server_header=False,
                    date_header=False,
                    log_config=None,
                    log_level="critical",
                    timeout_graceful_shutdown=5,
                )
            )

            def terminate(_signal, _frame):
                # Uvicorn re-raises SIGTERM after graceful shutdown. Use a
                # Python exit so the ready-file/lock finally block still runs.
                raise SystemExit(0)

            signal.signal(signal.SIGTERM, terminate)
            server.run(sockets=[sock])
            require(server.started, "upstream startup failed")
    finally:
        (path / "ready.json").unlink(missing_ok=True)
        os.close(fd)


def main():
    os.umask(0o077)
    args = sys.argv[1:]
    if args == ["prepare"]:
        prepare()
    elif args == ["serve"]:
        serve()
    elif len(args) == 2 and args[0] == "wait-ready":
        wait_ready(args[1])
    elif len(args) == 4 and args[0] == "stop":
        stop(*args[1:])
    elif len(args) == 2 and args[0] == "clean":
        clean(args[1])
    else:
        raise Refused("usage: prepare | serve | wait-ready RUN_DIR | stop PID RUN_DIR NONCE | clean RUN_DIR")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never echo environment values, HTTP data or upstream exception text.
        message = str(error) if isinstance(error, Refused) else "operation failed"
        print(json.dumps({"error": "qq-module: " + message}), file=sys.stderr)
        sys.exit(1)
