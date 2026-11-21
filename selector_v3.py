"""Refine V2 using base observations. No answers or commit identifiers enter the rules."""
import argparse
import ast
import copy
import json
import re
from pathlib import Path

from selector_v2 import Experiment, Options, apply_file, base_tree
from select_workers import schedule

flags = Options(True, True, True, True, True, True)


def same(a, b):
    if isinstance(a, ast.AST) and isinstance(b, ast.AST):
        return ast.dump(a) == ast.dump(b)
    return a == b


def differing_branch(old, new):
    """Descend only while a single field or single list element differs."""
    while isinstance(old, ast.AST) and isinstance(new, ast.AST):
        yield old, new
        if type(old) != type(new):
            return
        pairs = []
        for name, left in ast.iter_fields(old):
            right = getattr(new, name)
            if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
                pairs.extend((a, b) for a, b in zip(left, right) if not same(a, b))
            elif not same(left, right):
                pairs.append((left, right))
        if len(pairs) != 1:
            return
        old, new = pairs[0]


def first_term(node):
    while isinstance(node, ast.BoolOp):
        node = node.values[0]
    return node


class ProfileIndex:
    def __init__(self, file, tests):
        data = json.loads(file.read_text("utf8"))
        positions = {node_id: i for i, node_id in enumerate(data["ids"])}
        self.bits = {t["number"]: 1 << positions[t["id"]] for t in tests if t["id"] in positions}
        self.sites = {site: {v: int(m, 16) for v, m in states.items()}
                      for site, states in data["sites"].items()}

    def values(self, site, n):
        return {v for v, m in self.sites.get(site, {}).items() if m & self.bits.get(n, 0)}


def derive_rules(path, old_tree, new_tree):
    rules = []
    chain = list(differing_branch(old_tree, new_tree))
    for i, (a, b) in enumerate(chain):
        loc = f"{path}:{a.lineno}:{a.col_offset}" if hasattr(a, "lineno") else ""
        if isinstance(a, ast.Compare) and isinstance(b, ast.Compare) and len(a.ops) == len(b.ops) == 1:
            changed = copy.copy(b)
            changed.ops = a.ops
            if same(a, changed):
                if isinstance(a.ops[0], ast.Is) and isinstance(b.ops[0], ast.Eq):
                    rhs = a.comparators[0]
                    is_none = isinstance(rhs, ast.Constant) and rhs.value is None
                    rules.append(("identity", loc + ":cmp", "None" if is_none else None))
                if isinstance(a.ops[0], ast.Lt) and isinstance(b.ops[0], ast.LtE):
                    rules.append(("boundary", loc + ":cmp", None))
        if isinstance(a, ast.BoolOp) and isinstance(b, ast.BoolOp):
            parent = chain[i-1][0] if i else None
            if (isinstance(parent, (ast.If, ast.IfExp, ast.While)) and parent.test is a
                    and len(a.values) == len(b.values) == 2 and all(isinstance(v, ast.Name) for v in a.values)
                    and type(a.op) != type(b.op) and all(same(l, r) for l, r in zip(a.values, b.values))):
                rules.append(("truth_pair", loc + ":pair", None))
            fn = next((n for n, _ in reversed(chain[:i]) if isinstance(n, ast.FunctionDef)), None)
            if (fn and a.end_lineno >= a.lineno + 2 and not isinstance(a.values[0], ast.BoolOp)
                    and same(a.values[0], first_term(b))):
                rules.append(("unreached", loc + ":area", f"{path}:{fn.lineno}:{fn.col_offset}:fn"))
        if (isinstance(a, ast.Assign) and isinstance(b, ast.Assign) and len(a.targets) == 1
                and isinstance(a.targets[0], ast.Name) and a.targets[0].id == "NUMERIC_ESCAPES"
                and len(b.targets) == 1 and same(a.targets[0], b.targets[0])
                and path.startswith("sqlglot/dialects/") and isinstance(a.value, ast.Dict)
                and isinstance(b.value, ast.Dict)):
            try:
                left, right = ast.literal_eval(a.value), ast.literal_eval(b.value)
            except (ValueError, TypeError, SyntaxError):
                continue
            # Only existing value edits; size, key order, truth and membership stay fixed.
            if (list(left) == list(right) and len(left) == len(a.value.keys) == len(b.value.keys)
                    and all(type(v) is str for v in left)
                    and all(type(v) is tuple and len(v) == 4 and all(type(x) is int for x in v)
                            for v in (*left.values(), *right.values()))):
                changed_keys = sorted(k for k in left if left[k] != right[k])
                if changed_keys:
                    rules.append(("escape_key", "tokenizer:escape-key", changed_keys))
    return rules


def exclusion(rule, profile, n):
    kind, site, param = rule
    values = profile.values(site, n)
    if kind == "identity" and param == "None" and "1?" in values:
        # An identity match with literal None fixes both operands, even on
        # Python builds where NoneType owns its own comparison slot.
        values = (values - {"1?"}) | {"11"}
    if kind in {"identity", "truth_pair"}:
        return bool(values) and values <= {"00", "11"}
    if kind == "boundary":
        return bool(values) and values <= {"L", "G"}
    if kind == "unreached":
        return not values and bool(profile.values(param, n))
    if kind == "escape_key":
        return bool(profile.values("tokenizer:entry", n)) and values.isdisjoint(param)
    return False


def run(data, source, maps):
    tests = json.loads((data / "tests.json").read_text("utf8"))["tests"]
    profile = ProfileIndex(maps / "base_profiles.json", tests)
    index = Experiment(data / "tests.json", maps, source)
    for patch in sorted((data / "commits").glob("*.patch")):
        text = patch.read_text("utf8")
        chunks = [s for s in re.split(r"(?m)(?=^diff --git )", text) if s.startswith("diff --git ")]
        selected = set(index.base.uncertain)
        for chunk in chunks:
            base_selection = index.file_selection(chunk, flags)
            if not base_selection:
                continue
            rules = []
            old = re.search(r"(?m)^--- a/([^\n]+)$", chunk)
            new = re.search(r"(?m)^\+\+\+ b/([^\n]+)$", chunk)
            path = old[1] if old else ""
            if old and new and path == new[1] and path in index.base.scopes and path.endswith(".py"):
                try:
                    original, tree = base_tree(source / path)
                    updated = apply_file(original, chunk)[0]
                    if updated != original:
                        rules = derive_rules(path, tree, ast.parse(updated, type_comments=True))
                except (ValueError, SyntaxError, OSError):
                    rules = []
            protect = index.readers.get(path, set()) | index.base.uncertain
            selected.update(n for n in base_selection if n in protect or index.times[n] < 10
                            or not any(exclusion(rule, profile, n) for rule in rules))
        workers, _ = schedule(selected if chunks else index.base.all, index.times)
        yield {"commit": patch.stem, "workers": workers}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("data", type=Path)
    p.add_argument("source", type=Path)
    args = p.parse_args()
    for row in run(args.data, args.source, Path(__file__).resolve().parent):
        print(json.dumps(row, separators=(",", ":")))
