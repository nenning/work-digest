"""Run the draw.io vision comparison with several models over saved image pairs and print the results.

Pairs are PNG files named <name>.old.png / <name>.new.png in one directory. Uses provider, key and
endpoint from config.yaml's llm block; bypasses the spec monitor cache. Every call is a real, billed
API request (2 images + prompt each), so keep the pair count small.

    python scripts/compare_vision_models.py pairs/ --models claude-sonnet-5-5,claude-opus-5-5
    python scripts/compare_vision_models.py pairs/ --models a,b --runs 3 --language en
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digest.config import VisionLLMConfig, load_config  # noqa: E402
from digest.spec_monitor import vision  # noqa: E402


def find_pairs(directory: Path):
    pairs = []
    for old in sorted(directory.glob("*.old.png")):
        name = old.name[: -len(".old.png")]
        new = directory / f"{name}.new.png"
        if new.exists():
            pairs.append((name, old.read_bytes(), new.read_bytes()))
        else:
            print(f"skipping {old.name}: no {new.name}", file=sys.stderr)
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pairs_dir", type=Path)
    ap.add_argument("--models", required=True, help="comma-separated model names")
    ap.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "digest" / "config.yaml")
    ap.add_argument("--runs", type=int, default=1, help="repeat each pair/model to see run-to-run variance")
    ap.add_argument("--language", default=None, help="output language (default: config language)")
    args = ap.parse_args()

    config = load_config(args.config)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    pairs = find_pairs(args.pairs_dir)
    if not pairs or not models:
        print("need at least one <name>.old.png/<name>.new.png pair and one model", file=sys.stderr)
        return 2
    print(f"{len(pairs)} pair(s) x {len(models)} model(s) x {args.runs} run(s) = "
          f"{len(pairs) * len(models) * args.runs} billed calls", file=sys.stderr)

    timeout = config.spec_monitor.llm_timeout if config.spec_monitor else max(config.llm.llm_timeout * 3, 90)
    language = args.language or config.language
    for name, old, new in pairs:
        print(f"\n=== {name} ===")
        for model in models:
            vcfg = VisionLLMConfig(provider=config.llm.provider, api_key=config.llm.api_key, model=model,
                                   endpoint=config.llm.endpoint, timeout=timeout)
            for run in range(1, args.runs + 1):
                start = time.monotonic()
                try:
                    diff = vision.compare_images(old, new, diagram_name=name, context="", vision=vcfg,
                                                 language=language, cache=None)
                except vision.VisionError as exc:
                    print(f"[{model} #{run}] ERROR: {exc}")
                    continue
                print(f"[{model} #{run}] {time.monotonic() - start:.1f}s, confidence={diff.confidence}")
                print(f"  {diff.summary}")
                for change in diff.changes:
                    print(f"  - {change}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
