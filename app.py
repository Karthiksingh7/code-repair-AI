from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from queue import Queue
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from repair_engine import (
    MODEL,
    RepositoryIndex,
    RepairError,
    clone_github_repository,
    extract_repository_zip,
    git_command,
    path_is_inside,
    resolve_repository_path,
    safe_relative_path,
    select_bug_location,
    stream_repair_events,
    validate_github_url,
)


ROOT = Path(__file__).resolve().parent
STATE_ROOT = ROOT / ".patchwork"
REPOSITORY_STORE = STATE_ROOT / "repositories"
UPLOAD_STORE = STATE_ROOT / "uploads"
BACKUP_STORE = STATE_ROOT / "backups"
MAX_REPOSITORIES = 12
MAX_UPLOAD_BYTES = 55_000_000
PATCH_MEMORY_LIMIT = 40
PATCH_EXPIRY_SECONDS = 3_600
MINIMUM_TEST_CONSENT = (
    "I trust this repository and its test command. Test commands execute project code, "
    "which can access files available to this computer."
)

STATE_ROOT.mkdir(exist_ok=True)
REPOSITORY_STORE.mkdir(exist_ok=True)
UPLOAD_STORE.mkdir(exist_ok=True)
BACKUP_STORE.mkdir(exist_ok=True)

logging.basicConfig(
    level=os.environ.get("PATCHWORK_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("patchwork")
app = Flask(
    __name__,
    static_folder=str(ROOT),
    static_url_path="/assets",
    template_folder=str(ROOT),
)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

registry_lock = threading.RLock()
patch_lock = threading.RLock()
index_lock = threading.RLock()
repositories: dict[str, dict[str, Any]] = {}
patches: dict[str, dict[str, Any]] = {}
applying_patches: set[str] = set()
indexes: dict[str, tuple[float, RepositoryIndex]] = {}


@contextmanager
def _database() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(STATE_ROOT / "patchwork.sqlite3", timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS repositories (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        connection.commit()
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _ensure_registry() -> None:
    with registry_lock:
        with _database() as connection:
            for row in connection.execute(
                "SELECT id, name, path, source, created_at FROM repositories ORDER BY created_at DESC"
            ):
                root = Path(row["path"])
                if root.is_dir():
                    name = row["name"]
                    if row["source"] == "github" and re.fullmatch(r"[a-f0-9]{32}", name):
                        remote = git_command(root, ["remote", "get-url", "origin"]).strip()
                        parsed = urlparse(remote)
                        parts = [part for part in parsed.path.strip("/").split("/") if part]
                        if (
                            parsed.scheme == "https"
                            and parsed.hostname == "github.com"
                            and not parsed.username
                            and not parsed.password
                            and len(parts) == 2
                        ):
                            name = parts[-1].removesuffix(".git")[:120]
                            connection.execute(
                                "UPDATE repositories SET name = ? WHERE id = ?",
                                (name, row["id"]),
                            )
                    repositories[row["id"]] = {
                        "id": row["id"],
                        "name": name,
                        "path": root,
                        "source": row["source"],
                        "created_at": row["created_at"],
                    }


def _list_repositories() -> list[dict[str, Any]]:
    _ensure_registry()
    with registry_lock:
        items = []
        for repository in sorted(repositories.values(), key=lambda item: item["created_at"], reverse=True):
            items.append({
                "id": repository["id"],
                "name": repository["name"],
                "source": repository["source"],
                "created_at": repository["created_at"],
            })
        return items


def _register_repository(
    root: Path,
    source: str,
    *,
    display_name: str | None = None,
) -> dict[str, Any]:
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RepairError("The repository folder could not be opened.") from exc
    if not resolved.is_dir():
        raise RepairError("The repository must be a folder.")
    if not isinstance(source, str) or source not in {"local", "github", "zip"}:
        raise RepairError("Select a supported repository source.")
    with registry_lock:
        _ensure_registry()
        existing = next(
            (item for item in repositories.values() if item["path"].resolve() == resolved),
            None,
        )
        if existing:
            if display_name:
                existing["name"] = display_name[:120]
                with _database() as connection:
                    connection.execute(
                        "UPDATE repositories SET name = ? WHERE id = ?",
                        (existing["name"], existing["id"]),
                    )
            return _public_repository(existing, reindex=True)
        if len(repositories) >= MAX_REPOSITORIES:
            raise RepairError(f"A workspace can connect up to {MAX_REPOSITORIES} repositories.")
        record = {
            "id": uuid.uuid4().hex,
            "name": (display_name or resolved.name)[:120] or "Repository",
            "path": resolved,
            "source": source,
            "created_at": time.time(),
        }
        # Index before persisting the connection: invalid or unreadable folders
        # must not appear as successfully connected repositories.
        index = RepositoryIndex(resolved)
        if not index.source_files:
            raise RepairError("No supported source files were found. Connect a repository containing application code.")
        with _database() as connection:
            connection.execute(
                "INSERT INTO repositories (id, name, path, source, created_at) VALUES (?, ?, ?, ?, ?)",
                (record["id"], record["name"], str(resolved), record["source"], record["created_at"]),
            )
        repositories[record["id"]] = record
        indexes[record["id"]] = (time.time(), index)
        return _public_repository(record, index=index)


def _public_repository(
    repository: dict[str, Any],
    *,
    index: RepositoryIndex | None = None,
    reindex: bool = False,
) -> dict[str, Any]:
    if index is None:
        index = _get_index(repository["id"], reindex=reindex)
    return {
        "id": repository["id"],
        "name": repository["name"],
        "source": repository["source"],
        "analysis": index.summary(),
    }


def _get_index(repository_id: str, *, reindex: bool = False) -> RepositoryIndex:
    record = _get_repository(repository_id)
    with index_lock:
        cached = indexes.get(repository_id)
        if cached and not reindex:
            previous_mtime, index = cached
            # Rebuild from current disk contents so newly added/edited files
            # cannot silently bypass repository indexing between repairs.
        try:
            index = RepositoryIndex(record["path"])
        except (OSError, RepairError):
            raise
        except Exception as exc:
            logger.exception("Repository indexing failed for id=%s", repository_id)
            raise RepairError("The repository couldn't be indexed. Check its permissions and try again.") from exc
        if not index.source_files:
            raise RepairError("No supported source files remain in this repository. Reconnect a source-code project.")
        indexes[repository_id] = (time.time(), index)
        return index


def _get_repository(repository_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{32}", repository_id or ""):
        raise RepairError("That repository identifier is invalid.")
    _ensure_registry()
    with registry_lock:
        record = repositories.get(repository_id)
    if not record or not record["path"].is_dir():
        raise RepairError("This repository is no longer connected. Connect it again and retry.")
    return record


def _store_candidates(repository_id: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = time.time()
    public = []
    with patch_lock:
        expired = [key for key, item in patches.items() if now - item["created_at"] > PATCH_EXPIRY_SECONDS]
        for key in expired:
            patches.pop(key, None)
        for candidate in candidates:
            while len(patches) >= PATCH_MEMORY_LIMIT:
                patches.pop(next(iter(patches)))
            patch_id = candidate["id"]
            patches[patch_id] = {
                **candidate,
                "created_at": now,
                "repository_id": repository_id,
            }
            public.append({
                "id": patch_id,
                "summary": candidate["summary"],
                "tier": candidate["tier"],
                "diff": candidate["diff"],
                "test": candidate["test"],
                "files": [
                    {"path": path, "added": len(bytes.fromhex(value))}
                    for path, value in candidate["files"].items()
                ],
            })
    return public


@app.get("/")
def home() -> str:
    return render_template("index.html")


@app.get("/<path:unused>")
def missing_page(unused: str) -> tuple[Response, int]:
    del unused
    return jsonify({"error": "Not found."}), 404


@app.get("/health")
@app.get("/api/status")
def status() -> Response:
    return jsonify({
        "ready": True,
        "model": MODEL,
        "groq_configured": bool(os.environ.get("GROQ_API_KEY", "").strip()),
        "repositories": _list_repositories(),
        "test_execution": "user-opt-in subprocess; trusted repositories only",
    })


@app.get("/api/repos")
def list_repos() -> Response:
    return jsonify({"repositories": _list_repositories()})


@app.post("/api/repos/connect")
def connect_repo() -> tuple[Response, int] | Response:
    data = request.get_json(silent=True) or {}
    source = data.get("source", "local")
    if source == "local":
        root = resolve_repository_path(data.get("path", ""))
    elif source == "github":
        url = validate_github_url(data.get("url", ""))
        repository_name = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        repository_id = uuid.uuid4().hex
        clone_target = REPOSITORY_STORE / repository_id
        clone_github_repository(url, clone_target)
        root = clone_target
    else:
        return jsonify({"error": "Choose a local folder or a public GitHub repository."}), 400
    try:
        if source == "github":
            repository = _register_repository(root, source, display_name=repository_name)
        else:
            repository = _register_repository(root, source)
        return jsonify({"repository": repository})
    except RepairError:
        if source == "github" and root.exists():
            shutil.rmtree(root, ignore_errors=True)
        raise


@app.post("/api/repos/upload")
def upload_repo() -> Response:
    uploaded = request.files.get("archive")
    if not uploaded or not uploaded.filename or not uploaded.filename.lower().endswith(".zip"):
        raise RepairError("Choose a repository ZIP file.")
    repository_id = uuid.uuid4().hex
    destination = REPOSITORY_STORE / repository_id
    destination.mkdir(parents=True, exist_ok=False)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=UPLOAD_STORE, suffix=".zip", delete=False) as handle:
            temp_path = Path(handle.name)
            received = 0
            while chunk := uploaded.stream.read(64 * 1024):
                received += len(chunk)
                if received > 50_000_000:
                    raise RepairError("The uploaded repository ZIP exceeds the 50 MB limit.")
                handle.write(chunk)
        root = extract_repository_zip(temp_path, destination)
        repository = _register_repository(root, "zip")
        return jsonify({"repository": repository}), 201
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


@app.post("/api/repos/<repository_id>/reindex")
def reindex_repo(repository_id: str) -> Response:
    repository = _get_repository(repository_id)
    indexed = _get_index(repository_id, reindex=True)
    return jsonify({"repository": _public_repository(repository, index=indexed)})


@app.post("/api/repair/stream")
def create_repair() -> Response:
    data = request.get_json(silent=True) or {}
    repository_id = data.get("repository_id", "")
    repository = _get_repository(repository_id)
    index = _get_index(repository_id)
    if not data.get("share_context") is True:
        raise RepairError(
            "Confirm that your selected source code, bug description, and retrieved context "
            "may be sent to Groq before starting this repair."
        )
    if data.get("test_consent") != MINIMUM_TEST_CONSENT:
        raise RepairError(
            "Test execution is opt-in. Confirm that you trust this repository before running its test command."
        )
    description = data.get("description", "").strip()
    failing_tests = data.get("failing_tests", "").strip()
    if not description or len(description) > 10_000:
        raise RepairError("Describe the bug in 1–10,000 characters.")
    if len(failing_tests) > 12_000:
        raise RepairError("Keep the failing-test output below 12,000 characters.")
    test_command = data.get("test_command", "")
    if not isinstance(test_command, str) or not test_command.strip():
        raise RepairError("Enter the failing test command so each patch can be evaluated.")
    if len(test_command) > 400:
        raise RepairError("Test commands must be 400 characters or shorter.")
    supplied_location = data.get("file_path", "")
    if not isinstance(supplied_location, str):
        raise RepairError("Select a source file from the connected repository.")
    relative, line = select_bug_location(index, supplied_location, description)
    repair_id = uuid.uuid4().hex

    @stream_with_context
    def generate():
        def emit(event: dict[str, Any]) -> str:
            if event.get("type") == "result" and event.get("candidates"):
                event = {
                    **event,
                    "candidates": _store_candidates(repository_id, event["candidates"]),
                }
            return "data: " + json.dumps(event, ensure_ascii=True) + "\n\n"

        events: Queue[dict[str, Any] | None] = Queue()

        def produce() -> None:
            try:
                stream_repair_events(
                    root=repository["path"],
                    index=index,
                    relative=relative,
                    line=line,
                    description=description,
                    failing_tests=failing_tests,
                    test_command=test_command.strip(),
                    repair_id=repair_id,
                    on_event=events.put,
                )
            except RepairError as exc:
                events.put({"type": "error", "message": str(exc)})
            except Exception:
                logger.exception("Repair generation failed; repair_id=%s", repair_id)
                events.put({
                    "type": "error",
                    "message": f"Repair failed unexpectedly (reference {repair_id[:12]}). "
                    "Check the local terminal log and retry.",
                })
            finally:
                events.put(None)

        threading.Thread(target=produce, name=f"patchwork-{repair_id[:8]}", daemon=True).start()
        while True:
            event = events.get()
            if event is None:
                break
            yield emit(event)
        yield "data: [DONE]\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/repair/<patch_id>/apply")
def apply_repair(patch_id: str) -> Response:
    if not re.fullmatch(r"[a-f0-9]{32}", patch_id or ""):
        raise RepairError("That validated patch identifier is invalid.")
    data = request.get_json(silent=True) or {}
    if data.get("confirm") is not True:
        raise RepairError("Review and confirm the exact patch before applying it to your repository.")
    with patch_lock:
        candidate = patches.get(patch_id)
        if not candidate or time.time() - candidate["created_at"] > PATCH_EXPIRY_SECONDS:
            raise RepairError("This validated patch has expired. Generate and review it again.")
        if patch_id in applying_patches:
            raise RepairError("This patch is already being applied.")
        applying_patches.add(patch_id)
    try:
        repository = _get_repository(candidate["repository_id"])
        root = repository["path"].resolve(strict=True)
        updated: list[tuple[Path, bytes]] = []
        for relative, encoded in candidate["files"].items():
            relative = safe_relative_path(relative)
            target = root.joinpath(*PurePosixPath(relative).parts)
            if not path_is_inside(root, target) or target.is_symlink() or not target.is_file():
                raise RepairError(f"Refusing to apply the patch outside an existing source file: {relative}.")
            current = target.read_bytes()
            expected_hash = candidate["sha256"][relative]
            import hashlib
            if hashlib.sha256(current).hexdigest() != expected_hash:
                raise RepairError(
                    f"{relative} changed after validation. Re-index the repository and generate the patch again."
                )
            updated.append((target, bytes.fromhex(encoded)))
        # Verify every input before changing a file. Per-file replacement is
        # atomic; repository-wide backup copies allow recovery after a failure.
        backup_root = BACKUP_STORE / candidate["repository_id"] / patch_id
        backup_root.mkdir(parents=True, exist_ok=False)
        backups: list[tuple[Path, Path]] = []
        for index_number, (target, _) in enumerate(updated):
            backup = backup_root / f"{index_number:03d}.original"
            shutil.copyfile(target, backup)
            backups.append((target, backup))
        replaced: list[Path] = []
        try:
            for target, content in updated:
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".patchwork-", delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.replace(temporary, target)
                    replaced.append(target)
                finally:
                    if temporary.exists():
                        temporary.unlink()
        except OSError:
            for target, backup in backups:
                if target not in replaced or not backup.exists():
                    continue
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".patchwork-restore-", delete=False) as handle:
                    restore = Path(handle.name)
                    with backup.open("rb") as source:
                        shutil.copyfileobj(source, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(restore, target)
            raise
        with index_lock:
            indexes.pop(candidate["repository_id"], None)
        with patch_lock:
            patches.pop(patch_id, None)
        shutil.rmtree(backup_root, ignore_errors=True)
        return jsonify({
            "ok": True,
            "message": f"Applied the test-passing patch to {len(updated)} file(s).",
            "files": [path.relative_to(root).as_posix() for path, _ in updated],
        })
    except RepairError:
        raise
    except OSError as exc:
        logger.exception("Could not apply validated patch %s", patch_id)
        raise RepairError(f"Unable to apply this patch safely: {exc}") from exc
    finally:
        with patch_lock:
            applying_patches.discard(patch_id)


@app.errorhandler(RepairError)
def handle_repair_error(error: RepairError) -> tuple[Response, int]:
    return jsonify({"error": str(error)}), 400


@app.errorhandler(413)
def handle_large_upload(_: Exception) -> tuple[Response, int]:
    return jsonify({"error": "Uploads are limited to 50 MB."}), 413


@app.errorhandler(404)
def handle_not_found(_: Exception) -> tuple[Response, int]:
    return jsonify({"error": "The requested resource was not found."}), 404


if __name__ == "__main__":
    if os.name == "nt":
        from werkzeug.serving import WSGIRequestHandler
        WSGIRequestHandler.protocol_version = "HTTP/1.1"
    logger.info("Starting Patchwork at http://127.0.0.1:5000; model=%s", MODEL)
    if not os.environ.get("GROQ_API_KEY", "").strip():
        logger.warning("GROQ_API_KEY is unset. Add it in PowerShell before starting the application.")
    app.run(host="127.0.0.1", port=int(os.environ.get("PATCHWORK_PORT", "5000")), debug=False, threaded=True)
