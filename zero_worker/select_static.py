"""Read a patch and base-revision evidence. Each invocation produces one batch of JSONL."""
import argparse
import ast
import json
from pathlib import Path
import re
import sys

RELEASE = Path(__file__).resolve().parents[1] / "rwx-submit"
sys.path.insert(0, str(RELEASE))
from selector_v3 import (
    Experiment, ProfileIndex, apply_file, base_tree, derive_rules, exclusion,
    flags, run as release_run, schedule,
)


def rules_for(chunk, source, index, refinement=None, candidates=()):
    old = re.search(r"(?m)^--- a/([^\n]+)$", chunk)
    new = re.search(r"(?m)^\+\+\+ b/([^\n]+)$", chunk)
    path = old[1] if old else ""
    if not (new and path == new[1] and path.endswith(".py") and path in index.base.scopes):
        return path, [], set()
    try:
        original, tree = base_tree(source / path)
        updated, removed, added, aligned = apply_file(original, chunk)
        if updated != original:
            new = ast.parse(updated, type_comments=True)
            rules = derive_rules(path, tree, new)
            excluded = (refinement.excluded(index, path, original, tree, new, removed, added,
                                             aligned, candidates, rules) if refinement else set())
            return path, rules, excluded
    except (ValueError, SyntaxError, OSError):
        pass
    return path, [], set()


def run(mode, data, source, refinement_factory=None):
    if mode == "v3":
        yield from release_run(data, source, RELEASE)
        return
    inventory = data / "tests.json"
    profile = ProfileIndex(RELEASE / "base_profiles.json", json.loads(inventory.read_text("utf8"))["tests"])
    index = Experiment(inventory, RELEASE, source)
    refinement = refinement_factory(inventory, profile) if refinement_factory else None
    for patch in sorted((data / "commits").glob("*.patch")):
        text = patch.read_text("utf8")
        chunks = [s for s in re.split(r"(?m)(?=^diff --git )", text) if s.startswith("diff --git ")]
        selected = set(index.base.uncertain if chunks else index.base.all)
        short_excluded = set()
        for chunk in chunks:
            candidates = index.file_selection(chunk, flags)
            if not candidates:
                continue
            path, rules, excluded = rules_for(chunk, source, index,
                                             refinement if len(chunks) == 1 else None, candidates)
            protected = index.readers.get(path, set()) | index.base.uncertain
            for n in candidates:
                if n in excluded:
                    continue
                if n in protected or not any(exclusion(rule, profile, n) for rule in rules):
                    selected.add(n)
                elif mode == "empty-only" and index.times[n] < 10:
                    short_excluded.add(n)
        # In empty-only mode exactly reproduce V3 on all nonempty changes.
        if mode == "empty-only" and selected:
            selected.update(short_excluded)
        yield {"commit": patch.stem, "workers": schedule(selected, index.times)[0]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("v3", "empty-only", "all-durations"))
    parser.add_argument("data", type=Path)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path, nargs="?")
    args = parser.parse_args()
    stream = args.output.open("w", encoding="utf8", newline="\n") if args.output else sys.stdout
    try:
        for row in run(args.mode, args.data, args.source):
            print(json.dumps(row, separators=(",", ":")), file=stream)
    finally:
        if stream is not sys.stdout:
            stream.close()
