"""Select from base coverage only. All sample answers are outside the selector."""
import argparse
import ast
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from select_workers import DependencyIndex, schedule


@dataclass
class Options:
    comments: bool = False
    lines: bool = False
    config: bool = False
    receiver: bool = False
    keep_source_readers: bool = False
    whole_conditions: bool = False


def same_shape(before, after):
    if len(before) != len(after):
        return False
    return all(re.match(r"[ \t]*", old)[0] == re.match(r"[ \t]*", new)[0]
               for old, new in zip(before, after))


def apply_file(old, chunk):
    source = old.splitlines(keepends=True)
    result, removed, added, groups = [], [], [], []
    cursor = 0
    hunks = re.split(r"(?m)^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@[^\n]*\n", chunk)
    if len(hunks) == 1:
        raise ValueError("Unsupported diff")
    for start, hunk in zip(hunks[1::2], hunks[2::2]):
        pos = max(0, int(start) - 1)
        if pos < cursor:
            raise ValueError("Overlapping hunks")
        result.extend(source[cursor:pos])
        cursor = pos
        old_group, new_group = [], []
        for line in hunk.splitlines(keepends=True):
            if line[0] in " -":
                if cursor >= len(source) or source[cursor].rstrip("\r\n") != line[1:].rstrip("\r\n"):
                    raise ValueError("Base text mismatch")
                if line[0] == "-":
                    removed.append(cursor + 1)
                    old_group.append(source[cursor])
                else:
                    if old_group or new_group:
                        groups.append((old_group, new_group))
                        old_group, new_group = [], []
                    result.append(source[cursor])
                cursor += 1
            elif line[0] == "+":
                added.append(line[1:])
                new_group.append(line[1:])
                result.append(line[1:])
            else:
                raise ValueError("Unsupported diff line")
        if old_group or new_group:
            groups.append((old_group, new_group))
    aligned = all(same_shape(before, after) for before, after in groups)
    return "".join(result + source[cursor:]), removed, added, aligned


@lru_cache(maxsize=32)
def base_tree(path):
    text = path.read_text("utf-8-sig")
    return text, ast.parse(text, type_comments=True)


def config_location(tree, removed):
    found = []
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for n in cls.body:
            if isinstance(n, (ast.Assign, ast.AnnAssign)) and all(n.lineno <= i <= n.end_lineno for i in removed):
                target = n.targets[0] if isinstance(n, ast.Assign) else n.target
                if isinstance(target, ast.Name):
                    found.append((n.end_lineno - n.lineno, cls.name, target.id))
    return min(found)[1:] if found else None


class Experiment:
    def __init__(self, inventory, folder, source):
        tests = json.loads(inventory.read_text("utf-8"))["tests"]
        self.times = {t["number"]: round(t["seconds"] * 1000) for t in tests}
        dependencies = json.loads((folder / "dependencies.json").read_text("utf-8"))
        self.base = DependencyIndex(dependencies, tests)
        self.data = json.loads((folder / "lines.json").read_text("utf-8"))
        self.masks = [int(v, 16) for v in self.data["masks"]]
        current = {t["id"]: t["number"] for t in tests}
        self.ids = [current.get(test) for test in self.data["test_ids"]]
        self.readers = {}
        for t in dependencies["tests"]:
            if t["id"] in current:
                for filename in t["reads"]:
                    self.readers.setdefault(filename, set()).add(current[t["id"]])
        self.source = source

    def hits(self, mask):
        return {n for i, n in enumerate(self.ids) if n is not None and mask & (1 << i)}

    def mask(self, mapping, key):
        return self.masks[mapping[key]] if key in mapping else 0

    def file_selection(self, chunk, flags):
        previous, _ = self.base.select(chunk)
        oldpath = re.search(r"(?m)^--- a/([^\n]+)$", chunk)
        newpath = re.search(r"(?m)^\+\+\+ b/([^\n]+)$", chunk)
        if not oldpath or not newpath or oldpath[1] != newpath[1]:
            return self.base.all
        path = oldpath[1]
        if not path.endswith(".py") or not any(vars(flags).values()):
            return previous
        # Only access source paths present in the pinned checkout's scope index.
        if path not in self.base.scopes:
            return self.base.all
        try:
            source, tree = base_tree(self.source / path)
            updated, removed, added, aligned = apply_file(source, chunk)
            new_tree = ast.parse(updated, type_comments=True)
        except (OSError, ValueError, SyntaxError):
            return self.base.all
        edited = added + [source.splitlines()[i-1] for i in removed]
        if (flags.comments and edited and all(not x.strip() or x.lstrip().startswith("#") for x in edited)
                and ast.dump(tree) == ast.dump(new_tree)):
            return self.readers.get(path, set()) if flags.keep_source_readers else set()
        if not removed or not aligned:
            return previous
        line_data = self.data["lines"].get(path, {})
        masks = [self.mask(line_data, str(i)) for i in removed]
        if flags.lines and all(masks):
            bodies = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
            if all(any((n.body.lineno if isinstance(n, ast.Lambda) else n.body[0].lineno)
                       <= line <= n.end_lineno for n in bodies) for line in removed):
                mask = 0
                for part in masks:
                    mask |= part
                if flags.whole_conditions:
                    # A short-circuited operand can become reachable after a change.
                    # Include visits to the original condition, including earlier operands.
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.If, ast.IfExp, ast.While)):
                            condition = node.test
                        elif isinstance(node, (ast.BoolOp, ast.Compare)):
                            condition = node
                        else:
                            continue
                        if any(condition.lineno <= i <= condition.end_lineno for i in removed):
                            for i in range(condition.lineno, condition.end_lineno + 1):
                                mask |= self.mask(line_data, str(i))
                return self.hits(mask)
        if flags.config:
            config = config_location(tree, removed)
            if config:
                name, attribute = config
                read = self.mask(self.data["attributes"], attribute)
                if read:
                    readers = self.hits(read)
                    if flags.receiver:
                        # Static type references and observed dialect-family files provide
                        # additional evidence about the receiver, including indirect use.
                        family = self.hits(self.mask(self.data["names"], name))
                        parts = Path(path).parts
                        if len(parts) == 3 and parts[1] in {"dialects", "parsers", "generators", "typing"}:
                            for sub in ("dialects", "parsers", "generators", "typing"):
                                family.update(self.base.files.get(f"sqlglot/{sub}/{parts[-1]}", set()))
                            family.update(self.base.files.get(f"tests/dialects/test_{parts[-1]}", set()))
                        if readers & family:
                            return readers & family
                    return readers
        return previous

    def predict(self, text, flags):
        blocks = re.split(r"(?m)(?=^diff --git )", text)
        files = [b for b in blocks if b.startswith("diff --git ")]
        selected = set(self.base.uncertain)
        for chunk in files:
            selected.update(self.file_selection(chunk, flags))
        return schedule(selected if files else self.base.all, self.times)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("data", type=Path)
    parser.add_argument("source", type=Path, help="base sqlglot checkout at eb11aec")
    parser.add_argument("--maps", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--broad-config", action="store_true", help="do not narrow by likely receiver type")
    parser.add_argument("--skip-source-readers", action="store_true")
    args = parser.parse_args()
    index = Experiment(args.data / "tests.json", args.maps, args.source)
    for patch in sorted((args.data / "commits").glob("*.patch")):
        flags = Options(comments=True, lines=True, config=True, receiver=not args.broad_config,
                        keep_source_readers=not args.skip_source_readers, whole_conditions=True)
        workers, _ = index.predict(patch.read_text("utf-8"), flags)
        print(json.dumps(dict(commit=patch.stem, workers=workers), separators=(",", ":")))
