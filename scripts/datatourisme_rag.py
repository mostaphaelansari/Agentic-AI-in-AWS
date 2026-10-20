#!/usr/bin/env python3
"""Download, prepare, and ingest a structured DATAtourisme Paris corpus."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag.datatourisme import (  # noqa: E402
    DATASET_API_URL,
    S3_PREFIX,
    read_paris_pois,
    sha256_file,
    write_prepared_corpus,
)
from rag.ingest import IngestError, ingest_prepared  # noqa: E402

DEFAULT_RESOURCE_TITLE = "datatourisme-reg-idf.csv"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def resolve_resource_url(api_url: str = DATASET_API_URL) -> str:
    """Resolve the current stable-titled Île-de-France CSV resource."""
    request = urllib.request.Request(api_url, headers={"User-Agent": "aws-agentic-blueprint/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        dataset = json.load(response)
    matches = [
        resource
        for resource in dataset.get("resources") or []
        if resource.get("title") == DEFAULT_RESOURCE_TITLE and resource.get("format") == "csv"
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one {DEFAULT_RESOURCE_TITLE!r} resource, found {len(matches)}"
        )
    url = str(matches[0].get("url") or "")
    if not url.startswith("https://"):
        raise RuntimeError("DATAtourisme resource URL is missing or not HTTPS")
    return url


def download(url: str, output: Path) -> dict:
    """Atomically download a resource without leaving a partial target."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "aws-agentic-blueprint/1"})
            with urllib.request.urlopen(request, timeout=120) as response:
                while block := response.read(1024 * 1024):
                    temporary.write(block)
            temporary.flush()
            temporary_path.replace(output)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
    return {
        "path": str(output),
        "url": url,
        "bytes": output.stat().st_size,
        "sha256": sha256_file(output),
    }


def _download(args: argparse.Namespace) -> None:
    url = args.url or resolve_resource_url(args.api_url)
    print(json.dumps(download(url, args.output), indent=2))


def _prepare(args: argparse.Namespace) -> None:
    pois = read_paris_pois(args.csv)
    manifest = write_prepared_corpus(
        pois,
        args.output,
        corpus_version=args.corpus_version,
        snapshot_date=args.snapshot_date,
        source_url=args.source_url or str(args.csv),
        source_checksum=sha256_file(args.csv),
    )
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


def _ingest(args: argparse.Namespace) -> None:
    result = ingest_prepared(
        args.prepared,
        prefix=S3_PREFIX,
        bucket=args.bucket,
        namespace=args.namespace,
        knowledge_base_id=args.knowledge_base_id,
        data_source_id=args.data_source_id,
        region=args.region,
        workers=args.workers,
        dry_run=args.dry_run,
        wait=not args.no_wait,
        progress=lambda line: print(line, flush=True),
    )
    print(json.dumps(result, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    download_parser = subparsers.add_parser(
        "download", help="Download the current official Île-de-France CSV"
    )
    download_parser.add_argument("--output", required=True, type=Path)
    download_parser.add_argument("--url", help="Use an exact resource URL instead of resolving it")
    download_parser.add_argument("--api-url", default=DATASET_API_URL)
    download_parser.set_defaults(func=_download)

    prepare_parser = subparsers.add_parser(
        "prepare", help="Filter Paris, deduplicate POIs, and create entity documents"
    )
    prepare_parser.add_argument("--csv", required=True, type=Path)
    prepare_parser.add_argument("--output", required=True, type=Path)
    prepare_parser.add_argument("--corpus-version", default="datatourisme-paris-v1")
    prepare_parser.add_argument("--snapshot-date", required=True)
    prepare_parser.add_argument("--source-url")
    prepare_parser.set_defaults(func=_prepare)

    ingest_parser = subparsers.add_parser(
        "ingest", help="Upload the prepared POIs and synchronize the isolated Knowledge Base"
    )
    ingest_parser.add_argument("--prepared", required=True, type=Path)
    ingest_parser.add_argument("--bucket", required=True)
    ingest_parser.add_argument("--knowledge-base-id", required=True)
    ingest_parser.add_argument("--data-source-id", required=True)
    ingest_parser.add_argument("--namespace", default="datatourisme-paris-v1")
    ingest_parser.add_argument("--region", default="eu-central-1")
    ingest_parser.add_argument("--workers", type=_positive_int, default=8)
    ingest_parser.add_argument("--dry-run", action="store_true")
    ingest_parser.add_argument("--no-wait", action="store_true")
    ingest_parser.set_defaults(func=_ingest)
    return parser


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    args = _parser().parse_args()
    try:
        args.func(args)
    except (IngestError, RuntimeError, ValueError) as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
