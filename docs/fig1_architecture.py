"""Generate Figure 1 — EasyBluesky architecture diagram."""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

# ── palette ────────────────────────────────────────────────────────────────────
C_BG     = "#f8f9fa"
C_CLIENT = "#dce8fc"
C_SERVER = "#ddf0df"
C_ZMQ    = "#1565c0"
C_SSH    = "#bf360c"
C_CA     = "#1b5e20"
C_MONGO  = "#6a1b9a"
C_SAFETY = "#4e342e"
C_LOCAL  = "#37474f"
C_INNER  = "#ffffff"
C_BORDER = "#455a64"
C_TEXT   = "#212121"
C_DIM    = "#607d8b"

FW, FH = 16.5, 10.5
fig, ax = plt.subplots(figsize=(FW, FH))
ax.set_xlim(0, FW); ax.set_ylim(0, FH)
ax.axis("off")
fig.patch.set_facecolor(C_BG)
ax.set_facecolor(C_BG)


# ── helpers ────────────────────────────────────────────────────────────────────
def rbox(x, y, w, h, fc, ec, lw=1.4, r=0.22, zorder=2, alpha=1.0):
    p = FancyBboxPatch((x, y), w, h,
                       boxstyle=f"round,pad=0,rounding_size={r}",
                       fc=fc, ec=ec, lw=lw, zorder=zorder, alpha=alpha)
    ax.add_patch(p)

def t(x, y, s, fs=9, color=C_TEXT, bold=False,
      ha="center", va="center", style="normal", zorder=5, wrap=False):
    ax.text(x, y, s, fontsize=fs, color=color,
            fontweight="bold" if bold else "normal",
            fontstyle=style, ha=ha, va=va, zorder=zorder,
            clip_on=False)

def arrow(x0, y0, x1, y1, color, lw=2.1, style="<->", ls="-", zorder=4):
    ax.annotate("", xy=(x1, y1), xytext=(x0, y0),
                arrowprops=dict(arrowstyle=style, color=color, lw=lw,
                                linestyle=ls,
                                connectionstyle="arc3,rad=0.0"),
                zorder=zorder)

def arrow_label(x, y, s, color, fs=8.0):
    ax.text(x, y, s, fontsize=fs, color=color, fontweight="bold",
            ha="center", va="center", zorder=6,
            bbox=dict(fc=C_BG, ec="none", pad=2.5))


# ── overall zones ──────────────────────────────────────────────────────────────
# Outer boxes
CL, CB, CW, CH = 0.40, 1.05, 7.10, 8.75   # client
SL, SB, SW, SH = 9.00, 1.05, 7.10, 8.75   # server

# Inside client box: channel-label strip + component boxes
STRIP_W  = 0.88          # width of the left channel-label strip
COMP_PAD = 0.18          # gap between strip and component box
CI_L = CL + STRIP_W + COMP_PAD          # left x of client component boxes
CI_W = CW - STRIP_W - COMP_PAD - 0.18  # width
CI_R = CI_L + CI_W                      # right x (for arrow origin)

SI_L = SL + 0.18         # left x of server component boxes
SI_W = SW - 0.36
SI_R = SI_L + SI_W

# Arrow lane x positions
AX_L  = CI_R + 0.10      # arrow starts (right of client inner box)
AX_R  = SI_L - 0.10      # arrow ends   (left of server inner box)
AX_MX = (AX_L + AX_R) / 2   # midpoint label x

# Row definitions: (tag, color, y_bottom, height)
H1, H2 = 0.68, 1.10
ROWS = [
    ("ZMQ",      C_ZMQ,    8.28, H2),
    ("SSH",       C_SSH,    6.82, H2),
    ("CA",        C_CA,     5.80, H1),
    ("MongoDB",   C_MONGO,  4.78, H1),
    ("Safety",    C_SAFETY, 3.42, H2),
    ("Local",     C_LOCAL,  1.98, H2),
]


# ── outer boxes + titles ───────────────────────────────────────────────────────
rbox(CL, CB, CW, CH, C_CLIENT, C_BORDER, lw=2.2, r=0.38)
rbox(SL, SB, SW, SH, C_SERVER, C_BORDER, lw=2.2, r=0.38)

# Title bars inside each box
rbox(CL+0.12, CB+CH-0.62, CW-0.24, 0.52, C_BORDER, C_BORDER, lw=0, r=0.18, zorder=3)
t(CL+CW/2, CB+CH-0.36,
  "Client Workstation  (Mac / Windows / Linux)",
  fs=10.5, bold=True, color="white", zorder=4)

rbox(SL+0.12, SB+SH-0.62, SW-0.24, 0.52, C_BORDER, C_BORDER, lw=0, r=0.18, zorder=3)
t(SL+SW/2, SB+SH-0.36,
  "Beamline Computer  (Linux, remote)",
  fs=10.5, bold=True, color="white", zorder=4)


# ── component box drawing ──────────────────────────────────────────────────────
FS_C = 8.5   # component label size
FS_D = 7.5   # dim note size

def comp_box(side, row_i, l1, d1, l2=None, d2=None):
    _, _, yb, h = ROWS[row_i]
    if side == "client":
        xl, xw = CI_L, CI_W
        dr = CI_L + CI_W - 0.12
    else:
        xl, xw = SI_L, SI_W
        dr = SI_L + SI_W - 0.12

    rbox(xl, yb, xw, h, C_INNER, "#90a4ae", lw=1.0, r=0.14, zorder=3)

    if l2 is None:
        # single-row
        my = yb + h / 2
        t(xl + 0.22, my, l1, fs=FS_C, ha="left")
        if d1:
            t(dr, my, d1, fs=FS_D, ha="right", color=C_DIM)
    else:
        y1 = yb + h - 0.26
        y2 = yb + 0.24
        t(xl + 0.22, y1, l1, fs=FS_C, ha="left")
        t(xl + 0.22, y2, l2, fs=FS_C, ha="left")
        if d1: t(dr, y1, d1, fs=FS_D, ha="right", color=C_DIM)
        if d2: t(dr, y2, d2, fs=FS_D, ha="right", color=C_DIM)


# Client components
comp_box("client", 0,
         "ZMQWorker  — 1 Hz status poll (REQ/REP)",    "ctrl port 60615",
         "ZMQDocThread  — live document subscriber",    "doc port 60630  (SUB)")
comp_box("client", 1,
         "_SSHLogTailer  — streams RE Manager log",     "tail -f log  (PTY)",
         "SSH lifecycle commands + SFTP upload",        "start / stop / restart")
comp_box("client", 2,
         "pyepics CA monitor  — DBR_CTRL subscriptions","direct to EPICS IOC")
comp_box("client", 3,
         "MongoDB Browser  — pymongo run queries",      "TCP 27017")
comp_box("client", 4,
         "Operator lock checker / writer",              "/tmp/…operator  (SSH)",
         "Client heartbeat file writer",               "/tmp/…client_<host>.json")
comp_box("client", 5,
         "plans_log.jsonl  ·  device_metadata.json",   "local disk",
         "~/.easy_bluesky/connection.json  (profiles)", "local disk")

# Server components
comp_box("server", 0,
         "bluesky-queueserver  v0.0.25",               "ctrl 60615  info 60625",
         "RunEngine  +  ophyd devices",                "doc PUB 60630")
comp_box("server", 1,
         "RE Manager process  (procServ wrapper)",      "PID file + crash restart",
         "RE Manager log  /tmp/re-manager-*.log",      "tail target")
comp_box("server", 2,
         "EPICS IOC  — motors, detectors, scalers",    "CA / PVA")
comp_box("server", 3,
         "MongoDB  — run_start / event / run_stop",    "suitcase + writer")
comp_box("server", 4,
         "/tmp/.easy_bluesky_<slug>.operator",          "operator lock",
         "/tmp/.easy_bluesky_<slug>_client_<host>.json","heartbeats")
comp_box("server", 5,
         "~/.easy_bluesky/scripts/  (startup, devices)","SFTP target",
         "<exp_dir>/runs/<uid>.jsonl  (JSONL fallback)","NFS / shared FS")


# ── channel-label strip (inside left of client box) ───────────────────────────
for tag, color, yb, h in ROWS:
    strip_xc = CL + STRIP_W / 2  # centre of the strip
    strip_my  = yb + h / 2
    rbox(CL + 0.08, yb + 0.08, STRIP_W - 0.10, h - 0.16,
         color, color, lw=0, r=0.14, zorder=3, alpha=0.12)
    t(strip_xc, strip_my, tag,
      fs=8.4, bold=True, color=color, zorder=4)


# ── inter-box arrows ──────────────────────────────────────────────────────────
arrow_defs = [
    # row_i, color,    style,  ls,    label
    (0, C_ZMQ,    "<->", "-",  "ZMQ\nREQ/REP + PUB/SUB"),
    (1, C_SSH,    "<->", "-",  "SSH / SFTP\n(key-pair only)"),
    (2, C_CA,     "<->", "-",  "EPICS CA\n(direct, bypass QS)"),
    (3, C_MONGO,  "<->", "-",  "pymongo\nTCP 27017"),
    (4, C_SAFETY, "->",  "--", "SSH r/w\n(safety files)"),
]

for row_i, color, style, ls, label in arrow_defs:
    _, _, yb, h = ROWS[row_i]
    my = yb + h / 2
    arrow(AX_L, my, AX_R, my, color=color, lw=2.2, style=style, ls=ls)
    arrow_label(AX_MX, my, label, color, fs=7.8)


# ── second-client note (inside bottom of client box) ─────────────────────────
note_y = CB + 0.10
note_h = 0.56
rbox(CL + 0.12, note_y, CW - 0.24, note_h,
     "#fff3e0", C_SSH, lw=1.2, r=0.14, zorder=3)
t(CL + CW / 2, note_y + note_h / 2,
  "A second client may connect to the same ZMQ ports simultaneously.\n"
  "Operator lock + connected-clients chip coordinate concurrent access.",
  fs=7.8, color=C_SSH, ha="center", zorder=4)

# Mirror note on server side
rbox(SL + 0.12, note_y, SW - 0.24, note_h,
     "#fff3e0", C_SSH, lw=1.2, r=0.14, zorder=3)
t(SL + SW / 2, note_y + note_h / 2,
  "ss -tn queries active ZMQ TCP connections every 30 s;\n"
  "combined with client heartbeat files for robustness.",
  fs=7.8, color=C_SSH, ha="center", zorder=4)


# ── figure caption at very bottom ────────────────────────────────────────────
t(FW / 2, 0.30,
  "Figure 1.  EasyBluesky client–server architecture.  "
  "Three independent channels link the workstation to the beamline computer: "
  "ZMQ (queue control and live run documents), SSH/SFTP (process lifecycle, "
  "log streaming, and multi-client safety files), and EPICS CA "
  "(direct low-latency device readback, bypassing the queue-server).",
  fs=8.2, color=C_DIM, style="italic", ha="center")


plt.subplots_adjust(left=0, right=1, top=1, bottom=0)
plt.savefig("docs/fig1_architecture.pdf", dpi=300, bbox_inches="tight",
            facecolor=C_BG)
plt.savefig("docs/fig1_architecture.png", dpi=300, bbox_inches="tight",
            facecolor=C_BG)
print("Saved docs/fig1_architecture.pdf  and  docs/fig1_architecture.png")
