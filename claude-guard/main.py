import hmac
import logging
import os
import subprocess
import threading
import time

import docker
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

TOKEN = os.environ.get("DEPLOY_TOKEN", "")
CONTAINER = os.environ.get("MANAGED_CONTAINER", "claude-works")
IMAGE = os.environ.get("MANAGED_IMAGE", "ghcr.io/mirkofelt/claude-works:latest")
COMPOSE_FILE = os.environ.get("COMPOSE_FILE", "")
COMPOSE_SERVICE = os.environ.get("COMPOSE_SERVICE", "claude-works")
RATE_LIMIT_SECONDS = 300
HEALTH_WAIT_SECONDS = 30

_lock = threading.Lock()
_last_deploy: float = 0.0
_prev_image: str | None = None


def _check_token() -> bool:
    t = request.args.get("token", "")
    return bool(TOKEN) and hmac.compare_digest(t, TOKEN)


@app.route("/health")
def health():
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 401
    try:
        client = docker.from_env()
        container = client.containers.get(CONTAINER)
        return jsonify({"status": "ok", "container": CONTAINER, "container_status": container.status})
    except docker.errors.NotFound:
        return jsonify({"status": "ok", "container": CONTAINER, "container_status": "not_found"})
    except Exception as e:
        return jsonify({"status": "ok", "container": CONTAINER, "docker_error": str(e)})


@app.route("/deploy", methods=["POST"])
def deploy():
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 401

    global _last_deploy
    with _lock:
        now = time.time()
        remaining = RATE_LIMIT_SECONDS - (now - _last_deploy)
        if remaining > 0:
            return jsonify({"error": "rate_limited", "retry_after": int(remaining)}), 429
        _last_deploy = now

    log.info("Deploy triggered for %s (%s)", CONTAINER, IMAGE)
    try:
        if COMPOSE_FILE:
            _deploy_compose()
        else:
            _deploy_standalone()
        log.info("Deploy succeeded")
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error("Deploy failed: %s", e)
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/rollback", methods=["POST"])
def rollback():
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 401

    log.info("Rollback triggered for %s (prev_image=%s)", CONTAINER, _prev_image)
    try:
        if COMPOSE_FILE:
            subprocess.run(
                ["docker", "compose", "-f", COMPOSE_FILE, "restart", COMPOSE_SERVICE],
                check=True, capture_output=True,
            )
        elif _prev_image:
            client = docker.from_env()
            _stop_and_remove(client)
            _recreate(client, _prev_image)
        else:
            return jsonify({"error": "no previous image saved"}), 409
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error("Rollback failed: %s", e)
        return jsonify({"status": "error", "detail": str(e)}), 500


def _deploy_compose() -> None:
    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "pull", COMPOSE_SERVICE],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "--no-deps", COMPOSE_SERVICE],
        check=True, capture_output=True,
    )
    _wait_for_running(COMPOSE_WAIT := HEALTH_WAIT_SECONDS)


def _deploy_standalone() -> None:
    global _prev_image
    client = docker.from_env()

    try:
        container = client.containers.get(CONTAINER)
        _prev_image = container.attrs["Config"]["Image"]
        log.info("Saved previous image: %s", _prev_image)
        _container_attrs = container.attrs
    except docker.errors.NotFound:
        _container_attrs = None

    log.info("Pulling %s", IMAGE)
    client.images.pull(IMAGE)

    if _container_attrs:
        _stop_and_remove(client)
        _recreate(client, IMAGE, _container_attrs)
    else:
        log.warning("Container %s not found — cold start", CONTAINER)
        client.containers.run(IMAGE, name=CONTAINER, detach=True,
                              restart_policy={"Name": "unless-stopped"})

    time.sleep(HEALTH_WAIT_SECONDS)
    try:
        c = client.containers.get(CONTAINER)
        if c.status != "running":
            log.error("Container not running after deploy (status=%s) — rolling back", c.status)
            _stop_and_remove(client)
            if _prev_image:
                _recreate(client, _prev_image, _container_attrs)
            raise RuntimeError(f"Container unhealthy after deploy, rolled back to {_prev_image}")
    except docker.errors.NotFound:
        if _prev_image and _container_attrs:
            _recreate(client, _prev_image, _container_attrs)
        raise RuntimeError("Container vanished after deploy, rolled back")


def _stop_and_remove(client: docker.DockerClient) -> None:
    try:
        c = client.containers.get(CONTAINER)
        c.stop(timeout=30)
        c.remove()
    except docker.errors.NotFound:
        pass


def _recreate(client: docker.DockerClient, image: str, attrs: dict | None = None) -> None:
    kwargs: dict = {"name": CONTAINER, "detach": True}
    if attrs:
        hc = attrs.get("HostConfig", {})
        cfg = attrs.get("Config", {})
        port_bindings = hc.get("PortBindings") or {}
        kwargs["ports"] = {p: [(b["HostIp"], b["HostPort"]) for b in bs] for p, bs in port_bindings.items() if bs}
        kwargs["volumes"] = hc.get("Binds") or []
        kwargs["environment"] = cfg.get("Env") or []
        kwargs["network_mode"] = hc.get("NetworkMode", "bridge")
        rp = hc.get("RestartPolicy", {})
        if rp.get("Name"):
            kwargs["restart_policy"] = {"Name": rp["Name"]}
    client.containers.run(image, **kwargs)
    log.info("Recreated %s with image %s", CONTAINER, image)


def _wait_for_running(wait: int = HEALTH_WAIT_SECONDS) -> None:
    time.sleep(wait)
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", CONTAINER],
        capture_output=True, text=True,
    )
    status = result.stdout.strip()
    if status != "running":
        raise RuntimeError(f"Container status after deploy: {status!r}")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DEPLOY_TOKEN environment variable is required")
    log.info("deploy-guard starting — managing container '%s'", CONTAINER)
    app.run(host="0.0.0.0", port=9876)
