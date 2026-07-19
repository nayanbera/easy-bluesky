#!/usr/bin/env python3
"""
fix_bob_colors.py
-----------------
Batch-fixes Phoebus BOB files by injecting explicit black foreground colors
into label and text_update widgets that have no <foreground> tag defined.

Usage:
    python3 fix_bob_colors.py /path/to/bob/files [--dry-run] [--recursive]

Options:
    --dry-run     Show what would be changed without modifying any files
    --recursive   Search for BOB files recursively in subdirectories

Examples:
    python3 fix_bob_colors.py ~/displays/
    python3 fix_bob_colors.py ~/displays/ --dry-run
    python3 fix_bob_colors.py ~/displays/ --recursive
"""

import os
import sys
import re
import shutil
from pathlib import Path

# Widget types to fix
TARGET_WIDGETS = {"label", "text_update"}

# The foreground color block to inject (explicit black)
FOREGROUND_XML = """      <foreground>
        <color red="0" green="0" blue="0" />
      </foreground>"""

# Optional: also fix text_entry and other widgets — add to this set
# TARGET_WIDGETS = {"label", "text_update", "text_entry", "combo"}


def has_foreground(widget_block: str) -> bool:
    """Check if a widget block already has a <foreground> tag."""
    return "<foreground>" in widget_block


def get_widget_type(widget_block: str) -> str:
    """Extract the widget type from the opening tag."""
    match = re.search(r'<widget\s+type=["\']([^"\']+)["\']', widget_block)
    return match.group(1) if match else ""


def fix_widget_block(widget_block: str) -> str:
    """
    Inject <foreground> after the <name> tag if missing.
    Falls back to injecting after the opening <widget> tag.
    """
    # Try to insert after </name> tag
    if "</name>" in widget_block:
        return widget_block.replace(
            "</name>",
            f"</name>\n{FOREGROUND_XML}",
            1
        )
    # Fallback: insert after the opening widget tag line
    match = re.search(r'(<widget\s+[^>]+>)', widget_block)
    if match:
        return widget_block.replace(
            match.group(1),
            f"{match.group(1)}\n{FOREGROUND_XML}",
            1
        )
    return widget_block


def process_file(filepath: Path, dry_run: bool) -> tuple[int, int]:
    """
    Process a single BOB file.
    Returns (widgets_found, widgets_fixed) counts.
    """
    content = filepath.read_text(encoding="utf-8")

    # Split on widget boundaries, keeping delimiters
    # Each widget block runs from <widget type="..."> to </widget>
    pattern = re.compile(
        r'(<widget\s+type=["\'][^"\']+["\'][^>]*>.*?</widget>)',
        re.DOTALL
    )

    found = 0
    fixed = 0
    new_content = content

    # Find all widget blocks and process them
    for match in pattern.finditer(content):
        widget_block = match.group(1)
        wtype = get_widget_type(widget_block)

        if wtype not in TARGET_WIDGETS:
            continue

        found += 1

        if not has_foreground(widget_block):
            fixed += 1
            fixed_block = fix_widget_block(widget_block)
            new_content = new_content.replace(widget_block, fixed_block, 1)

    if fixed > 0 and not dry_run:
        # Backup original file
        backup_path = filepath.with_suffix(".bob.bak")
        shutil.copy2(filepath, backup_path)
        # Write fixed content
        filepath.write_text(new_content, encoding="utf-8")

    return found, fixed


def main():
    args = sys.argv[1:]

    if not args:
        print(__doc__)
        sys.exit(1)

    bob_dir = Path(args[0])
    dry_run = "--dry-run" in args
    recursive = "--recursive" in args

    if not bob_dir.exists():
        print(f"ERROR: Path does not exist: {bob_dir}")
        sys.exit(1)

    if dry_run:
        print("DRY RUN MODE — no files will be modified\n")

    # Find all BOB files
    if recursive:
        bob_files = list(bob_dir.rglob("*.bob"))
    else:
        bob_files = list(bob_dir.glob("*.bob"))

    if not bob_files:
        print(f"No BOB files found in: {bob_dir}")
        sys.exit(0)

    print(f"Found {len(bob_files)} BOB file(s) in: {bob_dir}\n")
    print(f"Targeting widget types: {', '.join(sorted(TARGET_WIDGETS))}\n")

    total_found = 0
    total_fixed = 0
    files_modified = 0

    for bobfile in sorted(bob_files):
        found, fixed = process_file(bobfile, dry_run)
        total_found += found
        total_fixed += fixed

        if fixed > 0:
            files_modified += 1
            status = "would fix" if dry_run else "fixed"
            print(f"  {bobfile.name}: {status} {fixed}/{found} widgets")
        else:
            print(f"  {bobfile.name}: {found} widgets, none needed fixing")

    print(f"\n{'='*50}")
    print(f"Total widgets found : {total_found}")
    print(f"Total widgets fixed : {total_fixed}")
    print(f"Files {'that would be ' if dry_run else ''}modified: {files_modified}")

    if not dry_run and files_modified > 0:
        print(f"\nOriginal files backed up as *.bob.bak")
        print("To restore originals: rename *.bob.bak back to *.bob")


if __name__ == "__main__":
    main()
