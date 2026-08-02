"""
analyzer.py
-----------
Local, offline static-analysis engine for Pythonic.

Everything in this file runs purely on Python's built-in `ast` module.
No network calls, no API keys, no tokens spent. This powers the
Flowchart / Issues / Complexity / Optimize buttons so the Groq quota
is reserved only for the "Explain it" button.
"""

import ast
import builtins
import re
from dataclasses import dataclass, field, asdict
from typing import Any


# ============================================================
# Shared helpers
# ============================================================

_MERMAID_SAFE_CHARS = re.compile(r"[^A-Za-z0-9 _.,:=+\-*/<>!'()]")


def _mermaid_escape(text: str) -> str:
    """Whitelist-based sanitizer for Mermaid node labels. Instead of trying
    to enumerate every character that could break the parser (impossible —
    lambdas, walrus operators, decorators, f-strings, unicode, etc. all
    introduce new symbols), we keep only a small safe character set and
    replace everything else. This can never leave a stray structural
    character behind, no matter what Python syntax is thrown at it."""
    text = text.replace("\n", " ").replace("\r", " ")
    # Friendly substitutions first, so common code still reads naturally
    text = (
        text.replace('"', "'")
        .replace("{", "(").replace("}", ")")
        .replace("[", "(").replace("]", ")")
        .replace("|", "/").replace("\\", "/")
    )
    # Whitelist strip: anything left that isn't in the safe set becomes a space
    text = _MERMAID_SAFE_CHARS.sub(" ", text)
    text = " ".join(text.split())  # collapse repeated spaces left by stripping
    return text


def _safe_unparse(node: ast.AST) -> str:
    """ast.unparse with a fallback, Mermaid-safe escaping, and a hard
    length cap for display. Escaping happens before truncation so the
    cut is always safe."""
    try:
        text = ast.unparse(node)
    except Exception:
        text = type(node).__name__
    text = " ".join(text.split())  # collapse newlines/indentation
    text = _mermaid_escape(text)
    if len(text) > 60:
        text = text[:57] + "..."
    return text


class AnalysisError(ValueError):
    """Raised when the submitted text isn't parseable Python."""


def _parse(code: str) -> ast.AST:
    try:
        return ast.parse(code)
    except SyntaxError as e:
        raise AnalysisError(f"Syntax error on line {e.lineno}: {e.msg}") from e


# ============================================================
# 1. FLOWCHART GENERATOR
# ------------------------------------------------------------
# Builds a structured graph (nodes + edges with row/col layout
# hints) that the frontend renders as SVG directly. No external
# diagramming library and no text-based diagram language — this
# sidesteps an entire class of "my label broke the parser" bugs
# and gives us full control over how it looks.
# ============================================================

def _clean_label(text: str) -> str:
    """Whitelist-based sanitizer. Keeps labels readable and safe
    for direct use as SVG <text> content, regardless of what odd
    Python syntax (lambdas, walrus, f-strings, decorators...)
    produced them."""
    text = text.replace("\n", " ").replace("\r", " ")
    text = (
        text.replace('"', "'")
        .replace("{", "(").replace("}", ")")
        .replace("[", "(").replace("]", ")")
    )
    text = _MERMAID_SAFE_CHARS.sub(" ", text)
    return " ".join(text.split())


def _label_for(node: ast.AST) -> str:
    try:
        text = ast.unparse(node)
    except Exception:
        text = type(node).__name__
    text = _clean_label(text)
    if len(text) > 52:
        text = text[:49] + "..."
    return text


class _FlowchartGraphBuilder:
    """Walks the AST and emits nodes/edges with a row (vertical
    position, by creation order) and col (horizontal branch
    offset) for a simple, readable top-down layout."""

    def __init__(self):
        self._counter = 0
        self._row = 0
        self.nodes: list[dict] = []
        self.edges: list[dict] = []

    def _new_node(self, label: str, shape: str, col: int) -> str:
        self._counter += 1
        self._row += 1
        nid = f"n{self._counter}"
        self.nodes.append({
            "id": nid, "shape": shape, "label": _clean_label(label),
            "row": self._row, "col": col,
        })
        return nid

    def _edge(self, a, b, label: str = ""):
        if a is None or b is None:
            return
        self.edges.append({"from": a, "to": b, "label": label})

    def build_block(self, stmts: list, entry: str, col: int, entry_label: str = ""):
        prev = entry
        label = entry_label
        for stmt in stmts:
            prev = self._build_stmt(stmt, prev, col, label)
            label = ""
            if prev is None:
                return None
        return prev

    def _build_stmt(self, stmt: ast.AST, prev: str, col: int, edge_label: str = ""):
        if isinstance(stmt, ast.If):
            cond = self._new_node(f"if {_label_for(stmt.test)}", "decision", col)
            self._edge(prev, cond, edge_label)
            merge = self._new_node("", "merge", col)
            true_exit = self.build_block(stmt.body, cond, col, "True")
            if true_exit:
                self._edge(true_exit, merge)
            if stmt.orelse:
                false_exit = self.build_block(stmt.orelse, cond, col + 1, "False")
                if false_exit:
                    self._edge(false_exit, merge)
            else:
                self._edge(cond, merge, "False")
            return merge

        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            loop = self._new_node(
                f"for {_label_for(stmt.target)} in {_label_for(stmt.iter)}", "decision", col
            )
            self._edge(prev, loop, edge_label)
            body_exit = self.build_block(stmt.body, loop, col + 1, "each item")
            if body_exit:
                self._edge(body_exit, loop, "next")
            after = self._new_node("", "merge", col)
            self._edge(loop, after, "done")
            return after

        elif isinstance(stmt, ast.While):
            cond = self._new_node(f"while {_label_for(stmt.test)}", "decision", col)
            self._edge(prev, cond, edge_label)
            body_exit = self.build_block(stmt.body, cond, col + 1, "True")
            if body_exit:
                self._edge(body_exit, cond, "loop")
            after = self._new_node("", "merge", col)
            self._edge(cond, after, "False")
            return after

        elif isinstance(stmt, ast.Try):
            node = self._new_node("try", "process", col)
            self._edge(prev, node, edge_label)
            body_exit = self.build_block(stmt.body, node, col)
            merge = self._new_node("", "merge", col)
            if body_exit:
                self._edge(body_exit, merge)
            for handler in stmt.handlers:
                exc_name = handler.type
                label = f"except {_label_for(exc_name)}" if exc_name else "except"
                h_node = self._new_node(label, "decision", col + 1)
                self._edge(node, h_node)
                h_exit = self.build_block(handler.body, h_node, col + 1)
                if h_exit:
                    self._edge(h_exit, merge)
            return merge

        elif isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
            n = self._new_node(_label_for(stmt), "terminal", col)
            self._edge(prev, n, edge_label)
            return None  # terminates this branch

        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            n = self._new_node(f"def {stmt.name}(...)", "process", col)
            self._edge(prev, n, edge_label)
            return n

        else:
            n = self._new_node(_label_for(stmt), "process", col)
            self._edge(prev, n, edge_label)
            return n


def generate_flowchart(code: str, max_nodes: int = 60) -> dict:
    """
    Returns {"nodes": [...], "edges": [...], "function_analyzed": str, "truncated": bool}.
    Picks the first function found, or falls back to module-level code.
    Frontend renders this directly as SVG — no external diagram library.
    """
    tree = _parse(code)
    body = tree.body
    target_name = "module"
    funcs = [n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if funcs:
        target = funcs[0]
        body = target.body
        target_name = target.name

    builder = _FlowchartGraphBuilder()
    start = builder._new_node(f"Start: {target_name}", "start", 0)
    exit_node = builder.build_block(body, start, 0)
    if exit_node:
        end = builder._new_node("End", "start", 0)
        builder._edge(exit_node, end)

    truncated = len(builder.nodes) > max_nodes
    if truncated:
        keep_ids = {n["id"] for n in builder.nodes[:max_nodes]}
        builder.nodes = builder.nodes[:max_nodes]
        builder.edges = [e for e in builder.edges if e["from"] in keep_ids and e["to"] in keep_ids]

    return {
        "nodes": builder.nodes,
        "edges": builder.edges,
        "function_analyzed": target_name,
        "truncated": truncated,
    }


# ============================================================
# 2. ISSUE / CODE-SMELL DETECTOR
# ============================================================

@dataclass
class Issue:
    line: int
    severity: str  # "warning" | "info" | "error"
    category: str
    message: str
    fix: str = ""
    learn_more: list = field(default_factory=list)


# Curated, static reference material per issue/optimization category.
# No network calls — this is just a lookup table shipped with the app.
RESOURCES: dict[str, dict] = {
    "mutable-default": {
        "fix": "def f(items=None):\n    if items is None:\n        items = []\n    ...",
        "learn_more": [
            {"title": "Python docs — Default Argument Values",
             "url": "https://docs.python.org/3/tutorial/controlflow.html#default-argument-values"},
        ],
    },
    "bare-except": {
        "fix": "try:\n    risky_call()\nexcept ValueError as e:\n    handle(e)",
        "learn_more": [
            {"title": "Python docs — Errors and Exceptions",
             "url": "https://docs.python.org/3/tutorial/errors.html"},
        ],
    },
    "silent-except": {
        "fix": "try:\n    risky_call()\nexcept ValueError as e:\n    logging.warning(\"failed: %s\", e)",
        "learn_more": [
            {"title": "Python docs — logging module",
             "url": "https://docs.python.org/3/library/logging.html"},
        ],
    },
    "unused-import": {
        "fix": "# remove the import, or use it — e.g.\nimport json\ndata = json.loads(text)",
        "learn_more": [
            {"title": "Python docs — the import system",
             "url": "https://docs.python.org/3/reference/import.html"},
        ],
    },
    "dangerous-call": {
        "fix": "import ast\nvalue = ast.literal_eval(user_input)  # safe subset instead of eval()",
        "learn_more": [
            {"title": "Python docs — ast.literal_eval",
             "url": "https://docs.python.org/3/library/ast.html#ast.literal_eval"},
        ],
    },
    "none-comparison": {
        "fix": "if value is None:\n    ...",
        "learn_more": [
            {"title": "PEP 8 — Programming Recommendations",
             "url": "https://peps.python.org/pep-0008/#programming-recommendations"},
        ],
    },
    "deep-nesting": {
        "fix": "def f(x):\n    if not x:\n        return None\n    # early return removes one level of nesting\n    ...",
        "learn_more": [
            {"title": "Refactoring Guru — Replace Nested Conditional with Guard Clauses",
             "url": "https://refactoring.guru/replace-nested-conditional-with-guard-clauses"},
        ],
    },
    "long-function": {
        "fix": "def f(x):\n    step1 = _prepare(x)\n    step2 = _process(step1)\n    return _finalize(step2)",
        "learn_more": [
            {"title": "Refactoring Guru — Extract Function",
             "url": "https://refactoring.guru/extract-method"},
        ],
    },
    "long-signature": {
        "fix": "@dataclass\nclass Options:\n    a: int\n    b: int\n    c: int\n\ndef f(opts: Options):\n    ...",
        "learn_more": [
            {"title": "Python docs — dataclasses",
             "url": "https://docs.python.org/3/library/dataclasses.html"},
        ],
    },
    "shadow-builtin": {
        "fix": "def my_list(items):  # renamed from list(...)\n    ...",
        "learn_more": [
            {"title": "Python docs — Built-in Functions",
             "url": "https://docs.python.org/3/library/functions.html"},
        ],
    },
    "global-statement": {
        "fix": "def f(state):\n    state['count'] += 1\n    return state",
        "learn_more": [
            {"title": "Python docs — the global statement",
             "url": "https://docs.python.org/3/reference/simple_stmts.html#the-global-statement"},
        ],
    },
    "list-comprehension": {
        "fix": "result = [x * 2 for x in items if x > 0]",
        "learn_more": [
            {"title": "Python docs — List Comprehensions",
             "url": "https://docs.python.org/3/tutorial/datastructures.html#list-comprehensions"},
        ],
    },
    "string-join": {
        "fix": "pieces = []\nfor t in tags:\n    pieces.append(t)\nresult = ''.join(pieces)",
        "learn_more": [
            {"title": "Python docs — str.join",
             "url": "https://docs.python.org/3/library/stdtypes.html#str.join"},
        ],
    },
    "enumerate": {
        "fix": "for i, val in enumerate(items):\n    ...",
        "learn_more": [
            {"title": "Python docs — enumerate()",
             "url": "https://docs.python.org/3/library/functions.html#enumerate"},
        ],
    },
    "set-membership": {
        "fix": "valid = set(some_list)\nif x in valid:\n    ...",
        "learn_more": [
            {"title": "Python docs — set objects",
             "url": "https://docs.python.org/3/library/stdtypes.html#set"},
        ],
    },
    "memoize": {
        "fix": "from functools import lru_cache\n\n@lru_cache(maxsize=None)\ndef fib(n):\n    if n <= 1:\n        return n\n    return fib(n-1) + fib(n-2)",
        "learn_more": [
            {"title": "Python docs — functools.lru_cache",
             "url": "https://docs.python.org/3/library/functools.html#functools.lru_cache"},
        ],
    },
}


def _attach_resources(category: str) -> dict:
    info = RESOURCES.get(category)
    if not info:
        return {"fix": "", "learn_more": []}
    return {"fix": info.get("fix", ""), "learn_more": info.get("learn_more", [])}


_BUILTIN_NAMES = set(dir(builtins))


class _IssueVisitor(ast.NodeVisitor):
    def __init__(self, source_lines: list[str]):
        self.issues: list[Issue] = []
        self.source_lines = source_lines
        self._imported_names: dict[str, int] = {}
        self._used_names: set[str] = set()

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            self._imported_names[name] = node.lineno
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        for alias in node.names:
            if alias.name == "*":
                continue
            name = alias.asname or alias.name
            self._imported_names[name] = node.lineno
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Load):
            self._used_names.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._check_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._check_function(node)
        self.generic_visit(node)

    def _check_function(self, node):
        # mutable default arguments
        defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d]
        for d in defaults:
            if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                self.issues.append(Issue(
                    node.lineno, "warning", "mutable-default",
                    f"Function '{node.name}' uses a mutable default argument "
                    f"({type(d).__name__.lower()}). It's shared across all calls — "
                    f"use `None` and initialize inside the function instead."
                ))

        # too many arguments
        n_args = len(node.args.args) + len(node.args.kwonlyargs)
        if n_args > 5:
            self.issues.append(Issue(
                node.lineno, "info", "long-signature",
                f"Function '{node.name}' takes {n_args} arguments — consider "
                f"grouping related ones into a dataclass or config object."
            ))

        # long function
        end_line = getattr(node, "end_lineno", node.lineno)
        length = end_line - node.lineno
        if length > 50:
            self.issues.append(Issue(
                node.lineno, "info", "long-function",
                f"Function '{node.name}' is {length} lines long — consider "
                f"splitting it into smaller helper functions."
            ))

        # shadowing a builtin with the function name
        if node.name in _BUILTIN_NAMES:
            self.issues.append(Issue(
                node.lineno, "warning", "shadow-builtin",
                f"Function name '{node.name}' shadows a Python builtin."
            ))

        # nesting depth inside this function
        depth = self._max_depth(node)
        if depth > 4:
            self.issues.append(Issue(
                node.lineno, "warning", "deep-nesting",
                f"Function '{node.name}' has nesting depth {depth} — deeply "
                f"nested code is hard to read; consider early returns or "
                f"extracting inner blocks into helper functions."
            ))

    def _max_depth(self, node, current=0) -> int:
        best = current
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.If, ast.For, ast.While, ast.Try,
                                   ast.With, ast.AsyncFor, ast.AsyncWith)):
                best = max(best, self._max_depth(child, current + 1))
            else:
                best = max(best, self._max_depth(child, current))
        return best

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        if node.type is None:
            self.issues.append(Issue(
                node.lineno, "warning", "bare-except",
                "Bare `except:` catches everything, including "
                "KeyboardInterrupt and SystemExit — catch a specific "
                "exception type instead."
            ))
        # silent swallow: except ...: pass
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            self.issues.append(Issue(
                node.lineno, "warning", "silent-except",
                "Exception is caught and silently ignored (`pass`) — "
                "this can hide real bugs. Log it or handle it explicitly."
            ))
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare):
        for op, comparator in zip(node.ops, node.comparators):
            if isinstance(op, (ast.Eq, ast.NotEq)) and (
                (isinstance(comparator, ast.Constant) and comparator.value is None)
            ):
                self.issues.append(Issue(
                    node.lineno, "info", "none-comparison",
                    "Use `is None` / `is not None` instead of `==`/`!=` "
                    "when comparing to None."
                ))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec"):
            self.issues.append(Issue(
                node.lineno, "warning", "dangerous-call",
                f"`{node.func.id}()` executes arbitrary code — avoid it, "
                f"especially on untrusted input."
            ))
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global):
        self.issues.append(Issue(
            node.lineno, "info", "global-statement",
            f"`global {', '.join(node.names)}` makes state harder to "
            f"trace — consider passing values as arguments/return values."
        ))
        self.generic_visit(node)


def detect_issues(code: str) -> list[dict]:
    tree = _parse(code)
    lines = code.splitlines()
    visitor = _IssueVisitor(lines)
    visitor.visit(tree)

    unused = [
        (name, ln) for name, ln in visitor._imported_names.items()
        if name not in visitor._used_names
    ]
    for name, ln in unused:
        visitor.issues.append(Issue(
            ln, "info", "unused-import",
            f"'{name}' is imported but never used."
        ))

    visitor.issues.sort(key=lambda i: i.line)
    out = []
    for i in visitor.issues:
        d = asdict(i)
        d.update(_attach_resources(i.category))
        out.append(d)
    return out


# ============================================================
# 3. COMPLEXITY / BIG-O ESTIMATOR
# ============================================================

_DECISION_NODES = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
    ast.With, ast.AsyncWith, ast.BoolOp, ast.Assert,
)


def _cyclomatic_complexity(node: ast.AST) -> int:
    """McCabe-style: 1 + number of decision points."""
    count = 1
    for n in ast.walk(node):
        if isinstance(n, _DECISION_NODES):
            count += 1
        if isinstance(n, ast.comprehension):
            count += 1 + len(n.ifs)
    return count


def _max_loop_nesting(node: ast.AST) -> int:
    def depth(n, cur=0):
        best = cur
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.For, ast.AsyncFor, ast.While)):
                best = max(best, depth(child, cur + 1))
            else:
                best = max(best, depth(child, cur))
        return best
    return depth(node)


def _is_recursive(func_node) -> bool:
    name = func_node.name
    for n in ast.walk(func_node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name:
            return True
    return False


def _has_memoization(func_node) -> bool:
    src = ""
    try:
        src = ast.unparse(func_node)
    except Exception:
        pass
    return "lru_cache" in src or "cache" in src.lower() or "memo" in src.lower()


def _big_o_guess(func_node) -> str:
    if _is_recursive(func_node) and not _has_memoization(func_node):
        return "O(2^n) (exponential) — unmemoized recursion; consider @lru_cache or an iterative/DP rewrite"
    if _is_recursive(func_node):
        return "O(n) or better — recursive but memoized"
    depth = _max_loop_nesting(func_node)
    if depth == 0:
        return "O(1) — no loops"
    if depth == 1:
        return "O(n) — single loop"
    if depth == 2:
        return "O(n\u00b2) — nested loops"
    if depth == 3:
        return "O(n\u00b3) — triple-nested loops"
    return f"O(n^{depth}) — {depth} levels of nested loops"


def compute_complexity(code: str) -> list[dict]:
    tree = _parse(code)
    results = []
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    if not funcs:
        cc = _cyclomatic_complexity(tree)
        results.append({
            "name": "<module>",
            "line": 1,
            "cyclomatic_complexity": cc,
            "rating": _rate(cc),
            "big_o": _big_o_guess(tree) if hasattr(tree, "name") else _module_big_o(tree),
            "lines": len(code.splitlines()),
        })
        return results

    for f in funcs:
        cc = _cyclomatic_complexity(f)
        end_line = getattr(f, "end_lineno", f.lineno)
        results.append({
            "name": f.name,
            "line": f.lineno,
            "cyclomatic_complexity": cc,
            "rating": _rate(cc),
            "big_o": _big_o_guess(f),
            "lines": end_line - f.lineno + 1,
        })
    return results


def _module_big_o(tree) -> str:
    depth = _max_loop_nesting(tree)
    return {0: "O(1)", 1: "O(n)", 2: "O(n\u00b2)"}.get(depth, f"O(n^{depth})")


def _rate(cc: int) -> str:
    if cc <= 5:
        return "simple"
    if cc <= 10:
        return "moderate"
    if cc <= 20:
        return "complex"
    return "very complex — strongly consider refactoring"


# ============================================================
# 4. OPTIMIZATION SUGGESTER (pattern-based, static, offline)
# ============================================================

@dataclass
class Suggestion:
    line: int
    title: str
    detail: str
    category: str = ""


class _OptimizeVisitor(ast.NodeVisitor):
    def __init__(self):
        self.suggestions: list[Suggestion] = []

    def visit_For(self, node: ast.For):
        self._check_append_loop(node)
        self._check_string_concat_loop(node)
        self._check_range_len(node)
        self._check_membership_in_list(node)
        self.generic_visit(node)

    def _check_append_loop(self, node: ast.For):
        # for x in y: result.append(f(x))  [optionally guarded by an if]
        body = node.body
        target_call = None
        if len(body) == 1 and isinstance(body[0], ast.Expr):
            target_call = body[0].value
        elif (len(body) == 1 and isinstance(body[0], ast.If)
              and len(body[0].body) == 1 and isinstance(body[0].body[0], ast.Expr)):
            target_call = body[0].body[0].value

        if (isinstance(target_call, ast.Call)
                and isinstance(target_call.func, ast.Attribute)
                and target_call.func.attr == "append"):
            self.suggestions.append(Suggestion(
                node.lineno, "Use a list comprehension",
                "This loop just appends one value per iteration — a list "
                "comprehension (or generator expression) is typically "
                "faster and more Pythonic than repeated `.append()` calls.",
                "list-comprehension"
            ))

    def _check_string_concat_loop(self, node: ast.For):
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.AugAssign) and isinstance(stmt.op, ast.Add):
                # heuristic: target is a plain Name, likely a string accumulator
                if isinstance(stmt.target, ast.Name):
                    self.suggestions.append(Suggestion(
                        stmt.lineno, "Avoid string concatenation in a loop",
                        "Repeated `+=` on a string inside a loop is O(n\u00b2) "
                        "because strings are immutable. Collect pieces in a "
                        "list and use `''.join(pieces)` once at the end.",
                        "string-join"
                    ))
                    break  # one hint per loop is enough

    def _check_range_len(self, node: ast.For):
        it = node.iter
        if (isinstance(it, ast.Call) and isinstance(it.func, ast.Name)
                and it.func.id == "range" and len(it.args) == 1
                and isinstance(it.args[0], ast.Call)
                and isinstance(it.args[0].func, ast.Name)
                and it.args[0].func.id == "len"):
            self.suggestions.append(Suggestion(
                node.lineno, "Use enumerate() instead of range(len(...))",
                "`for i in range(len(x))` followed by `x[i]` is usually "
                "clearer and slightly faster as `for i, val in enumerate(x)`.",
                "enumerate"
            ))

    def _check_membership_in_list(self, node: ast.For):
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Compare):
                for op, comp in zip(stmt.ops, stmt.comparators):
                    if isinstance(op, (ast.In, ast.NotIn)) and isinstance(comp, ast.Name):
                        self.suggestions.append(Suggestion(
                            stmt.lineno, "Consider a set for membership tests",
                            "`x in some_list` inside a loop is O(n) per check "
                            "(O(n\u00b2) overall). If `some_list` doesn't need "
                            "order/duplicates, use a `set` for O(1) lookups.",
                            "set-membership"
                        ))
                        return

    def visit_FunctionDef(self, node: ast.FunctionDef):
        if _is_recursive(node) and not _has_memoization(node):
            self.suggestions.append(Suggestion(
                node.lineno, f"Consider memoizing '{node.name}'",
                "This function calls itself without caching results. If "
                "it's a pure function (same input -> same output), add "
                "`@functools.lru_cache(maxsize=None)` above the def to "
                "avoid recomputing the same values exponentially.",
                "memoize"
            ))
        self.generic_visit(node)

    def visit_ListComp(self, node: ast.ListComp):
        # list comp only used to check membership → could be `any()`
        self.generic_visit(node)


def suggest_optimizations(code: str) -> list[dict]:
    tree = _parse(code)
    visitor = _OptimizeVisitor()
    visitor.visit(tree)
    # de-duplicate identical (line, title) pairs
    seen = set()
    out = []
    for s in sorted(visitor.suggestions, key=lambda s: s.line):
        key = (s.line, s.title)
        if key in seen:
            continue
        seen.add(key)
        d = asdict(s)
        d.update(_attach_resources(s.category))
        out.append(d)
    return out


# ============================================================
# 5. LOCAL "EXPLAIN" ENGINE (rule-based, no LLM, no API call)
# ============================================================
#
# This describes STRUCTURE mechanically (loops, branches, returns) by
# walking the AST — it can't infer high-level intent the way an LLM
# can ("this implements Dijkstra's algorithm"), but it's completely
# free, instant, private, and never depends on an external API being
# up or funded.

MAX_STMTS_TO_DESCRIBE = 14


def _plain_unparse(node) -> str:
    """Like _safe_unparse but for prose, not Mermaid — keeps real quotes."""
    if node is None:
        return ""
    try:
        text = ast.unparse(node)
    except Exception:
        text = type(node).__name__
    text = " ".join(text.split())
    if len(text) > 80:
        text = text[:77] + "..."
    return text


def _format_args(args: ast.arguments) -> str:
    parts = [a.arg for a in args.args]
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    for a in args.kwonlyargs:
        parts.append(a.arg)
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)
    return ", ".join(parts)


def _describe_stmt(stmt: ast.AST, indent: int = 0) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []

    if isinstance(stmt, ast.Assign):
        targets = ", ".join(_plain_unparse(t) for t in stmt.targets)
        lines.append(f"{pad}- Sets {targets} to `{_plain_unparse(stmt.value)}`.")

    elif isinstance(stmt, ast.AnnAssign):
        ann = f" ({_plain_unparse(stmt.annotation)})" if stmt.annotation else ""
        val = f", set to `{_plain_unparse(stmt.value)}`" if stmt.value else " (declared, not assigned)"
        lines.append(f"{pad}- {_plain_unparse(stmt.target)}{ann}{val}.")

    elif isinstance(stmt, ast.AugAssign):
        lines.append(f"{pad}- Updates {_plain_unparse(stmt.target)} using `{_plain_unparse(stmt.target)} {_op_symbol(stmt.op)}= {_plain_unparse(stmt.value)}`.")

    elif isinstance(stmt, ast.If):
        lines.append(f"{pad}- If `{_plain_unparse(stmt.test)}`:")
        for s in stmt.body:
            lines.extend(_describe_stmt(s, indent + 1))
        if stmt.orelse:
            lines.append(f"{pad}- Otherwise:")
            for s in stmt.orelse:
                lines.extend(_describe_stmt(s, indent + 1))

    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
        lines.append(f"{pad}- For each {_plain_unparse(stmt.target)} in `{_plain_unparse(stmt.iter)}`:")
        for s in stmt.body:
            lines.extend(_describe_stmt(s, indent + 1))

    elif isinstance(stmt, ast.While):
        lines.append(f"{pad}- While `{_plain_unparse(stmt.test)}`:")
        for s in stmt.body:
            lines.extend(_describe_stmt(s, indent + 1))

    elif isinstance(stmt, ast.Try):
        lines.append(f"{pad}- Tries:")
        for s in stmt.body:
            lines.extend(_describe_stmt(s, indent + 1))
        for h in stmt.handlers:
            exc = _plain_unparse(h.type) if h.type else "any exception"
            lines.append(f"{pad}- If {exc} occurs:")
            for s in h.body:
                lines.extend(_describe_stmt(s, indent + 1))
        if stmt.finalbody:
            lines.append(f"{pad}- Finally (always runs):")
            for s in stmt.finalbody:
                lines.extend(_describe_stmt(s, indent + 1))

    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        items = ", ".join(_plain_unparse(i.context_expr) for i in stmt.items)
        lines.append(f"{pad}- Opens a context with `{items}`:")
        for s in stmt.body:
            lines.extend(_describe_stmt(s, indent + 1))

    elif isinstance(stmt, ast.Return):
        val = f"`{_plain_unparse(stmt.value)}`" if stmt.value is not None else "nothing"
        lines.append(f"{pad}- Returns {val}.")

    elif isinstance(stmt, ast.Raise):
        exc = _plain_unparse(stmt.exc) if stmt.exc else "the current exception"
        lines.append(f"{pad}- Raises `{exc}`.")

    elif isinstance(stmt, ast.Break):
        lines.append(f"{pad}- Breaks out of the loop.")

    elif isinstance(stmt, ast.Continue):
        lines.append(f"{pad}- Skips ahead to the next loop iteration.")

    elif isinstance(stmt, ast.Pass):
        lines.append(f"{pad}- Does nothing here (`pass`).")

    elif isinstance(stmt, ast.Global):
        lines.append(f"{pad}- Refers to the global variable(s) {', '.join(stmt.names)}.")

    elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        lines.append(f"{pad}- Defines a nested function `{stmt.name}({_format_args(stmt.args)})`.")

    elif isinstance(stmt, ast.ClassDef):
        lines.append(f"{pad}- Defines a nested class `{stmt.name}`.")

    elif isinstance(stmt, ast.Expr):
        lines.append(f"{pad}- Runs `{_plain_unparse(stmt.value)}`.")

    else:
        lines.append(f"{pad}- {type(stmt).__name__.replace('_', ' ')}.")

    return lines


_OP_SYMBOLS = {
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
    ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**",
    ast.LShift: "<<", ast.RShift: ">>", ast.BitOr: "|",
    ast.BitAnd: "&", ast.BitXor: "^",
}


def _op_symbol(op: ast.AST) -> str:
    return _OP_SYMBOLS.get(type(op), "")


def explain_code(code: str) -> str:
    """Fully local, rule-based plain-English walkthrough. No network call."""
    tree = _parse(code)
    lines: list[str] = []

    mod_doc = ast.get_docstring(tree)
    if mod_doc:
        lines.append(mod_doc.strip().splitlines()[0])
        lines.append("")

    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    if imports:
        parts = []
        for imp in imports:
            if isinstance(imp, ast.Import):
                parts.append(", ".join(a.asname or a.name for a in imp.names))
            else:
                mod = imp.module or "."
                names = ", ".join(a.asname or a.name for a in imp.names)
                parts.append(f"{names} from {mod}")
        lines.append("Imports: " + "; ".join(parts) + ".")
        lines.append("")

    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    handled_ids = {id(n) for n in imports + funcs + classes}
    other_top = [n for n in tree.body if id(n) not in handled_ids]

    complexity_by_name = {c["name"]: c for c in compute_complexity(code)}
    issues = detect_issues(code)

    for fn in funcs:
        lines.append(f"def {fn.name}({_format_args(fn.args)}):")
        doc = ast.get_docstring(fn)
        if doc:
            lines.append(f'  "{doc.strip().splitlines()[0]}"')

        comp = complexity_by_name.get(fn.name)
        if comp:
            lines.append(
                f"  Estimated: {comp['big_o']} "
                f"(cyclomatic complexity {comp['cyclomatic_complexity']}, {comp['rating']})."
            )

        end_line = getattr(fn, "end_lineno", fn.lineno)
        fn_issues = [i for i in issues if fn.lineno <= i["line"] <= end_line]
        if fn_issues:
            cats = ", ".join(sorted({i["category"] for i in fn_issues}))
            lines.append(f"  Flagged by Find issues: {cats} — see that tab for fixes.")

        body = fn.body
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]  # skip docstring, already shown above

        for stmt in body[:MAX_STMTS_TO_DESCRIBE]:
            lines.extend(_describe_stmt(stmt, indent=1))
        if len(body) > MAX_STMTS_TO_DESCRIBE:
            lines.append(f"  ... plus {len(body) - MAX_STMTS_TO_DESCRIBE} more statement(s) not shown.")
        lines.append("")

    for cls in classes:
        bases = ", ".join(_plain_unparse(b) for b in cls.bases)
        lines.append(f"class {cls.name}" + (f"({bases})" if bases else "") + ":")
        doc = ast.get_docstring(cls)
        if doc:
            lines.append(f'  "{doc.strip().splitlines()[0]}"')
        methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if methods:
            for m in methods:
                lines.append(f"  method {m.name}({_format_args(m.args)})")
        else:
            lines.append("  (no methods)")
        lines.append("")

    if other_top:
        lines.append("Module-level code:")
        for stmt in other_top[:MAX_STMTS_TO_DESCRIBE]:
            lines.extend(_describe_stmt(stmt, indent=1))
        if len(other_top) > MAX_STMTS_TO_DESCRIBE:
            lines.append(f"  ... plus {len(other_top) - MAX_STMTS_TO_DESCRIBE} more statement(s) not shown.")
        lines.append("")

    if not funcs and not classes and not other_top and not imports:
        lines.append("This snippet doesn't contain any statements to explain.")

    return "\n".join(lines).strip()

def _one_line_description(stmt) -> str:
    """Single-sentence description of one statement, for the line-by-line view."""
    if isinstance(stmt, ast.Assign):
        targets = ", ".join(_plain_unparse(t) for t in stmt.targets)
        return f"Sets {targets} to {_plain_unparse(stmt.value)}."
    elif isinstance(stmt, ast.AnnAssign):
        ann = f" ({_plain_unparse(stmt.annotation)})" if stmt.annotation else ""
        val = f", set to {_plain_unparse(stmt.value)}" if stmt.value else " (declared, not assigned)"
        return f"{_plain_unparse(stmt.target)}{ann}{val}."
    elif isinstance(stmt, ast.AugAssign):
        return (f"Updates {_plain_unparse(stmt.target)} using "
                f"{_plain_unparse(stmt.target)} {_op_symbol(stmt.op)}= {_plain_unparse(stmt.value)}.")
    elif isinstance(stmt, ast.If):
        return f"Checks whether {_plain_unparse(stmt.test)}."
    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
        return f"Loops over {_plain_unparse(stmt.iter)}, using {_plain_unparse(stmt.target)} each time."
    elif isinstance(stmt, ast.While):
        return f"Repeats while {_plain_unparse(stmt.test)} stays true."
    elif isinstance(stmt, ast.Try):
        return "Starts a block that watches for errors below."
    elif isinstance(stmt, ast.ExceptHandler):
        exc = _plain_unparse(stmt.type) if stmt.type else "any exception"
        return f"Handles {exc}."
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        items = ", ".join(_plain_unparse(i.context_expr) for i in stmt.items)
        return f"Opens a context with {items}."
    elif isinstance(stmt, ast.Return):
        val = _plain_unparse(stmt.value) if stmt.value is not None else None
        return f"Returns {val}." if val else "Returns nothing (exits the function)."
    elif isinstance(stmt, ast.Raise):
        exc = _plain_unparse(stmt.exc) if stmt.exc else "the current exception"
        return f"Raises {exc}."
    elif isinstance(stmt, ast.Break):
        return "Exits the nearest loop immediately."
    elif isinstance(stmt, ast.Continue):
        return "Skips ahead to the next loop iteration."
    elif isinstance(stmt, ast.Pass):
        return "Does nothing here (placeholder)."
    elif isinstance(stmt, ast.Global):
        return f"Refers to the global variable(s) {', '.join(stmt.names)}."
    elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return f"Defines a function {stmt.name}({_format_args(stmt.args)})."
    elif isinstance(stmt, ast.ClassDef):
        return f"Defines a class {stmt.name}."
    elif isinstance(stmt, ast.Expr):
        return f"Runs {_plain_unparse(stmt.value)}."
    else:
        return type(stmt).__name__.replace("_", " ") + "."


def _flatten_explain(stmts, source_lines, out, depth, max_lines):
    for stmt in stmts:
        if len(out) >= max_lines:
            return
        line_no = getattr(stmt, "lineno", None)
        code_text = source_lines[line_no - 1].strip() if line_no and 0 <= line_no - 1 < len(source_lines) else ""
        out.append({
            "line": line_no, "code": code_text,
            "explanation": _one_line_description(stmt), "depth": depth,
        })
        if isinstance(stmt, ast.If):
            _flatten_explain(stmt.body, source_lines, out, depth + 1, max_lines)
            if stmt.orelse:
                _flatten_explain(stmt.orelse, source_lines, out, depth + 1, max_lines)
        elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
            _flatten_explain(stmt.body, source_lines, out, depth + 1, max_lines)
        elif isinstance(stmt, ast.Try):
            _flatten_explain(stmt.body, source_lines, out, depth + 1, max_lines)
            for h in stmt.handlers:
                if len(out) >= max_lines:
                    return
                htext = source_lines[h.lineno - 1].strip() if 0 <= h.lineno - 1 < len(source_lines) else ""
                out.append({
                    "line": h.lineno, "code": htext,
                    "explanation": _one_line_description(h), "depth": depth,
                })
                _flatten_explain(h.body, source_lines, out, depth + 1, max_lines)
            if stmt.finalbody:
                _flatten_explain(stmt.finalbody, source_lines, out, depth + 1, max_lines)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _flatten_explain(stmt.body, source_lines, out, depth + 1, max_lines)


def explain_line_by_line(code: str, max_lines: int = 200) -> dict:
    """Flat, line-numbered version of the local explainer — one row per
    statement with its exact source line, for a code | explanation
    split view. Fully offline, same engine as explain_code()."""
    tree = _parse(code)
    source_lines = code.splitlines()
    body = tree.body
    target_name = "module"
    funcs = [n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if funcs:
        target = funcs[0]
        body = target.body
        target_name = target.name

    out: list[dict] = []
    _flatten_explain(body, source_lines, out, depth=0, max_lines=max_lines)
    return {
        "function_analyzed": target_name,
        "lines": out,
        "truncated": len(out) >= max_lines,
    }


def full_report(code: str) -> dict:
    return {
        "flowchart": generate_flowchart(code),
        "issues": detect_issues(code),
        "complexity": compute_complexity(code),
        "optimizations": suggest_optimizations(code),
    }
