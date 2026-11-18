#!/usr/bin/env python3
"""Install on the VPS as a root-owned script and run with the SII deployment timer."""

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode
from urllib.request import Request, urlopen


REPOSITORY = "FisterraSRL/ExtraccionDocumentosSII"
GIT_URL = f"https://github.com/{REPOSITORY}.git"
WORKFLOW = "sii-main.yml"
MAIN_REF = "refs/heads/main"
PORTAL = "https://sii.fisterragroup.com"
STATE = Path("/var/lib/sii-deploy")
COMPOSE = ("docker", "compose", "--project-name", "sii", "-f", "/opt/sii/deploy/compose.production.yml")
GIT = ("git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null")
SOURCE = (".dockerignore", "app", "requirements.txt", "requirements-sync.txt", "deploy/Dockerfile")
LIVE_IMAGE = "sii-web:latest"
CANDIDATE = "sii-web:deploy-candidate"
ROLLBACK = "sii-web:deploy-previous"
MANAGED_LABEL = "com.fisterragroup.sii.automatic-deploy"
SHA = re.compile(r"[0-9a-f]{40}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
MIN_FREE = 3 * 1024**3


def run(*command, timeout=60):
    return subprocess.check_output(command, stderr=subprocess.PIPE, timeout=timeout).decode().strip()


def read_json(url):
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "sii-deploy", "Cache-Control": "no-cache"})
    with urlopen(request, timeout=15) as response:
        if response.geturl() != url:
            raise ValueError("Unexpected verification URL")
        return json.load(response)


def main_sha():
    fields = run(*GIT, "ls-remote", "--exit-code", GIT_URL, MAIN_REF).split()
    if len(fields) != 2 or fields[1] != MAIN_REF or not SHA.fullmatch(fields[0]):
        raise ValueError("Unexpected GitHub main reference")
    return fields[0]


def successful_run(payload, sha):
    """A previous success cannot authorize a SHA whose latest run is pending or failed."""
    if not isinstance(payload, dict):
        return False
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list) or not runs or not isinstance(runs[0], dict):
        return False
    result = runs[0]
    for field in ("repository", "head_repository"):
        repo = result.get(field)
        if not isinstance(repo, dict) or str(repo.get("full_name", "")).casefold() != REPOSITORY.casefold():
            return False
    return (
        result.get("head_sha") == sha
        and result.get("head_branch") == "main"
        and result.get("event") == "push"
        and result.get("status") == "completed"
        and result.get("conclusion") == "success"
    )


def tests_passed(sha):
    query = urlencode({"branch": "main", "event": "push", "head_sha": sha, "per_page": 1})
    url = f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/runs?{query}"
    return successful_run(read_json(url), sha)


def marker(name):
    path = STATE / name
    return path.read_text().strip() if path.exists() else ""


def save_state(name, text):
    pending = STATE / f".{name}.tmp"
    with pending.open("w") as stream:
        stream.write(text + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(STATE / name)
    fd = os.open(STATE, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def running_image():
    container = run(*COMPOSE, "ps", "-q", "web")
    if not re.fullmatch(r"[0-9a-f]{12,64}", container):
        raise RuntimeError("Expected one running SII web container")
    return run("docker", "inspect", "--format", "{{.Image}}", container)


def replace_web(image):
    run("docker", "image", "tag", image, LIVE_IMAGE)
    run(*COMPOSE, "up", "-d", "--no-deps", "--no-build", "--wait", "--wait-timeout", "60", "web", timeout=90)
    if running_image() != image:
        raise RuntimeError("SII web image does not match the requested image")


def verify_portal(expected=None):
    for attempt in range(6):
        try:
            healthy = read_json(f"{PORTAL}/health") == {"status": "ok"}
            if healthy and (expected is None or read_json(f"{PORTAL}/api/version") == {"version": expected}):
                return
        except (OSError, ValueError):
            pass
        if attempt < 5:
            time.sleep(3)
    raise RuntimeError("Public SII health or version verification failed")


def recover_pending():
    pending = STATE / "pending-release.json"
    if not pending.exists():
        return
    release = json.loads(pending.read_text())
    if not SHA.fullmatch(release["sha"]) or not IMAGE_ID.fullmatch(release["previous_image"]):
        raise ValueError("Unexpected pending release metadata")
    if marker("deployed-sha") != release["sha"]:
        print(f"Restoring previous SII image for {release['sha']}", flush=True)
        replace_web(release["previous_image"])
        verify_portal()
        save_state("failed-sha", release["sha"])
    pending.unlink()


def archive_source(cache, sha, directory):
    """Only the Docker inputs from the tested commit can enter the build context."""
    archive = directory / "source.tar"
    context = directory / "source"
    context.mkdir()
    with archive.open("wb") as stream:
        subprocess.run((*GIT, "-C", str(cache), "archive", sha, *SOURCE), stdout=stream, check=True, timeout=60)
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            path = PurePosixPath(member.name)
            if not (member.isfile() or member.isdir()) or path.is_absolute() or ".." in path.parts:
                raise ValueError("Unexpected file in the release archive")
            if any(part.startswith(".env") or part == "secrets" for part in path.parts):
                raise ValueError("Restricted file in the release archive")
            if path.suffix.lower() in {".pfx", ".p12", ".pem", ".key", ".db", ".sqlite", ".sqlite3"}:
                raise ValueError("Restricted file in the release archive")
        source.extractall(context, filter="data")
    # The tar data filter ignores directory modes. With a private service umask,
    # Docker would copy root-only directories that the application user cannot read.
    for directory, _, _ in os.walk(context):
        Path(directory).chmod(0o755)
    return context


def build_image(cache, sha):
    with tempfile.TemporaryDirectory(dir=STATE) as scratch:
        source = archive_source(cache, sha, Path(scratch))
        version = re.search(r'^__version__ = "([0-9]\.[0-9]\.[0-9])"$', (source / "app/version.py").read_text(encoding="utf-8"), re.M)
        if not version:
            raise ValueError("Missing canonical app version in the tested commit")
        image_id = run("docker", "build", "--quiet", "--tag", CANDIDATE,
                       "--label", f"{MANAGED_LABEL}=true", "--label", f"org.opencontainers.image.revision={sha}",
                       "--file", str(source / "deploy/Dockerfile"), str(source), timeout=1200)
        if not IMAGE_ID.fullmatch(image_id):
            raise ValueError("Unexpected Docker build image ID")
    return image_id, version[1]


SMOKE = """
import json
import sqlite3
import subprocess
import sys
import time
import urllib.request

with subprocess.Popen((sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--no-access-log")) as web:
    try:
        for _ in range(30):
            if web.poll() is not None:
                raise RuntimeError("Candidate application stopped during startup")
            try:
                with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=1) as response:
                    if json.load(response) != {"status": "ok"}:
                        raise RuntimeError("Candidate health check failed")
                with urllib.request.urlopen("http://127.0.0.1:8000/api/version", timeout=1) as response:
                    version = json.load(response)["version"]
                    if sys.argv[1] and version != sys.argv[1]:
                        raise RuntimeError("Candidate version mismatch")
                with sqlite3.connect("file:/tmp/sii-deploy-smoke.db?mode=ro", uri=True) as connection:
                    print(json.dumps(connection.execute(
                        "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
                    ).fetchall()))
                break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("Candidate application did not become ready")
    finally:
        web.terminate()
"""


def scratch_schema(image, version=""):
    return run("docker", "run", "--rm", "--network", "none",
               "-e", "DATABASE_URL=sqlite:////tmp/sii-deploy-smoke.db",
               "-e", "PORTAL_USUARIO=fixture",
               "-e", "PORTAL_PASSWORD=fixture_only",
               "-e", "SECRET_KEY=disposable-test-key",
               "-e", "PORTAL_SIN_LOGIN=0",
               "--entrypoint", "python", image, "-c", SMOKE, version, timeout=50)


def remove_unused_managed_image(image):
    if not IMAGE_ID.fullmatch(image):
        return
    try:
        if run("docker", "image", "inspect", "--format",
               '{{index .Config.Labels "com.fisterragroup.sii.automatic-deploy"}} {{len .RepoTags}}', image) == "true 0":
            run("docker", "image", "rm", image)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print("Docker retained a previous release image", flush=True)


def deploy_sha(sha):
    if shutil.disk_usage(run("docker", "info", "--format", "{{.DockerRootDir}}")).free < MIN_FREE:
        raise RuntimeError("Docker storage needs at least 3 GiB free for a deployment")

    cache = STATE / "repository.git"
    if not cache.exists():
        run(*GIT, "init", "--bare", str(cache))
    run(*GIT, "-C", str(cache), "fetch", "--no-tags", "--depth=1", GIT_URL, f"+{MAIN_REF}:{MAIN_REF}")
    if run(*GIT, "-C", str(cache), "rev-parse", MAIN_REF) != sha:
        print("Main moved while fetching; waiting for its latest test result", flush=True)
        return

    previous = running_image()
    try:
        image, version = build_image(cache, sha)
        if scratch_schema(image, version) != scratch_schema(previous):
            raise RuntimeError("SQLite schema changed in scratch startup; production database was not opened by the deployment check")
    except Exception:
        save_state("failed-sha", sha)
        raise

    if main_sha() != sha or not tests_passed(sha):
        print("Main or its test result changed during the build; keeping the current release", flush=True)
        return

    if running_image() != previous:
        raise RuntimeError("Live image changed during validation; awaiting a stable release")
    old_rollback = run("docker", "image", "ls", "--filter", f"reference={ROLLBACK}", "--no-trunc", "--format", "{{.ID}}")
    run("docker", "image", "tag", previous, ROLLBACK)
    save_state("pending-release.json", json.dumps({"sha": sha, "previous_image": previous}))
    try:
        replace_web(image)
        verify_portal(version)
        save_state("deployed-sha", sha)
    except Exception:
        recover_pending()
        raise
    (STATE / "pending-release.json").unlink()
    print(f"Deployed {sha} ({version}) with verified public health and version", flush=True)
    remove_unused_managed_image(old_rollback)


def main():
    import fcntl

    with (STATE / "deploy.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        recover_pending()
        sha = main_sha()
        if sha not in {marker("deployed-sha"), marker("failed-sha")} and tests_passed(sha):
            print(f"GitHub Actions approved {sha}; building SII", flush=True)
            deploy_sha(sha)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit(f"SII deploy: {error}")
