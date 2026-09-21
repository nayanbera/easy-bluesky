"""report_generator.py — Generate an HTML experiment report and open in browser."""

import json
import webbrowser
from datetime import datetime
from pathlib import Path


# ── HTML template ──────────────────────────────────────────────────────────────

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: 'Helvetica Neue', Arial, sans-serif;
    font-size: 13px;
    color: #1a1a1a;
    background: #fff;
    padding: 32px 40px;
    max-width: 1100px;
    margin: 0 auto;
}
h1 { font-size: 22px; font-weight: 700; margin-bottom: 4px; }
h2 { font-size: 14px; font-weight: 600; color: #444;
     margin: 24px 0 8px 0; letter-spacing: 0.05em; text-transform: uppercase; }
.subtitle { font-size: 12px; color: #666; margin-bottom: 20px; }
.header-block { border-left: 4px solid #1a6fa8; padding-left: 14px; margin-bottom: 24px; }
.meta-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 10px;
    margin-bottom: 24px;
}
.meta-card {
    background: #f7f9fc;
    border: 1px solid #dde3ec;
    border-radius: 6px;
    padding: 10px 14px;
}
.meta-card .label { font-size: 10px; color: #888; text-transform: uppercase;
                    letter-spacing: 0.06em; margin-bottom: 2px; }
.meta-card .value { font-size: 13px; font-weight: 600; color: #1a1a1a; }
.meta-card .value a { color: #1a6fa8; text-decoration: none; }
.summary-row {
    display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap;
}
.stat-box {
    background: #1a6fa8; color: #fff; border-radius: 8px;
    padding: 12px 20px; min-width: 110px; text-align: center;
}
.stat-box .num { font-size: 26px; font-weight: 700; line-height: 1; }
.stat-box .lbl { font-size: 10px; opacity: 0.85; margin-top: 4px; }
table {
    width: 100%; border-collapse: collapse; margin-bottom: 24px;
    font-size: 12px;
}
thead th {
    background: #1a6fa8; color: #fff; text-align: left;
    padding: 8px 10px; font-weight: 600; white-space: nowrap;
}
tbody tr:nth-child(even) { background: #f5f8fc; }
tbody tr:hover { background: #e8f0fa; }
td { padding: 7px 10px; border-bottom: 1px solid #e8e8e8; vertical-align: top; }
.badge {
    display: inline-block; font-size: 10px; font-weight: 700;
    padding: 2px 7px; border-radius: 10px; white-space: nowrap;
}
.badge-ok  { background: #d4edda; color: #155724; }
.badge-ab  { background: #fff3cd; color: #856404; }
.badge-err { background: #f8d7da; color: #721c24; }
.badge-unk { background: #e2e3e5; color: #383d41; }
.param-key { color: #888; }
.footer {
    margin-top: 32px; padding-top: 12px; border-top: 1px solid #ddd;
    font-size: 11px; color: #999;
}
@media print {
    body { padding: 16px; }
    .stat-box { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    thead th  { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
"""

# ── Helpers ────────────────────────────────────────────────────────────────────

def _h(text: str) -> str:
    """HTML-escape a string."""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _fmt_dur(seconds) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _fmt_ts(unix_ts) -> str:
    if not unix_ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(unix_ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(unix_ts)


def _params_html(kwargs: dict) -> str:
    """Render plan kwargs as compact key=value pairs (skip md dict)."""
    md = kwargs.get("md", {}) or {}
    parts = []
    for k, v in kwargs.items():
        if k == "md":
            continue
        parts.append(f'<span class="param-key">{_h(k)}=</span>{_h(v)}')
    # Pull a few useful md fields
    for field in ("sample_name", "energy", "exposure_time", "num_images"):
        val = md.get(field)
        if val is not None:
            parts.append(f'<span class="param-key">md.{_h(field)}=</span>{_h(val)}')
    return "<br>".join(parts) if parts else "—"


def _status_badge(exit_status: str) -> str:
    es = (exit_status or "").lower()
    if es == "success":
        return '<span class="badge badge-ok">✓ success</span>'
    if es == "aborted":
        return '<span class="badge badge-ab">⊘ aborted</span>'
    if es in ("failed", "error"):
        return '<span class="badge badge-err">✗ failed</span>'
    return '<span class="badge badge-unk">? unknown</span>'


# ── Report generation ──────────────────────────────────────────────────────────

def generate_report(exp_dir: str, profile_name: str = "") -> str:
    """Generate an HTML report for the experiment at *exp_dir*.

    Returns the path to the written report file.
    """
    exp_path = Path(exp_dir)

    # ── Load experiment.json ────────────────────────────────────────────────
    exp_json_path = exp_path / "experiment.json"
    exp_info: dict = {}
    if exp_json_path.exists():
        try:
            exp_info = json.loads(exp_json_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    exp_name    = exp_info.get("name") or exp_path.name
    created_raw = exp_info.get("created", "")
    created_str = _fmt_ts(created_raw) if isinstance(created_raw, (int, float)) else str(created_raw)

    # ── Load esaf_info.json ─────────────────────────────────────────────────
    esaf_path = exp_path / "esaf_info.json"
    esaf_info: dict = {}
    doi_str = ""
    if esaf_path.exists():
        try:
            raw = json.loads(esaf_path.read_text(encoding="utf-8"))
            esaf_info = raw.get("esaf", {}) or {}
            doi_str   = raw.get("doi", "") or ""
        except Exception:
            pass

    esaf_id     = esaf_info.get("esaf_id", "")
    pi_name     = esaf_info.get("pi_name") or esaf_info.get("pi_group", "")
    proposal_id = esaf_info.get("proposal_id", "")
    esaf_start  = esaf_info.get("esaf_start_date") or esaf_info.get("start_date", "")
    esaf_end    = esaf_info.get("esaf_end_date")   or esaf_info.get("end_date",   "")

    # ── Load plans_log.jsonl ────────────────────────────────────────────────
    log_path = exp_path / "plans_log.jsonl"
    all_entries: list = []
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    all_entries.append(json.loads(line))
                except Exception:
                    pass

    # Completed = exit_status == "success"
    completed = [e for e in all_entries
                 if (e.get("exit_status") or "").lower() == "success"]

    # ── Compute summary stats ───────────────────────────────────────────────
    total_scans = len(completed)
    total_dur_s = sum(e.get("duration_s") or 0 for e in completed)
    samples     = sorted({e.get("sample_name", "") or "" for e in completed} - {""})

    start_times = [e["start_time"] for e in completed if e.get("start_time")]
    date_range  = ""
    if start_times:
        t0 = datetime.fromtimestamp(min(start_times)).strftime("%Y-%m-%d %H:%M")
        t1 = datetime.fromtimestamp(max(start_times)).strftime("%Y-%m-%d %H:%M")
        date_range = t0 if t0 == t1 else f"{t0} → {t1}"

    # ── Build HTML ──────────────────────────────────────────────────────────
    # Meta cards
    meta_items = [
        ("Experiment",  exp_name),
        ("Created",     created_str),
        ("Profile",     profile_name or "—"),
        ("Path",        str(exp_path)),
    ]
    if esaf_id:
        meta_items.append(("ESAF ID",    str(esaf_id)))
    if proposal_id:
        meta_items.append(("Proposal",   str(proposal_id)))
    if pi_name:
        meta_items.append(("PI / Group", str(pi_name)))
    if esaf_start:
        meta_items.append(("ESAF Dates", f"{esaf_start} – {esaf_end or '?'}"))
    if doi_str:
        meta_items.append(("DOI", f'<a href="https://doi.org/{_h(doi_str)}">{_h(doi_str)}</a>'))

    meta_html = "".join(
        f'<div class="meta-card">'
        f'<div class="label">{_h(label)}</div>'
        f'<div class="value">{value if label == "DOI" else _h(value)}</div>'
        f'</div>'
        for label, value in meta_items
    )

    # Stat boxes
    stats_html = (
        f'<div class="stat-box"><div class="num">{total_scans}</div>'
        f'<div class="lbl">Completed Scans</div></div>'
        f'<div class="stat-box"><div class="num">{_fmt_dur(total_dur_s)}</div>'
        f'<div class="lbl">Total Exposure</div></div>'
        f'<div class="stat-box"><div class="num">{len(samples) or "—"}</div>'
        f'<div class="lbl">Unique Samples</div></div>'
    )
    if date_range:
        stats_html += (
            f'<div class="stat-box" style="background:#2d7a4f;min-width:200px">'
            f'<div class="num" style="font-size:14px">{_h(date_range)}</div>'
            f'<div class="lbl">Measurement Period</div></div>'
        )

    # Scan table rows
    if completed:
        rows_html = ""
        for i, e in enumerate(completed, 1):
            sn   = e.get("scan_num", i)
            name = e.get("name", "?")
            samp = e.get("sample_name", "") or "—"
            kw   = e.get("plan_kwargs") or e.get("kwargs", {}) or {}
            ts   = _fmt_ts(e.get("start_time"))
            dur  = _fmt_dur(e.get("duration_s"))
            badge = _status_badge(e.get("exit_status", ""))
            motors    = ", ".join(e.get("motors", []) or []) or "—"
            detectors = ", ".join(e.get("detectors", []) or []) or "—"
            num_ev    = e.get("num_events") or "—"
            rows_html += (
                f"<tr>"
                f"<td>#{sn}</td>"
                f"<td><b>{_h(name)}</b></td>"
                f"<td>{_h(samp)}</td>"
                f"<td style='font-size:11px'>{_params_html(kw)}</td>"
                f"<td>{_h(motors)}</td>"
                f"<td>{_h(detectors)}</td>"
                f"<td style='text-align:right'>{_h(str(num_ev))}</td>"
                f"<td>{_h(ts)}</td>"
                f"<td style='text-align:right'>{_h(dur)}</td>"
                f"<td>{badge}</td>"
                f"</tr>"
            )
        table_html = (
            "<table>"
            "<thead><tr>"
            "<th>#</th><th>Plan</th><th>Sample</th><th>Parameters</th>"
            "<th>Motors</th><th>Detectors</th><th>Pts</th>"
            "<th>Start</th><th>Duration</th><th>Status</th>"
            "</tr></thead>"
            f"<tbody>{rows_html}</tbody>"
            "</table>"
        )
    else:
        table_html = '<p style="color:#888">No completed scans found.</p>'

    # Samples section
    if samples:
        sample_list = "".join(f"<li>{_h(s)}</li>" for s in samples)
        samples_html = f"<h2>Samples</h2><ul style='margin-left:20px;margin-bottom:16px'>{sample_list}</ul>"
    else:
        samples_html = ""

    generated_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Experiment Report — {_h(exp_name)}</title>
  <style>{_CSS}</style>
</head>
<body>
  <div class="header-block">
    <h1>Experiment Report</h1>
    <div class="subtitle">Generated {_h(generated_ts)} &nbsp;·&nbsp; EasyBluesky</div>
  </div>

  <h2>Experiment Info</h2>
  <div class="meta-grid">{meta_html}</div>

  <h2>Summary</h2>
  <div class="summary-row">{stats_html}</div>

  {samples_html}

  <h2>Completed Scans</h2>
  {table_html}

  <div class="footer">
    Generated by EasyBluesky &nbsp;·&nbsp; {_h(str(exp_path))}
    &nbsp;·&nbsp; {_h(generated_ts)}
  </div>
</body>
</html>"""

    # Write and open
    report_path = exp_path / "report.html"
    report_path.write_text(html, encoding="utf-8")
    webbrowser.open(report_path.as_uri())
    return str(report_path)
