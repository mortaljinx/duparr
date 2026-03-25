#!/usr/bin/env python3
"""
Duparr - main orchestrator
Usage:
  python main.py scan              # scan and populate DB
  python main.py dupes --dry-run   # find duplicates (preview only)
  python main.py dupes --move      # move duplicates to /duplicates
  python main.py covers --dry-run  # preview missing covers
  python main.py covers --fetch    # fetch and embed missing covers
  python main.py all --dry-run     # full pipeline, dry run
  python main.py all --move --fetch
"""

import argparse
import sys
from scanner import Scanner
from deduper import Deduper
from cover import CoverFixer
from db import Database
import config

def cmd_scan(args):
    db = Database(config.DB_PATH)
    scanner = Scanner(db)
    print(f"🔍 Scanning {config.MUSIC_DIR}...")
    count = scanner.scan()
    print(f"✅ Scanned {count} tracks")

def cmd_dupes(args):
    db = Database(config.DB_PATH)
    deduper = Deduper(db)
    dry_run = not args.move
    if dry_run:
        print("🔍 Dry run — no files will be moved\n")
    groups = deduper.find_duplicates()
    if not groups:
        print("✅ No duplicates found")
        return
    saved = deduper.process(groups, dry_run=dry_run)
    print(f"\n💾 Would free {_fmt_bytes(saved)}" if dry_run else f"\n💾 Freed {_fmt_bytes(saved)}")

def cmd_covers(args):
    db = Database(config.DB_PATH)
    fixer = CoverFixer(db)
    dry_run = not args.fetch
    if dry_run:
        print("🔍 Dry run — no covers will be fetched\n")
    missing = fixer.find_missing()
    print(f"🎨 Found {len(missing)} albums with missing covers")
    if not dry_run and missing:
        fixer.fix_all(missing)
        if config.NAVIDROME_URL:
            fixer.trigger_navidrome_scan()

def cmd_all(args):
    # Determine effective flags
    class ScanArgs: pass
    class DupeArgs: move = args.move
    class CoverArgs: fetch = args.fetch

    cmd_scan(ScanArgs())
    print()
    cmd_dupes(DupeArgs())
    print()
    cmd_covers(CoverArgs())

def _fmt_bytes(b):
    for unit in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

def main():
    parser = argparse.ArgumentParser(description="Duparr")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("scan", help="Scan music directory into DB")

    p_dupes = sub.add_parser("dupes", help="Find and handle duplicates")
    p_dupes.add_argument("--dry-run", action="store_true", default=True)
    p_dupes.add_argument("--move", action="store_true", help="Actually move duplicates")

    p_covers = sub.add_parser("covers", help="Find and fetch missing covers")
    p_covers.add_argument("--dry-run", action="store_true", default=True)
    p_covers.add_argument("--fetch", action="store_true", help="Actually fetch covers")

    p_all = sub.add_parser("all", help="Full pipeline")
    p_all.add_argument("--move", action="store_true")
    p_all.add_argument("--fetch", action="store_true")

    args = parser.parse_args()

    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "dupes":
        cmd_dupes(args)
    elif args.command == "covers":
        cmd_covers(args)
    elif args.command == "all":
        cmd_all(args)
    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()
