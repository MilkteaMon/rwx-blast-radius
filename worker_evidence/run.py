import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "zero_worker"))
from select_static import run
from refine import Refinement

if __name__ == "__main__":
    data, source = map(Path, sys.argv[1:3])
    out = Path(sys.argv[3]).open("w", encoding="utf8", newline="\n") if len(sys.argv) > 3 else sys.stdout
    try:
        for row in run("all-durations", data, source, Refinement):
            print(json.dumps(row, separators=(",", ":")), file=out)
    finally:
        if out is not sys.stdout:
            out.close()
