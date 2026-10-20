#!/usr/bin/env python3
"""Prepare and ingest a versioned Wikivoyage RAG corpus.

Examples:
  uv run python scripts/wikivoyage_rag.py download --output /tmp/enwikivoyage.xml.bz2
  uv run python scripts/wikivoyage_rag.py prepare --dump /tmp/enwikivoyage.xml.bz2 \
      --output /tmp/wikivoyage-pilot --limit 1000 --snapshot-date 2026-08-20
  uv run python scripts/wikivoyage_rag.py ingest --prepared /tmp/wikivoyage-pilot \
      --bucket BUCKET --knowledge-base-id KB_ID --data-source-id DATA_SOURCE_ID \
      --namespace wikivoyage-pilot-v1 --region eu-central-1
"""

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

from rag.ingest import IngestError, backfill_filter_metadata, ingest_prepared  # noqa: E402
from rag.wikivoyage import (  # noqa: E402
    iter_articles,
    select_articles,
    sha256_file,
    validate_chunking_parameters,
    write_prepared_corpus,
)

S3_PREFIX = "wikivoyage"

DEFAULT_DUMP_URL = (
    "https://dumps.wikimedia.org/enwikivoyage/latest/"
    "enwikivoyage-latest-pages-articles-multistream.xml.bz2"
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _download(url: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "aws-agentic-blueprint/1"})
            with urllib.request.urlopen(request, timeout=60) as response:
                while block := response.read(1024 * 1024):
                    temporary.write(block)
            temporary.flush()
            temporary_path.replace(output)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
    print(
        json.dumps(
            {"path": str(output), "bytes": output.stat().st_size, "sha256": sha256_file(output)}
        )
    )


def _title_selection(path: str | None) -> tuple[set[str] | None, set[str] | None]:
    if not path:
        return None, None
    selectors = {
        line.strip()
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    prefixes = {selector[:-2] for selector in selectors if selector.endswith("/*")}
    titles = selectors - {f"{prefix}/*" for prefix in prefixes}
    return titles or None, prefixes or None


def _prepare(args: argparse.Namespace) -> None:
    dump = Path(args.dump)
    output = Path(args.output)
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"Output directory is not empty: {output}. Use a new versioned directory.")
    if args.overlap_tokens >= args.target_tokens:
        raise SystemExit("--overlap-tokens must be smaller than --target-tokens")
    if args.minimum_chunk_tokens > args.target_tokens:
        raise SystemExit("--minimum-chunk-tokens must not exceed --target-tokens")
    try:
        validate_chunking_parameters(
            target_tokens=args.target_tokens,
            overlap_tokens=args.overlap_tokens,
            minimum_tokens=args.minimum_chunk_tokens,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    requested, requested_prefixes = _title_selection(args.titles_file)
    selected, missing = select_articles(
        iter_articles(dump),
        limit=None if requested or requested_prefixes else args.limit,
        titles=requested,
        title_prefixes=requested_prefixes,
        seed=args.seed,
        minimum_words=args.minimum_article_words,
    )
    if missing:
        missing_preview = ", ".join(sorted(missing)[:20])
        raise SystemExit(f"Requested titles absent from the dump: {missing_preview}")
    manifest = write_prepared_corpus(
        selected,
        output,
        corpus_version=args.corpus_version,
        snapshot_date=args.snapshot_date,
        source_url=args.source_url,
        source_checksum=sha256_file(dump),
        target_tokens=args.target_tokens,
        overlap_tokens=args.overlap_tokens,
        minimum_tokens=args.minimum_chunk_tokens,
    )
    print(json.dumps(manifest, indent=2))


def _ingest(args: argparse.Namespace) -> None:
    result = ingest_prepared(
        Path(args.prepared),
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


def _backfill_metadata(args: argparse.Namespace) -> None:
    result = backfill_filter_metadata(
        prefix=S3_PREFIX,
        bucket=args.bucket,
        namespace=args.namespace,
        knowledge_base_id=args.knowledge_base_id,
        data_source_id=args.data_source_id,
        region=args.region,
        workers=args.workers,
        dry_run=args.dry_run,
        wait=not args.no_wait,
        shard=args.shard,
        sync=not args.no_sync,
        progress=lambda line: print(line, flush=True),
    )
    print(json.dumps(result, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    download = subparsers.add_parser("download", help="Download the official article dump")
    download.add_argument("--url", default=DEFAULT_DUMP_URL)
    download.add_argument("--output", required=True, type=Path)
    download.set_defaults(func=lambda args: _download(args.url, args.output))

    prepare = subparsers.add_parser("prepare", help="Parse, select, clean, and chunk pages")
    prepare.add_argument("--dump", required=True)
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--corpus-version", default="wikivoyage-pilot-v1")
    prepare.add_argument("--snapshot-date", required=True)
    prepare.add_argument("--source-url", default=DEFAULT_DUMP_URL)
    prepare.add_argument(
        "--titles-file",
        help="File of exact titles and/or descendant patterns such as Paris/*",
    )
    prepare.add_argument("--limit", type=_positive_int, default=1000)
    prepare.add_argument("--seed", type=int, default=20260820)
    prepare.add_argument("--minimum-article-words", type=_positive_int, default=80)
    prepare.add_argument("--target-tokens", type=_positive_int, default=250)
    prepare.add_argument("--overlap-tokens", type=_nonnegative_int, default=30)
    prepare.add_argument("--minimum-chunk-tokens", type=_positive_int, default=20)
    prepare.set_defaults(func=_prepare)

    ingest = subparsers.add_parser(
        "ingest",
        help="Upload chunk documents and sync the Bedrock Knowledge Base",
    )
    ingest.add_argument("--prepared", required=True)
    ingest.add_argument("--bucket", required=True)
    ingest.add_argument("--knowledge-base-id", required=True)
    ingest.add_argument("--data-source-id", required=True)
    ingest.add_argument("--namespace", default="wikivoyage-pilot-v1")
    ingest.add_argument("--region", default="eu-central-1")
    ingest.add_argument("--workers", type=_positive_int, default=8)
    ingest.add_argument("--dry-run", action="store_true")
    ingest.add_argument("--no-wait", action="store_true")
    ingest.set_defaults(func=_ingest)

    backfill = subparsers.add_parser(
        "backfill-metadata",
        help="Add exact destination filters to deployed Bedrock metadata sidecars",
    )
    backfill.add_argument("--bucket", required=True)
    backfill.add_argument("--knowledge-base-id", required=True)
    backfill.add_argument("--data-source-id", required=True)
    backfill.add_argument("--namespace", default="wikivoyage-pilot-v1")
    backfill.add_argument("--region", default="eu-central-1")
    backfill.add_argument("--workers", type=_positive_int, default=8)
    backfill.add_argument("--dry-run", action="store_true")
    backfill.add_argument("--no-wait", action="store_true")
    backfill.add_argument("--shard", choices="0123456789abcdef")
    backfill.add_argument("--no-sync", action="store_true")
    backfill.set_defaults(func=_backfill_metadata)
    return parser


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    args = _parser().parse_args()
    try:
        args.func(args)
    except IngestError as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
