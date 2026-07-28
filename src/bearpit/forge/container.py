"""Container runtime abstraction for Forge (M2).

`ContainerRuntime` is the small surface Forge needs from Docker: per-realm networks, named
volumes with seeded files, and container lifecycle. Keeping it a Protocol lets Forge's
orchestration be unit-tested with a fake, and lets a Kubernetes runtime slot in later (v4)
without touching Forge. `DockerRuntime` is the real, thin implementation.
"""

from __future__ import annotations

import contextlib
from typing import Protocol


class ContainerRuntime(Protocol):
    def create_network(self, name: str, *, internal: bool) -> str: ...
    def connect_network(self, network: str, container: str) -> None: ...
    def remove_network(self, name: str) -> None: ...

    def create_volume(self, name: str) -> str: ...
    def seed_volume(self, name: str, files: dict[str, str]) -> None: ...
    def read_volume(self, name: str) -> dict[str, str]: ...  # {relative_path: content}
    def remove_volume(self, name: str) -> None: ...

    def run_container(
        self,
        *,
        name: str,
        image: str,
        network: str,
        volumes: dict[str, str],
        environment: dict[str, str],
        command: list[str],
        mem_limit: str | None = None,
        pids_limit: int | None = None,
        nano_cpus: int | None = None,
    ) -> str: ...
    def stop_container(self, container_id: str, *, timeout: int) -> None: ...
    def remove_container(self, container_id: str) -> None: ...
    # the agent's stdout/stderr (the runtime's own diagnostics: MCP connects, tool errors, model
    # calls) — captured at teardown as the realm's flight recorder
    def container_logs(self, container_id: str, *, tail: int = 4000) -> str: ...

    # run python IN an agent's own container (its `run_code` tool). No new authority: the code
    # runs as the agent's user, inside the agent's sandbox, under the egress policy already
    # applied to it. Returns (exit_code, combined output).
    def exec_python(
        self, container_id: str, code: str, *, timeout_s: int = 30, user: str = "10000"
    ) -> tuple[int, str]: ...

    # every container whose name starts with `prefix` -> {name: id}. The reaper needs to SEE what
    # is running to know what should not be.
    def list_containers(self, prefix: str) -> dict[str, str]: ...
    def list_volumes(self, prefix: str) -> list[str]: ...
    def list_networks(self, prefix: str) -> list[str]: ...


class DockerRuntime:
    """Thin docker-py wrapper. Integration-tested against a real daemon, not in unit tests."""

    def __init__(self, base_url: str | None = None) -> None:
        import docker  # type: ignore[import-untyped]  # lazy so unit tests need no Docker

        self._client = docker.DockerClient(base_url=base_url) if base_url else docker.from_env()

    def create_network(self, name: str, *, internal: bool) -> str:
        net = self._client.networks.create(name, driver="bridge", internal=internal)
        return str(net.id)

    def connect_network(self, network: str, container: str) -> None:
        # Attach an existing service container (Conduit/LiteLLM) to a realm network so agents on
        # it can resolve the service by name. Idempotent: ignore "already connected".
        import docker.errors  # type: ignore[import-untyped]

        # ignore "already connected" and transient container/network states
        with contextlib.suppress(docker.errors.APIError):
            self._client.networks.get(network).connect(container)

    def remove_network(self, name: str) -> None:
        for net in self._client.networks.list(names=[name]):
            with contextlib.suppress(Exception):
                net.reload()  # refresh endpoints; the attached bus/proxy must be disconnected
                for container in net.containers:
                    with contextlib.suppress(Exception):
                        net.disconnect(container, force=True)
            with contextlib.suppress(Exception):
                net.remove()

    def create_volume(self, name: str) -> str:
        return str(self._client.volumes.create(name).id)

    def seed_volume(self, name: str, files: dict[str, str]) -> None:
        # Write files into a named volume via a short-lived helper container (alpine),
        # mounting the volume at /seed and using a base64 tar to avoid quoting issues.
        import base64
        import io
        import tarfile
        import time

        # stamp a real mtime — a stale (epoch) mtime makes Hermes treat a seeded metadata cache as
        # expired and re-fetch it from the (unreachable) openrouter host, stalling each turn
        now = int(time.time())
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for rel, content in files.items():
                data = content.encode()
                info = tarfile.TarInfo(rel)
                info.size = len(data)
                info.mtime = now
                tar.addfile(info, io.BytesIO(data))
        b64 = base64.b64encode(buf.getvalue()).decode()
        # extract as root, then chown so the Hermes user (HERMES_UID:GID = 10000) owns its home —
        # a named volume is root-owned by default and Hermes expects to own /opt/data.
        script = f"echo {b64} | base64 -d | tar -x -C /seed && chown -R 10000:10000 /seed"
        self._client.containers.run(
            "alpine:3",
            command=["sh", "-c", script],
            volumes={name: {"bind": "/seed", "mode": "rw"}},
            remove=True,
        )

    def read_volume(self, name: str) -> dict[str, str]:
        """Read all files from a named volume (used to watch the shared folder for the file
        termination condition). Returns {relative_path: content}; skips binary/oversized files."""
        import io
        import tarfile

        logs = self._client.containers.run(
            "alpine:3",
            command=["sh", "-c", "cd /seed && tar -c . 2>/dev/null | base64 -w0 || true"],
            volumes={name: {"bind": "/seed", "mode": "ro"}},
            remove=True,
        )
        import base64

        out: dict[str, str] = {}
        try:
            raw = base64.b64decode(logs)
            with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
                for member in tar.getmembers():
                    if not member.isfile() or member.size > 1_000_000:
                        continue
                    f = tar.extractfile(member)
                    if f is None:
                        continue
                    try:
                        out[member.name.lstrip("./")] = f.read().decode()
                    except UnicodeDecodeError:
                        continue
        except Exception:  # best-effort
            return {}
        return out

    def remove_volume(self, name: str) -> None:
        with contextlib.suppress(Exception):  # best-effort teardown
            self._client.volumes.get(name).remove(force=True)

    def run_container(
        self,
        *,
        name: str,
        image: str,
        network: str,
        volumes: dict[str, str],
        environment: dict[str, str],
        command: list[str],
        mem_limit: str | None = None,
        pids_limit: int | None = None,
        nano_cpus: int | None = None,
    ) -> str:
        vols: dict[str, dict[str, str]] = {v: {"bind": b, "mode": "rw"} for v, b in volumes.items()}
        # Host-safety is a realm-boundary guarantee: an adversarial or looping agent must not
        # exhaust the operator's RAM, CPU or PID table. Caps ride here so every agent gets them.
        limits: dict[str, object] = {}
        if mem_limit:
            limits["mem_limit"] = mem_limit
        if pids_limit:
            limits["pids_limit"] = pids_limit
        if nano_cpus:
            limits["nano_cpus"] = nano_cpus
        c = self._client.containers.run(
            image, command=command, name=name, network=network, volumes=vols,
            environment=environment, detach=True,
            **limits,
            # The container boundary is one of the four control boundaries (architecture §6), and
            # what runs inside it is code a language model chose to write. So: drop every
            # capability, then add back only what the image's init system needs to start.
            #
            # `cap_drop=["ALL"]` ALONE KILLS THE AGENT. The runtime image runs s6-overlay, which
            # starts as root and drops to the agent uid — without SETUID/SETGID that fails with
            # "s6-applyuidgid: fatal: unable to set supplementary group list" and the container
            # exits 111 before the agent ever runs. It is invisible to the test suite, because no
            # unit test starts a real runtime container; it took a live realm to surface.
            #
            # What stays dropped is the part that matters: NET_ADMIN, NET_RAW, SYS_ADMIN,
            # SYS_PTRACE, SYS_MODULE, MKNOD and the rest. Nothing here lets an agent touch the
            # host, the network stack, or another container.
            cap_drop=["ALL"],
            cap_add=["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID", "KILL"],
            security_opt=["no-new-privileges:true"],
            # NOT "unless-stopped". That resurrects an agent on every Docker/Colima restart — and
            # it comes back with a live model key, no budget enforcement, no termination and no
            # platform watching it. Three agents from `relayclaude4` ran for 22 hours that way,
            # long after their realm was gone. `on-failure` still restarts an agent that CRASHES
            # inside a live realm (which is what we actually want), but a container that was merely
            # running when the daemon stopped stays stopped.
            restart_policy={"Name": "on-failure", "MaximumRetryCount": 3},
        )
        return str(c.id)

    def list_containers(self, prefix: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for c in self._client.containers.list(all=True):
            name = str(c.name)
            if name.startswith(prefix):
                out[name] = str(c.id)
        return out

    def list_volumes(self, prefix: str) -> list[str]:
        return [str(v.name) for v in self._client.volumes.list()
                if str(v.name).startswith(prefix)]

    def list_networks(self, prefix: str) -> list[str]:
        return [str(n.name) for n in self._client.networks.list()
                if str(n.name).startswith(prefix)]

    def stop_container(self, container_id: str, *, timeout: int) -> None:
        self._client.containers.get(container_id).stop(timeout=timeout)

    def remove_container(self, container_id: str) -> None:
        with contextlib.suppress(Exception):  # best-effort teardown
            self._client.containers.get(container_id).remove(force=True)

    def container_logs(self, container_id: str, *, tail: int = 4000) -> str:
        raw = self._client.containers.get(container_id).logs(tail=tail, timestamps=True)
        return raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)

    def exec_python(
        self, container_id: str, code: str, *, timeout_s: int = 30, user: str = "10000"
    ) -> tuple[int, str]:
        """Run `code` with python3 inside `container_id` — the agent's `run_code` tool.

        argv form, never a shell string: the agent's code is DATA and cannot inject shell syntax.
        `timeout` bounds a runaway loop, and it runs as the agent's own user in its own sandbox, so
        this grants no authority the agent did not already have."""
        container = self._client.containers.get(container_id)
        res = container.exec_run(
            ["timeout", str(timeout_s), "python3", "-c", code],
            user=user, workdir="/tmp", demux=False,
        )
        out = res.output.decode(errors="replace") if isinstance(res.output, bytes) else str(
            res.output or "")
        return int(res.exit_code if res.exit_code is not None else 0), out
