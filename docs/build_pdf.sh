#!/bin/bash
# Build docs/easy_bluesky_jsr_draft.pdf from the markdown draft (pandoc + tectonic).
# Runs from docs/ so that relative figure paths (figures/…) resolve.
set -e
cd "$(dirname "$0")"
pandoc -f markdown-implicit_figures easy_bluesky_jsr_draft.md \
  -o easy_bluesky_jsr_draft.pdf \
  --pdf-engine=tectonic \
  -V geometry:margin=2.5cm \
  -V fontsize=11pt \
  -V mainfont="Arial Unicode MS" \
  -V colorlinks=true \
  -H pdf_headings.tex \
  --resource-path=.
