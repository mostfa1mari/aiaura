"""Guard: AI AURA never references order-execution functionality.

Scans every AI AURA Python file (services/, scripts/, apps/ — the vendored
library itself is excluded, it merely *contains* the code we must never call)
and asserts no identifier matching the order-execution surface is used in
actual code. Docstrings and comments are ignored (AST-based), so documenting
the blocklist stays legal while calling it never is.
"""

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ["services", "scripts", "apps"]
VENDOR_MARKER = f"{PROJECT_ROOT.name}-vendor"

FORBIDDEN_IDENTIFIERS = {
    "buy",
    "buyv3",
    "Buyv3",
    "Buyv3_by_raw_expired",
    "check_win",
    "get_order_result",
    "openOrder",
    "open_order",
}


def aiaura_python_files():
    for directory in SCAN_DIRS:
        base = PROJECT_ROOT / directory
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "vendor" in path.parts:
                continue
            yield path


def forbidden_uses(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
        if name in FORBIDDEN_IDENTIFIERS:
            hits.append((path, node.lineno, name))
    return hits


def test_files_are_scanned_at_all():
    files = list(aiaura_python_files())
    assert len(files) >= 5, f"scanner found too few files: {files}"


def test_no_order_execution_identifiers_in_aiaura_code():
    hits = []
    for path in aiaura_python_files():
        hits.extend(forbidden_uses(path))
    assert not hits, f"order-execution identifiers found in AI AURA code: {hits}"


def test_provider_exposes_no_order_methods():
    from services.market_data.pocket_option_provider import (
        PocketOptionMarketDataProvider,
    )
    from services.market_data.provider import MarketDataProvider

    for cls in (MarketDataProvider, PocketOptionMarketDataProvider):
        exposed = set(dir(cls))
        overlap = exposed & FORBIDDEN_IDENTIFIERS
        assert not overlap, f"{cls.__name__} exposes order methods: {overlap}"
