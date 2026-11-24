"""Verify an observation archive and restore only selected time-series and trace blocks."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess

TEMPO_CONFIG = """server:
  http_listen_port: 3200
distributor:
  receivers:
    otlp:
      protocols:
        grpc:
          endpoint: 127.0.0.1:4317
        http:
          endpoint: 127.0.0.1:4318
storage:
  trace:
    backend: local
    wal:
      path: /var/tempo/wal
    local:
      path: /var/tempo/blocks
overrides:
  defaults:
    compaction:
      block_retention: 2160h
usage_report:
  reporting_enabled: false
"""


def extract(tool, archive, destination, *args):
    subprocess.run([tool, "-xf", str(archive), "-C", str(destination), *args], check=True)


def ranges(args):
    metadata = args.out / "metadata"
    metadata.mkdir(parents=True)
    extract(args.tar, args.archive, metadata, "--include", "prometheus/*/meta.json", "--include", "tempo/*/*/meta.json")
    prom_paths = list((metadata / "prometheus").glob("*/meta.json"))
    tempo_paths = list((metadata / "tempo").rglob("meta.json"))
    prom_window = {"start": datetime.fromisoformat(args.prom_start).timestamp(),
                   "end": datetime.fromisoformat(args.prom_end).timestamp()}
    trace_window = {"start": datetime.fromisoformat(args.trace_start).timestamp(),
                    "end": datetime.fromisoformat(args.trace_end).timestamp()}
    selected = []
    inventories = {}
    for category, paths, window in (("prometheus", prom_paths, prom_window), ("tempo", tempo_paths, trace_window)):
        blocks = []
        for path in paths:
            meta = json.loads(path.read_bytes())
            if category == "prometheus":
                start, end = meta["minTime"] / 1000, meta["maxTime"] / 1000
            else:
                start, end = (datetime.fromisoformat(meta[key]).timestamp() for key in ("startTime", "endTime"))
            blocks.append((start, end))
            if start < window["end"] and end > window["start"]:
                selected.append(path.parent.relative_to(metadata).as_posix())
        inventories[category] = {"blocks": len(blocks), "start_epoch": min((x[0] for x in blocks), default=None),
                                 "end_epoch": max((x[1] for x in blocks), default=None)}
    return inventories, selected


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--tar", default="tar", help="bsdtar executable with zstd support")
    parser.add_argument("--prom-start", default="2026-11-10T00:00:00+09:00")
    parser.add_argument("--prom-end", default="2026-11-13T00:00:00+09:00")
    parser.add_argument("--trace-start", default="2026-11-11T07:14:50+09:00")
    parser.add_argument("--trace-end", default="2026-11-11T07:17:10+09:00")
    args = parser.parse_args()
    expected = Path(f"{args.archive}.sha256").read_text(encoding="utf-8").split()[0].lower()
    with args.archive.open("rb") as file:
        actual = hashlib.file_digest(file, "sha256").hexdigest()
    if actual != expected:
        parser.error("Archive SHA-256 did not match its sidecar")
    args.out.mkdir(parents=True)
    inventory, selected = ranges(args)
    if selected:
        index = args.out / "blocks.txt"
        index.write_text("\n".join(selected) + "\n", encoding="ascii")
        extract(args.tar, args.archive, args.out, "-T", str(index.resolve()))
        (args.out / "tempo.yaml").write_text(TEMPO_CONFIG, encoding="ascii")
    result = {"archive": args.archive.name, "sha256": actual, "inventory": inventory, "selected": selected}
    (args.out / "inventory.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
