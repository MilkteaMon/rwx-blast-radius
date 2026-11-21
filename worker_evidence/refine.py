"""Additional exclusions tied to baseline observations and the pinned library semantics."""
import ast
import copy
import json
from pathlib import Path
import re
from selector_v3 import ProfileIndex, base_tree, same


def simple_attribute(node, namespace, attr):
    return (isinstance(node, ast.Attribute) and node.attr == attr and
            isinstance(node.value, ast.Name) and node.value.id == namespace)


def boolean_flip(a, b):
    return (isinstance(a, ast.Constant) and isinstance(b, ast.Constant) and
            type(a.value) is bool and type(b.value) is bool and a.value != b.value)


def keyword_flip(a, b, keyword):
    if not isinstance(a, ast.Call) or not isinstance(b, ast.Call):
        return False
    left = [k for k in a.keywords if k.arg == keyword]
    right = [k for k in b.keywords if k.arg == keyword]
    if len(left) != 1 or len(right) != 1 or not boolean_flip(left[0].value, right[0].value):
        return False
    clone = copy.copy(b)
    clone.keywords = [left[0] if k.arg == keyword else k for k in b.keywords]
    return same(a, clone)


def imports(tree, module, symbol, alias):
    return any(isinstance(n, ast.ImportFrom) and n.module == module and
               any(x.name == symbol and (x.asname or x.name) == alias for x in n.names)
               for n in tree.body)


def fresh_copy(a, b, tree):
    if not (keyword_flip(a, b, "copy") and simple_attribute(a.func, "exp", "alias_") and
            (imports(tree, "sqlglot", "exp", "exp") or imports(tree, "sqlglot", "expressions", "exp"))):
        return False
    if len(a.args) != 1 or {k.arg for k in a.keywords} != {"alias", "copy"}:
        return False
    arg = a.args[0]
    return (isinstance(arg, ast.Call) and simple_attribute(arg.func, "exp", "table_") and
            len(arg.args) == 1 and not arg.keywords and isinstance(arg.args[0], (ast.Name, ast.Constant)))


def earlier_return(tree, line):
    for parent in ast.walk(tree):
        if not isinstance(parent, ast.FunctionDef):
            continue
        for prev, cur in zip(parent.body, parent.body[1:]):
            if (isinstance(cur, ast.If) and cur.lineno == line and cur.test.end_lineno == line and
                    isinstance(prev, ast.If) and not prev.orelse and len(prev.body) == 1 and
                    isinstance(prev.body[0], ast.Return) and prev.body[0].lineno == prev.body[0].end_lineno):
                return prev.body[0].lineno
    return None


def changed_schema(tree, new, line):
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef) or not cls.name.endswith("Properties"):
            continue
        for field in cls.body:
            if not (isinstance(field, ast.Assign) and len(field.targets) == 1 and
                    isinstance(field.targets[0], ast.Name) and field.targets[0].id == "arg_types" and
                    isinstance(field.value, ast.Dict) and field.lineno <= line <= field.end_lineno):
                continue
            others = [n for n in ast.walk(new) if isinstance(n, ast.Dict) and n.lineno == field.value.lineno]
            if len(others) != 1:
                continue
            old, current = ast.literal_eval(field.value), ast.literal_eval(others[0])
            if list(old) != list(current) or len(old) != len(field.value.keys) or len(current) != len(others[0].keys):
                continue
            changed = [k for k in old if old[k] is not current[k]]
            if len(changed) == 1 and old[changed[0]] is False and current[changed[0]] is True:
                return cls.name, changed[0]
    return None


class Refinement:
    def __init__(self, inventory, profile):
        self.profile = profile
        tests = json.loads(inventory.read_text("utf8"))["tests"]
        self.extra = ProfileIndex(Path(__file__).with_name("base_extra.json"), tests)
        self.context = {t["number"]: t["file"] for t in tests}
        self.escape_sites = [s for s in self.extra.sites if s.startswith("escape:")]

    def excluded(self, index, path, source, tree, new, removed, added, aligned, candidates, rules):
        prof = self.extra
        candidates = {n for n in candidates if n not in index.base.uncertain and n in prof.bits
                      and not prof.values("process", n) and "text" not in prof.values("read:" + path, n)}
        if not candidates:
            return set()
        if not removed and added and all(re.fullmatch(r"\s*# [A-Za-z][A-Za-z ,.'-]*\s*", s) for s in added):
            if same(tree, new):
                return {n for n in candidates if prof.values("read:" + path, n) == {"import"}}
        candidates = {n for n in candidates if n not in index.readers.get(path, set())
                      or prof.values("read:" + path, n) == {"import"}}
        if not (aligned and len(removed) == len(added) == 1 and candidates and path.startswith("sqlglot/")):
            return set()
        line = removed[0]
        for kind, _, keys in rules:
            if kind == "escape_key":
                for node in ast.walk(tree):
                    if isinstance(node, ast.Dict) and node.lineno <= line <= node.end_lineno:
                        cfg = json.dumps(ast.literal_eval(node), sort_keys=True, separators=(",", ":"))
                        # Require the refined map to have observed every globally recorded key on
                        # this test, so a missing subprocess or reader cannot justify an exclusion.
                        other = "escape:" + cfg
                        return {n for n in candidates if self.covered_keys(n) and
                                set(keys).isdisjoint(prof.values(other, n))}

        old_nodes = [n for n in ast.walk(tree) if getattr(n, "lineno", None) == line]
        new_nodes = {(type(n), n.col_offset): n for n in ast.walk(new) if getattr(n, "lineno", None) == line}
        for a in old_nodes:
            b = new_nodes.get((type(a), a.col_offset))
            if not b or a.end_lineno != line or b.end_lineno != line:
                continue
            if isinstance(a, ast.Call):
                # Compare the entire changed line's statement as well as its call: a
                # neighbouring change on the same physical line must retain the tests.
                if fresh_copy(a, b, tree) and self.only_edit(source, line, added[0], "copy", a, b):
                    return candidates
                if (keyword_flip(a, b, "target") and isinstance(a.func, ast.Name) and a.func.id == "_show_parser"
                        and path.startswith("sqlglot/parsers/") and imports(tree, "sqlglot.parsers.mysql", "_show_parser", "_show_parser")
                        and len(a.args) == 1 and isinstance(a.args[0], ast.Constant) and type(a.args[0].value) is str
                        and self.only_edit(source, line, added[0], "target", a, b)
                        and "show:handler" in prof.sites):
                    return {n for n in candidates if a.args[0].value not in prof.values("show:handler", n)
                            and (prof.values("show:handler", n) or self.line_hit(index, path, line, n))}
            if isinstance(a, ast.Compare) and isinstance(b, ast.Compare):
                if (len(a.ops) == len(b.ops) == 1 and {type(a.ops[0]), type(b.ops[0])} == {ast.Eq, ast.NotEq}
                        and source.splitlines()[line-1].strip().startswith("if ")
                        and re.sub(r"==|!=", "CMP", source.splitlines()[line-1]).rstrip() ==
                            re.sub(r"==|!=", "CMP", added[0]).rstrip()
                        and sum(source.splitlines()[line-1].count(v) for v in ("==", "!=")) == 1):
                    prev = earlier_return(tree, line)
                    if prev:
                        return {n for n in candidates if self.line_hit(index, path, prev, n)
                                and not self.line_hit(index, path, line, n)}
        if path.startswith("sqlglot/expressions/") and source.splitlines()[line-1].replace("False","True").rstrip() == added[0].rstrip():
            schema = changed_schema(tree, new, line)
            if schema:
                cls, key = schema
                site = "valid:" + path[:-3].replace("/", ".") + "." + cls
                # In the pinned library only the missing-field check consumes the
                # required flag. Retain tests that inspect the schema in their source.
                if site in prof.sites:
                    result = set()
                    for n in candidates:
                        test_source = (index.source / self.context[n]).read_text("utf8")
                        if re.search(r"\b(?:arg_types|required_args)\b", test_source):
                            continue
                        states = prof.values(site, n)
                        if (states or self.visited_validator(index, n)) and all(
                            key in json.loads(state) for state in states
                        ):
                            result.add(n)
                    return result
        return set()

    def covered_keys(self, n):
        observed = set().union(*(self.extra.values(s, n) for s in self.escape_sites))
        original = self.profile.values("tokenizer:escape-key", n)
        return bool(original) and original <= observed

    @classmethod
    def visited_validator(cls, index, n):
        path = "sqlglot/expressions/core.py"
        _, tree = base_tree(index.source / path)
        return any(cls.line_hit(index, path, node.lineno, n) for node in ast.walk(tree)
                   if isinstance(node, ast.For) and simple_attribute(node.iter, "self", "required_args"))

    @staticmethod
    def line_hit(index, path, line, n):
        mask = index.mask(index.data["lines"].get(path, {}), str(line))
        return n in index.hits(mask)

    @staticmethod
    def only_edit(source, line, added, keyword, a, b):
        old = next(k.value.value for k in a.keywords if k.arg == keyword)
        new = next(k.value.value for k in b.keywords if k.arg == keyword)
        new_line, count = re.subn(rf"\b{keyword}\s*=\s*{old}\b", lambda m: m[0].replace(str(old), str(new)),
                                  source.splitlines()[line-1])
        return count == 1 and new_line.rstrip() == added.rstrip()
