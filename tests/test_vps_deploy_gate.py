import importlib.util
import io
import os
import stat
import tarfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


spec = importlib.util.spec_from_file_location(
    "sii_deploy", Path(__file__).resolve().parents[1] / "deploy/sii-deploy.py"
)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)

COMMIT = "a" * 40
SUCCESS = {
    "head_sha": COMMIT,
    "head_branch": "main",
    "event": "push",
    "status": "completed",
    "conclusion": "success",
    "repository": {"full_name": deploy.REPOSITORY},
    "head_repository": {"full_name": deploy.REPOSITORY},
}


@pytest.mark.parametrize(
    "override",
    (
        {},
        {"head_sha": "b" * 40},
        {"head_branch": "other"},
        {"event": "pull_request"},
        {"status": "in_progress", "conclusion": None},
        {"conclusion": "failure"},
        {"conclusion": "cancelled"},
        {"head_repository": {"full_name": "other/repo"}},
    ),
)
def test_latest_exact_main_run_controls_deployment(override):
    latest = {**SUCCESS, **override}
    payload = {"workflow_runs": [latest, SUCCESS]}
    assert deploy.successful_run(payload, COMMIT) is (not override)


def test_workflow_query_is_pinned_to_production_repo_and_commit(monkeypatch):
    def api(url):
        parts = urlsplit(url)
        assert parts.netloc == "api.github.com"
        assert parts.path == f"/repos/{deploy.REPOSITORY}/actions/workflows/sii-main.yml/runs"
        assert parse_qs(parts.query) == {
            "branch": ["main"],
            "event": ["push"],
            "head_sha": [COMMIT],
            "per_page": ["1"],
        }
        return {"workflow_runs": []}

    monkeypatch.setattr(deploy, "read_json", api)
    assert not deploy.tests_passed(COMMIT)


def test_archive_context_is_readable_with_private_service_umask(monkeypatch, tmp_path):
    def git_archive(command, *, stdout, check, timeout):
        with tarfile.open(fileobj=stdout, mode="w") as archive:
            app = tarfile.TarInfo("app")
            app.type = tarfile.DIRTYPE
            app.mode = 0o755
            archive.addfile(app)
            contents = b"value = 1\n"
            module = tarfile.TarInfo("app/routers/documents.py")
            module.size = len(contents)
            module.mode = 0o644
            archive.addfile(module, io.BytesIO(contents))

    monkeypatch.setattr(deploy.subprocess, "run", git_archive)
    scratch = tmp_path / "private"
    scratch.mkdir(mode=0o700)
    old_umask = os.umask(0o077)
    try:
        context = deploy.archive_source(tmp_path / "repo", COMMIT, scratch)
    finally:
        os.umask(old_umask)

    assert (context / "app/routers/documents.py").read_bytes() == b"value = 1\n"
    if os.name == "posix":
        assert stat.S_IMODE(scratch.stat().st_mode) == 0o700
        for directory in (context, context / "app", context / "app/routers"):
            assert stat.S_IMODE(directory.stat().st_mode) == 0o755
