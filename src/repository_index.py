"""Repository exploration abstraction.

Backend selection:
- `CodeGraphBackend` (default) — subprocess `codegraph` CLI; structural queries
  (symbol, callers, callees, references, implementation, impact) are O(1)
  via the parsed AST graph rather than linear filesystem searches.
- `FilesystemBackend` (fallback) — grep/find when `codegraph` isn't on
  PATH or the project's `.codegraph/` index isn't built.

Policy: prefer CodeGraph when a structural question is asked. Only fall back
to filesystem for plain text search (which the brief notes is not in the
codegraph surface today).

The application code talks to `RepositoryExplorer` and never knows which
backend is active — substitution happens in `make_explorer()`.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("refine.repo_index")


# ---- public dataclass(es) ---------------------------------------------------


@dataclass(frozen=True)
class SymbolHit:
    name: str
    kind: str
    file: str
    line: int
    signature: str | None = None


@dataclass(frozen=True)
class ReferenceHit:
    file: str
    line: int
    symbol: str | None = None


# ---- backend contract -------------------------------------------------------


class ExplorerBackend(ABC):
    """Structural / textual exploration of a single project directory."""

    @abstractmethod
    def status(self, project_path: Path) -> dict[str, Any]: ...

    @abstractmethod
    def search_text(self, query: str, *, project_path: Path, top_k: int = 50) -> list[str]:
        """Plain text search. Returns `path:line:snippet` strings."""

    @abstractmethod
    def find_symbol(self, name: str, *, project_path: Path, kind: str | None = None) -> list[SymbolHit]: ...

    @abstractmethod
    def find_callers(self, symbol: str, *, project_path: Path) -> list[ReferenceHit]: ...

    @abstractmethod
    def find_callees(self, symbol: str, *, project_path: Path) -> list[ReferenceHit]: ...

    @abstractmethod
    def find_references(self, name: str, *, project_path: Path) -> list[ReferenceHit]: ...

    @abstractmethod
    def find_implementations(self, name: str, *, project_path: Path) -> list[SymbolHit]: ...

    @abstractmethod
    def impact_analysis(self, symbol: str, *, project_path: Path, depth: int = 2) -> dict[str, Any]: ...


# ---- CodeGraph backend ------------------------------------------------------


class CodeGraphBackend(ExplorerBackend):
    """Subprocess wrapper around the `codegraph` CLI.

    `codegraph` is a tree-sitter-parsed AST index per project. It answers
    structural queries (callers/callees/impact) in O(1) instead of O(files).
    """

    def __init__(self, *, cli: str | None = None) -> None:
        self._cli = cli or shutil.which("codegraph")
        if not self._cli:
            raise FileNotFoundError("codegraph CLI not on PATH")

    def status(self, project_path: Path) -> dict[str, Any]:
        try:
            out = self._run(["status"], project_path, as_json=False)
            return {"backend": "codegraph", "ok": True, "raw": out}
        except subprocess.CalledProcessError as e:
            return {"backend": "codegraph", "ok": False, "error": str(e)}

    def search_text(self, query: str, *, project_path: Path, top_k: int = 50) -> list[str]:
        # CodeGraph focuses on structural queries. For literal text search we
        # delegate to a filesystem grep and tell the caller why in the log.
        log.debug("repo_index: codegraph has no native text search, falling back to grep")
        try:
            out = subprocess.run(
                ["grep", "-RHn", "--", query, "."],
                cwd=str(project_path),
                capture_output=True, text=True, timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("text search via grep failed: %s", e)
            return []
        return out.stdout.splitlines()[:top_k]

    def find_symbol(self, name: str, *, project_path: Path, kind: str | None = None) -> list[SymbolHit]:
        args = ["query", name, "-l", "20"]
        if kind:
            args += ["-k", kind]
        nodes = self._run(args, project_path)
        return [_hit_from_node(n["node"]) for n in nodes]

    def find_callers(self, symbol: str, *, project_path: Path) -> list[ReferenceHit]:
        rows = self._run(["callers", symbol, "-l", "50"], project_path)
        return [_hit_from_row(r) for r in rows]

    def find_callees(self, symbol: str, *, project_path: Path) -> list[ReferenceHit]:
        rows = self._run(["callees", symbol, "-l", "50"], project_path)
        return [_hit_from_row(r) for r in rows]

    def find_references(self, name: str, *, project_path: Path) -> list[ReferenceHit]:
        # CodeGraph groups callers+references under "callers" since direct
        # identifier references show up as call sites and type references.
        return self.find_callers(name, project_path=project_path)

    def find_implementations(self, name: str, *, project_path: Path) -> list[SymbolHit]:
        # No dedicated "implementations" subcommand; reuse symbol search.
        return self.find_symbol(name, project_path=project_path, kind="class")

    def impact_analysis(self, symbol: str, *, project_path: Path, depth: int = 2) -> dict[str, Any]:
        return self._run(["impact", symbol, "-d", str(depth)], project_path)

    # ---- helpers ----------------------------------------------------------

    def _run(self, args: list[str], project_path: Path, *, as_json: bool = True, timeout: int = 30) -> Any:
        cmd = [self._cli, *args, "-p", str(project_path)]
        if as_json:
            cmd += ["-j"]
        log.debug("repo_index: %s", cmd)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr)
        if not as_json:
            return proc.stdout
        # CodeGraph may emit a non-JSON preamble (e.g. progress lines). Scan
        # for the first '{' / '[' that begins a JSON value, then decode.
        text = proc.stdout.strip()
        if not text:
            return {}
        start = next((i for i, c in enumerate(text) if c in "[{"), -1)
        try:
            return json.loads(text[start:] if start >= 0 else text)
        except json.JSONDecodeError:
            log.warning("codegraph returned non-JSON output; passing raw")
            return {}


def _hit_from_node(node: dict) -> SymbolHit:
    return SymbolHit(
        name=node.get("qualifiedName") or node.get("name", "?"),
        kind=node.get("kind", "?"),
        file=node.get("filePath", ""),
        line=int(node.get("startLine", 0) or 0),
        signature=node.get("signature"),
    )


def _hit_from_row(row: dict) -> ReferenceHit:
    """`codegraph callers/callees` rows differ slightly across versions.

    Accept any of:
      {"caller": {...}} / {"callee": {...}} (v0.x)
      {"node": {...}} (alt form)
      the row itself (defensive)
    """
    payload = (
        row.get("caller")
        or row.get("callee")
        or row.get("node")
        or row
    )
    return ReferenceHit(
        file=payload.get("filePath", ""),
        line=int(payload.get("startLine", 0) or 0),
        symbol=payload.get("qualifiedName") or payload.get("name"),
    )


# ---- Filesystem fallback ----------------------------------------------------


class FilesystemBackend(ExplorerBackend):
    """Pure-filesystem grep-based implementation.

    Only used when CodeGraph is unavailable. The brief mandates minimising
    filesystem traversal, so callers should not reach for this backend unless
    CodeGraph isn't installed.
    """

    def __init__(self, *, top_k: int = 50) -> None:
        self._top_k = top_k

    def status(self, project_path: Path) -> dict[str, Any]:
        return {"backend": "filesystem", "ok": project_path.exists()}

    def search_text(self, query: str, *, project_path: Path, top_k: int = 50) -> list[str]:
        try:
            proc = subprocess.run(
                ["grep", "-RHn", "--", query, "."],
                cwd=str(project_path),
                capture_output=True, text=True, timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError):
            return []
        return proc.stdout.splitlines()[: min(top_k, self._top_k)]

    def find_symbol(self, name: str, *, project_path: Path, kind: str | None = None) -> list[SymbolHit]:
        # Filesystem can only grep for the literal name. No AST kinds.
        hits: list[SymbolHit] = []
        for line in self.search_text(f"\\b{name}\\b", project_path=project_path):
            try:
                path, ln, _ = line.split(":", 2)
            except ValueError:
                continue
            target = project_path / path
            hits.append(SymbolHit(name=name, kind=kind or "?", file=str(target), line=int(ln)))
        return hits

    def find_callers(self, symbol: str, *, project_path: Path) -> list[ReferenceHit]:
        # Approximation: callers ≈ references (with word-boundary search).
        return self._references(symbol, project_path)

    def find_callees(self, symbol: str, *, project_path: Path) -> list[ReferenceHit]:
        # Filesystem cannot resolve the call graph. Return empty; structural
        # callers should fall back to CodeGraph instead.
        return []

    def find_references(self, name: str, *, project_path: Path) -> list[ReferenceHit]:
        return self._references(name, project_path)

    def find_implementations(self, name: str, *, project_path: Path) -> list[SymbolHit]:
        return []  # filesystem can't infer inheritance

    def impact_analysis(self, symbol: str, *, project_path: Path, depth: int = 2) -> dict[str, Any]:
        return {
            "depth": depth,
            "approximate": True,
            "note": "FilesystemBackend cannot resolve an AST impact graph. Use CodeGraphBackend.",
            "callers": [r.__dict__ for r in self._references(symbol, project_path)],
        }

    def _references(self, name: str, project_path: Path) -> list[ReferenceHit]:
        out: list[ReferenceHit] = []
        for line in self.search_text(f"\\b{name}\\b", project_path=project_path):
            try:
                path, ln, _ = line.split(":", 2)
            except ValueError:
                continue
            out.append(ReferenceHit(file=str(project_path / path), line=int(ln), symbol=name))
        return out


# ---- facade -----------------------------------------------------------------


class RepositoryExplorer:
    """Thin facade in front of a backend. Single instance per project."""

    def __init__(self, backend: ExplorerBackend, *, project_path: Path) -> None:
        self._backend = backend
        self._project_path = project_path

    @property
    def backend_name(self) -> str:
        return type(self._backend).__name__

    @property
    def project_path(self) -> Path:
        return self._project_path

    def status(self) -> dict[str, Any]:
        return self._backend.status(self._project_path)

    def search_text(self, query: str, *, top_k: int = 50) -> list[str]:
        return self._backend.search_text(query, project_path=self._project_path, top_k=top_k)

    def find_symbol(self, name: str, *, kind: str | None = None) -> list[SymbolHit]:
        return self._backend.find_symbol(name, project_path=self._project_path, kind=kind)

    def find_callers(self, symbol: str) -> list[ReferenceHit]:
        return self._backend.find_callers(symbol, project_path=self._project_path)

    def find_callees(self, symbol: str) -> list[ReferenceHit]:
        return self._backend.find_callees(symbol, project_path=self._project_path)

    def find_references(self, name: str) -> list[ReferenceHit]:
        return self._backend.find_references(name, project_path=self._project_path)

    def find_implementations(self, name: str) -> list[SymbolHit]:
        return self._backend.find_implementations(name, project_path=self._project_path)

    def impact_analysis(self, symbol: str, *, depth: int = 2) -> dict[str, Any]:
        return self._backend.impact_analysis(symbol, project_path=self._project_path, depth=depth)


def make_explorer(
    *, project_path: Path, force_backend: str | None = None, cli: str | None = None,
) -> RepositoryExplorer:
    """Pick the best backend available. `force_backend` is for tests.

    `codegraph` CLI on PATH → CodeGraphBackend.
    otherwise → FilesystemBackend.
    """
    if force_backend == "codegraph":
        return RepositoryExplorer(CodeGraphBackend(cli=cli), project_path=project_path)
    if force_backend == "filesystem":
        return RepositoryExplorer(FilesystemBackend(), project_path=project_path)
    try:
        backend: ExplorerBackend = CodeGraphBackend(cli=cli)
    except FileNotFoundError:
        backend = FilesystemBackend()
    return RepositoryExplorer(backend, project_path=project_path)
