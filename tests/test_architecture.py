"""Pacing code must have no way to reach the carrier, the allocator, or the safety controller."""
import ast
import pathlib

FORBIDDEN = ("allocator", "safety", "providers", "lifecycle", "recovery", "engine", "db", "repo", "health")


def test_pacing_cannot_import_anything_that_can_place_a_call():
    for f in pathlib.Path("dialer/pacing").glob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [(node.module or "")] + [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for n in names:
                parts = n.replace("..", ".").split(".")
                assert not any(p in FORBIDDEN for p in parts), f"{f}: imports {n}"
