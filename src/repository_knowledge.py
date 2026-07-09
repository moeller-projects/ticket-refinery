"""Repository knowledge abstraction.

Backend selection:
- `GraphifyBackend` (default) — subprocess `graphify` CLI; structural queries
  (callers/callees/impact) come from a parsed AST graph stored at
  `<project>/graphify-out/graph.json` after `graphify extract --code-only`.
- `FilesystemBackend` (fallback) — grep/find when `graphify` is unavailable.
  Cannot resolve call graphs; curated ops return degraded markers.

The application talks to `RepositoryKnowledge` and never knows which backend
is active — substitution happens in `make_knowledge()`.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("refine.repo_knowledge")


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


@dataclass(frozen=True)
class DependencyNode:
    """One node in a dependency graph."""

    path: str
    language: str | None = None
    kind: str | None = None
    label: str | None = None


@dataclass(frozen=True)
class DependencyEdge:
    """Directed edge: `source` references or depends on `target`."""

    source: str
    target: str
    kind: str = "references"


@dataclass(frozen=True)
class DependencyGraph:
    nodes: tuple[DependencyNode, ...] = ()
    edges: tuple[DependencyEdge, ...] = ()
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [{"path": n.path, "language": n.language, "kind": n.kind,
                       "label": n.label} for n in self.nodes],
            "edges": [{"source": e.source, "target": e.target, "kind": e.kind}
                      for e in self.edges],
            "degraded": self.degraded,
        }


@dataclass(frozen=True)
class ArchitectureSummary:
    text: str
    modules: tuple[str, ...] = ()
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "modules": list(self.modules), "degraded": self.degraded}


@dataclass(frozen=True)
class ExecutionPath:
    symbol: str
    path: tuple[ReferenceHit, ...] = ()
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "path": [{"file": r.file, "line": r.line, "symbol": r.symbol} for r in self.path],
            "degraded": self.degraded,
        }


@dataclass(frozen=True)
class RelevantFiles:
    query: str
    files: tuple[str, ...] = ()
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"query": self.query, "files": list(self.files), "degraded": self.degraded}


# ---- backend contract -------------------------------------------------------


class KnowledgeBackend(ABC):
    """Structural / textual exploration of a single project directory."""

    @abstractmethod
    def status(self, project_path: Path) -> dict[str, Any]: ...

    @abstractmethod
    def search_text(self, query: str, *, project_path: Path, top_k: int = 50) -> list[str]: ...

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

    # Concrete defaults return degraded markers; rich backends override.
    def architecture_summary(self, *, project_path: Path) -> ArchitectureSummary:
        return ArchitectureSummary(text="Architecture summary unavailable in this backend.",
                                    modules=(), degraded=True)

    def dependency_graph(self, *, project_path: Path) -> DependencyGraph:
        return DependencyGraph(degraded=True)

    def execution_path(self, symbol: str, *, project_path: Path) -> ExecutionPath:
        return ExecutionPath(symbol=symbol, degraded=True)

    def relevant_files(self, query: str, *, project_path: Path, top_k: int = 20) -> RelevantFiles:
        return RelevantFiles(query=query, degraded=True)


# ---- Graphify backend -------------------------------------------------------


# ponytail: graphify writes its parsed AST graph here. The default name is
# relative — calling graphify subcommands with --graph resolves it under
# $PWD; we always pass an absolute path so the backend works regardless of
# the subprocess cwd.
_GRAPH_DIR_DEFAULT = "graphify-out"
_GRAPH_FILE = "graph.json"

_NODE_PATH_KEYS = ("source_file", "file", "path")


class _MiniGraph:
    """Tiny in-memory graph for `graph.json` data.

    ponytail: avoids pulling networkx into the host image. graphify already
    vendors networkx for its own use; reimplementing BFS / neighbour lookup
    against a small dict-graph is <100 lines and matches the small subset
    of networkx API we actually touch.

    Stores edge data keyed by (u, v) tuples. Directed.
    """

    __slots__ = ("_nodes", "_in", "_out", "_edges")

    def __init__(self) -> None:
        self._nodes: dict[str, dict] = {}
        self._in: dict[str, set[str]] = {}
        self._out: dict[str, set[str]] = {}
        self._edges: dict[tuple[str, str], dict] = {}

    def add_node(self, nid: str, attrs: dict | None = None) -> None:
        if nid not in self._nodes:
            self._nodes[nid] = attrs or {}
            self._in[nid] = set()
            self._out[nid] = set()

    def add_edge(self, u: str, v: str, attrs: dict | None = None) -> None:
        self.add_node(u)
        self.add_node(v)
        self._out[u].add(v)
        self._in[v].add(u)
        self._edges[(u, v)] = attrs or {}

    @property
    def nodes(self) -> dict[str, dict]:
        return self._nodes

    def successors(self, nid: str) -> set[str]:
        return self._out.get(nid, set())

    def predecessors(self, nid: str) -> set[str]:
        return self._in.get(nid, set())

    def out_edges(self, nid: str) -> list[tuple[str, str, dict]]:
        return [(nid, v, self._edges[(nid, v)]) for v in self._out.get(nid, set())]

    def in_edges(self, nid: str) -> list[tuple[str, str, dict]]:
        return [(u, nid, self._edges[(u, nid)]) for u in self._in.get(nid, set())]


def _parse_graph_json(raw: dict) -> tuple[_MiniGraph, dict[str, dict]]:
    """Parse a node_link_graph JSON payload (edges-key or links-key) into
    a `_MiniGraph` plus the raw node attributes.
    """
    g = _MiniGraph()
    nodes_raw = raw.get("nodes", {})
    edges_raw = raw.get("links") or raw.get("edges") or []
    nodes: dict[str, dict] = {}
    if isinstance(nodes_raw, dict):
        for nid, attrs in nodes_raw.items():
            nid_s = str(nid)
            attrs_dict = dict(attrs) if isinstance(attrs, dict) else {}
            g.add_node(nid_s, attrs_dict)
            nodes[nid_s] = attrs_dict
    elif isinstance(nodes_raw, list):
        for entry in nodes_raw:
            if isinstance(entry, dict):
                nid = str(entry.get("id"))
                attrs_dict = dict(entry)
            else:
                nid = str(entry)
                attrs_dict = {}
            g.add_node(nid, attrs_dict)
            nodes[nid] = attrs_dict
    for edge in edges_raw:
        if not isinstance(edge, dict):
            continue
        u = str(edge.get("source"))
        v = str(edge.get("target"))
        if not u or not v:
            continue
        edata = {k: edge[k] for k in edge if k not in ("source", "target")}
        if not g._nodes.get(u):
            g.add_node(u)
        if not g._nodes.get(v):
            g.add_node(v)
        g.add_edge(u, v, edata)
    return g, nodes


class GraphifyBackend(KnowledgeBackend):
    """Subprocess wrapper around the `graphify` CLI.

    graphify produces a parsed AST graph at `<project>/graphify-out/graph.json`
    via `graphify extract --code-only`. The backend reads that file directly
    for structural queries (no per-query CLI invocation needed); curated ops
    use BFS over the in-memory graph for rich traversals.

    First call to any operation triggers a build if the graph is missing;
    subsequent calls reuse the cached graph. The build is fast (AST only,
    no LLM) and safe to run on already-checked-out repos.
    """

    def __init__(self, *, cli: str | None = None, graph_dir: str = _GRAPH_DIR_DEFAULT) -> None:
        self._cli = cli or shutil.which("graphify")
        if not self._cli:
            raise FileNotFoundError("graphify CLI not on PATH")
        self._graph_dir = graph_dir

    def status(self, project_path: Path) -> dict[str, Any]:
        graph_path = self._graph_path(project_path)
        if graph_path.exists():
            return {"backend": "graphify", "ok": True, "graph": str(graph_path)}
        try:
            self._ensure_graph(project_path)
            return {"backend": "graphify", "ok": True, "graph": str(graph_path)}
        except subprocess.CalledProcessError as e:
            return {"backend": "graphify", "ok": False, "error": str(e)}

    def search_text(self, query: str, *, project_path: Path, top_k: int = 50) -> list[str]:
        log.debug("repo_knowledge: graphify has no native text search, falling back to grep")
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
        graph = self._load_graph(project_path)
        if graph is None:
            return []
        out: list[SymbolHit] = []
        target = name.lower()
        for nid, data in graph["nodes"].items():
            label = str(data.get("label") or nid).lower()
            if target not in label:
                continue
            if kind and str(data.get("kind") or "").lower() != kind.lower():
                continue
            out.append(SymbolHit(
                name=str(data.get("label") or nid),
                kind=str(data.get("kind") or "?"),
                file=_node_file(data) or "",
                line=int(data.get("start_line") or 0),
                signature=_node_signature(data),
            ))
            if len(out) >= 50:
                break
        return out

    def find_callers(self, symbol: str, *, project_path: Path) -> list[ReferenceHit]:
        return self._neighbors(symbol, project_path, direction="in")

    def find_callees(self, symbol: str, *, project_path: Path) -> list[ReferenceHit]:
        return self._neighbors(symbol, project_path, direction="out")

    def find_references(self, name: str, *, project_path: Path) -> list[ReferenceHit]:
        return self._neighbors(name, project_path, direction="in")

    def find_implementations(self, name: str, *, project_path: Path) -> list[SymbolHit]:
        graph = self._load_graph(project_path)
        if graph is None:
            return []
        out: list[SymbolHit] = []
        target = name.lower()
        for nid, data in graph["nodes"].items():
            if str(data.get("kind") or "").lower() != "class":
                continue
            label = str(data.get("label") or nid)
            base = _base_class_name(label)
            if base.lower() == target:
                out.append(SymbolHit(
                    name=label, kind="class",
                    file=_node_file(data) or "", line=int(data.get("start_line") or 0),
                    signature=_node_signature(data),
                ))
        return out[:50]

    def impact_analysis(self, symbol: str, *, project_path: Path, depth: int = 2) -> dict[str, Any]:
        graph = self._load_graph(project_path)
        if graph is None:
            return {"symbol": symbol, "callers": [], "depth": depth, "approximate": True}
        seed = self._match_node(graph, symbol)
        if seed is None:
            return {"symbol": symbol, "callers": [], "depth": depth, "approximate": True}
        visited, queue, callers = {seed}, [(seed, 0)], []
        while queue:
            nid, d = queue.pop(0)
            if d >= depth:
                continue
            for src in graph["graph"].predecessors(nid):
                if src in visited:
                    continue
                visited.add(src)
                edata = graph["graph_edges"].get((src, nid), {})
                callers.append(_make_reference(src, edata, graph, source=symbol))
                if d + 1 < depth:
                    queue.append((src, d + 1))
        return {
            "symbol": symbol,
            "resolved": str(graph["nodes"][seed].get("label", seed)),
            "depth": depth,
            "callers": [c.__dict__ for c in callers],
        }

    # ponytail: file suffixes that count as real source code. Project
    # files (.sln, .csproj, .ps1, .sh, ...) and configs are noise — the
    # curated prompt must not regurgitate them. Keep this list short and
    # obvious; the curated primer only needs CODE for orientation.
    _SOURCE_FILE_SUFFIXES = frozenset({
        ".cs", ".java", ".kt", ".scala",
        ".py", ".rb", ".go", ".rs",
        ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
        ".c", ".cpp", ".cc", ".h", ".hpp", ".m", ".mm",
        ".swift", ".php",
        ".vue", ".svelte", ".astro",
        ".dart", ".lua", ".pl", ".r",
    })

    def architecture_summary(self, *, project_path: Path) -> ArchitectureSummary:
        graph = self._load_graph(project_path)
        if graph is None:
            return ArchitectureSummary(text="(no architecture summary available)", degraded=True)
        # ponytail: filter to actual source files. .sln, .csproj, .ps1 etc.
        # are project-level artefacts, not code. The curated primer must
        # not regurgitate them.
        source_files: list[str] = []
        for data in graph["nodes"].values():
            f = _node_file(data)
            if f and Path(f).suffix.lower() in self._SOURCE_FILE_SUFFIXES:
                source_files.append(f)
        if not source_files:
            return ArchitectureSummary(text="(no source files in graph)", modules=(),
                                        degraded=True)
        # Group by the meaningful module segment. Source files in a
        # multi-module repo typically live at
        # `repo/<module>/<rest>/File.cs`; the meaningful group is `<module>`
        # (= the second segment from the repo root or the workspace name).
        groups: dict[str, list[str]] = {}
        for f in source_files:
            parts = Path(f).parts
            if len(parts) >= 3 and parts[0].startswith("__r="):
                # Merged workspace node id (`__r=RepoName__/<rest>`).
                # parts: ["__r=RepoName__", "<rest>[0]", "<rest>[1]", ...]
                top = parts[2]
            elif len(parts) >= 3:
                # Repo-rooted absolute path:
                # /tmp/refine-XX/RepoName/modules/<module>/<rest>/File.cs
                # parts[-4] is the module if it sits at the canonical depth.
                # Use the second-from-last directory for narrower grouping;
                # fall back to the immediate parent.
                top = parts[-2]
            else:
                top = parts[-2] if len(parts) >= 2 else "."
            groups.setdefault(top, []).append(f)
        # Top 6 modules by file count; ignore single-file "modules" that
        # are really edge cases.
        modules = [m for m in sorted(groups, key=lambda m: -len(groups[m]))[:6]
                   if len(groups[m]) >= 2]
        lines = [f"{len(source_files)} source files across {len(groups)} modules."]
        for m in modules:
            sample = sorted(groups[m])[:2]
            names = [Path(p).name for p in sample]
            lines.append(f"- `{m}/` ({len(groups[m])} files) — e.g. " +
                         ", ".join(f"`{n}`" for n in names))
        if not modules:
            lines.append("(no module has more than one source file)")
        return ArchitectureSummary(text="\n".join(lines), modules=tuple(modules), degraded=False)

    def dependency_graph(self, *, project_path: Path) -> DependencyGraph:
        graph = self._load_graph(project_path)
        if graph is None:
            return DependencyGraph(degraded=True)
        file_to_nodes: dict[str, set[str]] = {}
        edges_by_file: list[DependencyEdge] = []
        seen_edges: set[tuple[str, str, str]] = set()
        for nid, data in graph["nodes"].items():
            for u, v, edata in graph["graph"].out_edges(nid):
                fu = _node_file(graph["nodes"].get(u, {})) or ""
                fv = _node_file(graph["nodes"].get(v, {})) or ""
                if not fu or not fv or fu == fv:
                    continue
                kind = str(edata.get("relation") or "references")
                key = (fu, fv, kind)
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                file_to_nodes.setdefault(fu, set()).add(u)
                file_to_nodes.setdefault(fv, set()).add(v)
                edges_by_file.append(DependencyEdge(source=fu, target=fv, kind=kind))
        nodes = tuple(
            DependencyNode(path=f, language="python", kind="file", label=f)
            for f in sorted(file_to_nodes)
        )
        return DependencyGraph(
            nodes=nodes[:200], edges=tuple(edges_by_file[:500]), degraded=False,
        )

    def execution_path(self, symbol: str, *, project_path: Path) -> ExecutionPath:
        graph = self._load_graph(project_path)
        if graph is None:
            return ExecutionPath(symbol=symbol, degraded=True)
        seed = self._match_node(graph, symbol)
        if seed is None:
            return ExecutionPath(symbol=symbol, degraded=True)
        hops: list[ReferenceHit] = []
        for _, target, edata in graph["graph"].out_edges(seed):
            hops.append(_make_reference(target, edata, graph, source=symbol))
        return ExecutionPath(symbol=symbol, path=tuple(hops[:20]), degraded=False)

    def relevant_files(self, query: str, *, project_path: Path, top_k: int = 20) -> RelevantFiles:
        graph = self._load_graph(project_path)
        if not query or graph is None:
            return RelevantFiles(query=query, degraded=True)
        terms = _query_terms(query)
        scored: list[tuple[int, str]] = []
        for nid, data in graph["nodes"].items():
            label = str(data.get("norm_label") or data.get("label") or "").lower()
            if not label:
                continue
            score = sum(1 for t in terms if t in label)
            if not score:
                continue
            scored.append((score, _node_file(data) or ""))
        scored.sort(key=lambda x: -x[0])
        seen: set[str] = set()
        files: list[str] = []
        for _, f in scored:
            if not f or f in seen:
                continue
            seen.add(f)
            files.append(f)
            if len(files) >= top_k:
                break
        return RelevantFiles(query=query, files=tuple(files), degraded=False)

    # ---- graph lifecycle -------------------------------------------------

    def _ensure_graph(self, project_path: Path) -> None:
        """Build the graph via `graphify extract --code-only` (no LLM)."""
        cmd = [self._cli, "extract", str(project_path),
               "--out", str(project_path), "--code-only", "--no-cluster"]
        log.debug("repo_knowledge: %s", cmd)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr,
            )

    def _graph_path(self, project_path: Path) -> Path:
        return project_path / self._graph_dir / _GRAPH_FILE

    def graph_path(self, project_path: Path) -> Path:
        """Where `graphify extract` writes the index. Public so the
        orchestrator can mention the path to the agent without knowing the
        file naming convention.
        """
        return self._graph_path(project_path)

    # ponytail: a project_path may be either a single repo OR a workspace
    # dir containing one or more cloned repos as subdirs (each with their
    # own `graphify-out/graph.json`). When it's the latter, look in each
    # subdir; the first one with a graph.json wins, the rest merge in.
    # We don't re-extract — the workspace step already did that.
    def _discover_repo_graphs(self, project_path: Path) -> list[tuple[Path, Path]]:
        out: list[tuple[Path, Path]] = []
        if not project_path.exists() or not project_path.is_dir():
            return out
        # ponytail: graphify extract on a symlink-empty workspace produces
        # an empty graph (extract doesn't follow symlinks by default). To
        # avoid that, scan each direct subdir for an extracted graph.
        try:
            entries = list(project_path.iterdir())
        except OSError:
            return out
        # First try: graph.json directly under project_path (single-repo).
        direct = self._graph_path(project_path)
        if direct.exists():
            out.append((project_path, direct))
        # Second try: each subdir with its own graph.json (workspace layout).
        for sub in sorted(entries):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            candidate = sub / self._graph_dir / _GRAPH_FILE
            if candidate.exists():
                out.append((sub, candidate))
        return out

    def _load_graph(self, project_path: Path) -> dict | None:
        """Load the parsed AST graph(s).

        Supports two shapes:

        1. `project_path` is a single repo: graph.json lives at
           `project_path/graphify-out/graph.json`.
        2. `project_path` is a workspace dir containing cloned-repo
           subdirs, each with its own `graph.json` (the typical runtime
           case). We merge all of them — node IDs are path-derived so we
           prefix them per-repo to avoid collisions.

        If nothing exists, we don't try to re-extract: extract is run
        per-repo by the workspace step. The orchestrator sees only the
        parser output, never filesystem layout.
        """
        candidates = self._discover_repo_graphs(project_path)
        if not candidates:
            return None
        return self._merge_repo_graphs(candidates)

    def _merge_repo_graphs(self, candidates: list[tuple[Path, Path]]) -> dict | None:
        g_merged = _MiniGraph()
        nodes_merged: dict[str, dict] = {}
        edges_data: dict[tuple[str, str], dict] = {}
        for repo_root, graph_file in candidates:
            try:
                raw = json.loads(graph_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                log.warning("could not parse graph at %s: %s", graph_file, e)
                continue
            try:
                g, nodes = _parse_graph_json(raw)
            except Exception as e:
                log.warning("graph parse failed for %s: %s", graph_file, e)
                continue
            # ponytail: namespace node IDs by repo so two repos with the
            # same file basename (e.g., `Program.cs`) don't collide.
            repo_prefix = f"__r={repo_root.name}__/"
            for nid, attrs in g.nodes.items():
                new_id = repo_prefix + nid
                if new_id in g_merged.nodes:
                    continue
                new_attrs = dict(attrs)
                # Re-root source_file so downstream file references are
                # resolvable against the actual repo path.
                sf = new_attrs.get("source_file") or _node_file(new_attrs)
                if sf:
                    new_attrs["source_file"] = str(repo_root / sf)
                g_merged.add_node(new_id, new_attrs)
                nodes_merged[new_id] = new_attrs
            for nid in g.nodes:
                for u, v, edata in g.out_edges(nid):
                    new_u = repo_prefix + u
                    new_v = repo_prefix + v
                    if new_u not in nodes_merged or new_v not in nodes_merged:
                        continue
                    if (new_u, new_v) in edges_data:
                        continue
                    g_merged.add_edge(new_u, new_v, dict(edata))
                    edges_data[(new_u, new_v)] = dict(edata)
        if not nodes_merged:
            return None
        return {"graph": g_merged, "graph_edges": edges_data, "nodes": nodes_merged}

    def _match_node(self, graph: dict, label: str) -> str | None:
        """Resolve a label to one node id. Prefers exact, then prefix, then substring."""
        target = label.lower()
        prefix, substring = None, None
        for nid, data in graph["nodes"].items():
            lbl = str(data.get("label") or nid).lower()
            if lbl == target:
                return nid
            if prefix is None and lbl.startswith(target):
                prefix = nid
            if substring is None and target in lbl:
                substring = nid
        return prefix or substring

    def _neighbors(self, symbol: str, project_path: Path, *, direction: str) -> list[ReferenceHit]:
        graph = self._load_graph(project_path)
        if graph is None:
            return []
        seed = self._match_node(graph, symbol)
        if seed is None:
            return []
        edges = (graph["graph"].out_edges(seed) if direction == "out"
                 else graph["graph"].in_edges(seed))
        out: list[ReferenceHit] = []
        for u, v, edata in edges:
            other = v if direction == "out" else u
            out.append(_make_reference(other, edata, graph))
        return out[:100]


# ---- graph helpers ----------------------------------------------------------


def _query_terms(query: str) -> list[str]:
    return [t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]+", query) if len(t) >= 3]


def _node_file(data: dict) -> str | None:
    for key in _NODE_PATH_KEYS:
        val = data.get(key)
        if val:
            return str(val)
    return None


def _node_signature(data: dict) -> str | None:
    sig = data.get("signature")
    return str(sig) if sig else None


def _base_class_name(label: str) -> str:
    return label.split("(", 1)[0].strip()


def _make_reference(nid: str, edata: dict, graph: dict, *, source: str | None = None) -> ReferenceHit:
    data = graph["nodes"].get(nid, {})
    label = data.get("label") or nid
    file_path = _node_file(data) or ""
    line = int(edata.get("source_line") or edata.get("line")
                or data.get("start_line") or data.get("line") or 0)
    sym = source if source else str(label)
    return ReferenceHit(file=file_path, line=line, symbol=str(sym))


# ---- Filesystem fallback ----------------------------------------------------


class FilesystemBackend(KnowledgeBackend):
    """Pure-filesystem grep-based implementation.

    Used when Graphify is unavailable. Cannot resolve call graphs or AST
    structure; curated operations return degraded markers and `relevant_files`
    falls back to a literal text search so callers still get *something*.
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
        return self._references(symbol, project_path)

    def find_callees(self, symbol: str, *, project_path: Path) -> list[ReferenceHit]:
        return []

    def find_references(self, name: str, *, project_path: Path) -> list[ReferenceHit]:
        return self._references(name, project_path)

    def find_implementations(self, name: str, *, project_path: Path) -> list[SymbolHit]:
        return []

    def impact_analysis(self, symbol: str, *, project_path: Path, depth: int = 2) -> dict[str, Any]:
        return {
            "depth": depth,
            "approximate": True,
            "note": "FilesystemBackend cannot resolve an AST impact graph. Use GraphifyBackend.",
            "callers": [r.__dict__ for r in self._references(symbol, project_path)],
        }

    def relevant_files(self, query: str, *, project_path: Path, top_k: int = 20) -> RelevantFiles:
        files: list[str] = []
        seen: set[str] = set()
        for line in self.search_text(query, project_path=project_path, top_k=top_k):
            try:
                path, _, _ = line.split(":", 2)
            except ValueError:
                continue
            if path in seen:
                continue
            seen.add(path)
            files.append(str(project_path / path))
        return RelevantFiles(query=query, files=tuple(files[:top_k]), degraded=True)

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


class RepositoryKnowledge:
    """Thin facade in front of a backend. One instance per project."""

    def __init__(self, backend: KnowledgeBackend, *, project_path: Path) -> None:
        self._backend = backend
        self._project_path = project_path

    @property
    def backend_name(self) -> str:
        return type(self._backend).__name__

    @property
    def project_path(self) -> Path:
        return self._project_path

    @property
    def backend(self) -> KnowledgeBackend:
        return self._backend

    def status(self) -> dict[str, Any]:
        return self._backend.status(self._project_path)

    def search(self, query: str, *, top_k: int = 50) -> list[str]:
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

    def architecture_summary(self) -> ArchitectureSummary:
        return self._backend.architecture_summary(project_path=self._project_path)

    def dependency_graph(self) -> DependencyGraph:
        return self._backend.dependency_graph(project_path=self._project_path)

    def execution_path(self, symbol: str) -> ExecutionPath:
        return self._backend.execution_path(symbol, project_path=self._project_path)

    def relevant_files(self, query: str, *, top_k: int = 20) -> RelevantFiles:
        return self._backend.relevant_files(query, project_path=self._project_path, top_k=top_k)


def make_knowledge(
    *, project_path: Path, force_backend: str | None = None, cli: str | None = None,
) -> RepositoryKnowledge:
    """Pick the best backend available. `force_backend` is for tests."""
    if force_backend == "graphify":
        return RepositoryKnowledge(GraphifyBackend(cli=cli), project_path=project_path)
    if force_backend == "filesystem":
        return RepositoryKnowledge(FilesystemBackend(), project_path=project_path)
    try:
        backend: KnowledgeBackend = GraphifyBackend(cli=cli)
    except FileNotFoundError:
        backend = FilesystemBackend()
    return RepositoryKnowledge(backend, project_path=project_path)


# ponytail: legacy aliases so old imports keep working during the transition.
RepositoryExplorer = RepositoryKnowledge
ExplorerBackend = KnowledgeBackend
CodeGraphBackend = GraphifyBackend  # type: ignore[misc, assignment]
