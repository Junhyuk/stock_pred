"""Keep CI tests independent of the live, daily-rewritten public data and of the wall clock.

The daily Actions run rewrites docs/data (and the shadow job appends to research/premarket_us_shadow),
so a test that reads those files directly passes today and fails after the next publication. Tests that
the workflows run must copy a pinned fixture into tmp_path, or be listed below with the reason the read
is stable. The same goes for bare wall-clock calls.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
LIVE_PREFIXES = ("docs/data", "research/premarket_us_shadow", "data/processed")
ROOT_NAMES = {"ROOT", "REPO", "REPO_ROOT"}
COPY_CALLS = {"copy", "copy2", "copyfile", "copytree"}
CLOCK_ATTRS = {"now", "utcnow", "today"}
CLOCK_OWNERS = {"datetime", "date", "Timestamp"}

# (test file, repo-relative path prefix) -> why this reference to a live-looking path is deliberate.
ALLOWED_LIVE_PATHS = {
    ("test_premarket_us_shadow_ops.py", "research/premarket_us_shadow/preregistration.json"):
        "frozen preregistration pinned by SHA-256; the shadow job only appends to ledger.jsonl",
    ("test_premarket_us_shadow_ops.py", "docs/data/should_not_exist.json"):
        "negative path handed to the CLI to prove it refuses; never read",
    ("test_probability_research.py", "docs/data/should_not_exist"):
        "negative path handed to _refuse_docs; never read",
}
# (test file, call) -> why a bare wall-clock call cannot go stale.
ALLOWED_CLOCK_CALLS = {
    ("test_market_indicators.py", "datetime.now"):
        "injected observations must be newer than any seed, so they are stamped with the current time",
    ("test_accuracy_calendar_audit.py", "datetime.now"):
        "measures a delayed fetch against the run start; only relative order is asserted",
}


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def workflow_test_files() -> set[str]:
    """Every tests/*.py named on a pytest line of a workflow, plus the test helpers they import."""
    names = set()
    for workflow in (ROOT / ".github/workflows").glob("*.yml"):
        for command in _strings(yaml.safe_load(workflow.read_text())):
            if "pytest" in command:
                names.update(Path(name).name for name in re.findall(r"tests/[\w./-]+\.py", command))
    for name in list(names):
        source = (TESTS / name).read_text()
        for helper in re.findall(r"^\s*(?:from|import) (test_\w+|automation_cli_harness)\b", source, re.M):
            names.add(f"{helper}.py")
    return names


def _chain(node):
    """Flatten `head / "a" / "b"` into (head, [segments]); a non-literal segment becomes None."""
    segments = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        segments.append(node.right.value if isinstance(node.right, ast.Constant)
                        and isinstance(node.right.value, str) else None)
        node = node.left
    return node, segments[::-1]


def _is_repo_root(head) -> bool:
    if isinstance(head, ast.Name):
        return head.id in ROOT_NAMES
    return any(isinstance(item, ast.Attribute) and item.attr in {"parents", "parent"}
               for item in ast.walk(head)) and any(
        isinstance(item, ast.Name) and item.id in {"__file__", "SCRIPT"} for item in ast.walk(head))


def _live_reads(tree):
    """Yield (line, repo-relative path) for each non-copy reference to a live data location."""
    copied = {id(arg) for call in ast.walk(tree) if isinstance(call, ast.Call)
              and getattr(call.func, "attr", getattr(call.func, "id", None)) in COPY_CALLS
              for arg in call.args}
    inner = {id(node.left) for node in ast.walk(tree)
             if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)}
    for node in ast.walk(tree):
        if id(node) in copied or id(node) in inner:
            continue
        path = None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            head, segments = _chain(node)
            if _is_repo_root(head) and None not in segments:
                path = "/".join(part.strip("/") for part in segments)
        elif (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Path" and node.args
                and all(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in node.args)):
            path = "/".join(arg.value.strip("/") for arg in node.args)
        if path and path.startswith(LIVE_PREFIXES):
            yield node.lineno, path


def _clock_calls(tree):
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner = node.func.value
        owner = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", None)
        if (node.func.attr in CLOCK_ATTRS and owner in CLOCK_OWNERS) or (
                owner == "time" and node.func.attr in {"time", "time_ns"}):
            yield node.lineno, f"{owner}.{node.func.attr}"


def test_workflow_tests_are_found_and_exist():
    names = workflow_test_files()
    assert {"test_market_indicators.py", "test_postclose_protocol.py",
            "test_premarket_us_shadow_ops.py"} <= names
    assert all((TESTS / name).is_file() for name in names), sorted(names)


def test_workflow_tests_do_not_read_live_public_data_unless_allowlisted():
    violations, used = [], set()
    for name in sorted(workflow_test_files()):
        for line, path in _live_reads(ast.parse((TESTS / name).read_text())):
            key = next((entry for entry in ALLOWED_LIVE_PATHS
                        if entry[0] == name and path.startswith(entry[1])), None)
            if key:
                used.add(key)
            else:
                violations.append(f"{name}:{line} reads live {path}; copy a pinned fixture to tmp_path")
    assert not violations, "\n".join(violations)
    stale = sorted(set(ALLOWED_LIVE_PATHS) - used)
    assert not stale, f"stale allowlist entries: {stale}"


def test_workflow_tests_do_not_use_the_wall_clock_unless_allowlisted():
    violations, used = [], set()
    for name in sorted(workflow_test_files()):
        for line, call in _clock_calls(ast.parse((TESTS / name).read_text())):
            if (name, call) in ALLOWED_CLOCK_CALLS:
                used.add((name, call))
            else:
                violations.append(f"{name}:{line} calls {call}(); inject a fixed clock instead")
    assert not violations, "\n".join(violations)
    stale = sorted(set(ALLOWED_CLOCK_CALLS) - used)
    assert not stale, f"stale allowlist entries: {stale}"


def test_guard_flags_the_patterns_that_broke_ci():
    def reads(code):
        return [path for _, path in _live_reads(ast.parse(code))]

    assert reads('seed = ROOT / "docs/data"') == ["docs/data"]
    assert reads('seed = ROOT / "docs" / "data" / "x.json"') == ["docs/data/x.json"]
    assert reads('seed = Path("docs/data/x.json")') == ["docs/data/x.json"]
    assert reads('p = Path(__file__).resolve().parents[1] / "research/premarket_us_shadow/ledger.jsonl"')
    assert not reads('shutil.copytree(ROOT / "docs/data", tmp_path / "seed")')
    assert not reads('p = checkout.root / "docs/data/x.json"')
    assert not reads('p = tmp_path / "docs/data"')
    assert [call for _, call in _clock_calls(ast.parse("n = datetime.now(UTC)"))] == ["datetime.now"]
    assert [call for _, call in _clock_calls(ast.parse("t = time.time()"))] == ["time.time"]
    assert not list(_clock_calls(ast.parse("n = Clock.now(UTC)")))
