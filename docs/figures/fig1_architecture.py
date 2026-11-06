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

# ── figure size ────────────────────────────────────────────────────────────────
FW, FH = 16.6, 13.0
fig, ax = plt.subplots(figsize=(FW, FH))
ax.set_xlim(0, FW); ax.set_ylim(0.95, 12.50)
ax.axis("off")
fig.patch.set_facecolor(C_BG)
ax.set_facecolor(C_BG)

# ── font sizes ─────────────────────────────────────────────────────────────────
FS_TITLE  = 16.5   # box header titles
FS_COMP   = 14.0   # component label (main text)
FS_DIM    = 11.5   # right-aligned dim/port notes
FS_STRIP  = 13.0   # channel strip labels
FS_ARROW  = 11.5   # inter-box arrow labels
FS_NOTE   = 12.0   # second-client notes
FS_CAP    = 12.0   # figure caption


# ── helpers ────────────────────────────────────────────────────────────────────
def rbox(x, y, w, h, fc, ec, lw=1.4, r=0.22, zorder=2, alpha=1.0):
    p = FancyBboxPatch((x, y), w, h,
                       boxstyle=f"round,pad=0,rounding_size={r}",
                       fc=fc, ec=ec, lw=lw, zorder=zorder, alpha=alpha)
    ax.add_patch(p)

def t(x, y, s, fs=12, color=C_TEXT, bold=False,
      ha="center", va="center", style="normal", zorder=5):
    ax.text(x, y, s, fontsize=fs, color=color,
            fontweight="bold" if bold else "normal",
            fontstyle=style, ha=ha, va=va, zorder=zorder, clip_on=False)

def arrow(x0, y0, x1, y1, color, lw=2.4, style="<->", ls="-", zorder=4):
    ax.annotate("", xy=(x1, y1), xytext=(x0, y0),
                arrowprops=dict(arrowstyle=style, color=color, lw=lw,
                                linestyle=ls,
                                connectionstyle="arc3,rad=0.0"),
                zorder=zorder)

def arrow_label(x, y, s, color, fs=FS_ARROW):
    ax.text(x, y, s, fontsize=fs, color=color, fontweight="bold",
            ha="center", va="center", zorder=6,
            bbox=dict(fc=C_BG, ec="none", pad=3.0))


# ── layout constants ───────────────────────────────────────────────────────────
H1 = 0.95    # single-row component box height
H2 = 1.52    # double-row component box height
GAP = 0.22   # vertical gap between rows

# Row definitions bottom-up: (tag, color, y_bottom, height)
# Computed to fill the outer box cleanly.
# Stack from y=2.34 upward with GAP between rows:
#   Local:   2.34  h=1.52  top=3.86
#   Safety:  4.08  h=1.52  top=5.60
#   MongoDB: 5.82  h=0.95  top=6.77
#   CA:      6.99  h=0.95  top=7.94
#   SSH:     8.16  h=1.52  top=9.68
#   ZMQ:     9.90  h=1.52  top=11.42
ROWS = [
    ("ZMQ",     C_ZMQ,    9.90, H2),
    ("SSH",      C_SSH,    8.16, H2),
    ("CA",       C_CA,     6.99, H1),
    ("MongoDB",  C_MONGO,  5.82, H1),
    ("Safety",   C_SAFETY, 4.08, H2),
    ("Local",    C_LOCAL,  2.34, H2),
]

# Outer box geometry
CB = 1.15                # bottom y
TITLE_H = 0.70           # title bar height
NOTE_H  = 0.80           # second-client note height
NOTE_Y  = CB + 0.12      # note bottom y
CH = 11.42 + 0.14 + TITLE_H - CB   # outer box height (rows top + pad + title - base)
CL, CW = 0.40, 6.90
SL, SW = 9.20, 6.90

# Channel-label strip (inside left of client box)
STRIP_W  = 1.25
COMP_PAD = 0.20
CI_L = CL + STRIP_W + COMP_PAD
CI_W = CW - STRIP_W - COMP_PAD - 0.20
CI_R = CI_L + CI_W

SI_L = SL + 0.20
SI_W = SW - 0.40
SI_R = SI_L + SI_W

# Arrow lane
AX_L  = CI_R + 0.12
AX_R  = SI_L - 0.12
AX_MX = (AX_L + AX_R) / 2

TITLE_Y = CB + CH - TITLE_H   # title bar bottom y


# ── outer boxes + dark title bars ─────────────────────────────────────────────
rbox(CL, CB, CW, CH, C_CLIENT, C_BORDER, lw=2.4, r=0.40)
rbox(SL, CB, SW, CH, C_SERVER, C_BORDER, lw=2.4, r=0.40)

rbox(CL+0.14, TITLE_Y, CW-0.28, TITLE_H, C_BORDER, C_BORDER, lw=0, r=0.20, zorder=3)
t(CL+CW/2, TITLE_Y+TITLE_H/2,
  "Client Workstation  (Mac / Windows / Linux)",
  fs=FS_TITLE, bold=True, color="white", zorder=4)

rbox(SL+0.14, TITLE_Y, SW-0.28, TITLE_H, C_BORDER, C_BORDER, lw=0, r=0.20, zorder=3)
t(SL+SW/2, TITLE_Y+TITLE_H/2,
  "Beamline Computer  (Linux, remote)",
  fs=FS_TITLE, bold=True, color="white", zorder=4)


# ── component boxes ────────────────────────────────────────────────────────────
def comp_box(side, row_i, l1, d1, l2=None, d2=None):
    _, _, yb, h = ROWS[row_i]
    if side == "client":
        xl, xw = CI_L, CI_W
        dr = CI_L + CI_W - 0.15
    else:
        xl, xw = SI_L, SI_W
        dr = SI_L + SI_W - 0.15

    rbox(xl, yb, xw, h, C_INNER, "#90a4ae", lw=1.1, r=0.16, zorder=3)

    if l2 is None:
        my = yb + h / 2
        t(xl + 0.26, my, l1, fs=FS_COMP, ha="left")
        if d1:
            t(dr, my, d1, fs=FS_DIM, ha="right", color=C_DIM)
    else:
        y1 = yb + h - 0.34
        y2 = yb + 0.30
        t(xl + 0.26, y1, l1, fs=FS_COMP, ha="left")
        t(xl + 0.26, y2, l2, fs=FS_COMP, ha="left")
        if d1: t(dr, y1, d1, fs=FS_DIM, ha="right", color=C_DIM)
        if d2: t(dr, y2, d2, fs=FS_DIM, ha="right", color=C_DIM)


# Client
comp_box("client", 0,
         "ZMQWorker  — 1 Hz poll",              "ctrl 60615",
         "ZMQDocThread  — live docs",           "doc 60630 (SUB)")
comp_box("client", 1,
         "_SSHLogTailer  — log stream",         "tail -f (PTY)",
         "SSH lifecycle + SFTP upload",         "start / stop")
comp_box("client", 2,
         "pyepics CA monitor",                  "DBR_CTRL subs")
comp_box("client", 3,
         "MongoDB Browser  — pymongo",          "TCP 27017")
comp_box("client", 4,
         "Operator lock check / write",         "/tmp/…operator",
         "Client heartbeat writer",             "/tmp/…client_<host>.json")
comp_box("client", 5,
         "connection.json  ·  plans_log.jsonl", "local disk",
         "HDF5 export / viewer  (.h5)",         "MongoDB or .jsonl")

# Server
comp_box("server", 0,
         "bluesky-queueserver  v0.0.25  +  Redis", "ctrl 60615  info 60625",
         "RunEngine  +  ophyd  +  baseline",       "doc PUB 60630")
comp_box("server", 1,
         "RE Manager  (procServ wrapper)",      "procServ port + PID",
         "RE Manager log  /tmp/re-manager-*.log", "tail target")
comp_box("server", 2,
         "EPICS IOC  — motors, detectors",      "CA / PVA")
comp_box("server", 3,
         "MongoDB  — run documents",            "host configurable")
comp_box("server", 4,
         "/tmp/.easy_bluesky_<slug>.operator",  "operator lock",
         "/tmp/.easy_bluesky_<slug>_client_<host>.json", "heartbeats")
comp_box("server", 5,
         "~/.easy_bluesky/scripts/",            "SFTP: startup, devices, plans",
         "<remote_exp_dir>/runs/<uid>.jsonl  (backup)", "NFS / shared FS")


# ── channel-label strips ───────────────────────────────────────────────────────
for tag, color, yb, h in ROWS:
    strip_xc = CL + STRIP_W / 2
    rbox(CL + 0.10, yb + 0.10, STRIP_W - 0.12, h - 0.20,
         color, color, lw=0, r=0.16, zorder=3, alpha=0.13)
    t(strip_xc, yb + h / 2, tag,
      fs=FS_STRIP, bold=True, color=color, zorder=4)


# ── inter-box arrows ───────────────────────────────────────────────────────────
arrow_defs = [
    (0, C_ZMQ,    "<->", "-",  "ZMQ\nREQ/REP + PUB/SUB"),
    (1, C_SSH,    "<->", "-",  "SSH / SFTP\n(key-pair only)"),
    (2, C_CA,     "<->", "-",  "EPICS CA\n(direct)"),
    (3, C_MONGO,  "<->", "-",  "pymongo\nTCP 27017"),
    (4, C_SAFETY, "->",  "--", "SSH r/w\n(safety files)"),
]
for row_i, color, style, ls, label in arrow_defs:
    _, _, yb, h = ROWS[row_i]
    my = yb + h / 2
    arrow(AX_L, my, AX_R, my, color=color, lw=2.4, style=style, ls=ls)
    arrow_label(AX_MX, my, label, color, fs=FS_ARROW)


# ── second-client notes ────────────────────────────────────────────────────────
rbox(CL+0.14, NOTE_Y, CW-0.28, NOTE_H, "#fff3e0", C_SSH, lw=1.3, r=0.16, zorder=3)
t(CL+CW/2, NOTE_Y+NOTE_H/2,
  "A second client may connect to the same ZMQ ports.\n"
  "Operator lock + connected-clients chip coordinate access.",
  fs=FS_NOTE, color=C_SSH, ha="center", zorder=4)

rbox(SL+0.14, NOTE_Y, SW-0.28, NOTE_H, "#fff3e0", C_SSH, lw=1.3, r=0.16, zorder=3)
t(SL+SW/2, NOTE_Y+NOTE_H/2,
  "ss -tn lists active ZMQ TCP connections every 30 s;\n"
  "combined with client heartbeat files for robustness.",
  fs=FS_NOTE, color=C_SSH, ha="center", zorder=4)


plt.subplots_adjust(left=0, right=1, top=1, bottom=0)
plt.savefig("docs/figures/fig1_architecture.pdf", dpi=300, bbox_inches="tight",
            facecolor=C_BG)
plt.savefig("docs/figures/fig1_architecture.png", dpi=300, bbox_inches="tight",
            facecolor=C_BG)
print("Saved docs/figures/fig1_architecture.pdf  and  docs/figures/fig1_architecture.png")
