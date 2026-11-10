from __future__ import annotations

import json
import hashlib
import os
import shutil
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import app as backend
import repair_engine as engine
from repair_engine import (
    MAX_ZIP_EXPANDED_BYTES,
    PythonSymbols,
    RepairError,
    RepositoryIndex,
    _candidate_diffs,
    _parse_model_json,
    build_repair_prompt,
    extract_repository_zip,
    run_test_command,
    safe_relative_path,
    validate_github_url,
)


class TemporaryProject(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="patchwork-test-")
        self.root = Path(self.temp.name)
        self.file = self.root / "calculator.py"
        self.file.write_text(
            "def total(amount):\n"
            "    return amount * 2\n",
            encoding="utf-8",
        )
        (self.root / "README.md").write_text(
            "# Calculator\nReturn a doubled amount.\n",
            encoding="utf-8",
        )
        (self.root / "test_calculator.py").write_text(
            "from calculator import total\n",
            encoding="utf-8",
        )
        self.git = shutil.which("git")
        if self.git:
            import subprocess

            subprocess.run(
                [self.git, "init", "--quiet", str(self.root)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                [self.git, "-C", str(self.root), "config", "user.email", "patchwork-test@example.com"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                [self.git, "-C", str(self.root), "config", "user.name", "Patchwork Test"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                [self.git, "-C", str(self.root), "add", "."],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                [self.git, "-C", str(self.root), "commit", "-m", "Add Python repair fixture"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

    def tearDown(self) -> None:
        self.temp.cleanup()


class RepositoryAndPathTests(TemporaryProject):
    def test_source_file_paths_are_normalized_and_reject_traversal_and_secrets(self) -> None:
        self.assertEqual(safe_relative_path("src\\calculator.py"), "src/calculator.py")
        for unsafe in ("../outside.py", "C:\\outside.py", "src/.env", "src/token.py"):
            with self.subTest(path=unsafe), self.assertRaises(RepairError):
                safe_relative_path(unsafe)

    def test_source_index_counts_tests_documentation_and_python_symbols(self) -> None:
        index = RepositoryIndex(self.root)
        self.assertIn("calculator.py", index.files)
        self.assertTrue(index.files["test_calculator.py"]["test"])
        self.assertEqual(len(index.test_files), 1)
        self.assertEqual(len(index.docs), 1)
        self.assertIn("def total", index.bug_context("calculator.py", 2, "wrong value"))
        project_context = index.project_context("calculator.py", "doubled amount")
        self.assertIn("README.md", project_context)
        self.assertIn("Return a doubled amount.", project_context)

    def test_git_history_adds_related_commit_information_when_available(self) -> None:
        if not self.git:
            self.skipTest("Git is not installed")
        index = RepositoryIndex(self.root)
        self.assertIn("Recent Git history touching calculator.py", index.repository_context("calculator.py"))

    def test_secret_values_are_redacted_from_sent_context(self) -> None:
        context = "GROQ_API_KEY=gsk_123456789012345678901234\npassword safe secret"
        filtered = engine.redact_secrets(context)
        self.assertNotIn("gsk_123456789012345678901234", filtered)
        self.assertIn("password safe secret", filtered)

    def test_public_github_url_rejects_credentials_and_other_hosts(self) -> None:
        self.assertEqual(
            validate_github_url("https://github.com/acme/project"),
            "https://github.com/acme/project.git",
        )
        for url in (
            "https://attacker.example/acme/project",
            "https://user:token@github.com/acme/project",
            "http://github.com/acme/project",
            "https://github.com/acme/project/../../../attacker",
        ):
            with self.subTest(url=url), self.assertRaises(RepairError):
                validate_github_url(url)

    def test_zip_extraction_prevalidates_traversal_paths(self) -> None:
        archive_path = self.root / "attack.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("../escaped.py", "outside")
        destination = self.root / "extracted"
        destination.mkdir()
        with self.assertRaises(RepairError):
            extract_repository_zip(archive_path, destination)
        self.assertFalse((self.root.parent / "escaped.py").exists())

    def test_zip_skips_credentials_and_extracts_nested_project_files(self) -> None:
        archive_path = self.root / "source.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("source/calculator.py", "def total(amount):\n    return amount\n")
            archive.writestr("source/.env", "GROQ_API_KEY=not-included\n")
        destination = self.root / "safe"
        destination.mkdir()
        result = extract_repository_zip(archive_path, destination)
        self.assertTrue((result / "calculator.py").exists())
        self.assertFalse((result / ".env").exists())

    def test_zip_enforces_archive_expansion_limit(self) -> None:
        archive_path = self.root / "large.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("huge.py", b"A" * (MAX_ZIP_EXPANDED_BYTES + 1))
        destination = self.root / "safe-expanded"
        destination.mkdir()
        with self.assertRaises(RepairError):
            extract_repository_zip(archive_path, destination)


class PatchAndTestIsolationTests(TemporaryProject):
    def test_patch_validation_requires_valid_relative_application_source(self) -> None:
        index = RepositoryIndex(self.root)
        candidate = _candidate_diffs(
            {
                "candidates": [{
                    "summary": "Use the expected multiplier",
                    "diff": (
                        "--- a/calculator.py\n"
                        "+++ b/calculator.py\n"
                        "@@ -1,2 +1,2 @@\n"
                        " def total(amount):\n"
                        "-    return amount * 2\n"
                        "+    return amount * 3\n"
                    ),
                }]
            },
            index,
        )
        self.assertEqual(json.loads(candidate[0]["paths"]), ["calculator.py"])
        self.assertIn("diff", _parse_model_json(json.dumps({"diff": "value"})))
        unsafe = (
            "--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-a\n+b\n"
        )
        with self.assertRaises(RepairError):
            _candidate_diffs({"diff": unsafe}, index)

    def test_candidate_diff_must_apply_cleanly_in_git_snapshot(self) -> None:
        if not self.git:
            self.skipTest("Git is not installed")
        import subprocess

        index = RepositoryIndex(self.root)
        candidate = _candidate_diffs(
            {
                "diff": (
                    "--- a/calculator.py\n"
                    "+++ b/calculator.py\n"
                    "@@ -1,2 +1,2 @@\n"
                    " def total(amount):\n"
                    "-    return amount * 2\n"
                    "+    return amount * 3\n"
                )
            },
            index,
        )[0]
        snapshot = index.snapshot_files()
        safe_root = engine._prepare_candidate(self.root, snapshot, candidate, {"calculator.py"})
        try:
            result = subprocess.run(
                [self.git, "-C", str(safe_root), "diff", "--", "calculator.py"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("return amount * 3", (safe_root / "calculator.py").read_text())
        finally:
            shutil.rmtree(safe_root.parent, ignore_errors=True)
        self.assertIn("return amount * 2", self.file.read_text())

    def test_multifile_candidate_patches_every_file_in_isolated_tree(self) -> None:
        if not self.git:
            self.skipTest("Git is not installed")
        index = RepositoryIndex(self.root)
        (self.root / "constants.py").write_text("FACTOR = 2\n", encoding="utf-8")
        index = RepositoryIndex(self.root)
        diff = (
            "--- a/calculator.py\n"
            "+++ b/calculator.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def total(amount):\n"
            "-    return amount * 2\n"
            "+    return amount * 3\n"
            "--- a/constants.py\n"
            "+++ b/constants.py\n"
            "@@ -1 +1 @@\n"
            "-FACTOR = 2\n"
            "+FACTOR = 3\n"
        )
        candidate = _candidate_diffs({"diff": diff}, index)[0]
        snapshot = index.snapshot_files()
        root = engine._prepare_candidate(self.root, snapshot, candidate, {"calculator.py", "constants.py"})
        try:
            self.assertIn("return amount * 3", (root / "calculator.py").read_text())
            self.assertIn("FACTOR = 3", (root / "constants.py").read_text())
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)
        self.assertIn("return amount * 2", (self.root / "calculator.py").read_text())

    def test_python_test_command_uses_the_current_interpreter_without_a_shell(self) -> None:
        result = run_test_command(
            "python -c \"print('isolated test passed')\"",
            self.root,
            timeout=10,
        )
        self.assertTrue(result["passed"], result["output"])
        self.assertIn("isolated test passed", result["output"])
        with self.assertRaises(RepairError):
            run_test_command("python -c \"print(1)\" && whoami", self.root)
        with self.assertRaises(RepairError):
            run_test_command("git reset --hard", self.root)

    def test_timing_out_test_runner_is_reported_as_failure(self) -> None:
        result = run_test_command(
            "python -c \"import time; time.sleep(2)\"",
            self.root,
            timeout=1,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(result["timed_out"])


class StagedGroqPipelineTests(TemporaryProject):
    def setUp(self) -> None:
        super().setUp()
        self.patches = [
            {
                "summary": "Candidate from the bug-context tier",
                "diff": (
                    "--- a/calculator.py\n"
                    "+++ b/calculator.py\n"
                    "@@ -1,2 +1,2 @@\n"
                    " def total(amount):\n"
                    "-    return amount * 2\n"
                    "+    return amount * 3\n"
                ),
            }
        ]
        self.inputs: list[str] = []
        self.tiers: list[str] = []

    def mock_groq(self, patches_by_call: list[list[dict[str, str]]]):
        instance = self

        class FakeGroq:
            def __init__(self, *, api_key: str, **kwargs: Any) -> None:
                instance.assertEqual(api_key, "test-only-groq-key")
                instance.assertGreaterEqual(kwargs.get("timeout", 0), 30)

            @property
            def chat(self):
                return self

            @property
            def completions(self):
                return self

            def create(self, **kwargs: Any):
                prompt = kwargs["messages"][0]["content"]
                instance.inputs.append(prompt)
                instance.tiers.append(kwargs["messages"][0]["content"].split("KNOWLEDGE TIER: ", 1)[1].split(".", 1)[0])
                self.assert_generation_parameters(kwargs)
                results = patches_by_call[len(instance.inputs) - 1]

                class Choice:
                    def __init__(self, content: str) -> None:
                        self.choices = [type("Patch", (), {
                            "delta": type("Delta", (), {"content": content})()
                        })()]

                def stream():
                    yield Choice(json.dumps({"candidates": results}))

                return stream()

            @staticmethod
            def assert_generation_parameters(kwargs: dict[str, Any]) -> None:
                assert kwargs["model"] == engine.MODEL
                assert kwargs["stream"] is True
                assert kwargs["reasoning_effort"] == "medium"
                assert kwargs["max_completion_tokens"] == 2_048

        return FakeGroq

    def test_first_passing_bug_candidate_stops_before_escalating(self) -> None:
        passing_patch = {
            "summary": "Fix amount calculation",
            "diff": (
                "--- a/calculator.py\n"
                "+++ b/calculator.py\n"
                "@@ -1,2 +1,2 @@\n"
                " def total(amount):\n"
                "-    return amount * 2\n"
                "+    return amount * 3\n"
            ),
        }
        source = self.root / "candidate_tests.py"
        source.write_text("", encoding="utf-8")
        events: list[dict[str, Any]] = []
        with patch.dict(os.environ, {"GROQ_API_KEY": "test-only-groq-key"}), \
             patch.dict("sys.modules", {"groq": type("GroqSDK", (), {"Groq": self.mock_groq([[passing_patch]])})()}), \
             patch.object(engine, "run_test_command", return_value={
                 "passed": True, "timed_out": False, "return_code": 0, "duration": 0.02,
                 "output": "tests passed",
             }):
            candidates = engine.stream_repair_events(
                root=self.root,
                index=RepositoryIndex(self.root),
                relative="calculator.py",
                line=2,
                description="The expected total is tripled.",
                failing_tests="",
                test_command="python -m unittest",
                repair_id="a" * 32,
                on_event=events.append,
            )
        self.assertEqual(self.tiers, ["Bug Knowledge"])
        self.assertEqual(len(candidates), 1)
        self.assertIn("candidate", [event["type"] for event in events])
        self.assertEqual(events[-1]["type"], "result")
        self.assertEqual(events[-1]["tier"], "bug")
        self.assertIn("calculator.py", candidates[0]["files"])

    def test_failed_bug_candidate_escalates_to_repository_with_test_feedback(self) -> None:
        patch_for_failure = {
            "summary": "Change the multiplier",
            "diff": (
                "--- a/calculator.py\n"
                "+++ b/calculator.py\n"
                "@@ -1,2 +1,2 @@\n"
                " def total(amount):\n"
                "-    return amount * 2\n"
                "+    return amount * 3\n"
            ),
        }
        index = RepositoryIndex(self.root)
        events: list[dict[str, Any]] = []
        test_results = [
            {"passed": False, "timed_out": False, "return_code": 1, "duration": 0.1, "output": "AssertionError: expected 8, got 6"},
            {"passed": True, "timed_out": False, "return_code": 0, "duration": 0.1, "output": "tests passed"},
        ]
        with patch.dict(os.environ, {"GROQ_API_KEY": "test-only-groq-key"}), \
             patch.dict("sys.modules", {"groq": type("GroqSDK", (), {"Groq": self.mock_groq([[patch_for_failure], [patch_for_failure]])})()}), \
             patch.object(engine, "run_test_command", side_effect=test_results):
            candidates = engine.stream_repair_events(
                root=self.root,
                index=index,
                relative="calculator.py",
                line=2,
                description="The amount should be tripled.",
                failing_tests="AssertionError: expected 8, got 6",
                test_command="python -m unittest",
                repair_id="b" * 32,
                on_event=events.append,
            )
        self.assertEqual(self.tiers, ["Bug Knowledge", "Repository Knowledge"])
        self.assertIn("Recent Git history touching calculator.py", self.inputs[1])
        self.assertIn("AssertionError: expected 8, got 6", self.inputs[1])
        self.assertEqual(candidates[0]["tier"], "repository")

    def test_python_symbols_find_caller_and_callee_names(self) -> None:
        symbols = PythonSymbols()
        import ast

        symbols.visit(ast.parse("def render():\n    calculate()\ndef caller():\n    render()\n"))
        self.assertTrue(any(edge["caller"] == "render" and edge["callee"] == "calculate" for edge in symbols.edges))


class LocalHTTPIntegrationTests(TemporaryProject):
    def setUp(self) -> None:
        super().setUp()
        self.client = backend.app.test_client()
        self.saved_backend_state = {
            "state_root": backend.STATE_ROOT,
            "repository_store": backend.REPOSITORY_STORE,
            "upload_store": backend.UPLOAD_STORE,
            "backup_store": backend.BACKUP_STORE,
            "repositories": backend.repositories.copy(),
            "indexes": backend.indexes.copy(),
            "patches": backend.patches.copy(),
        }
        isolated_state = self.root / "patchwork-state"
        backend.STATE_ROOT = isolated_state
        backend.REPOSITORY_STORE = isolated_state / "repositories"
        backend.UPLOAD_STORE = isolated_state / "uploads"
        backend.BACKUP_STORE = isolated_state / "backups"
        for directory in (
            backend.STATE_ROOT,
            backend.REPOSITORY_STORE,
            backend.UPLOAD_STORE,
            backend.BACKUP_STORE,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        backend.repositories.clear()
        backend.indexes.clear()
        backend.patches.clear()

    def tearDown(self) -> None:
        backend.STATE_ROOT = self.saved_backend_state["state_root"]
        backend.REPOSITORY_STORE = self.saved_backend_state["repository_store"]
        backend.UPLOAD_STORE = self.saved_backend_state["upload_store"]
        backend.BACKUP_STORE = self.saved_backend_state["backup_store"]
        backend.repositories.clear()
        backend.repositories.update(self.saved_backend_state["repositories"])
        backend.indexes.clear()
        backend.indexes.update(self.saved_backend_state["indexes"])
        backend.patches.clear()
        backend.patches.update(self.saved_backend_state["patches"])
        super().tearDown()

    def test_home_and_status_serve_correct_local_assets(self) -> None:
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn(b"/assets/app.js", home.data)
        self.assertEqual(self.client.get("/health").json["ready"], True)
        script = self.client.get("/assets/app.js")
        stylesheet = self.client.get("/assets/app.css")
        self.assertEqual(script.status_code, 200)
        self.assertEqual(stylesheet.status_code, 200)
        script.close()
        stylesheet.close()

    def test_connect_real_local_repository_and_index_source(self) -> None:
        response = self.client.post("/api/repos/connect", json={"source": "local", "path": str(self.root)})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIn("calculator.py", [item["path"] for item in response.json["repository"]["analysis"]["files"]])
        self.assertEqual(response.json["repository"]["analysis"]["test_count"], 1)

    def test_connect_public_github_uses_repo_name_and_indexes_locally(self) -> None:
        def clone_repository(url: str, destination: Path) -> None:
            self.assertEqual(url, "https://github.com/example/project.git")
            destination.mkdir(parents=True)
            (destination / "main.py").write_text("print('hello')\n", encoding="utf-8")

        with patch.object(backend, "clone_github_repository", side_effect=clone_repository):
            response = self.client.post(
                "/api/repos/connect",
                json={"source": "github", "url": "https://github.com/example/project"},
            )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["repository"]["name"], "project")
        self.assertEqual(response.json["repository"]["analysis"]["source_count"], 1)

    def test_missing_user_consents_cannot_start_generation_or_execute_test(self) -> None:
        connection = self.client.post("/api/repos/connect", json={"source": "local", "path": str(self.root)})
        repository_id = connection.json["repository"]["id"]
        response = self.client.post(
            "/api/repair/stream",
            json={
                "repository_id": repository_id,
                "file_path": "calculator.py",
                "description": "Fix output",
                "test_command": "python -m unittest",
                "share_context": True,
            },
            buffered=True,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Test execution is opt-in", response.data)

    def test_missing_groq_key_is_reported_as_a_stream_error(self) -> None:
        connection = self.client.post("/api/repos/connect", json={"source": "local", "path": str(self.root)})
        repository_id = connection.json["repository"]["id"]
        with patch.dict(os.environ, {"GROQ_API_KEY": ""}):
            response = self.client.post(
                "/api/repair/stream",
                json={
                    "repository_id": repository_id,
                    "file_path": "calculator.py",
                    "description": "Fix the total.",
                    "test_command": "python -m unittest",
                    "share_context": True,
                    "test_consent": backend.MINIMUM_TEST_CONSENT,
                },
                buffered=False,
            )
            first_event = next(response.response)
        self.assertIn(b"repository scan is local", first_event)
        self.assertIn(b"GROQ_API_KEY", first_event)
        response.close()

    def test_repair_events_are_sent_before_generation_finishes(self) -> None:
        connection = self.client.post("/api/repos/connect", json={"source": "local", "path": str(self.root)})
        repository_id = connection.json["repository"]["id"]
        generation_finished = threading.Event()

        def slow_generation(**kwargs: Any) -> None:
            kwargs["on_event"]({"type": "progress", "message": "generation started"})
            time.sleep(0.2)
            generation_finished.set()

        with patch.object(backend, "stream_repair_events", side_effect=slow_generation):
            response = self.client.post(
                "/api/repair/stream",
                json={
                    "repository_id": repository_id,
                    "file_path": "calculator.py",
                    "description": "Fix the total.",
                    "test_command": "python -m unittest",
                    "share_context": True,
                    "test_consent": backend.MINIMUM_TEST_CONSENT,
                },
                buffered=False,
            )
            first_event = next(response.response)
            self.assertIn(b"generation started", first_event)
            self.assertFalse(generation_finished.is_set())
            response.close()

    def test_patch_application_checks_hash_and_can_only_succeed_once(self) -> None:
        connection = self.client.post("/api/repos/connect", json={"source": "local", "path": str(self.root)})
        repository_id = connection.json["repository"]["id"]
        original = self.file.read_bytes()
        patch_id = "c" * 32
        candidate = {
            "created_at": time.time(),
            "repository_id": repository_id,
            "files": {"calculator.py": b"def total(amount):\n    return amount * 3\n".hex()},
            "sha256": {"calculator.py": hashlib.sha256(original).hexdigest()},
        }
        with backend.patch_lock:
            backend.patches[patch_id] = candidate
        with patch.object(backend, "BACKUP_STORE", self.root / "backups"):
            applied = self.client.post(
                f"/api/repair/{patch_id}/apply", json={"confirm": True}
            )
            self.assertEqual(applied.status_code, 200, applied.json)
            self.assertEqual(self.file.read_text(), "def total(amount):\n    return amount * 3\n")
            repeated = self.client.post(
                f"/api/repair/{patch_id}/apply", json={"confirm": True}
            )
            self.assertEqual(repeated.status_code, 400)
            self.assertIn("expired", repeated.json["error"])

    def test_patch_application_rejects_files_changed_after_validation(self) -> None:
        connection = self.client.post("/api/repos/connect", json={"source": "local", "path": str(self.root)})
        repository_id = connection.json["repository"]["id"]
        patch_id = "d" * 32
        with backend.patch_lock:
            backend.patches[patch_id] = {
                "created_at": time.time(),
                "repository_id": repository_id,
                "files": {"calculator.py": b"def total(amount):\n    return amount * 3\n".hex()},
                "sha256": {"calculator.py": hashlib.sha256(b"older contents").hexdigest()},
            }
        response = self.client.post(
            f"/api/repair/{patch_id}/apply", json={"confirm": True}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("changed after validation", response.json["error"])
        self.assertEqual(self.file.read_text(), "def total(amount):\n    return amount * 2\n")


if __name__ == "__main__":
    unittest.main()
