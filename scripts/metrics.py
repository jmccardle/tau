"""Write the Rust metrics data behind docs/as-built/metrics/README.md.

    scripts/metrics.sh      # runs this plus the external tools

Outputs in docs/as-built/metrics/data: rust.json (every function, type and
file, src and tests apart) and summary.json (distributions, comparable with
tau015-python.json from scripts/py_baseline.py).
"""

import json
import sys

import rustmetrics as rm

CC_WARN, CC_LIMIT = 10, 15
OUT = rm.ROOT / "docs/as-built/metrics/data"


def summarize(m: dict) -> dict:
    out = {}
    for kind in ("src", "tests"):
        fns = m[kind]["functions"]
        files = [f for f in m["files"] if f["kind"] == kind]
        s = {
            "files": len(files),
            "total_lines": sum(f["lines"] for f in files),
            "cc": rm.dist([f["cc"] for f in fns], (10, 15, 20, 40)),
            "cognitive": rm.dist([f["cognitive"] for f in fns], (10, 15, 25)),
            "function_loc": rm.dist([f["loc"] for f in fns], (50, 100)),
            "file_lines": rm.dist([f["lines"] for f in files], (500, 800, 1500)),
            "top_cc": sorted(fns, key=lambda f: (-f["cc"], f["file"]))[:15],
            "top_files": sorted(files, key=lambda f: -f["lines"])[:10],
        }
        if kind == "src":
            inherent = [t for t in m["src"]["types"] if t["impl"] == "inherent"]
            trait = [t for t in m["src"]["types"] if t["impl"] == "trait"]
            s["methods_per_type_inherent"] = rm.dist([t["methods"] for t in inherent], (25,))
            s["methods_per_type_trait"] = rm.dist([t["methods"] for t in trait], (25,))
            s["top_types"] = sorted(m["src"]["types"], key=lambda t: -t["methods"])[:10]
            s["per_crate"] = {}
            for crate in sorted({f["crate"] for f in files}):
                cf = [f for f in fns if f["crate"] == crate]
                cfiles = [f for f in files if f["crate"] == crate]
                s["per_crate"][crate] = {
                    "files": len(cfiles),
                    "lines": sum(f["lines"] for f in cfiles),
                    "functions": len(cf),
                    "max_cc": max((f["cc"] for f in cf), default=0),
                    "over_10": sum(1 for f in cf if f["cc"] > CC_WARN),
                    "over_15": sum(1 for f in cf if f["cc"] > CC_LIMIT),
                }
        out[kind] = s
    out["tool"] = rm.rca_version()
    return out


def main() -> int:
    m = rm.analyze()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "rust.json").write_text(json.dumps(m, indent=1) + "\n")
    (OUT / "summary.json").write_text(json.dumps(summarize(m), indent=1) + "\n")
    print(f"wrote {OUT.relative_to(rm.ROOT)}/rust.json and summary.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
