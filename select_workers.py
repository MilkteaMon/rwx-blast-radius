"""Read only the patch directory, test inventory and precomputed base-revision map."""
import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path


def edits(patch):
    changes = defaultdict(list)
    file = None
    cursor = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            cursor = None
        elif cursor is None and line.startswith("+++ "):
            file = line[4:].removeprefix("b/")
            changes[file]
        elif line.startswith("@@ "):
            cursor = int(re.match(r"@@ -(\d+)", line)[1])
        elif cursor is not None:
            if line.startswith("-"):
                changes[file].append((cursor,))
                cursor += 1
            elif line.startswith("+"):
                changes[file].append((max(1, cursor-1), cursor))
            elif line.startswith(" "):
                cursor += 1
    return changes


class DependencyIndex:
    def __init__(self, data, tests):
        number = {t["id"]: t["number"] for t in tests}
        self.all = set(number.values())
        self.files, self.functions = defaultdict(set), defaultdict(set)
        self.uncertain = set(self.all)
        self.scopes = data["scopes"]
        for t in data["tests"]:
            if t["id"] not in number:
                continue
            n = number[t["id"]]
            if t["status"] == "passed":
                self.uncertain.discard(n)
            for file in t["files"]:
                self.files[file].add(n)
            for i in t["functions"]:
                file, _, symbol = data["symbols"][i]
                self.functions[file, symbol.replace(".<locals>.", ".")].add(n)

    def select(self, patch):
        result, reasons = set(self.uncertain), []
        changes = edits(patch)
        if not changes:
            return self.all, ["无可定位改动，回退全量"]
        for file, edits_in_file in changes.items():
            if not file.endswith(".py"):
                hits = self.files[file]
                result.update(hits or self.all)
                reasons.append(file + (" · 文件" if hits else " · 无记录，回退全量"))
                continue
            spans = sorted(self.scopes.get(file, []), key=lambda s: s[1] - s[0])
            targets = set()
            for points in edits_in_file:
                scope = next((name for a, b, name in spans if all(a <= p <= b for p in points)), None)
                targets.add(scope)
            if not targets or None in targets:
                result.update(self.all)
                reasons.append(file + " · 含函数外改动，回退全量")
                continue
            for target in sorted(targets):
                hits = self.functions[file, target]
                result.update(hits or self.files[file] or self.all)
                kind = "函数" if hits else ("回退文件" if self.files[file] else "回退全量")
                reasons.append(f"{file} · {target} · {kind}")
        return result, reasons


def schedule(selected, durations):
    ordered = sorted(selected, key=lambda n: (-durations[n], n))
    if not ordered:
        return [], 0
    best, best_cost = [], math.inf
    for k in range(1, min(8, len(ordered)) + 1):
        workers = [[] for _ in range(k)]
        loads = [0] * k
        for n in ordered:
            w = min(range(k), key=lambda i: (loads[i], i))
            workers[w].append(n)
            loads[w] += durations[n]
        occupied = [w for w in workers if w]
        cost = max(loads) + 2000 * len(occupied)
        if cost < best_cost:
            best, best_cost = occupied, cost
    return best, best_cost


def run(data_dir, map_path):
    tests = json.loads((data_dir / "tests.json").read_text("utf-8"))["tests"]
    durations = {t["number"]: math.floor(1000 * t["seconds"] + 0.5) for t in tests}
    index = DependencyIndex(json.loads(map_path.read_text("utf-8")), tests)
    for path in sorted((data_dir / "commits").glob("*.patch")):
        selected, reasons = index.select(path.read_text("utf-8"))
        workers, cost = schedule(selected, durations)
        yield path.stem, workers, cost, reasons


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--map", type=Path, default=Path(__file__).with_name("dependencies.json"))
    args = parser.parse_args()
    for commit, workers, _, _ in run(args.data_dir, args.map):
        print(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))
