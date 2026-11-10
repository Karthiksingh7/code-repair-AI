from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import signal
import shlex
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse


MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
MAX_FILES = 2_500
MAX_FILE_BYTES = 1_000_000
MAX_REPOSITORY_BYTES = 80_000_000
MAX_CONTEXT_BYTES = 32_000
MAX_DIFF_BYTES = 30_000
MAX_ZIP_BYTES = 50_000_000
MAX_ZIP_EXPANDED_BYTES = 80_000_000
MAX_TEST_SECONDS = 120

SOURCE_SUFFIXES = {
    ".py", ".pyi", ".pyx", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".vue", ".svelte", ".go", ".rs", ".java", ".kt", ".scala", ".cs", ".fs",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".hh", ".rb", ".php", ".swift", ".ex",
    ".exs", ".erl", ".hs", ".lua", ".sh", ".bash", ".ps1", ".sql", ".html",
    ".css",
}
TEST_PARTS = {"test", "tests", "spec", "specs", "__tests__"}
IGNORED_PARTS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", ".idea",
    ".vscode", ".next", ".nuxt", ".cache", ".tox", ".mypy_cache",
    ".pytest_cache", "__pycache__", "dist", "build", "target", "vendor",
    "coverage", ".coverage", ".patchwork",
}
SECRET_NAMES = re.compile(
    r"(^|[._-])(?:\.env(?:\..*)?|id_rsa(?:\..*)?|id_ed25519(?:\..*)?|"
    r"secrets?(?:\..*)?|credentials?(?:\..*)?|token(?:\..*)?|"
    r"[^/\\]*(?:private[_-]?key|\.pem|\.p12|\.pfx))$",
    re.IGNORECASE,
)
SECRET_TEXT = re.compile(
    r"\b(?:gsk_[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16})\b"
    r"|^(\s*(?:GROQ_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|"
    r"AWS_SECRET_ACCESS_KEY|AWS_ACCESS_KEY_ID|GITHUB_TOKEN|GH_TOKEN|"
    r"API_KEY|SECRET_KEY|PRIVATE_KEY)\s*[:=]\s*).+$",
    re.IGNORECASE | re.MULTILINE,
)

IGNORED_TEST_ENV = {
    "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "GITHUB_TOKEN", "GH_TOKEN", "NPM_TOKEN",
}


class RepairError(Exception):
    """A user-correctable repository or repair request error."""


@dataclass(frozen=True)
class RepositoryInfo:
    id: str
    name: str
    path: Path
    source: str
    created_at: float


def safe_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise RepairError("Select a repository file using its relative path.")
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RepairError("Repository file paths must stay inside the selected repository.")
    if re.match(r"^[A-Za-z]:", normalized) or normalized.startswith("//"):
        raise RepairError("Absolute paths are not valid repository file paths.")
    if any(SECRET_NAMES.search(part) for part in path.parts):
        raise RepairError("Sensitive configuration and credential files are never sent to Groq.")
    if path.suffix.lower() not in SOURCE_SUFFIXES:
        raise RepairError(f"Choose a supported source-code file, not {path.suffix or 'this file type'}.")
    return path.as_posix()


def path_is_inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def git_command(root: Path, args: list[str], timeout: int = 4) -> str:
    env = os.environ.copy()
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    for key in ("GROQ_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"):
        env.pop(key, None)
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.decode("utf-8", errors="replace")


def resolve_repository_path(raw_path: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip() or len(raw_path) > 1_024:
        raise RepairError("Enter the full path to a repository on this computer.")
    user_path = Path(raw_path.strip()).expanduser()
    if not user_path.is_absolute():
        raise RepairError("Repository folder paths must be absolute paths.")
    try:
        resolved = user_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RepairError("The repository folder could not be found.") from exc
    if not resolved.is_dir():
        raise RepairError("The repository path must point to a folder.")
    if user_path.is_symlink() or resolved.parent == resolved:
        raise RepairError("Select a repository folder, not a filesystem root or symbolic link.")
    if resolved in {Path.home().resolve(), Path.home().resolve().parent}:
        raise RepairError("For safety, select a specific project folder, not your whole home directory.")
    git_root = git_command(resolved, ["rev-parse", "--show-toplevel"]).strip()
    if git_root:
        try:
            resolved = Path(git_root).resolve(strict=True)
        except OSError as exc:
            raise RepairError("Git returned an invalid repository path.") from exc
    return resolved


def validate_github_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 350:
        raise RepairError("Enter a public GitHub URL such as https://github.com/org/project.")
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.port not in (None, 443):
        raise RepairError("For secure cloning, only public HTTPS github.com repository URLs are supported.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RepairError("GitHub repository URLs cannot contain credentials, query strings, or fragments.")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2 or any(part in {".", ".."} for part in parts):
        raise RepairError("Enter a repository URL in the form https://github.com/owner/name.")
    owner, name = parts
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner):
        raise RepairError("Invalid GitHub owner name.")
    repository = name.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository):
        raise RepairError("Invalid GitHub repository name.")
    return f"https://github.com/{owner}/{repository}.git"


def infer_language(path: str) -> str:
    return {
        ".py": "Python", ".pyi": "Python", ".js": "JavaScript",
        ".jsx": "JavaScript / JSX", ".mjs": "JavaScript", ".cjs": "JavaScript",
        ".ts": "TypeScript", ".tsx": "TypeScript / JSX", ".vue": "Vue",
        ".svelte": "Svelte", ".go": "Go", ".rs": "Rust", ".java": "Java",
        ".kt": "Kotlin", ".cs": "C#", ".c": "C", ".cpp": "C++",
        ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
        ".ex": "Elixir", ".exs": "Elixir",
    }.get(Path(path).suffix.lower(), "Source")


def _include_in_index(path: str, suffix: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts or any(part.lower() in IGNORED_PARTS for part in parts):
        return False
    if any(SECRET_NAMES.search(part) for part in parts):
        return False
    name = parts[-1].lower()
    is_docs = (
        suffix.lower() in {".md", ".mdx", ".rst", ".adoc"}
        or (name in {"readme", "changelog", "contributing", "architecture"}
            and not Path(name).suffix)
    )
    is_issue_history = parts[0].lower() in {"issues", "issue-history"}
    return suffix.lower() in SOURCE_SUFFIXES or is_docs or is_issue_history or name in {
        "pyproject.toml", "requirements.txt", "requirements-dev.txt",
        "package.json", "go.mod", "cargo.toml", "composer.json",
        "gemfile", "pom.xml", "build.gradle", "build.gradle.kts",
        "dockerfile", "makefile", "tsconfig.json",
    }


class PythonSymbols(ast.NodeVisitor):
    def __init__(self) -> None:
        self.definitions: dict[str, list[dict[str, Any]]] = {}
        self.edges: list[dict[str, str]] = []
        self.current: list[str] = []
        self.imports: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.update(alias.name for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.imports.add(node.module)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.definitions.setdefault(node.name, []).append({
            "name": node.name,
            "line": node.lineno,
            "end_line": node.end_lineno or node.lineno,
            "kind": "function",
        })
        self.current.append(node.name)
        for child in node.body:
            self.visit(child)
        self.current.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.definitions.setdefault(node.name, []).append({
            "name": node.name,
            "line": node.lineno,
            "end_line": node.end_lineno or node.lineno,
            "kind": "class",
        })
        self.current.append(node.name)
        for child in node.body:
            self.visit(child)
        self.current.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if self.current:
            function_name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else None
            )
            if function_name:
                self.edges.append({"caller": self.current[-1], "callee": function_name})
        self.generic_visit(node)


class RepositoryIndex:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        self.files: dict[str, dict[str, Any]] = {}
        self.python: dict[str, PythonSymbols] = {}
        self.languages: Counter[str] = Counter()
        self.skipped: list[str] = []
        self.total_bytes = 0
        self._index()

    def _candidate_paths(self) -> list[str]:
        tracked = git_command(self.root, ["ls-files", "-c", "-o", "--exclude-standard", "-z"], 15)
        paths = {part for part in tracked.split("\x00") if part}
        if not paths:
            for directory, names, filenames in os.walk(self.root, followlinks=False):
                names[:] = [name for name in names if name.lower() not in IGNORED_PARTS]
                for name in filenames:
                    full = Path(directory, name)
                    if full.is_symlink():
                        continue
                    try:
                        paths.add(full.relative_to(self.root).as_posix())
                    except ValueError:
                        continue
                    if len(paths) > MAX_FILES:
                        raise RepairError(f"This repository has more than {MAX_FILES:,} files to inspect. Use a smaller repository.")
        if len(paths) > MAX_FILES:
            raise RepairError(f"This repository has more than {MAX_FILES:,} tracked files to inspect. Use a smaller repository.")
        return sorted(paths)

    def _index(self) -> None:
        for relative in self._candidate_paths():
            path = PurePosixPath(relative)
            if not _include_in_index(relative, path.suffix):
                continue
            full = self.root.joinpath(*path.parts)
            if not path_is_inside(self.root, full) or not full.is_file():
                continue
            try:
                size = full.stat().st_size
                if size > MAX_FILE_BYTES or self.total_bytes + size > MAX_REPOSITORY_BYTES:
                    self.skipped.append(relative)
                    continue
                data = full.read_bytes()
                content = data.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            content = redact_secrets(content)
            self.total_bytes += size
            suffix = path.suffix.lower()
            language = infer_language(relative)
            self.languages[language] += 1
            is_source = suffix in SOURCE_SUFFIXES
            is_test_name = bool(re.search(
                r"(^|[._-])(?:test|tests|spec|specs)(?=[._-]|$)",
                path.stem,
                re.IGNORECASE,
            ))
            entry: dict[str, Any] = {
                "path": relative,
                "content": content,
                "size": size,
                "language": language,
                "source": is_source,
                "test": is_source and (
                    any(part.lower() in TEST_PARTS for part in path.parts)
                    or is_test_name
                ),
                "documentation": suffix in {".md", ".mdx", ".rst", ".adoc"}
                    or (path.parts[0].lower() in {"issues", "issue-history"} if path.parts else False),
            }
            self.files[relative] = entry
            if suffix == ".py":
                try:
                    symbols = PythonSymbols()
                    symbols.visit(ast.parse(content, filename=relative))
                    self.python[relative] = symbols
                except SyntaxError:
                    continue

    @property
    def source_files(self) -> list[dict[str, Any]]:
        return [entry for entry in self.files.values() if entry["source"]]

    @property
    def test_files(self) -> list[dict[str, Any]]:
        return [entry for entry in self.source_files if entry["test"]]

    @property
    def docs(self) -> list[dict[str, Any]]:
        return [entry for entry in self.files.values() if entry["documentation"]]

    def summary(self) -> dict[str, Any]:
        counts = Counter(entry["language"] for entry in self.files.values() if entry["source"])
        return {
            "name": self.root.name,
            "root": str(self.root),
            "file_count": len(self.files),
            "source_count": len(self.source_files),
            "test_count": len(self.test_files),
            "doc_count": len(self.docs),
            "language": counts.most_common(1)[0][0] if counts else "Unknown",
            "languages": [{"name": name, "files": count} for name, count in counts.most_common(5)],
            "files": [
                {"path": entry["path"], "language": entry["language"], "test": entry["test"]}
                for entry in self.source_files[:180]
            ],
            "tests_preview": [
                {"path": entry["path"], "command": suggest_test_command(entry)}
                for entry in self.test_files[:20]
            ],
            "skip_count": len(self.skipped),
        }

    def read_source(self, relative: str, line: int = 0) -> str:
        entry = self.files.get(relative)
        if entry is None or not entry["source"]:
            raise RepairError("Choose an existing, indexed source-code file inside this repository.")
        content = entry["content"]
        lines = content.splitlines()
        symbols = self.python.get(relative)
        if symbols and line > 0:
            selected = [
                symbol for definitions in symbols.definitions.values() for symbol in definitions
                if symbol["line"] <= line <= symbol["end_line"]
                and symbol["kind"] == "function"
            ]
            if selected:
                symbol = min(selected, key=lambda item: item["end_line"] - item["line"])
                start = max(0, symbol["line"] - 16)
                end = min(len(lines), symbol["end_line"] + 15)
                return "\n".join(f"{number + 1}: {text}" for number, text in enumerate(lines[start:end], start))
        if len(content) > 45_000:
            center = max(0, line - 1) if line else 0
            start = max(0, center - 65)
            end = min(len(lines), start + 140)
            if start == 0 and line == 0:
                end = min(len(lines), 140)
            return "\n".join(f"{number + 1}: {text}" for number, text in enumerate(lines[start:end], start))
        return "\n".join(f"{number + 1}: {text}" for number, text in enumerate(lines))

    def bug_context(self, relative: str, line: int, description: str) -> str:
        return (
            f"Bug report:\n{redact_secrets(description)[:6_000]}\n\n"
            f"Failing source location:\nFile: {relative}\n"
            f"Line: {line or 'not supplied'}\n\n"
            f"Relevant source:\n{self.read_source(relative, line)}"
        )[:MAX_CONTEXT_BYTES]

    def repository_context(self, relative: str) -> str:
        documents: list[str] = []
        symbols = self.python.get(relative)
        if symbols:
            definitions = [
                (name, item, path) for path, module in self.python.items()
                for name, items in module.definitions.items() for item in items
            ]
            needed_names: Counter[str] = Counter(edge["callee"] for edge in symbols.edges)
            needed_names.update(imported.rsplit(".", 1)[-1] for imported in symbols.imports)
            for name, _ in needed_names.most_common(12):
                for definition, metadata, path in definitions:
                    if definition == name and path != relative:
                        source = self.files[path]["content"].splitlines()
                        start = max(0, metadata["line"] - 5)
                        end = min(len(source), metadata["end_line"] + 4)
                        documents.append(
                            f"Python dependency {path}:{metadata['line']} ({metadata['kind']} {definition}):\n"
                            + "\n".join(f"{idx + 1}: {text}" for idx, text in enumerate(source[start:end], start))
                        )
                        break
            relevant = sorted({
                f"{edge['caller']} -> {edge['callee']}"
                for edge in symbols.edges
            })
            if relevant:
                documents.append("Local Python caller/callee references found:\n" + "\n".join(relevant[:45]))
            if symbols.imports:
                documents.append("Imports in the failing Python module:\n" + "\n".join(sorted(symbols.imports)[:35]))
        dependencies = [
            f"{path}:\n{entry['content'][:4_500]}"
            for path, entry in self.files.items()
            if PurePosixPath(path).name.lower() in {
                "pyproject.toml", "requirements.txt", "requirements-dev.txt",
                "package.json", "go.mod", "cargo.toml",
            }
        ]
        if dependencies:
            documents.append("Repository dependency manifests:\n" + "\n\n".join(dependencies[:5]))
        commits = git_command(
            self.root, ["log", "-8", "--format=%h %s", "--", relative], 8
        ).strip()
        if commits:
            documents.append(f"Recent Git history touching {relative}:\n{commits[:2_500]}")
            commits_history = git_command(
                self.root, ["log", "-18", "--format=%H", "--", relative], 8
            ).splitlines()[:12]
            cooccurring: Counter[str] = Counter()
            for commit in commits_history:
                files = git_command(
                    self.root, ["diff-tree", "--no-commit-id", "--name-only", "-r", commit], 4
                )
                cooccurring.update(
                    name.strip() for name in files.splitlines()
                    if name.strip() and name.strip() != relative
                    and name.strip() in self.files
                )
            if cooccurring:
                related = "\n".join(f"{path} (co-committed {count} times)" for path, count in cooccurring.most_common(10))
                documents.append("Files frequently co-committed with the failing file (Git history heuristic):\n" + related)
        if not documents:
            documents.append("No related Python callers, project manifests, or Git history were available for this source file.")
        return "\n\n".join(documents)[:MAX_CONTEXT_BYTES]

    def project_context(self, relative: str, bug_description: str) -> str:
        query = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", f"{relative} {bug_description}".lower()))
        ranked: list[tuple[float, dict[str, Any]]] = []
        for entry in self.docs:
            content = entry["content"]
            tokens = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", content.lower()))
            if not tokens:
                continue
            overlap = len(query.intersection(tokens))
            score = overlap / max(1, len(query) ** 0.5 * len(tokens) ** 0.32)
            if score > 0:
                ranked.append((score, entry))
        ranked.sort(key=lambda result: result[0], reverse=True)
        if not ranked:
            return "No relevant README, Markdown documentation, or checked-in issue-history documents were found."
        return "\n\n".join(
            f"Local text-match result: {entry['path']} (relevance {score:.2f}):\n{entry['content'][:5_000]}"
            for score, entry in ranked[:4]
        )[:MAX_CONTEXT_BYTES]

    def snapshot_files(self) -> dict[str, str]:
        candidates: dict[str, str] = {}
        byte_count = 0
        for path in self._candidate_paths():
            pure = PurePosixPath(path)
            if not pure.parts or any(part.lower() in IGNORED_PARTS for part in pure.parts):
                continue
            if any(SECRET_NAMES.search(part) for part in pure.parts):
                continue
            full = self.root.joinpath(*pure.parts)
            if not path_is_inside(self.root, full) or not full.is_file():
                continue
            try:
                size = full.stat().st_size
                if size > 5_000_000 or byte_count + size > MAX_REPOSITORY_BYTES:
                    continue
                candidates[path] = str(full)
                byte_count += size
                if len(candidates) >= MAX_FILES:
                    raise RepairError(f"Test snapshots support at most {MAX_FILES:,} repository files.")
            except OSError:
                continue
        if not candidates:
            raise RepairError("No accessible project files were found to build a safe test snapshot.")
        return candidates


def redact_secrets(value: str) -> str:
    return SECRET_TEXT.sub(lambda match: f"{match.group(1) if match.lastindex else ''}[REDACTED]", value)


def suggest_test_command(entry: dict[str, Any]) -> str:
    path = entry["path"].replace("\\", "/")
    if path.endswith(".py"):
        return f'python -m pytest -q "{path}"'
    if Path(path).suffix.lower() in {".js", ".mjs", ".cjs"}:
        return f'node --test "{path}"'
    if Path(path).suffix.lower() in {".ts", ".tsx", ".jsx"}:
        return "Add your project's test command"
    if path.endswith("_test.go") or path.endswith("_test.rs"):
        return "go test ./... (or cargo test)"
    return "Enter the project's test command"


def clone_github_repository(url: str, destination: Path) -> None:
    env = os.environ.copy()
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ALLOW_PROTOCOL": "https",
    })
    for key in ("GROQ_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"):
        env.pop(key, None)
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "30", "--no-recurse-submodules",
             "--filter=blob:limit=2m", "--", url, str(destination)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
            check=False,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        raise RepairError("Git must be installed before connecting a GitHub repository.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RepairError("Cloning the public GitHub repository took too long. Try a smaller repository.") from exc
    if result.returncode:
        error = redact_secrets(result.stderr.decode("utf-8", errors="replace"))[:800]
        raise RepairError(f"Unable to clone the public repository. {error or 'Check the URL and your connection.'}")


def extract_repository_zip(archive_path: Path, destination: Path) -> Path:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES:
                raise RepairError(f"Repository ZIPs may contain up to {MAX_FILES:,} files.")
            expanded = sum(item.file_size for item in infos)
            compressed = sum(item.compress_size for item in infos)
            if expanded > MAX_ZIP_EXPANDED_BYTES or compressed > MAX_ZIP_BYTES:
                raise RepairError("Repository ZIP exceeds the supported archive size.")
            if expanded > max(10_000_000, compressed * 200):
                raise RepairError("This ZIP has an unsafe compression ratio and was not extracted.")
            for info in infos:
                relative = info.filename.replace("\\", "/")
                if relative.startswith("/") or re.match(r"^[A-Za-z]:", relative):
                    raise RepairError("The uploaded ZIP contains an absolute file path.")
                parts = PurePosixPath(relative).parts
                if not parts or any(part in {"", ".", ".."} for part in parts):
                    raise RepairError("The uploaded ZIP contains a file path outside its root.")
                unix_mode = info.external_attr >> 16
                if (unix_mode & 0o170000) == 0o120000:
                    raise RepairError("Symbolic links inside repository ZIP files are not supported.")
                if any(part.lower() in IGNORED_PARTS for part in parts):
                    continue
                if any(SECRET_NAMES.search(part) for part in parts):
                    continue
                target = destination.joinpath(*parts)
                destination_root = destination.resolve(strict=True)
                target_parent = target.parent.resolve(strict=False)
                if (
                    len(str(target)) > 1_024
                    or not target_parent.is_relative_to(destination_root)
                ):
                    raise RepairError("The uploaded ZIP contains an unsafe path.")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=64 * 1024)
        entries = list(destination.iterdir())
        repository_marker = next(
            (item for item in destination.rglob(".git") if item.parent.is_dir()), None
        )
        if repository_marker is not None:
            return repository_marker.parent.resolve()
        folders = [item for item in entries if item.is_dir()]
        if len(folders) == 1 and not any(item.is_file() for item in entries):
            return folders[0].resolve()
        return destination.resolve()
    except zipfile.BadZipFile as exc:
        raise RepairError("That file isn't a valid ZIP archive.") from exc
    except FileExistsError as exc:
        raise RepairError("The ZIP contains duplicate file paths.") from exc


def run_test_command(command: str, cwd: Path, timeout: int = MAX_TEST_SECONDS) -> dict[str, Any]:
    if not isinstance(command, str) or len(command) > 400:
        raise RepairError("Enter a test command shorter than 400 characters.")
    try:
        parts = [part.strip("\"'") for part in shlex.split(command, posix=os.name != "nt")]
    except ValueError as exc:
        raise RepairError("The test command contains unclosed quotes or invalid arguments.") from exc
    if not parts:
        raise RepairError("Enter a test command to run against each candidate patch.")
    if any(part in {";", "&&", "||", "|", ">", ">>", "<", "<<", "2>", "2>>"} for part in parts):
        raise RepairError("For safety, test commands cannot contain shell operators, redirects, or multiple commands.")
    executable = parts[0]
    program = Path(executable).name.lower().removesuffix(".exe")
    if program in {"python", "python3", "py"}:
        parts[0] = sys.executable
    elif program in {"pytest", "node", "go", "cargo", "dotnet", "java"}:
        resolved = shutil.which(executable)
        if not resolved:
            raise RepairError(f"The '{executable}' test runner isn't installed or isn't on PATH.")
        parts[0] = resolved
    else:
        raise RepairError(
            "Allowed test runners are Python, pytest, Node.js, Go, Cargo, dotnet, and Java. "
            "Shell scripts, package-manager commands, and custom executables are intentionally disabled."
        )
    safe_env = {
        key: value for key, value in os.environ.items()
        if key.upper() not in IGNORED_TEST_ENV
        and not re.search(r"(?:TOKEN|SECRET|PASSWORD|PRIVATE_KEY)", key, re.IGNORECASE)
    }
    started = time.monotonic()
    kwargs: dict[str, Any] = {
        "args": parts,
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "errors": "replace",
        "env": safe_env,
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(**kwargs)
    except FileNotFoundError as exc:
        raise RepairError(f"The '{executable}' test runner could not be started.") from exc
    except OSError as exc:
        raise RepairError(f"Unable to start the test runner: {exc}") from exc
    timed_out = False
    try:
        output, _ = process.communicate(timeout=min(timeout, MAX_TEST_SECONDS))
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
                output, _ = process.communicate(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                process.kill()
                output, _ = process.communicate()
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                output, _ = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                output, _ = process.communicate()
        output = (output or "") + f"\nTest command exceeded its {min(timeout, MAX_TEST_SECONDS)} second limit."
    output = redact_secrets(output or "")[-18_000:]
    return {
        "passed": process.returncode == 0 and not timed_out,
        "timed_out": timed_out,
        "return_code": process.returncode,
        "duration": round(time.monotonic() - started, 2),
        "output": output,
    }


def select_bug_location(index: RepositoryIndex, supplied_path: str, description: str) -> tuple[str, int]:
    if supplied_path.strip():
        match = re.match(r"^(.*?):(\d+)$", supplied_path.strip().replace("\\", "/"))
        raw_path, line = (match.group(1), int(match.group(2))) if match else (supplied_path.strip(), 0)
        relative = safe_relative_path(raw_path)
        if line > 1_000_000:
            raise RepairError("Line numbers must be less than 1,000,000.")
        if relative not in index.files or not index.files[relative]["source"]:
            raise RepairError("That source file isn't present in the connected repository.")
        return relative, line
    excerpt = description[:6_000].replace("\\", "/")
    for match in re.finditer(r"(?P<file>(?:[\w.@ -]+/)*[\w.@ -]+\.[A-Za-z]{1,8}):(?P<line>\d+)", excerpt):
        try:
            relative = safe_relative_path(match.group("file").strip())
            if relative in index.files and index.files[relative]["source"]:
                return relative, int(match.group("line"))
        except RepairError:
            continue
    raise RepairError("Enter the failing repository file (for example `src/app.py:42`) before generating a repair.")


def _parse_model_json(content: str) -> Any:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                pass
        raise RepairError("The Groq response did not contain valid JSON patches. Retry the repair.") from exc


def _candidate_diffs(response: Any, index: RepositoryIndex) -> list[dict[str, str]]:
    if isinstance(response, list):
        candidates = response
    elif isinstance(response, dict):
        candidates = response.get("candidates") or response.get("patches")
        if candidates is None and response.get("diff"):
            candidates = [response]
    else:
        candidates = None
    if not isinstance(candidates, list):
        raise RepairError("The Groq response didn't include a candidates list.")
    output = []
    for candidate in candidates[:3]:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("diff"), str):
            continue
        diff = candidate["diff"].strip() + "\n"
        if not diff or len(diff) > MAX_DIFF_BYTES:
            continue
        if "\x00" in diff or not re.search(r"^@@\s+-\d+", diff, flags=re.MULTILINE):
            continue
        file_paths = re.findall(r"^(?:---|\+\+\+)\s+(?:[ab]/)?([^\t\r\n]+)", diff, re.MULTILINE)
        if not file_paths:
            continue
        normalized_paths = []
        for file_path in file_paths:
            if file_path == "/dev/null":
                raise RepairError("New-file patches are disabled; a repair may only modify existing source files.")
            normalized = safe_relative_path(file_path)
            if normalized not in index.files or not index.files[normalized]["source"]:
                raise RepairError(f"Patch edits a file that is not indexed repository source: {normalized}.")
            normalized_paths.append(normalized)
        if set(normalized_paths) - set(index.files):
            continue
        paths_from, paths_to = normalized_paths[::2], normalized_paths[1::2]
        if len(paths_from) != len(paths_to) or paths_from != paths_to:
            raise RepairError("Patch source and destination paths must refer to the same existing file.")
        output.append({
            "summary": redact_secrets(str(candidate.get("summary", "AI-generated candidate patch")))[:300],
            "diff": diff,
            "paths": json.dumps(paths_from),
        })
    return output


def _prepare_candidate(root: Path, snapshot: dict[str, str], candidate: dict[str, str], allowed: set[str]) -> Path:
    temp = Path(tempfile.mkdtemp(prefix="patchwork-candidate-"))
    try:
        target_root = temp / "repository"
        target_root.mkdir()
        byte_total = 0
        file_total = 0
        for relative, source in snapshot.items():
            source_path = Path(source)
            if not path_is_inside(root, source_path):
                continue
            info = source_path.lstat()
            if info.st_nlink > 1 or source_path.is_symlink():
                continue
            target = target_root.joinpath(*PurePosixPath(relative).parts)
            if not path_is_inside(target_root, target.parent):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if source_path.is_file():
                if info.st_size > 5_000_000 or byte_total + info.st_size > MAX_REPOSITORY_BYTES:
                    continue
                shutil.copyfile(source_path, target)
                byte_total += info.st_size
                file_total += 1
                if file_total > MAX_FILES:
                    raise RepairError(f"Test snapshots support at most {MAX_FILES:,} repository files.")
        if not all((target_root.joinpath(*PurePosixPath(path).parts)).is_file() for path in allowed):
            raise RepairError("The repository changed while a repair was being prepared. Re-index it and try again.")
        result = subprocess.run(
            ["git", "init", "--quiet", str(target_root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise RepairError("Git must be installed to safely check and isolate candidate patches.")
        check = subprocess.run(
            ["git", "-C", str(target_root), "apply", "--check", "--"],
            input=candidate["diff"].encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if check.returncode:
            detail = redact_secrets(check.stderr.decode("utf-8", errors="replace"))[:700]
            raise RepairError(f"The generated diff doesn't apply cleanly to the selected repository. {detail}")
        apply = subprocess.run(
            ["git", "-C", str(target_root), "apply", "--"],
            input=candidate["diff"].encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if apply.returncode:
            raise RepairError("Git couldn't apply the generated patch in the isolated copy.")
        return target_root
    except (OSError, subprocess.TimeoutExpired):
        shutil.rmtree(temp, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def build_repair_prompt(
    tier: str,
    context: str,
    test_command: str,
    failed_tests: str,
    limit: int = MAX_CONTEXT_BYTES,
) -> str:
    context = redact_secrets(context)[:limit]
    failures = redact_secrets(failed_tests)[:5_000] if failed_tests else "No test results yet."
    return (
        "Repair the reported bug in this repository. Produce up to THREE genuinely distinct, minimal "
        "unified-diff candidates. Each candidate must modify only existing, indexed application source "
        "or test code files. Never create files. Never edit dependency, build, CI, workflow, shell, "
        "environment, credentials, or configuration files. Include correct unified-diff headers "
        "(--- a/path, +++ b/path) and valid @@ hunk ranges so git apply can apply your diff. "
        "Do not invent test results. A candidate is not successful until the supplied test command passes "
        "against that exact candidate in isolation. If a candidate cannot be justified, omit it. "
        "Return ONLY one JSON object exactly shaped as "
        '{"candidates":[{"summary":"short explanation","diff":"--- a/file.py\\n+++ b/file.py\\n@@ -1,2 +1,2 @@\\n ..."}]}. '
        "Do not put markdown fences or additional text around the JSON.\n\n"
        f"KNOWLEDGE TIER: {tier}.\n"
        f"REPRODUCTION TEST COMMAND: {test_command}\n"
        f"PREVIOUS FAILED TEST OUTPUT (if present):\n{failures}\n\n"
        f"AUTHORIZED, REDACTED REPOSITORY CONTEXT:\n{context}"
    )


def stream_repair_events(
    *,
    root: Path,
    index: RepositoryIndex,
    relative: str,
    line: int,
    description: str,
    failing_tests: str,
    test_command: str,
    repair_id: str,
    on_event: Any,
) -> list[dict[str, Any]]:
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key or len(api_key) > 1_000 or "\n" in api_key:
        raise RepairError(
            "The repository scan is local and does not need Groq. AI repair generation is unavailable "
            "because GROQ_API_KEY was not set when Patchwork started. Configure it in PowerShell, "
            "restart Patchwork, and reload this page. Never enter your key in this page."
        )
    context_by_tier = {
        "bug": index.bug_context(relative, line, description + (
            f"\n\nFailing test output:\n{failing_tests[:5_000]}" if failing_tests else ""
        )),
        "repository": "",
        "project": "",
    }
    failures: list[str] = []
    passing: list[dict[str, Any]] = []
    original_hashes: dict[str, str] = {}
    source_snapshot = index.snapshot_files()
    for tier_number, tier in enumerate(("bug", "repository", "project"), start=1):
        if tier == "repository":
            context_by_tier[tier] = index.repository_context(relative)
        elif tier == "project":
            context_by_tier[tier] = index.project_context(relative, description)
        on_event({
            "type": "progress",
            "tier": tier,
            "tier_number": tier_number,
            "message": (
                f"Gathering {tier} knowledge…"
                if tier != "bug" else "Checking bug details and the failing source first…"
            ),
        })
        prompt = build_repair_prompt(
            ("Bug Knowledge" if tier == "bug" else "Repository Knowledge" if tier == "repository" else "Project Knowledge"),
            context_by_tier[tier],
            test_command,
            "\n\n".join(failures[-2:]),
        )
        on_event({"type": "progress", "tier": tier, "tier_number": tier_number,
                  "message": f"Generating candidate patches with {MODEL}…"})
        try:
            from groq import Groq
        except ImportError as exc:
            raise RepairError("Install the Groq SDK with `python -m pip install -r requirements.txt`, then restart Patchwork.") from exc
        client = Groq(api_key=api_key, timeout=90, max_retries=1)
        stream = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=1,
            max_completion_tokens=2_048,
            top_p=1,
            reasoning_effort="medium",
            stream=True,
        )
        chunks = []
        stream_started = time.monotonic()
        try:
            for chunk in stream:
                if time.monotonic() - stream_started > 95:
                    raise RepairError("The Groq response exceeded its 95-second timeout. Try again.")
                piece = chunk.choices[0].delta.content or ""
                if piece:
                    chunks.append(piece)
                    if sum(map(len, chunks)) > 60_000:
                        raise RepairError("The Groq response exceeded the 60 KB safety limit.")
        except RepairError:
            raise
        except Exception as exc:
            error = str(exc)[:700]
            for secret in (api_key,):
                if secret:
                    error = error.replace(secret, "[redacted]")
            raise RepairError(f"Groq could not complete the repair: {error}") from exc
        candidates = _candidate_diffs(_parse_model_json("".join(chunks)), index)
        if not candidates:
            failures.append(f"{tier}: Groq did not return a well-formed patch to validate.")
            on_event({"type": "progress", "tier": tier, "tier_number": tier_number,
                      "message": "No applicable candidate this time; continuing with the next knowledge layer."})
            continue
        for number, candidate in enumerate(candidates, start=1):
            allowed_paths = set(json.loads(candidate["paths"]))
            if len(allowed_paths) > 8:
                failures.append(f"{tier}: candidate {number} modified more than 8 files.")
                continue
            if any(PurePosixPath(path).parts[0].lower() in {".github", ".vscode"} for path in allowed_paths):
                failures.append(f"{tier}: candidate {number} modified a workflow or workspace settings.")
                continue
            on_event({
                "type": "progress", "tier": tier, "tier_number": tier_number,
                "message": f"Checking candidate {number} of {len(candidates)} in an isolated copy…",
            })
            candidate_root = None
            try:
                candidate_root = _prepare_candidate(root, source_snapshot, candidate, allowed_paths)
                for path in allowed_paths:
                    original = root.joinpath(*PurePosixPath(path).parts)
                    if not path_is_inside(root, original):
                        raise RepairError(f"Candidate source path is no longer inside the repository: {path}.")
                    digest = hashlib.sha256(original.read_bytes()).hexdigest()
                    original_hashes[path] = digest
                result = run_test_command(test_command, candidate_root)
            except (RepairError, OSError, subprocess.TimeoutExpired) as exc:
                failures.append(f"{tier}, candidate {number}: {redact_secrets(str(exc))[:500]}")
                on_event({
                    "type": "candidate", "tier": tier, "tier_number": tier_number,
                    "candidate_number": number, "passed": False,
                    "message": f"Candidate {number} could not be validated: {redact_secrets(str(exc))[:250]}",
                })
                continue
            finally:
                if candidate_root is not None:
                    shutil.rmtree(candidate_root.parent, ignore_errors=True)
            if not result["passed"]:
                failures.append(
                    f"{tier}, candidate {number}: test command failed with exit code "
                    f"{result['return_code']}.\n{redact_secrets(result['output'])[:1_500]}"
                )
            if result["passed"]:
                candidate_id = hashlib.sha256(
                    f"{repair_id}:{tier}:{number}:{time.time_ns()}".encode()
                ).hexdigest()[:32]
                modified: dict[str, bytes] = {}
                for path in allowed_paths:
                    updated_path = candidate_root.parent / "repository"
                    # The temporary copy is removed below; keep bytes before cleanup.
                    del updated_path
                # Recreate each passing candidate to export the verified file content.
                verified_root = _prepare_candidate(root, source_snapshot, candidate, allowed_paths)
                try:
                    for path in allowed_paths:
                        updated = verified_root.joinpath(*PurePosixPath(path).parts)
                        modified[path] = updated.read_bytes()
                finally:
                    shutil.rmtree(verified_root.parent, ignore_errors=True)
                on_event({
                    "type": "candidate", "tier": tier, "tier_number": tier_number,
                    "candidate_number": number, "passed": True,
                    "message": f"Candidate {number} passed the selected test command ({result['duration']}s).",
                })
                passing.append({
                    "id": candidate_id,
                    "summary": candidate["summary"],
                    "tier": tier,
                    "diff": candidate["diff"],
                    "test": {**result, "output": result["output"][-10_000:]},
                    "files": {path: data.hex() for path, data in modified.items()},
                    "sha256": {path: original_hashes[path] for path in allowed_paths},
                })
        if passing:
            on_event({
                "type": "result", "repair_id": repair_id, "file": relative,
                "line": line, "tier": tier, "candidates": passing,
                "message": f"{len(passing)} patch(es) passed all tests at the {tier} tier.",
            })
            return passing
        on_event({
            "type": "progress", "tier": tier, "tier_number": tier_number,
            "message": f"No {tier} patch passed; escalating to the next knowledge tier.",
        })
    on_event({
        "type": "result", "repair_id": repair_id, "file": relative, "line": line,
        "tier": "complete", "candidates": [],
        "message": "No patch passed the complete selected test command. No unsuccessful patches were retained.",
        "failures": failures[:8],
    })
    return []
