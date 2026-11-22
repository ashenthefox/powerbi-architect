#!/usr/bin/env python3
"""pbi_graph.py — Power BI knowledge graph for AI agents (part of the powerbi-architect skill).

Reads every PBIP project in a repository (TMDL model, PBIR report, SQL loads, Markdown docs,
Python tooling) and writes a persistent, queryable graph so an agent can answer questions
from a few hundred tokens instead of opening dozens of *.tmdl / visual.json files.

Python 3.9+ standard library only. Offline. Never reads data, only metadata.

    python pbi_graph.py build   [ROOT] [--out DIR] [--quiet]     full build
    python pbi_graph.py update  [ROOT] [--out DIR] [--quiet]     rebuild only if files changed
    python pbi_graph.py query   "<question>" [--budget 1500] [--dfs]
    python pbi_graph.py explain "<name>"                          one object, all its links
    python pbi_graph.py impact  "<name>"                          what depends on it (change safety)
    python pbi_graph.py path    "<from>" "<to>"                   shortest link between two objects
    python pbi_graph.py stats
    python pbi_graph.py install   [REPO] [--no-hooks] [--no-agents] [--commit-graph]
    python pbi_graph.py uninstall [REPO]

Outputs (default ROOT/powerbi-graph/):
    GRAPH_REPORT.md   one-page overview for the agent: models, hubs, communities, findings
    wiki/<model>.md   compact card per semantic model (replaces reading TMDL + PBIR)
    graph.json        nodes + edges (GraphRAG-ready)
    graph.html        interactive, self-contained view (no network)
    manifest.json     file fingerprints for `update`
"""

from __future__ import annotations

import ast
import collections
import hashlib
import html
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_pbip as ap  # noqa: E402  (shared TMDL parser)

VERSION = "1.1.0"
OUT_DIR = "powerbi-graph"
BUILD_ID = VERSION + "+" + hashlib.sha1(Path(__file__).read_bytes() +
                                        (Path(__file__).parent / "audit_pbip.py").read_bytes()).hexdigest()[:10]
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "dist", "site-packages", OUT_DIR}
TEXT_EXT = {".tmdl", ".json", ".pbir", ".pbism", ".sql", ".md", ".txt", ".toml", ".py", ".dax"}
MAX_BYTES = 3_000_000
MANIFESTS = ("trabalho-da-ia/ficha.toml", "ficha.toml", "dashboard.toml")   # Power BI Workspaces 2.x · 1.x
MARK_START = "<!-- powerbi-graph:start -->"
MARK_END = "<!-- powerbi-graph:end -->"
SH_START = "# powerbi-graph:start"
SH_END = "# powerbi-graph:end"
STOP = set("""a o os as um uma de da do das dos e em no na nos nas por para com que qual quais quem como onde
quando porque se ao aos the of and or to in on for with what which who how where when why is are does do
this that from by it be uses use usa usam usado usada calcula calculado medida medidas tabela tabelas coluna
colunas visual visuais pagina paginas relatorio modelo measure measures table tables column columns page pages
report model dashboard dashboards me mostre mostra show list liste explain explique""".split())


# ───────────────────────────────────────────────────────────────────────────── helpers

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def words(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9_%]+", norm(s)) if len(w) > 1]


def rel(p: Path, root: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


def read(p: Path) -> str:
    try:
        if p.stat().st_size > MAX_BYTES:
            return ""
        return p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def long_path(p: Path) -> Path:
    """Windows: opt into >260-char paths (deep analytics/ trees silently vanish from glob otherwise)."""
    p = p.resolve()
    prefix = "\\\\?\\"
    if os.name == "nt" and not str(p).startswith(prefix):
        return Path(prefix + str(p))
    return p


def tokens(chars: int) -> int:
    return max(1, chars // 4)


def walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        for f in sorted(filenames):
            yield Path(dirpath) / f


def strip_dax(expr: str) -> str:
    expr = re.sub(r"/\*.*?\*/", " ", expr, flags=re.S)
    expr = re.sub(r"(//|--)[^\n]*", " ", expr)
    return re.sub(r'"(?:[^"]|"")*"', '""', expr)


def strip_sql(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


SQL_OBJ = re.compile(r"\b(?:FROM|JOIN)\s+((?:[\[\"`]?[\w$#@]+[\]\"`]?\.){0,3}[\[\"`]?[\w$#@]+[\]\"`]?)", re.I)
SQL_CTE = re.compile(r"\b(\w+)\s+AS\s*\(", re.I)


def sql_sources(sql: str) -> list[str]:
    text = strip_sql(sql)
    ctes = {c.lower() for c in SQL_CTE.findall(text)}
    out = []
    for m in SQL_OBJ.finditer(text):
        name = re.sub(r"[\[\]\"`]", "", m.group(1)).lower()
        if name.split(".")[-1] in ctes or name in {"dual", "unnest", "lateral"}:
            continue
        if name not in out:
            out.append(name)
    return out


def m_native_sql(m: str) -> str:
    """Pull the SQL text out of Value.NativeQuery(..., "SELECT ...") in an M partition."""
    hit = re.search(r'Value\.NativeQuery\s*\(.*?,\s*"((?:[^"]|"")*)"', m, re.S)
    if not hit:
        hit = re.search(r'\[\s*Query\s*=\s*"((?:[^"]|"")*)"', m, re.S)
    if not hit:
        return ""
    return hit.group(1).replace('""', '"').replace("#(lf)", "\n").replace("#(cr)", "").replace("#(tab)", "\t")


def load_toml(text: str) -> dict:
    try:
        import tomllib  # Python 3.11+
        return tomllib.loads(text)
    except Exception:
        out: dict = {}
        sect = out
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if line.startswith("[") and line.endswith("]"):
                sect = out.setdefault(line.strip("[]").strip(), {})
            elif "=" in line:
                k, _, v = line.partition("=")
                sect[k.strip()] = v.strip().strip('"')
        return out


# ───────────────────────────────────────────────────────────────────────────── graph

class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._ekeys: set = set()

    def node(self, nid: str, kind: str, label: str, **props) -> str:
        if nid not in self.nodes:
            self.nodes[nid] = {"id": nid, "kind": kind, "label": label}
        n = self.nodes[nid]
        for k, v in props.items():
            if v not in (None, "", [], {}):
                n[k] = v
        return nid

    def edge(self, a: str, b: str, rel_: str, confidence: str = "EXTRACTED", **props) -> None:
        if a == b or a not in self.nodes or b not in self.nodes:
            return
        key = (a, b, rel_)
        if key in self._ekeys:
            return
        self._ekeys.add(key)
        e = {"source": a, "target": b, "rel": rel_, "confidence": confidence}
        e.update({k: v for k, v in props.items() if v not in (None, "")})
        self.edges.append(e)


CONTAINS = {"contains", "has_column", "has_measure", "has_page", "has_visual", "defines"}
DEPENDS = {"uses_measure", "uses_column", "uses_table", "sorted_by", "hierarchy_level", "relationship",
           "reads_source", "loaded_by", "uses_parameter", "binds_model", "filters", "imports"}


class Builder:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.g = Graph()
        self.corpus_chars = 0
        self.files: dict[str, list] = {}
        self.models: list[dict] = []          # per semantic model: lookup tables
        self.notes: list[str] = []

    # -- bookkeeping
    def track(self, p: Path, text: str | None = None) -> str:
        st = p.stat()
        self.files[rel(p, self.root)] = [st.st_mtime_ns, st.st_size]
        if text is not None:
            self.corpus_chars += len(text)
        return rel(p, self.root)

    def dashboard_for(self, p: Path) -> str | None:
        for parent in [p] + list(p.parents):
            if parent == self.root.parent:
                break
            if parent.name == "trabalho-da-ia":
                continue
            if any((parent / m).exists() for m in MANIFESTS):
                return self.dashboard_node(parent)
        return None

    def dashboard_node(self, folder: Path) -> str:
        nid = "dash:" + rel(folder, self.root)
        if nid in self.g.nodes:
            return nid
        f = next(folder / m for m in MANIFESTS if (folder / m).exists())
        text = read(f)
        self.track(f, text)
        meta = load_toml(text)
        d, o, dt, sec = (meta.get(k, {}) for k in ("dashboard", "ownership", "data", "security"))
        self.g.node(nid, "dashboard", d.get("name") or folder.name, file=rel(f, self.root),
                    status=d.get("status"), version=d.get("version"), description=d.get("description"),
                    owner=o.get("business_owner"), audience=o.get("audience"),
                    connection=dt.get("connection"), storage_mode=dt.get("storage_mode"),
                    refresh=dt.get("refresh"), classification=sec.get("classification"),
                    rls=sec.get("rls"))
        if dt.get("connection"):
            cid = self.g.node("conn:" + str(dt["connection"]), "connection", str(dt["connection"]))
            self.g.edge(nid, cid, "uses_connection")
        return nid

    # -- main
    def build(self) -> Graph:
        all_files = list(walk(self.root))
        sm_dirs = sorted({p.parent for p in all_files if p.suffix == ".pbism"} |
                         {p.parents[1] for p in all_files if p.parent.name == "definition"
                          and p.parent.parent.name.endswith(".SemanticModel")})
        for sm in sm_dirs:
            self.parse_model(sm)
        rep_dirs = sorted({p.parent for p in all_files if p.suffix == ".pbir"})
        for rp in rep_dirs:
            self.parse_report(rp)
        for p in all_files:
            if p.suffix.lower() == ".sql":
                self.parse_sql(p)
        for p in all_files:
            if p.suffix.lower() == ".md":
                self.parse_doc(p)
            elif p.suffix.lower() == ".py":
                self.parse_py(p)
        self.link_py_imports()
        self.label_untitled_visuals()
        return self.g

    def label_untitled_visuals(self) -> None:
        fields = collections.defaultdict(list)
        for e in self.g.edges:
            if e["rel"] in {"uses_measure", "uses_column"}:
                fields[e["source"]].append(self.g.nodes[e["target"]]["label"])
        for n in self.g.nodes.values():
            if n["kind"] == "visual" and not n.get("title") and fields[n["id"]]:
                f = fields[n["id"]]
                n["label"] = f"{n['visual_type']}: " + ", ".join(x if "[" in x else f"[{x}]" for x in f[:2]) + \
                    (" …" if len(f) > 2 else "")

    # -- TMDL model
    def parse_model(self, sm: Path) -> None:
        definition = sm / "definition"
        name = sm.name[: -len(".SemanticModel")] if sm.name.endswith(".SemanticModel") else sm.name
        mid = "model:" + rel(sm, self.root)
        dash = self.dashboard_for(sm)
        if not definition.is_dir():
            self.g.node(mid, "model", name, file=rel(sm, self.root), format="model.bim (not TMDL)")
            self.notes.append(f"{rel(sm, self.root)}: model.bim format — save as TMDL to graph it")
            if dash:
                self.g.edge(dash, mid, "contains")
            return
        model = ap.parse_model(definition)
        lines_index: dict[tuple, tuple] = {}
        for f in sorted(definition.rglob("*.tmdl")):
            text = read(f)
            fr = self.track(f, text)
            for i, line in enumerate(text.splitlines(), 1):
                m = ap.OBJ_RE.match(line.strip())
                if m and ap._tabs(line) <= 1:
                    lines_index.setdefault((m.group("kind"), ap.unquote(m.group("name"))), (fr, i))
        culture = ""
        mt = read(definition / "model.tmdl")
        cm = re.search(r"^\s*culture:\s*(\S+)", mt, re.M)
        if cm:
            culture = cm.group(1)
        self.g.node(mid, "model", name, file=rel(definition, self.root), culture=culture,
                    auto_datetime=model.annotations.get("__PBI_TimeIntelligenceEnabled", "0") != "0")
        if dash:
            self.g.edge(dash, mid, "contains")
        tables: dict[str, str] = {}
        cols: dict[tuple, str] = {}
        measures: dict[str, str] = {}
        for t in model.tables:
            loc = lines_index.get(("table", t.name), (rel(t.file, self.root), 1))
            tid = self.g.node(f"{mid}::{t.name}", "table", t.name, model=name, file=loc[0], line=loc[1],
                              description=t.description, hidden=t.is_hidden or None,
                              date_table=(t.data_category == "Time") or None)
            tables[norm(t.name)] = tid
            self.g.edge(mid, tid, "contains")
            for c in t.columns:
                cl = lines_index.get(("column", c.name), loc)
                cid = self.g.node(f"{mid}::{t.name}[{c.name}]", "column", f"{t.name}[{c.name}]", model=name,
                                  table=t.name, file=cl[0], line=cl[1], data_type=c.data_type,
                                  hidden=c.is_hidden or None, key=c.is_key or None,
                                  calculated=bool(c.expression) or None, expression=c.expression,
                                  description=c.description, format=c.format_string)
                cols[(norm(t.name), norm(c.name))] = cid
                self.g.edge(tid, cid, "has_column")
            for ms in t.measures:
                ml = lines_index.get(("measure", ms.name), loc)
                msid = self.g.node(f"{mid}::[{ms.name}]", "measure", ms.name, model=name, table=t.name,
                                   file=ml[0], line=ml[1], expression=ms.expression,
                                   description=ms.description, format=ms.format_string,
                                   folder=ms.display_folder, hidden=ms.is_hidden or None)
                measures[norm(ms.name)] = msid
                self.g.edge(tid, msid, "has_measure")
        ctx = {"mid": mid, "name": name, "tables": tables, "cols": cols, "measures": measures,
               "dash": dash, "folder": sm.parent, "model": model}
        self.models.append(ctx)
        params: dict[str, str] = {}
        for pname, value in model.expressions.items():
            pl = lines_index.get(("expression", pname), (rel(definition, self.root), 1))
            pid = self.g.node(f"{mid}::param:{pname}", "parameter", pname, model=name, file=pl[0], line=pl[1],
                              value=value.split(" meta ")[0][:120])
            params[pname] = pid
            self.g.edge(mid, pid, "defines")
        for t in model.tables:
            tid = tables[norm(t.name)]
            for c in t.columns:
                cid = cols[(norm(t.name), norm(c.name))]
                if c.sort_by:
                    tgt = cols.get((norm(t.name), norm(c.sort_by)))
                    if tgt:
                        self.g.edge(cid, tgt, "sorted_by")
                if c.expression:
                    self.dax_edges(ctx, cid, c.expression, t.name)
            for level in sorted(getattr(t, "level_columns", ())):
                tgt = cols.get((norm(t.name), norm(level)))
                if tgt:
                    self.g.edge(tid, tgt, "hierarchy_level")
            for ms in t.measures:
                self.dax_edges(ctx, measures[norm(ms.name)], ms.expression, t.name)
            for part in t.partitions:
                if part.kind == "calculated":
                    self.dax_edges(ctx, tid, part.source, t.name)
                    self.g.nodes[tid]["calculated"] = True
                elif part.kind == "m":
                    self.g.nodes[tid]["mode"] = part.mode or "import"
                    for pname, pid in params.items():
                        if re.search(rf"(?<![\w\"]){re.escape(pname)}(?![\w\"])|#\"{re.escape(pname)}\"", part.source):
                            self.g.edge(tid, pid, "uses_parameter")
                    native = m_native_sql(part.source)
                    self.g.nodes[tid]["pq_steps"] = ap.steps(part.source)
                    for src in sql_sources(native):
                        sid = self.g.node("src:" + src, "source_object", src)
                        self.g.edge(tid, sid, "reads_source")
        for r in model.relationships:
            ft, fc, tt, tc = r.ends()
            a, b = cols.get((norm(ft), norm(fc))), cols.get((norm(tt), norm(tc)))
            if a and b:
                self.g.edge(a, b, "relationship", active=None if r.is_active else False,
                            direction="both" if r.cross_filter == "bothDirections" else None,
                            cardinality=f"{r.from_cardinality or 'many'}:{r.to_cardinality or 'one'}")
                ta, tb = tables.get(norm(ft)), tables.get(norm(tt))
                if ta and tb:
                    self.g.edge(ta, tb, "relates_to", active=None if r.is_active else False)
        for f in sorted((definition / "roles").glob("*.tmdl")) if (definition / "roles").is_dir() else []:
            text = read(f)
            rname = f.stem
            rid = self.g.node(f"{mid}::role:{rname}", "role", rname, model=name, file=rel(f, self.root))
            self.g.edge(mid, rid, "defines")
            for m in re.finditer(r"tablePermission\s+('(?:[^']|'')+'|\S+)\s*=\s*(.+)", text):
                self.dax_edges(ctx, rid, m.group(2), ap.unquote(m.group(1)), rel_prefix="filters")
        # roles: fact / dimension / measure table
        for t in model.tables:
            tid = tables[norm(t.name)]
            n = self.g.nodes[tid]
            out_rel = any(e["source"] == tid and e["rel"] == "relates_to" for e in self.g.edges)
            in_rel = any(e["target"] == tid and e["rel"] == "relates_to" for e in self.g.edges)
            if n.get("date_table"):
                n["role"] = "date"
            elif out_rel:
                n["role"] = "fact"
            elif in_rel:
                n["role"] = "dimension"
            elif t.measures and not [c for c in t.columns if not c.is_hidden]:
                n["role"] = "measures"
            elif t.name.startswith("_"):
                n["role"] = "system"
            else:
                n["role"] = "disconnected"

    DAX_REF = re.compile(r"(?:('(?:[^']|'')+'|[^\W\d][\w.]*)\s*)?\[([^\]]+)\]")
    DAX_TABLE = re.compile(r"'((?:[^']|'')+)'(?!\s*\[)")

    def dax_edges(self, ctx: dict, src: str, expr: str, host_table: str, rel_prefix: str = "uses") -> None:
        text = strip_dax(expr or "")
        for m in self.DAX_REF.finditer(text):
            tbl = ap.unquote(m.group(1)) if m.group(1) else ""
            name = m.group(2).strip()
            if tbl and norm(tbl) in ctx["tables"]:
                cid = ctx["cols"].get((norm(tbl), norm(name)))
                if cid:
                    self.g.edge(src, cid, "filters" if rel_prefix == "filters" else "uses_column")
                    continue
                mid = ctx["measures"].get(norm(name))
                if mid:
                    self.g.edge(src, mid, "uses_measure")
                continue
            mid = ctx["measures"].get(norm(name))
            if mid:
                self.g.edge(src, mid, "uses_measure")
                continue
            cid = ctx["cols"].get((norm(host_table), norm(name)))
            if cid:
                self.g.edge(src, cid, "uses_column")
        for m in self.DAX_TABLE.finditer(text):
            tid = ctx["tables"].get(norm(ap.unquote("'" + m.group(1) + "'")))
            if tid:
                self.g.edge(src, tid, "uses_table")
        for word in set(re.findall(r"(?<!['\[\w])([^\W\d]\w*)(?![\w'\[(])", text)):
            tid = ctx["tables"].get(norm(word))
            if tid and word.upper() != word:   # skip ALLCAPS functions/keywords
                self.g.edge(src, tid, "uses_table")

    # -- PBIR report
    def parse_report(self, rp: Path) -> None:
        name = rp.name[: -len(".Report")] if rp.name.endswith(".Report") else rp.name
        rid = self.g.node("report:" + rel(rp, self.root), "report", name, file=rel(rp, self.root))
        dash = self.dashboard_for(rp)
        if dash:
            self.g.edge(dash, rid, "contains")
        pbir = rp / "definition.pbir"
        ctx = None
        if pbir.exists():
            text = read(pbir)
            self.track(pbir, text)
            try:
                ref = json.loads(text).get("datasetReference", {})
            except ValueError:
                ref = {}
            path = (ref.get("byPath") or {}).get("path")
            if path:
                target = (rp / path).resolve()
                ctx = next((c for c in self.models if Path(self.root / self.g.nodes[c["mid"]]["file"]).resolve()
                            .parent == target), None)
                if ctx:
                    self.g.edge(rid, ctx["mid"], "binds_model")
            elif ref.get("byConnection"):
                self.g.nodes[rid]["dataset"] = "live connection (byConnection)"
        definition = rp / "definition"
        if (rp / "report.json").exists() and not definition.is_dir():
            self.g.nodes[rid]["format"] = "legacy report.json (not PBIR)"
            self.notes.append(f"{rel(rp, self.root)}: legacy report.json — save as PBIR to graph pages/visuals")
            return
        order: list[str] = []
        pj = definition / "pages" / "pages.json"
        if pj.exists():
            try:
                order = json.loads(read(pj)).get("pageOrder", [])
            except ValueError:
                pass
        pages = sorted((definition / "pages").glob("*/page.json"),
                       key=lambda p: order.index(p.parent.name) if p.parent.name in order else 999)
        for idx, page in enumerate(pages, 1):
            text = read(page)
            pr = self.track(page, text)
            try:
                pdata = json.loads(text)
            except ValueError:
                continue
            proto = any("PROTÓTIPO" in read(v) for v in page.parent.glob("visuals/*/visual.json"))
            pid = self.g.node(f"{rid}/{page.parent.name}", "page", pdata.get("displayName", page.parent.name),
                              report=name, file=pr, order=idx, hidden=(pdata.get("visibility") == "HiddenInViewMode") or None,
                              prototype=proto or None)
            self.g.edge(rid, pid, "has_page")
            self.visual_fields(ctx, pid, pdata.get("filterConfig", {}), "filters")
            for vj in sorted(page.parent.glob("visuals/*/visual.json")):
                vtext = read(vj)
                vr = self.track(vj, vtext)
                try:
                    v = json.loads(vtext)
                except ValueError:
                    continue
                vis = v.get("visual", {}) or {}
                vtype = vis.get("visualType") or ("group" if "visualGroup" in v else "?")
                title = self.visual_title(v)
                vid = self.g.node(f"{pid}/{vj.parent.name}", "visual", f"{vtype}: {title}" if title else vtype,
                                  page=pdata.get("displayName"), report=name, file=vr, visual_type=vtype,
                                  title=title)
                self.g.edge(pid, vid, "has_visual")
                self.visual_fields(ctx, vid, vis.get("query", {}), "uses")
                self.visual_fields(ctx, vid, v.get("filterConfig", {}), "filters")
                self.visual_fields(ctx, vid, vis.get("objects", {}), "uses")
                self.visual_fields(ctx, vid, v.get("visualContainerObjects", {}), "uses")
                self.visual_fields(ctx, vid, vis.get("visualContainerObjects", {}), "uses")

    @staticmethod
    def visual_title(v: dict) -> str:
        try:
            for item in v["visual"]["visualContainerObjects"]["title"]:
                val = item["properties"]["text"]["expr"]["Literal"]["Value"]
                return val.strip("'")
        except (KeyError, TypeError, IndexError):
            pass
        texts = []

        def grab(o):
            if isinstance(o, dict):
                for k, x in o.items():
                    if k == "value" and isinstance(x, str) and len(x) < 200:
                        texts.append(x)
                    else:
                        grab(x)
            elif isinstance(o, list):
                for x in o:
                    grab(x)
        grab(((v.get("visual") or {}).get("objects") or {}).get("general", []))
        return " ".join(texts)[:80]

    def visual_fields(self, ctx: dict | None, owner: str, obj, mode: str) -> None:
        if ctx is None or not obj:
            return
        found: list[tuple[str, str, str]] = []

        def walk_(o, parent=""):
            if isinstance(o, dict):
                ex = o.get("Expression")
                if "Property" in o and isinstance(ex, dict) and "SourceRef" in ex:
                    found.append((parent, ex["SourceRef"].get("Entity", ""), o["Property"]))
                for k, x in o.items():
                    walk_(x, k)
            elif isinstance(o, list):
                for x in o:
                    walk_(x, parent)
        walk_(obj)
        for kind, entity, prop in found:
            if kind == "Measure" or (kind != "Column" and norm(prop) in ctx["measures"]):
                tgt = ctx["measures"].get(norm(prop))
                if tgt:
                    self.g.edge(owner, tgt, "filters" if mode == "filters" else "uses_measure")
                    continue
            tgt = ctx["cols"].get((norm(entity), norm(prop)))
            if tgt:
                self.g.edge(owner, tgt, "filters" if mode == "filters" else "uses_column")
            elif entity:
                miss = self.g.node(f"{ctx['mid']}::missing:{entity}.{prop}", "missing", f"{entity}[{prop}]",
                                   model=ctx["name"])
                self.g.edge(owner, miss, "broken_ref")

    # -- SQL
    def parse_sql(self, p: Path) -> None:
        text = read(p)
        r = self.track(p, text)
        sid = self.g.node("sql:" + r, "sql_file", p.name, file=r)
        dash = self.dashboard_for(p)
        if dash:
            self.g.edge(dash, sid, "contains")
        for src in sql_sources(text):
            oid = self.g.node("src:" + src, "source_object", src)
            self.g.edge(sid, oid, "reads_source")
        for ctx in self.models:
            if ctx["dash"] != dash and not str(p).startswith(str(ctx["folder"])):
                continue
            tid = ctx["tables"].get(norm(p.stem))
            if tid and not {"briefing", "anexos"} & set(p.parts):
                self.g.edge(tid, sid, "loaded_by", "INFERRED")

    # -- Markdown docs
    def parse_doc(self, p: Path) -> None:
        text = read(p)
        r = self.track(p, text)
        head = re.search(r"^#\s+(.+)$", text, re.M)
        did = self.g.node("doc:" + r, "doc", head.group(1).strip()[:80] if head else p.name, file=r)
        dash = self.dashboard_for(p)
        if dash:
            self.g.edge(dash, did, "contains")
        if not dash:
            return
        body = norm(text)
        for ctx in self.models:
            if ctx["dash"] != dash:
                continue
            for key, nid in list(ctx["measures"].items()) + list(ctx["tables"].items()):
                if len(key) >= 4 and re.search(rf"(?<![\w]){re.escape(key)}(?![\w])", body):
                    self.g.edge(did, nid, "mentions", "INFERRED")

    # -- Python tooling
    def parse_py(self, p: Path) -> None:
        text = read(p)
        r = self.track(p, text)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return
        mod = p.stem if p.stem != "__init__" else p.parent.name
        mid = self.g.node("py:" + r, "py_module", mod, file=r, doc=(ast.get_docstring(tree) or "").split("\n")[0][:120])
        imports = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                sid = self.g.node(f"py:{r}:{node.name}", "py_symbol", f"{mod}.{node.name}", file=r, line=node.lineno,
                                  doc=(ast.get_docstring(node) or "").split("\n")[0][:120])
                self.g.edge(mid, sid, "defines")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name.split(".")[-1] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split(".")[-1])
                imports.update(a.name for a in node.names)
        self.g.nodes[mid]["_imports"] = sorted(imports)

    def link_py_imports(self) -> None:
        mods = collections.defaultdict(list)
        for n in self.g.nodes.values():
            if n["kind"] == "py_module":
                mods[n["label"]].append(n["id"])
        for n in list(self.g.nodes.values()):
            for imp in n.pop("_imports", []):
                for tgt in mods.get(imp, []):
                    self.g.edge(n["id"], tgt, "imports")


# ───────────────────────────────────────────────────────────────────────────── analysis

def adjacency(g: dict, directed: bool = False) -> dict[str, list]:
    adj: dict[str, list] = collections.defaultdict(list)
    for e in g["edges"]:
        adj[e["source"]].append((e["target"], e["rel"], "out"))
        if not directed:
            adj[e["target"]].append((e["source"], e["rel"], "in"))
    return adj


def communities(g: dict) -> dict[str, int]:
    """Deterministic label propagation over dependency + containment edges (stdlib only)."""
    adj = collections.defaultdict(list)
    for e in g["edges"]:
        w = 0.3 if e["rel"] in {"contains", "has_page"} else 1.0
        adj[e["source"]].append((e["target"], w))
        adj[e["target"]].append((e["source"], w))
    ids = sorted(g["nodes"])
    label = {n: i for i, n in enumerate(ids)}
    for _ in range(40):
        changed = 0
        for n in ids:
            if not adj[n]:
                continue
            score: dict[int, float] = collections.defaultdict(float)
            for m, w in adj[n]:
                score[label[m]] += w
            best = max(sorted(score), key=lambda k: score[k])
            if best != label[n] and score[best] > score.get(label[n], 0):
                label[n] = best
                changed += 1
        if not changed:
            break
    remap: dict[int, int] = {}
    sizes = collections.Counter(label.values())
    for lab, _ in sizes.most_common():
        remap[lab] = len(remap)
    return {n: remap[label[n]] for n in ids}


def findings(g: dict) -> dict[str, list]:
    nodes, edges = g["nodes"], g["edges"]
    incoming = collections.defaultdict(set)
    fields = collections.Counter()
    for e in edges:
        incoming[e["target"]].add(e["rel"])
        if e["rel"] == "relationship":          # both ends of a relationship are in use
            incoming[e["source"]].add(e["rel"])
        if e["rel"] in {"uses_measure", "uses_column"} and nodes[e["source"]]["kind"] == "visual":
            fields[e["source"]] += 1
    use_rels = {"uses_column", "uses_measure", "relationship", "sorted_by", "filters", "uses_table",
                "hierarchy_level"}
    out: dict[str, list] = collections.defaultdict(list)
    for nid, n in nodes.items():
        k = n["kind"]
        if k == "column" and not (incoming[nid] & use_rels) and not n.get("table", "").startswith("_"):
            out["unused_columns"].append(nid)
        elif k == "measure" and not (incoming[nid] & {"uses_measure", "filters"}):
            out["unused_measures"].append(nid)
        elif k == "table" and n.get("role") == "disconnected":
            out["disconnected_tables"].append(nid)
        elif k == "missing":
            out["broken_refs"].append(nid)
        elif k == "table" and (n.get("pq_steps") or 0) > 1:
            out["power_query_steps"].append(nid)
        elif k == "measure" and not n.get("description"):
            out["measures_without_description"].append(nid)
        elif k == "page":
            if n.get("prototype"):
                out["prototype_pages"].append(nid)
            nv = sum(1 for e in edges if e["source"] == nid and e["rel"] == "has_visual" and fields[e["target"]])
            if nv > 8:                           # data visuals only; textboxes/shapes do not count
                out["crowded_pages"].append(nid)
    table_models = {
        e["target"]: e["source"] for e in edges
        if e["rel"] == "contains" and nodes[e["source"]]["kind"] == "model"
        and nodes[e["target"]]["kind"] == "table"
    }
    measure_models = {
        e["target"]: table_models[e["source"]] for e in edges
        if e["rel"] == "has_measure" and e["source"] in table_models
    }
    readers = collections.defaultdict(set)
    for e in edges:
        if e["rel"] == "reads_source" and e["source"] in table_models:
            readers[e["target"]].add(table_models[e["source"]])
    out["shared_sources"] = [s for s, ms in readers.items() if len(ms) > 1]
    by_name = collections.defaultdict(dict)
    for nid, model_id in measure_models.items():
        n = nodes[nid]
        by_name[norm(n["label"])][model_id] = re.sub(r"\s+", " ", n.get("expression", "")).strip()
    out["divergent_measures"] = [k for k, d in by_name.items() if len(d) > 1 and len(set(d.values())) > 1]
    for e in edges:
        if e["rel"] == "relationship" and e.get("direction") == "both":
            out["bidirectional"].append(f"{nodes[e['source']]['label']} ↔ {nodes[e['target']]['label']}")
    return out


# ───────────────────────────────────────────────────────────────────────────── outputs

def script_cmd(root: Path) -> str:
    here = Path(os.path.abspath(__file__))
    try:
        return "python " + here.relative_to(root.resolve()).as_posix()
    except ValueError:
        return f'python "{here.as_posix()}"'


def loc(n: dict) -> str:
    if not n.get("file"):
        return ""
    return f"{n['file']}:{n['line']}" if n.get("line") else n["file"]


def report_md(g: dict, root: Path, comm: dict[str, int], fnd: dict, stats: dict) -> str:
    nodes, edges = g["nodes"], g["edges"]
    kinds = collections.Counter(n["kind"] for n in nodes.values())
    deg = collections.Counter()
    for e in edges:
        if e["rel"] in DEPENDS or e["rel"] in {"uses_measure", "uses_column"}:
            deg[e["source"]] += 1
            deg[e["target"]] += 1
    cmd = script_cmd(root)
    L = [f"# Power BI graph — {root.name}", "",
         f"> Generated {time.strftime('%Y-%m-%d %H:%M')} by powerbi-architect `pbi_graph.py` {VERSION}. "
         f"Read this before opening TMDL/PBIR/SQL files. Rebuild: `{cmd} update`.", "",
         f"**{len(nodes)} nodes · {len(edges)} edges** — " +
         " · ".join(f"{v} {k}" for k, v in kinds.most_common()), "",
         f"**Token budget:** source files ≈ {stats['corpus_tokens']:,} tokens · this report ≈ "
         f"{{REPORT_TOKENS}} · one model card ≈ {stats['avg_card_tokens']:,}.", "",
         "## How to use (cheapest first)", "",
         "| Need | Do |", "|---|---|",
         "| Overview, findings | this file |",
         "| Everything about one model (tables, columns, DAX, pages, visuals, loads) | `wiki/<model>.md` |",
         f"| Find objects for a question | `{cmd} query \"<question>\"` |",
         f"| One object, full detail + file:line | `{cmd} explain \"<name>\"` |",
         f"| What breaks if I change X | `{cmd} impact \"<name>\"` |",
         f"| How A relates to B | `{cmd} path \"<A>\" \"<B>\"` |",
         "| Edit | open only the file:line the graph gives you |", ""]
    dashes = [n for n in nodes.values() if n["kind"] == "dashboard"]
    models = [n for n in nodes.values() if n["kind"] == "model"]
    if dashes:
        L += ["## Dashboards", "", "| Dashboard | Status | Connection | Path |", "|---|---|---|---|"]
        for d in sorted(dashes, key=lambda x: x["id"]):
            L.append(f"| {d['label']} | {d.get('status', '')} | {d.get('connection', '')} | `{d['id'][5:]}` |")
        L.append("")
    if models:
        model_labels = collections.Counter(m["label"] for m in models)
        L += ["## Semantic models", "", "| Model | Tables (role) | Measures | Pages | Card |", "|---|---|---|---|---|"]
        for m in sorted(models, key=lambda x: x["id"]):
            tabs = [nodes[e["target"]] for e in edges if e["source"] == m["id"] and e["rel"] == "contains"
                    and nodes[e["target"]]["kind"] == "table"]
            nm = sum(1 for n in nodes.values() if n["kind"] == "measure" and n["id"].startswith(m["id"] + "::"))
            reps = [e["source"] for e in edges if e["target"] == m["id"] and e["rel"] == "binds_model"]
            npg = sum(1 for e in edges if e["source"] in reps and e["rel"] == "has_page")
            tl = ", ".join(f"{t['label']} ({t.get('role', '?')})" for t in sorted(tabs, key=lambda t: t["label"]))
            label = m["label"]
            if model_labels[label] > 1:
                path = m["id"].removeprefix("model:").replace("|", r"\|")
                label += f" (`{path}`)"
            L.append(f"| {label} | {tl} | {nm} | {npg} | [wiki/{slug(m)}.md](wiki/{slug(m)}.md) |")
        L.append("")
    hubs = [(nodes[n], d) for n, d in deg.most_common() if nodes[n]["kind"] in
            {"measure", "column", "table", "source_object", "parameter"}][:10]
    if hubs:
        L += ["## Hubs (most depended-on — change with care)", ""]
        for n, d in hubs:
            L.append(f"- **{n['label']}** · {n['kind']} · {d} links · `{loc(n)}`")
        L.append("")
    groups = collections.defaultdict(list)
    for nid, c in comm.items():
        groups[c].append(nid)
    big = [(c, ids) for c, ids in groups.items() if len(ids) >= 3][:12]
    if big:
        L += ["## Communities (objects that move together)", ""]
        for c, ids in big:
            top = sorted(ids, key=lambda i: -deg[i])[:5]
            L.append(f"- **C{c}** ({len(ids)}): " + ", ".join(nodes[i]["label"] for i in top))
        L.append("")
    titles = {
        "broken_refs": "Visual fields not found in the model (broken)",
        "unused_columns": "Columns imported but never used (law 3: import the minimum)",
        "unused_measures": "Measures used by no visual and no other measure",
        "disconnected_tables": "Tables with no relationship",
        "power_query_steps": "Tables with more than one Power Query step",
        "measures_without_description": "Measures without description",
        "crowded_pages": "Pages with more than 8 visuals",
        "prototype_pages": "Prototype pages (grey boxes from `pbw prototipo`; replace by real visuals)",
        "bidirectional": "Bidirectional relationships",
        "shared_sources": "Source objects read by more than one model (conformed-dimension / reuse candidates)",
        "divergent_measures": "Same measure name, different DAX in different models",
    }
    fl = []
    for key, title in titles.items():
        items = fnd.get(key) or []
        if not items:
            continue
        shown = [nodes[i]["label"] + (f" ({nodes[i].get('model')})" if nodes[i].get("model") else "")
                 if i in nodes else i for i in items[:15]]
        more = f" … +{len(items) - 15}" if len(items) > 15 else ""
        fl.append(f"- **{title}** ({len(items)}): " + ", ".join(shown) + more)
    L += ["## Findings", ""] + (fl or ["- none"]) + [""]
    if stats.get("notes"):
        L += ["## Not graphed", ""] + [f"- {x}" for x in stats["notes"]] + [""]
    sample = [n for n in nodes.values() if n["kind"] == "measure"][:1]
    col = [n for n in nodes.values() if n["kind"] == "column" and not n.get("hidden")][:1]
    q = ["- `query \"which measures feed the main page?\"`"]
    if sample:
        q.append(f"- `impact \"{sample[0]['label']}\"` — visuals and measures that depend on it")
    if col:
        q.append(f"- `explain \"{col[0]['label']}\"`")
    L += ["## Suggested questions", ""] + q + [""]
    return "\n".join(L)


def slug(n: dict) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", norm(n["label"])).strip("-") or "model"
    h = hashlib.sha1(n["id"].encode()).hexdigest()[:4]
    return f"{base}-{h}"


def model_card(g: dict, m: dict) -> str:
    nodes, edges = g["nodes"], g["edges"]
    out = collections.defaultdict(list)
    inc = collections.defaultdict(list)
    for e in edges:
        out[e["source"]].append(e)
        inc[e["target"]].append(e)
    pre = m["id"] + "::"
    base = (m.get("file", "") + "/") if m.get("file") else ""

    def loc(n: dict) -> str:  # paths relative to the model definition folder (shown once in the header)
        full = globals()["loc"](n)
        return full[len(base):] if base and full.startswith(base) else full
    L = [f"# Model card — {m['label']}", "", f"`{m.get('file', '')}` · culture {m.get('culture', '?')} · "
         f"auto date/time {'ON' if m.get('auto_datetime') else 'off'}", ""]
    dash = [nodes[e["source"]] for e in inc[m["id"]] if e["rel"] == "contains"]
    if dash:
        d = dash[0]
        L += [f"**Dashboard:** {d['label']} · status {d.get('status', '?')} · connection {d.get('connection', '?')} · "
              f"refresh {d.get('refresh', '?')} · {d.get('classification', '')}", ""]
        if d.get("description"):
            L += [f"> {d['description']}", ""]
    params = [nodes[e["target"]] for e in out[m["id"]] if nodes[e["target"]]["kind"] == "parameter"]
    if params:
        L += ["**Parameters:** " + ", ".join(f"{p['label']} = {p.get('value', '')}" for p in params), ""]
    tables = sorted((nodes[e["target"]] for e in out[m["id"]] if nodes[e["target"]]["kind"] == "table"),
                    key=lambda t: ({"fact": 0, "dimension": 1, "date": 2}.get(t.get("role"), 3), t["label"]))
    L += ["## Tables", ""]
    for t in tables:
        srcs = [nodes[e["target"]]["label"] for e in out[t["id"]] if e["rel"] == "reads_source"]
        sqlf = [nodes[e["target"]]["file"] for e in out[t["id"]] if e["rel"] == "loaded_by"]
        L.append(f"### {t['label']} · {t.get('role', '?')} · `{loc(t)}`")
        if t.get("description"):
            L.append(t["description"].replace("\n", " "))
        meta = []
        if srcs:
            meta.append("source " + ", ".join(srcs))
        if sqlf:
            meta.append("SQL " + ", ".join(f"`{s}`" for s in sqlf))
        if t.get("pq_steps"):
            meta.append(f"{t['pq_steps']} PQ step(s)")
        if meta:
            L.append(" · ".join(meta))
        cols = [nodes[e["target"]] for e in out[t["id"]] if e["rel"] == "has_column"]
        if cols:
            parts = []
            for c in cols:
                flags = "".join(x for x, ok in (("·key", c.get("key")), ("·h", c.get("hidden")),
                                                 ("·calc", c.get("calculated"))) if ok)
                used = sum(1 for e in inc[c["id"]] if e["rel"] in {"uses_column", "relationship", "sorted_by", "filters", "hierarchy_level"}
                           or (e["rel"] == "relationship"))
                used += sum(1 for e in out[c["id"]] if e["rel"] == "relationship")
                parts.append(f"{c['label'].split('[', 1)[1][:-1]}:{c.get('data_type', '?')}{flags}"
                             + ("" if used else "·UNUSED"))
            L.append("Columns: " + ", ".join(parts))
        L.append("")
    rels = [e for e in edges if e["rel"] == "relationship" and e["source"].startswith(pre)]
    if rels:
        L += ["## Relationships", ""]
        for e in rels:
            extra = " ".join(x for x in (e.get("cardinality", ""), "inactive" if e.get("active") is False else "",
                                         "BOTH" if e.get("direction") == "both" else "") if x)
            L.append(f"- {nodes[e['source']]['label']} → {nodes[e['target']]['label']} {extra}")
        L.append("")
    measures = sorted((n for n in nodes.values() if n["kind"] == "measure" and n["id"].startswith(pre)),
                      key=lambda n: (n.get("folder", ""), n["label"]))
    if measures:
        L += ["## Measures", ""]
        for ms in measures:
            users = [nodes[e["source"]] for e in inc[ms["id"]] if e["rel"] in {"uses_measure", "filters"}]
            nv = sum(1 for u in users if u["kind"] == "visual")
            nm = sum(1 for u in users if u["kind"] == "measure")
            L.append(f"- **[{ms['label']}]** · {ms.get('folder', '-')} · {ms.get('format', '')} · "
                     f"used by {nm} measure(s), {nv} visual(s) · `{loc(ms)}`")
            if ms.get("description"):
                L.append(f"  - {ms['description'].replace(chr(10), ' ')}")
            expr = re.sub(r"\s+", " ", ms.get("expression", "")).strip()
            L.append(f"  - `{expr[:400]}{'…' if len(expr) > 400 else ''}`")
        L.append("")
    reps = [nodes[e["source"]] for e in inc[m["id"]] if e["rel"] == "binds_model"]
    for r in reps:
        L += [f"## Report {r['label']} · `{r.get('file', '')}`", ""]
        pages = sorted((nodes[e["target"]] for e in out[r["id"]] if e["rel"] == "has_page"),
                       key=lambda p: p.get("order", 0))
        for p in pages:
            L.append(f"### {p.get('order', '')}. {p['label']}{' (hidden)' if p.get('hidden') else ''}")
            for e in out[p["id"]]:
                if e["rel"] != "has_visual":
                    continue
                v = nodes[e["target"]]
                fields = [nodes[x["target"]]["label"] for x in out[v["id"]]
                          if x["rel"] in {"uses_measure", "uses_column", "broken_ref"}]
                fl = [nodes[x["target"]]["label"] for x in out[v["id"]] if x["rel"] == "filters"]
                line = f"- {v['label']}: " + (", ".join(f"[{f}]" if "[" not in f else f for f in fields) or "—")
                if fl:
                    line += " · filters " + ", ".join(fl)
                L.append(line)
            L.append("")
    return "\n".join(L)


HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Power BI graph</title><style>
:root{--bg:#fff;--fg:#1f2328;--mut:#59636e;--line:#d0d7de;--panel:#f6f8fa}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mut:#8d96a0;--line:#30363d;--panel:#151b23}}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:13px/1.4 system-ui,Segoe UI,sans-serif}
#bar{position:fixed;top:0;left:0;right:0;display:flex;gap:8px;align-items:center;padding:8px 12px;background:var(--panel);border-bottom:1px solid var(--line);z-index:2;flex-wrap:wrap}
#bar input{flex:1;min-width:160px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
#side{position:fixed;right:0;top:48px;bottom:0;width:min(380px,90vw);overflow:auto;padding:12px;background:var(--panel);border-left:1px solid var(--line);display:none;z-index:2}
#side pre{white-space:pre-wrap;font:12px ui-monospace,Consolas,monospace}
canvas{display:block}.k{display:inline-block;width:9px;height:9px;border-radius:50%;margin:0 3px 0 8px}
</style></head><body><div id="bar"><b>Power BI graph</b><input id="q" placeholder="search table, measure, page, visual…"><span id="legend"></span></div>
<div id="side"></div><canvas id="c"></canvas><script>
const G=__DATA__;const C={dashboard:'#8250df',model:'#0969da',report:'#bf3989',page:'#d4a72c',visual:'#e16f24',table:'#1a7f37',column:'#6e7781',measure:'#c99a00',parameter:'#57606a',source_object:'#0550ae',sql_file:'#116329',doc:'#8c959f',py_module:'#6639ba',py_symbol:'#a475f9',missing:'#cf222e',role:'#953800',connection:'#57606a'};
const cv=document.getElementById('c'),cx=cv.getContext('2d'),side=document.getElementById('side');let W,H,dpr=devicePixelRatio||1;
function rs(){W=innerWidth;H=innerHeight;cv.width=W*dpr;cv.height=H*dpr;cv.style.width=W+'px';cv.style.height=H+'px';cx.setTransform(dpr,0,0,dpr,0,0)}rs();addEventListener('resize',rs);
const N=G.nodes,idx={};N.forEach((n,i)=>{idx[n.id]=i;const a=(n.c||0)*2.4+i*.05;n.x=W/2+Math.cos(a)*(120+(n.c||0)*14)+Math.random()*40;n.y=H/2+Math.sin(a)*(120+(n.c||0)*14)+Math.random()*40;n.vx=0;n.vy=0;n.d=0});
const E=G.edges.filter(e=>e.s in idx&&e.t in idx).map(e=>({a:idx[e.s],b:idx[e.t],r:e.r}));E.forEach(e=>{N[e.a].d++;N[e.b].d++});
document.getElementById('legend').innerHTML=[...new Set(N.map(n=>n.k))].map(k=>`<span class=k style="background:${C[k]||'#888'}"></span>${k}`).join('');
let sel=-1,hit=new Set(),ox=0,oy=0,sc=1,drag=null,pan=null,alpha=1;
function step(){if(alpha<.002)return;const n=N.length;for(let i=0;i<n;i++){const p=N[i];for(let j=i+1;j<n;j++){const q=N[j];let dx=p.x-q.x,dy=p.y-q.y,d2=dx*dx+dy*dy+.01;if(d2>90000)continue;const f=900/d2*alpha;p.vx+=dx*f;p.vy+=dy*f;q.vx-=dx*f;q.vy-=dy*f}}
E.forEach(e=>{const p=N[e.a],q=N[e.b],dx=q.x-p.x,dy=q.y-p.y,d=Math.sqrt(dx*dx+dy*dy)+.01,f=(d-60)*.01*alpha;p.vx+=dx/d*f;p.vy+=dy/d*f;q.vx-=dx/d*f;q.vy-=dy/d*f});
N.forEach(p=>{p.vx+=(W/2-p.x)*.0008*alpha;p.vy+=(H/2-p.y)*.0008*alpha;if(p!==drag){p.x+=p.vx;p.y+=p.vy}p.vx*=.6;p.vy*=.6});alpha*=.995}
function draw(){step();cx.clearRect(0,0,W,H);cx.save();cx.translate(ox,oy);cx.scale(sc,sc);const fg=getComputedStyle(document.body).color;
cx.globalAlpha=.25;cx.strokeStyle=fg;cx.lineWidth=.6/sc;E.forEach(e=>{const on=sel>=0&&(e.a===sel||e.b===sel);if(sel>=0&&!on)return;cx.globalAlpha=on?.8:.18;cx.beginPath();cx.moveTo(N[e.a].x,N[e.a].y);cx.lineTo(N[e.b].x,N[e.b].y);cx.stroke()});
cx.globalAlpha=1;N.forEach((p,i)=>{const r=3+Math.min(9,Math.sqrt(p.d));const dim=(hit.size&&!hit.has(i))||(sel>=0&&i!==sel&&!E.some(e=>(e.a===sel&&e.b===i)||(e.b===sel&&e.a===i)));cx.globalAlpha=dim?.15:1;cx.fillStyle=C[p.k]||'#888';cx.beginPath();cx.arc(p.x,p.y,r,0,7);cx.fill();
if(!dim&&(p.d>5||i===sel||hit.has(i)||sc>1.6)){cx.fillStyle=fg;cx.font=`${11/sc}px system-ui`;cx.fillText(p.l.slice(0,40),p.x+r+2,p.y+3)}});cx.restore();requestAnimationFrame(draw)}draw();
function at(x,y){x=(x-ox)/sc;y=(y-oy)/sc;let b=-1,bd=144/sc/sc;N.forEach((p,i)=>{const d=(p.x-x)**2+(p.y-y)**2;if(d<bd){bd=d;b=i}});return b}
function show(i){sel=i;if(i<0){side.style.display='none';return}const p=N[i];const nb=E.filter(e=>e.a===i||e.b===i).map(e=>(e.a===i?'→ ':'← ')+e.r+' '+N[e.a===i?e.b:e.a].l);
side.style.display='block';side.innerHTML=`<h3>${esc(p.l)}</h3><div>${p.k}${p.f?' · <code>'+esc(p.f)+'</code>':''}</div>${p.x2?'<pre>'+esc(p.x2)+'</pre>':''}<pre>${esc(nb.join('\\n'))}</pre>`}
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
cv.onmousedown=e=>{const i=at(e.clientX,e.clientY);if(i>=0){drag=N[i];show(i)}else{pan=[e.clientX-ox,e.clientY-oy];show(-1)}};
cv.onmousemove=e=>{if(drag){drag.x=(e.clientX-ox)/sc;drag.y=(e.clientY-oy)/sc;alpha=Math.max(alpha,.3)}else if(pan){ox=e.clientX-pan[0];oy=e.clientY-pan[1]}};onmouseup=()=>{drag=null;pan=null};
cv.onwheel=e=>{e.preventDefault();const k=e.deltaY<0?1.1:.9;ox=e.clientX-(e.clientX-ox)*k;oy=e.clientY-(e.clientY-oy)*k;sc*=k};
document.getElementById('q').oninput=e=>{const v=e.target.value.toLowerCase().normalize('NFKD').replace(/[\\u0300-\\u036f]/g,'');hit=new Set();if(v.length>1)N.forEach((p,i)=>{if(p.l.toLowerCase().normalize('NFKD').replace(/[\\u0300-\\u036f]/g,'').includes(v))hit.add(i)})};
</script></body></html>"""


def write_html(g: dict, comm: dict, path: Path) -> None:
    nodes = list(g["nodes"].values())
    if len(nodes) > 900:
        nodes = [n for n in nodes if n["kind"] not in {"column", "py_symbol"}]
    keep = {n["id"] for n in nodes}
    data = {"nodes": [{"id": n["id"], "l": n["label"], "k": n["kind"], "c": comm.get(n["id"], 0), "f": loc(n),
                       "x2": (n.get("description") or "") + ("\n\n" + n["expression"] if n.get("expression") else "")}
                      for n in nodes],
            "edges": [{"s": e["source"], "t": e["target"], "r": e["rel"]} for e in g["edges"]
                      if e["source"] in keep and e["target"] in keep]}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    path.write_text(HTML.replace("__DATA__", blob), encoding="utf-8")


def scan_fingerprint(root: Path) -> dict:
    out = {}
    for p in walk(root):
        if p.suffix.lower() in TEXT_EXT or p.name in {"ficha.toml", "dashboard.toml"}:
            try:
                st = p.stat()
            except OSError:
                continue
            out[rel(p, root)] = [st.st_mtime_ns, st.st_size]
    return out


def build(root: Path, out: Path, quiet: bool = False) -> dict:
    t0 = time.time()
    b = Builder(long_path(root))
    g = b.build()
    data = {"version": VERSION, "root": root.name, "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "nodes": g.nodes, "edges": g.edges}
    comm = communities(data)
    for nid, c in comm.items():
        data["nodes"][nid]["community"] = c
    fnd = findings(data)
    out.mkdir(parents=True, exist_ok=True)
    wiki = out / "wiki"
    if wiki.exists():
        shutil.rmtree(wiki)
    wiki.mkdir()
    cards = []
    index = ["# Model cards", "", "One file per semantic model: tables, columns (UNUSED flagged), relationships, "
             "measures with DAX, pages and the fields each visual uses.", ""]
    for m in sorted((n for n in data["nodes"].values() if n["kind"] == "model"), key=lambda n: n["id"]):
        card = model_card(data, m)
        cards.append(len(card))
        (wiki / f"{slug(m)}.md").write_text(card, encoding="utf-8")
        index.append(f"- [{m['label']}]({slug(m)}.md) — `{m.get('file', '')}`")
    (wiki / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    stats = {"corpus_tokens": tokens(b.corpus_chars),
             "avg_card_tokens": tokens(sum(cards) // len(cards)) if cards else 0, "notes": b.notes}
    rep = report_md(data, root, comm, fnd, stats)
    rep = rep.replace("{REPORT_TOKENS}", f"{tokens(len(rep)):,} tokens")
    (out / "GRAPH_REPORT.md").write_text(rep, encoding="utf-8")
    data["findings"] = fnd
    (out / "graph.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    write_html(data, comm, out / "graph.html")
    (out / "manifest.json").write_text(json.dumps({"version": BUILD_ID, "files": scan_fingerprint(long_path(root))}, indent=0),
                                       encoding="utf-8")
    if not (out / ".keep-in-git").exists():   # generated output stays out of Git unless --commit-graph
        (out / ".gitignore").write_text("# generated by pbi_graph.py — rebuild with `update`\n*\n", encoding="utf-8")
    summary = {"nodes": len(g.nodes), "edges": len(g.edges), "models": sum(1 for n in g.nodes.values() if n["kind"] == "model"),
               "corpus_tokens": stats["corpus_tokens"], "report_tokens": tokens(len(rep)), "seconds": round(time.time() - t0, 2)}
    if not quiet:
        ratio = summary["corpus_tokens"] / max(1, summary["report_tokens"])
        print(f"[ok] graph: {summary['nodes']} nodes · {summary['edges']} edges · {summary['models']} model(s) "
              f"in {summary['seconds']}s → {rel(out, root)}/")
        print(f"  source ≈ {summary['corpus_tokens']:,} tokens · GRAPH_REPORT.md ≈ {summary['report_tokens']:,} "
              f"tokens ({ratio:.0f}× smaller)")
    return summary


def needs_update(root: Path, out: Path) -> bool:
    mf = out / "manifest.json"
    if not (out / "graph.json").exists() or not mf.exists():
        return True
    try:
        old = json.loads(mf.read_text(encoding="utf-8"))
    except ValueError:
        return True
    return old.get("version") != BUILD_ID or old.get("files") != scan_fingerprint(long_path(root))


# ───────────────────────────────────────────────────────────────────────────── queries

def find_root(start: Path) -> Path:
    for p in [start] + list(start.parents):
        if (p / OUT_DIR / "graph.json").exists():
            return p
    return start


def load(root: Path, out: Path) -> dict:
    gp = out / "graph.json"
    if not gp.exists() or needs_update(root, out):
        build(root, out, quiet=True)
    return json.loads(gp.read_text(encoding="utf-8"))


def match(g: dict, text: str, limit: int = 5) -> list[str]:
    q = norm(text).strip("[]'\" ")
    qw = [w for w in words(text) if w not in STOP]
    scored = []
    for nid, n in g["nodes"].items():
        lab = norm(n["label"])
        bare = lab.split("[", 1)[1].rstrip("]") if "[" in lab else lab
        s = 0.0
        if q in (lab, bare):
            s = 100
        elif len(q) > 2 and (q in lab):
            s = 60 + 10 * len(q) / max(1, len(lab))
        else:
            lw = set(words(n["label"]))
            hits = sum(1 for w in qw if w in lw or (len(w) > 4 and any(x.startswith(w[:5]) for x in lw)))
            if hits:
                s = 20 * hits / max(1, len(lw)) + 10 * hits
        if s:
            s += {"measure": 6, "table": 5, "page": 4, "column": 3, "visual": 2}.get(n["kind"], 0)
            if n["kind"] in {"py_symbol", "doc"}:
                s -= 5
            scored.append((s, nid))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [nid for _, nid in scored[:limit]]


def describe(n: dict, full: bool = False) -> str:
    bits = [f"{n['kind']} **{n['label']}**"]
    if n.get("model") and n["kind"] not in {"model"}:
        bits.append(f"model {n['model']}")
    for k in ("role", "data_type", "folder", "format", "visual_type", "page", "status"):
        if n.get(k):
            bits.append(f"{k} {n[k]}")
    if loc(n):
        bits.append(f"`{loc(n)}`")
    s = " · ".join(bits)
    if n.get("description"):
        s += "\n  " + n["description"].replace("\n", " ")[: (2000 if full else 160)]
    if n.get("expression"):
        ex = n["expression"] if full else re.sub(r"\s+", " ", n["expression"])[:220]
        s += "\n  = " + ex
    return s


def cmd_query(g: dict, question: str, budget: int, dfs: bool) -> str:
    seeds = match(g, question)
    if not seeds:
        return "No match. Try a table, measure, page or column name, or read GRAPH_REPORT.md."
    adj = adjacency(g, directed=dfs)
    seen, order = set(seeds), list(seeds)
    frontier = list(seeds)
    for _ in range(4 if dfs else 2):
        nxt = []
        for nid in frontier:
            for m, r, _ in adj[nid]:
                if m not in seen and r not in {"contains", "has_column"}:
                    seen.add(m)
                    order.append(m)
                    nxt.append(m)
        frontier = nxt
    lines = [f"# query: {question}", ""]
    used = 0
    for nid in order:
        chunk = describe(g["nodes"][nid])
        if used + tokens(len(chunk)) > budget:
            lines.append(f"… {len(order) - len(lines) + 2} more (raise --budget or use explain)")
            break
        lines.append("- " + chunk)
        used += tokens(len(chunk))
    rels_ = [e for e in g["edges"] if e["source"] in seen and e["target"] in seen and e["rel"] not in CONTAINS]
    if rels_ and used < budget:
        lines += ["", "links:"]
        for e in rels_[: max(5, (budget - used) // 12)]:
            lines.append(f"  {g['nodes'][e['source']]['label']} -{e['rel']}-> {g['nodes'][e['target']]['label']}")
    return "\n".join(lines)


def cmd_explain(g: dict, name: str) -> str:
    hits = match(g, name, 3)
    if not hits:
        return f"Not found: {name}"
    n = g["nodes"][hits[0]]
    L = [describe(n, full=True)]
    extra = {k: v for k, v in n.items() if k not in {"id", "kind", "label", "file", "line", "description",
                                                     "expression", "model", "community"}}
    if extra:
        L.append("  " + " · ".join(f"{k}={v}" for k, v in extra.items()))
    outs = collections.defaultdict(list)
    ins = collections.defaultdict(list)
    for e in g["edges"]:
        if e["source"] == n["id"]:
            outs[e["rel"]].append(g["nodes"][e["target"]])
        elif e["target"] == n["id"]:
            ins[e["rel"]].append(g["nodes"][e["source"]])
    for title, grp, arrow in (("depends on / contains", outs, "→"), ("used by / part of", ins, "←")):
        if grp:
            L.append(f"\n{title}:")
            for r, ns in sorted(grp.items()):
                L.append(f"  {arrow} {r}: " + ", ".join(f"{x['label']}" + (f" `{loc(x)}`" if x["kind"] in
                                                                            {"measure", "table", "sql_file"} else "")
                                                         for x in ns[:25]) + (f" … +{len(ns) - 25}" if len(ns) > 25 else ""))
    if len(hits) > 1:
        L.append("\nalso matched: " + ", ".join(g["nodes"][h]["label"] + f" ({g['nodes'][h]['kind']})" for h in hits[1:]))
    return "\n".join(L)


def cmd_impact(g: dict, name: str) -> str:
    hits = match(g, name, 1)
    if not hits:
        return f"Not found: {name}"
    start = hits[0]
    dep = {"uses_measure", "uses_column", "uses_table", "sorted_by", "hierarchy_level", "filters", "relationship",
           "has_column", "has_measure", "reads_source", "loaded_by", "uses_parameter", "binds_model", "has_visual", "imports"}
    rev = collections.defaultdict(list)
    for e in g["edges"]:
        if e["rel"] in dep:
            if e["rel"] in {"has_column", "has_measure", "has_visual"}:
                continue
            rev[e["target"]].append(e["source"])
    if g["nodes"][start]["kind"] == "table":
        for e in g["edges"]:
            if e["source"] == start and e["rel"] in {"has_column", "has_measure"}:
                rev[start].append(e["target"])
    seen, q = {start}, collections.deque([start])
    while q:
        cur = q.popleft()
        for s in rev[cur]:
            if s not in seen:
                seen.add(s)
                q.append(s)
    seen.discard(start)
    by = collections.defaultdict(list)
    for nid in seen:
        by[g["nodes"][nid]["kind"]].append(g["nodes"][nid])
    pages = sorted({v.get("page", "") for v in by.get("visual", []) if v.get("page")})
    L = [f"# impact of changing {g['nodes'][start]['kind']} {g['nodes'][start]['label']}", ""]
    if not seen:
        L.append("Nothing in the repo depends on it (safe to change; check external reports/Excel).")
    for kind in ("measure", "column", "table", "visual", "role", "page", "report", "py_module"):
        if by.get(kind):
            L.append(f"- {len(by[kind])} {kind}(s): " + ", ".join(
                f"{n['label']}" + (f" `{loc(n)}`" if kind == "measure" else
                                   f" ({n.get('page')})" if kind == "visual" else "") for n in by[kind][:30]))
    if pages:
        L.append(f"- pages affected: {', '.join(pages)}")
    return "\n".join(L)


def cmd_path(g: dict, a: str, b: str) -> str:
    ha, hb = match(g, a, 1), match(g, b, 1)
    if not ha or not hb:
        return "One of the names was not found."
    adj = adjacency(g)
    loose = {"mentions", "contains", "defines", "uses_connection"}   # doc/folder links: last resort only
    for allowed in (lambda r: r not in loose, lambda r: True):
        prev = {ha[0]: None}
        q = collections.deque([ha[0]])
        while q:
            cur = q.popleft()
            if cur == hb[0]:
                break
            for m, r, d in adj[cur]:
                if m not in prev and allowed(r):
                    prev[m] = (cur, r, d)
                    q.append(m)
        if hb[0] in prev:
            break
    if hb[0] not in prev:
        return f"No path between {g['nodes'][ha[0]]['label']} and {g['nodes'][hb[0]]['label']}."
    chain, cur = [], hb[0]
    while prev[cur]:
        p, r, d = prev[cur]
        chain.append((p, r, d, cur))
        cur = p
    chain.reverse()
    L = [g["nodes"][ha[0]]["label"] + f" ({g['nodes'][ha[0]]['kind']})"]
    for p, r, d, c in chain:
        L.append(f"  {'-' + r + '->' if d == 'out' else '<-' + r + '-'} {g['nodes'][c]['label']} ({g['nodes'][c]['kind']})")
    return "\n".join(L)


# ───────────────────────────────────────────────────────────────────────────── install

def agent_section(cmd: str) -> str:
    return f"""{MARK_START}
## Power BI knowledge graph (powerbi-architect)

`powerbi-graph/` holds a prebuilt graph of every PBIP model, report, SQL load and doc in this repo.
It is refreshed automatically (session start, after edits, after commits). **Use it before opening
`*.tmdl`, `visual.json` or `*.sql` files** — it answers most questions for a fraction of the tokens.

1. Start with `powerbi-graph/GRAPH_REPORT.md` (models, hubs, unused columns/measures, broken refs).
2. Questions about one model → read `powerbi-graph/wiki/<model>.md` instead of the TMDL/PBIR files.
3. Targeted lookups:
   - `{cmd} query "<question>"` — relevant objects + links (add `--budget N` to cap tokens)
   - `{cmd} explain "<name>"` — one object with DAX, description, file:line and all links
   - `{cmd} impact "<name>"` — measures, visuals and pages that break if you change it
   - `{cmd} path "<A>" "<B>"` — how two objects connect
4. Open a source file only to edit it, at the file:line the graph gave you.
5. After editing outside Claude Code, run `{cmd} update` (no-op when nothing changed).
{MARK_END}
"""


def upsert_block(path: Path, block: str, start: str, end: str, create_header: str = "") -> str:
    text = path.read_text(encoding="utf-8") if path.exists() else create_header
    if start in text and end in text:
        new = re.sub(re.escape(start) + r".*?" + re.escape(end) + r"\n?", block.replace("\\", "\\\\"), text, flags=re.S)
        state = "updated"
    else:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else text) + block
        state = "added"
    if new != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new, encoding="utf-8")
    return state


def remove_block(path: Path, start: str, end: str) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    new = re.sub(r"\n*" + re.escape(start) + r".*?" + re.escape(end) + r"\n?", "\n", text, flags=re.S)
    if new != text:
        path.write_text(new.rstrip("\n") + "\n", encoding="utf-8")
        return True
    return False


def python_name() -> str:
    for exe in ("python3", "python"):
        p = shutil.which(exe)
        if p and "WindowsApps" not in p:
            return exe
    return "python"


def install(repo: Path, hooks: bool, agents: bool, commit_graph: bool) -> int:
    repo = repo.resolve()
    out = repo / OUT_DIR
    if commit_graph:
        out.mkdir(exist_ok=True)
        (out / ".keep-in-git").write_text("", encoding="utf-8")
        if (out / ".gitignore").exists():
            (out / ".gitignore").unlink()
    build(repo, out)
    py = python_name()
    cmd = script_cmd(repo).replace("python ", py + " ", 1)
    if agents:
        claude = repo / "CLAUDE.md"
        agents_md = repo / "AGENTS.md"
        sec = agent_section(cmd)
        print(f"[ok] AGENTS.md: {upsert_block(agents_md, sec, MARK_START, MARK_END, '# AGENTS.md' + chr(10))}")
        if claude.exists() and "@AGENTS.md" in claude.read_text(encoding="utf-8"):
            print("• CLAUDE.md imports AGENTS.md — not duplicated")
        else:
            print(f"[ok] CLAUDE.md: {upsert_block(claude, sec, MARK_START, MARK_END, '# CLAUDE.md' + chr(10))}")
        gh = repo / ".github" / "copilot-instructions.md"
        if gh.exists():
            print(f"[ok] .github/copilot-instructions.md: {upsert_block(gh, sec, MARK_START, MARK_END)}")
    if hooks:
        claude_hooks(repo, f"{cmd} update --quiet")
        git_hooks(repo, f"{cmd} update --quiet")
    print("\nDone. Agents now read powerbi-graph/GRAPH_REPORT.md first and query the graph instead of raw files.")
    return 0


def claude_hooks(repo: Path, command: str) -> None:
    sp = repo / ".claude" / "settings.json"
    try:
        settings = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    except ValueError:
        print(f"! {rel(sp, repo)} is not valid JSON — Claude Code hooks not added")
        return
    hooks = settings.setdefault("hooks", {})
    wanted = {"SessionStart": None, "PostToolUse": "Edit|Write|MultiEdit|NotebookEdit"}
    for event, matcher in wanted.items():
        groups = hooks.setdefault(event, [])
        groups[:] = [grp for grp in groups if not any("pbi_graph.py" in h.get("command", "")
                                                      for h in grp.get("hooks", []))]
        entry = {"hooks": [{"type": "command", "command": command, "timeout": 60}]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        groups.append(entry)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("[ok] .claude/settings.json: graph refresh on SessionStart and after edits")


def git_hooks(repo: Path, command: str) -> None:
    gd = repo / ".git"
    if not gd.is_dir():
        print("• not a git repository (or a worktree) — git hooks skipped")
        return
    block = f"{SH_START}\n{command} >/dev/null 2>&1 || true\n{SH_END}\n"
    for name in ("post-commit", "post-merge", "post-checkout"):
        hp = gd / "hooks" / name
        hp.parent.mkdir(exist_ok=True)
        if not hp.exists():
            hp.write_text("#!/bin/sh\n", encoding="utf-8")
        upsert_block(hp, block, SH_START, SH_END)
        try:
            hp.chmod(0o755)
        except OSError:
            pass
    print("[ok] .git/hooks: graph refresh after commit, merge and checkout")


def uninstall(repo: Path) -> int:
    repo = repo.resolve()
    for f in (repo / "AGENTS.md", repo / "CLAUDE.md", repo / ".github" / "copilot-instructions.md"):
        if remove_block(f, MARK_START, MARK_END):
            print(f"[ok] section removed: {rel(f, repo)}")
    for name in ("post-commit", "post-merge", "post-checkout"):
        if remove_block(repo / ".git" / "hooks" / name, SH_START, SH_END):
            print(f"[ok] git hook cleaned: {name}")
    sp = repo / ".claude" / "settings.json"
    if sp.exists():
        try:
            s = json.loads(sp.read_text(encoding="utf-8"))
            for ev, groups in list(s.get("hooks", {}).items()):
                groups[:] = [g for g in groups if not any("pbi_graph.py" in h.get("command", "") for h in g.get("hooks", []))]
                if not groups:
                    del s["hooks"][ev]
            if not s.get("hooks"):
                s.pop("hooks", None)
            sp.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print("[ok] .claude/settings.json hooks removed")
        except ValueError:
            pass
    if (repo / OUT_DIR).exists():
        shutil.rmtree(repo / OUT_DIR)
        print(f"[ok] {OUT_DIR}/ removed")
    return 0


# ───────────────────────────────────────────────────────────────────────────── cli

def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]

    def opt(name: str, default=None):
        if name in args:
            i = args.index(name)
            val = args[i + 1] if i + 1 < len(args) else default
            del args[i:i + 2]
            return val
        return default

    def flag(name: str) -> bool:
        if name in args:
            args.remove(name)
            return True
        return False

    out_opt = opt("--out")
    quiet = flag("--quiet")
    if cmd in {"build", "update"}:
        root = Path(args[0] if args else ".").resolve()
        out = Path(out_opt).resolve() if out_opt else root / OUT_DIR
        if cmd == "update" and not needs_update(root, out):
            if not quiet:
                print("[ok] graph up to date")
            return 0
        try:
            build(root, out, quiet)
        except Exception as exc:  # never break a hook
            print(f"! graph build failed: {exc}", file=sys.stderr)
            return 0 if quiet else 1
        return 0
    if cmd == "install":
        no_hooks, no_agents, commit = flag("--no-hooks"), flag("--no-agents"), flag("--commit-graph")
        return install(Path(args[0] if args else "."), hooks=not no_hooks, agents=not no_agents, commit_graph=commit)
    if cmd == "uninstall":
        return uninstall(Path(args[0] if args else "."))
    root = find_root(Path.cwd())
    out = Path(out_opt).resolve() if out_opt else root / OUT_DIR
    budget = int(opt("--budget", "1500"))
    dfs = flag("--dfs")
    g = load(root, out)
    if cmd == "query" and args:
        print(cmd_query(g, " ".join(args), budget, dfs))
    elif cmd == "explain" and args:
        print(cmd_explain(g, " ".join(args)))
    elif cmd == "impact" and args:
        print(cmd_impact(g, " ".join(args)))
    elif cmd == "path" and len(args) >= 2:
        print(cmd_path(g, args[0], args[1]))
    elif cmd == "stats":
        k = collections.Counter(n["kind"] for n in g["nodes"].values())
        print(f"{len(g['nodes'])} nodes · {len(g['edges'])} edges · built {g.get('built')}")
        print(" · ".join(f"{v} {x}" for x, v in k.most_common()))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
