# EasyBluesky: A Graphical Interface for Bluesky-based Beamline Control at Synchrotron Facilities

**Mrinal Bera**
*[Your institution — e.g., Advanced Photon Source, Argonne National Laboratory / University of Chicago]*

---

## Abstract

The Bluesky ecosystem provides a powerful and flexible framework for data acquisition at
synchrotron beamlines, but its command-line and Python-scripting interface presents a
significant barrier to researchers without programming experience. We present EasyBluesky,
an open-source, cross-platform desktop application that exposes the full capability of the
Bluesky queue-server through an intuitive graphical user interface. EasyBluesky manages
the complete lifecycle of the remote RunEngine Manager — from SSH-based start-up and
environment control to experiment logging and live data visualization — without requiring
the user to interact with a terminal. A simulation mode allows plan development and testing
against synthetic ophyd devices before committing to beamtime. For shared beamlines, a
multi-client safety layer provides operator locking, real-time client visibility, and
status disambiguation when multiple workstations share the same queue-server. Experiment
Safety Assessment Form (ESAF) integration automates folder creation and injects proposal
metadata into every plan. An integrated Visual Plan Composer assembles measurement
sequences from graphical building blocks without requiring the user to write Python. An
interactive curve-fitting module with per-dataset parameter memory supports rapid analysis
within the same interface used for data collection. EasyBluesky is implemented in Python
using PyQt6 and communicates with the queue-server via ZMQ. It is freely available at
https://github.com/nayanbera/easy-bluesky under the BSD licence.

**Keywords:** beamline control, data acquisition, Bluesky, graphical user interface,
Python, queue-server, synchrotron, EPICS

---

## 1. Introduction

Modern synchrotron beamlines generate data at rates and volumes that demand automated,
reproducible data acquisition workflows. The Bluesky ecosystem (Allan *et al.*, 2019)
has emerged as a community standard for this purpose, offering a run engine that
orchestrates hardware through ophyd device abstractions (Brookhaven National Laboratory,
2014) and records provenance-rich experimental metadata. The bluesky-queueserver extension
(Contribute *et al.*, 2021) further decouples the RunEngine from the user interface,
exposing it over a ZMQ network interface that permits remote control from any machine on
the network.

Despite this technical maturity, the day-to-day operation of a Bluesky-controlled beamline
still demands a working knowledge of Python, IPython, and the command-line tools used to
start, configure, and monitor the RunEngine. This requirement effectively restricts
self-directed operation to researchers with software experience — a constraint that is
increasingly at odds with the broad scientific user base of synchrotron facilities, which
includes chemists, biologists, and materials scientists who are domain experts but not
programmers.

Several graphical tools for Bluesky exist, including the Bluesky Widgets library and
PyDM-based displays used at individual beamlines, but these are typically tightly coupled
to specific facility configurations and require significant local customisation before
deployment. A general-purpose, self-contained application that can be installed by an end
user and pointed at any Bluesky queue-server over SSH has not previously been available.

Here we describe EasyBluesky (version 0.1.2), a PyQt6 desktop application that provides
a complete graphical interface to a Bluesky queue-server running on a remote beamline
computer. EasyBluesky targets the common scenario in which an experimenter works at a
local workstation — or carries their own laptop — while the RunEngine executes on
dedicated beamline hardware. The application has been developed and deployed at the
ASWAXS beamline [*your beamline details*] and is made available as open-source software
for adoption at other facilities.

---

## 2. Software Description

### 2.1 Architecture

EasyBluesky follows a client–server architecture in which all hardware interaction occurs
on the remote beamline computer and the graphical application runs locally on the
experimenter's workstation (Fig. 1). The design separates concerns across three
communication channels: ZMQ sockets for queue-server control and status polling, SSH for
process lifecycle management and log streaming, and EPICS Channel Access (CA) for
direct, low-latency device readback.

**ZMQ layer.** The background `ZMQWorker` thread polls the bluesky-queueserver at
approximately 1 Hz using REQ/REP messages to the control port (default 60615) and reads
status documents from the info port (60625). On each poll the worker compares the new
state against the previous one and emits typed Qt signals only when something changes —
`status_updated`, `console_line`, `devices_updated`, `connected`, `disconnected`. All
user-interface components are signal-driven and update reactively. A separate
`ZMQDocThread` subscribes to the bluesky document PUB socket and forwards run documents
(start, descriptor, event, stop) to the Live Viewer for real-time plotting without
passing through the poll loop. Long-running calls to `function_execute` — used for
device-status queries, PV name retrieval, and sim-device control — run in dedicated
`QThread` worker objects so the poll loop is never blocked.

**SSH layer.** SSH connectivity is handled via the Paramiko library using key-pair
authentication (no passwords stored). A `_SSHLogTailer` thread opens a persistent SSH
channel and runs `tail -n 50 -f <log_file>` on the remote machine, streaming RE Manager
stdout into the RE Console widget. The channel requests a pseudo-terminal (PTY) so that
the remote sshd sends SIGHUP to the `tail` process if the connection closes abruptly,
preventing orphaned processes. On each RunEngine Manager restart, EasyBluesky uploads the
startup script (`re_startup_mongo.py`) and YAML permission files via SFTP before starting
the new process, ensuring the remote always runs the current version without manual file
transfer. Stop and start use separate SSH `exec_command` channels to avoid the stop
channel being terminated by `pkill` matching its own command line.

**EPICS CA layer.** EPICS Channel Access monitoring is performed locally via pyepics with
direct subscriptions to control-system PVs. PV names are retrieved from the live
RunEngine environment once per environment open via `function_execute`; the resulting map
is fingerprinted so that CA subscriptions are only rebuilt when the device set genuinely
changes, avoiding unnecessary reconnections after a ZMQ reconnect. Callbacks from the
pyepics CA thread are forwarded to the Qt main thread via queued signals and coalesced in
a 100 ms flush timer, ensuring rapid PV changes produce a single tree repaint rather than
per-callback redraws.

**Data storage.** The startup script subscribes two callbacks in the RunEngine: a
`suitcase.jsonl` writer via a `RunRouter` that creates per-run JSONL files in the active
experiment directory (or a local fallback), and a MongoDB writer. Both run unconditionally
so that run data is preserved even when MongoDB is unavailable. A third ZMQ PUB
subscription on a configurable document port feeds the Live Viewer. The application
additionally maintains a lightweight `plans_log.jsonl` index on the client machine,
recording plan name, parameters, run UID, and scan number for every completed run in the
active experiment.

**Thread model summary.**

| Thread | Role |
|---|---|
| Qt main thread | All widget updates, user actions, CA signal dispatch |
| `ZMQWorker` (QThread) | 1 Hz poll loop; `function_execute` calls via sub-threads |
| `ZMQDocThread` (QThread) | ZMQ SUB — live bluesky documents to Live Viewer |
| `_SSHLogTailer` (QThread) | SSH `tail -f` → RE Console |
| `_RunListFetcher` etc. (QThread) | pymongo queries, HDF5 export, PV name fetch |
| pyepics CA thread (internal) | CA callbacks → Qt queued signals |

### 2.2 Connection Profiles

EasyBluesky stores connection parameters in a JSON file
(`~/.easy_bluesky/connection.json`) that supports multiple named profiles. Each profile
specifies the remote host, SSH key path, conda environment name, queue-server control and
info ports, the devices file to load on the remote machine, and EPICS network settings
(`EPICS_CA_ADDR_LIST`, `EPICS_CA_AUTO_ADDR_LIST`). Switching profiles requires a single
toolbar click and reconnects automatically. No passwords are stored; authentication uses
SSH key pairs only.

Profiles support both remote (SSH-managed) and local modes. A **local profile** starts
the RunEngine Manager as a subprocess on the same machine, assigns ports automatically
from a free-port range, and terminates the process cleanly on application exit. Local
profiles require no SSH configuration and are the recommended starting point for new
users. A **simulation profile** pairs a local or remote RunEngine Manager with an
auto-generated `devices_sim.py` devices file (§3.4). A startup dialog lists all configured
profiles, greying out any profile already open in another application window to enforce
single-instance-per-profile operation.

### 2.3 RunEngine Manager Lifecycle

EasyBluesky provides full lifecycle control of the remote queue-server process without
requiring the user to open a terminal. The restart sequence proceeds as follows: (1) an
SSH channel kills the existing process via its PID file and `pkill -f start-re-manager`;
(2) a two-second pause allows the process to exit and release ports; (3) a fresh SSH
channel writes a launcher shell script to `/tmp` via SFTP, exporting the selected devices
file path as `EASY_BLUESKY_DEVICES_FILE`, and starts `start-re-manager` wrapped in
`procServ` for automatic restart on crash. The startup script, devices file, and a
generated simulation devices file (if present) are uploaded at step (3) so that any
local edits propagate to the remote automatically.

The RE console output is streamed to the interface via `tail -f` over SSH and displayed
in a live console widget (§2.1), giving experimenters the same visibility they would have
at a terminal without requiring direct shell access. The `BestEffortCallback` subscriber
in the startup script prints a live scan table to the RE Manager log at each data point,
making per-point progress visible in the console widget during running scans.

---

## 3. Key Features

### 3.1 Queue Management

The Queue Manager tab (Fig. 2b) provides a complete interface to the Bluesky plan queue.
Experimenters can add plans by selecting from the list of plans registered in the
RunEngine environment, configure plan arguments through dynamically generated forms,
reorder queue items by drag-and-drop, and remove or edit items at any time. Queue
execution controls — Start, Pause, Resume, Abort, and Stop — are available both in the
Queue Manager and in the Experiments tab, so no navigation is required to manage a
running queue.

Two automation options reduce the manual steps required during a measurement session.
The **Auto-start** checkbox causes EasyBluesky to start the queue automatically as soon
as the first plan is added, provided the RunEngine is idle and the environment is open —
useful during unattended operation. The **Loop** control re-runs the complete queue a
specified number of times (zero selects infinite looping), re-adding all items from a
snapshot taken at the time the queue was started. The loop count spinbox is live: changing
it during a run adjusts the number of remaining iterations immediately, and the current
iteration is displayed next to the control.

### 3.2 Experiment and Data Management

EasyBluesky organises measurements into named experiments. Each experiment is a directory
on the local machine that accumulates JSONL run files written by a `suitcase.jsonl`
subscriber running inside the RunEngine environment. A plans log (`plans_log.jsonl`)
tracks every completed plan, recording start time, plan name, parameters, and the unique
run UID. The MongoDB Browser tab reads run data from a locally accessible MongoDB instance
and filters the view automatically to the active experiment using run UIDs from the plans
log; a regex fallback is used when the log is empty. Multiple runs can be selected
simultaneously for overlay plotting, with automatic detection of common X and Y fields.
Auto-plotting triggers without button presses when the selection or axis choices change.

Scan numbers are assigned at queue time rather than execution time: each plan's `md["scan_num"]`
is set from a counter that advances monotonically with the queue, so HDF5 filenames,
MongoDB records, and the plans log are always consistent even if plans are reordered or
removed before running. The current counter value is displayed as a **Next scan: #N**
label in the Plan Log header and updates on every queue change.

A tabular data browser allows experimenters to open HDF5 archives from any completed run.
Background filesystem monitoring detects if the active experiment folder is deleted or
the NFS mount is lost while the app is running, pausing running scans automatically and
alerting the user. RE Manager console output is appended to `<exp_dir>/console.log`
for the duration of each session, providing a permanent record alongside the run data.

### 3.3 Live Device Monitoring

The Available Devices tab displays all devices registered in the RunEngine environment as
a hierarchical tree, with columns for device class, current value, engineering units, and
description (Fig. 2c). In real-beamline mode, values are updated in real time via EPICS
Channel Access subscriptions using `DBR_CTRL` callbacks, which carry both value and
engineering units in a single network message. Description fields are populated by an
additional subscription to each record's `.DESC` field; field suffixes (e.g. `.RBV`) are
stripped before appending `.DESC` so that `IOC:M1.RBV` correctly fetches `IOC:M1.DESC`
rather than the invalid `IOC:M1.RBV.DESC`. Callbacks from the pyepics CA thread are
forwarded to the Qt main thread via queued signals and coalesced in a 100 ms timer so
that rapid PV changes produce a single tree repaint rather than per-update redraws.
PV names are fetched from the live RunEngine environment via `function_execute` on each
environment open; the device fingerprint is checked before rebuilding subscriptions so
that a ZMQ reconnect without a genuine environment restart does not re-subscribe
unnecessarily. Units and descriptions are cached to disk (`~/.easy_bluesky/device_metadata.json`)
after the first real-beamline session and reused in simulation mode without network access.

Motor-type devices — those exposing a `user_setpoint` signal — are provided with an
inline tweak widget (◀ step ▶) for manual positioning without opening a separate motor
screen. Mouse-wheel events on the step spinbox are suppressed to prevent accidental motor
moves during scrolling.

In simulation mode (§3.4), the device tree polls the RunEngine environment at 2 Hz via
`read_devices_status` and draws units and descriptions from the local cache. A search bar
filters the tree by device name, class, or description in real time.

### 3.4 Simulation Mode

EasyBluesky includes a simulation subsystem that allows experimenters to develop and test
measurement sequences on any computer, with no connection to beamline hardware. A menu
action (*File → Generate Sim Devices*) parses the real devices file and generates a
corresponding `devices_sim.py` that instantiates ophyd `SynAxis` and `SynGauss`
surrogates for each real motor and detector. A dedicated connection profile pointing at
this devices file starts a queue-server that executes plans against the synthetic devices,
exercising the full Bluesky data pipeline and producing JSONL output indistinguishable in
structure from real data. This capability is particularly valuable for training new users
and for verifying complex multi-step sequences before committing to scarce beamtime.

### 3.5 Plan Development Environment

An integrated code editor tab (Fig. 2d) provides syntax-highlighted editing of local plan
files, with the ability to add and remove plan directories that are automatically uploaded
to the remote RunEngine environment. Adding a directory triggers an automatic environment
reload cycle (close → upload → reopen), so newly written plans become available without
manual intervention. A keyboard shortcut (Ctrl+/) toggles line comments, the editor
supports standard undo/redo, and a floating Find / Replace bar (Ctrl+F / Ctrl+R) is
available in all text panels including the RE Console.

### 3.6 Visual Plan Composer

The Visual Plan Composer (Fig. 2e) provides a drag-and-drop interface for assembling
measurement sequences without writing Python code. Scan blocks are selected from a
library that includes `rel_scan`, `grid_scan`, `list_scan`, `count`, `mv`, `sleep`, and
custom energy-looping variants; each block exposes its arguments through typed form fields.
Blocks can be reordered by drag-and-drop and nested to produce inner–outer motor
combinations, enabling complex multi-dimensional scan patterns — for example, stepping an
energy monochromator in an outer loop while running a spatial grid at each energy point —
through point-and-click interaction. The Composer generates a complete Python plan
definition that is displayed in the adjacent code editor and can be submitted to the queue
directly, giving experienced users a starting point for further customisation.

### 3.7 ESAF Integration

At facilities that issue beam time through structured proposal and safety systems, manual
transcription of proposal metadata into every experiment introduces both effort and the
risk of errors. EasyBluesky integrates with Experiment Safety Assessment Form (ESAF)
databases via a bundled FastAPI REST server, local PDF parsing, or manual entry
(Fig. 2f). Selecting an ESAF from the picker dialog automatically creates the experiment
directory in a standard hierarchy (`<PI group>/<ESAF ID>_<date>/`) and registers PI name,
proposal title, ESAF identifier, user list, and institution as metadata fields. These
fields are injected into the `md` (metadata) dictionary of every plan added to the queue,
so they appear verbatim in run documents stored in MongoDB and in JSONL run files.

A technique dropdown in the ESAF picker narrows results to the relevant measurement
modality, and a client-side regex field searches across all record fields — PI name,
proposal title, ESAF ID, users, and institution — without a server round-trip. Arbitrary
extra fields (custom key-value pairs) can be attached to any ESAF record and are preserved
across synchronisation with the remote server. The REST server supports both MongoDB and
SQLite backends, so facilities without a MongoDB installation can use a local SQLite file.
Although the ESAF format used here is specific to the Advanced Photon Source, the
integration pattern — form-based metadata picker feeding a standard folder hierarchy and
plan metadata injection — is directly transferable to any facility with a structured
proposal system.

### 3.8 Multi-client Safety

Synchrotron beamlines are frequently shared between two or more operator workstations:
for example, a beamline scientist's desktop running alongside an experimenter's laptop, or
a Windows control-room terminal and a remote Mac session connected over a VPN. The
bluesky-queueserver protocol does not itself restrict the number of simultaneous ZMQ
clients, so concurrent operation is technically possible but introduces the risk of
conflicting commands or ambiguous state.

EasyBluesky addresses this through a multi-layered safety mechanism. An **operator lock**
file (`/tmp/.easy_bluesky_<profile>.operator`) is written to the remote machine on
connect and read by every subsequent client. If the file belongs to a different host, the
joining client is presented with a dialog offering either to claim exclusive control
(with an explicit warning that the incumbent operator is not notified) or to continue in
observer mode in which RunEngine Manager start and stop are disabled. Locks older than four
hours are treated as abandoned and reclaimed automatically. The lock is released on
disconnect or application quit.

A **connected-clients chip** in the toolbar displays the live count of clients sharing the
queue-server in real time, coloured green when the local machine is the sole operator and
amber when multiple clients are active. Hovering over the chip shows the IP address of
each connected client. The client list is determined every 30 seconds by combining two
independent sources: short-lived heartbeat files written by each client to the remote
machine at each poll (`/tmp/.easy_bluesky_<slug>_client_<hostname>.json`, expiring after
90 seconds of inactivity), and active TCP connections to the ZMQ control and info ports
observed via `ss -tn` on the remote host. Using both sources provides resilience: heartbeat
files record clients that are connected but momentarily quiet; the `ss` query catches
clients that missed a heartbeat but still hold an open socket.

When a second client connects to an already-occupied queue-server, the first client is
notified in the RE Console within one 30-second polling interval. At startup, the client
count check runs before the experiment selection dialog so that the user is aware of any
co-operators before choosing a measurement folder.

A further ambiguity arises when the `manager_state` field returned by the queue-server
reports `executing_task` while `re_state` is idle. This state is entered whenever any
client calls `function_execute` — including routine device-status polling from a second
client — and is therefore not an indication that the local client's queue is active.
EasyBluesky distinguishes these cases by tracking the task name of every `function_execute`
call it initiates (`_current_task`). When `executing_task` is observed without a matching
local task, an amber **BUSY (ext)** chip is shown in the RE state indicator after a 1.5 s
debounce, signalling external activity without alarming the operator. Local tasks are shown
as **BUSY: \<task name\>** without delay. This distinction prevents the Start Queue button
from being incorrectly disabled during sub-second device polls from a second client.

### 3.9 Integrated Curve Fitting

The MongoDB Browser and HDF5 Viewer tabs provide an interactive curve-fitting module
built on the lmfit library (Newville *et al.*, 2014). The fit dialog is non-modal and
displays a live preview curve on the plot immediately when opened, updating in real time
as parameters are adjusted. Five peak models (Gaussian, Lorentzian, Voigt, pseudo-Voigt,
and split-Lorentzian), four step and interface models, and four polynomial background
terms can be combined freely; six minimisation algorithms are available. Multi-peak fits
are supported through a peak-count spinbox that composes an *N*-component model with
prefixed parameters and uses a `scipy.signal.find_peaks`-based initial guess.

Fit parameters are remembered **per dataset**, keyed by the combination of run UIDs,
stream name, and selected X and Y fields. Re-opening the fit dialog for a previously
fitted dataset automatically restores the saved model and parameter values and re-runs
the fit, restoring the full results table and curve overlay without user intervention.
Switching to a different dataset opens a fresh dialog with no pre-loaded state.

First- and second-order derivatives are available directly in the browser via a Deriv
dropdown using central differences (`numpy.gradient`), producing a result with the same
number of points at the original X positions without any shift. Error bars are propagated
using the exact central-difference stencil formula. Fit overlays receive the same
derivative and log-Y transforms applied to the data, so they remain correctly aligned
after the view is changed. Fitted parameters and curves can be exported to CSV or copied
to the clipboard.

---

## 4. Application at the ASWAXS Beamline

EasyBluesky was developed and is in active use at the ASWAXS beamline
[*full beamline description*]. The beamline uses a Pilatus 300K area detector (Dectris
Ltd) controlled through an ophyd `EpicsAreaDetector` device. [*Add 2–3 sentences
describing a representative experiment — e.g. a SAXS or WAXS scan, number of plans in
the queue, data volume — and include the scan data figure here.*]

Fig. 3 shows a representative motor scan acquired through EasyBluesky.
[*Describe the scan: sample, motor, range, step size, signal, and what the peak
represents.*] The scan was defined and submitted to the queue entirely through the
graphical interface without user interaction with a Python interpreter. The RunEngine
Manager ran on a dedicated Linux workstation and was controlled from a MacBook Pro over
the facility network; latency between queue operations and RunEngine response was
consistently below 200 ms.

---

## 5. Conclusions

EasyBluesky provides a complete, self-contained graphical interface to the Bluesky
queue-server that requires no Python knowledge to operate. By encapsulating SSH
connectivity, RunEngine lifecycle management, EPICS device monitoring, experiment logging,
Visual Plan Composer, and plan development in a single desktop application, it
substantially lowers the barrier to autonomous operation of Bluesky-controlled beamlines.
The simulation mode enables beamtime preparation on personal hardware and supports user
training independent of beamline availability.

Beyond single-user operation, EasyBluesky addresses the practical reality of shared
beamline infrastructure through its multi-client safety layer. The combination of operator
locking, live client-count display, and BUSY-state disambiguation makes concurrent ZMQ
connections safe and transparent in a way that the queue-server protocol alone does not
provide. ESAF integration reduces the manual overhead of experiment bookkeeping and ensures
that proposal metadata is captured consistently in every run document. The integrated curve
fitting module with per-dataset memory closes the analysis loop within the acquisition
interface, reducing the time between scan completion and quantitative result.

EasyBluesky is open source, actively maintained, and designed to be deployable at any
facility running bluesky-queueserver 0.0.25 or later. Although developed at the ASWAXS
beamline at NSF's ChemMatCARS (Sector 15, Advanced Photon Source), the application makes
no assumptions about the underlying control system beyond the presence of a
bluesky-queueserver ZMQ interface, and has been tested against both EPICS-based real
beamlines and fully simulated environments. Source code, installation instructions, and
documentation are available at https://github.com/nayanbera/easy-bluesky.

---

## Acknowledgements

[*Funding sources, beamline staff contributions, facility acknowledgement.*]

---

## Figure Captions

**Figure 1.** Architecture of the EasyBluesky client–server system. The desktop
application runs on the experimenter's local workstation (Mac or PC) and communicates
with the bluesky-queueserver running on a dedicated beamline Linux computer via three
independent channels. (i) ZMQ REQ/REP sockets (control port 60615, info port 60625) are
used by the `ZMQWorker` poll thread for queue operations and 1 Hz status polling; a
separate ZMQ SUB socket on the document port feeds the Live Viewer with run documents in
real time. (ii) SSH (Paramiko, key-pair only) is used by a persistent `_SSHLogTailer`
thread to stream the RE Manager log via `tail -f`, and by explicit commands for lifecycle
operations (start, stop, restart) and SFTP upload of the startup script, permissions
file, and devices file. (iii) EPICS Channel Access (CA) subscriptions are established
directly from the local machine to the beamline control system, bypassing the
queue-server entirely for low-latency, sub-millisecond device readback. PV names are
fetched once per environment open via `function_execute`. For shared-beamline deployments,
an operator lock file and client heartbeat files are maintained on the remote machine via
the SSH channel; active TCP connections to the ZMQ ports are additionally queried via
`ss -tn` to provide a second, independent view of the connected-client count. All
blocking operations (pymongo queries, HDF5 export, PV name retrieval, SFTP upload) run in
`QThread` worker objects; the Qt main thread handles only widget updates and user
actions.

**Figure 2.** EasyBluesky graphical interface. *(a)* Main application window showing the
tab-based layout: Experiments, Queue Manager, Available Devices & Plans, Code Editor,
RE Console, and Data Browser tabs are accessible from the top navigation bar. The
persistent RE control bar at the top displays the current RunEngine state chip (IDLE,
RUNNING, PAUSED, BUSY, or BUSY (ext)), environment state, and the connected-clients chip
(green for sole operator, amber when sharing). *(b)* Queue Manager tab showing the plan
queue (left panel) with drag-and-drop reordering, queue execution controls (Start, Pause,
Resume, Abort, Stop), Auto-start checkbox, and Loop controls with an iteration counter.
Each queued plan displays its pre-assigned scan number (#N). The right panel shows the
plan detail view and RE console output. *(c)* Available Devices tab displaying the live
device tree with columns for device class, current value, engineering units, and EPICS
description. Motor-type devices expose inline tweak controls (◀ step ▶) for manual
positioning. The search bar above the tree filters entries by name, class, or description.
*(d)* Code Editor tab with syntax-highlighted Python plan editing, Plan Files management
(add/remove local plan directories), and the integrated plan builder for constructing
queue items from registered plans without writing code. *(e)* Visual Plan Composer showing
drag-and-drop scan blocks arranged into a multi-step sequence with inner and outer motor
loops; the right panel shows the generated Python code. *(f)* ESAF picker dialog showing
the technique filter dropdown, client-side regex search field, and the selected ESAF record
with PI name, proposal title, and user list; clicking Launch creates the experiment folder
and registers the metadata.

**Figure 3.** Representative motor scan acquired using EasyBluesky at the ASWAXS
beamline. [*Motor name*] was stepped through [*range and units*] in [*N*] steps; the
signal recorded at each point was [*detector/channel*]. The peak at [*position and
units*] corresponds to [*physical meaning — e.g. the rocking curve maximum, the
knife-edge midpoint*]. The scan was defined and submitted to the queue entirely through
the EasyBluesky graphical interface without user interaction with a Python interpreter.
Data were written automatically to a JSONL run file and are shown here as plotted by
[*your plotting tool — e.g. the integrated data browser / matplotlib*].

---

## References

Allan, D., Caswell, T., Campbell, S. & Rakitin, M. (2019). Bluesky's ahead: A
multi-facility collaboration for an a la carte software project for data acquisition and
management. *Synchrotron Radiation News*, **32**(3), 19–22.

Brookhaven National Laboratory (2014). ophyd: Python hardware abstraction library for
the Bluesky ecosystem. https://github.com/bluesky/ophyd

Caswell, T. A., Allan, D., Campbell, S., Lauer, K., Rakitin, M., Sauter, N. &
Sutton, M. (2021). bluesky-queueserver: A queue for bluesky plans.
https://github.com/bluesky/bluesky-queueserver

Chin, L. & Leiserson, C. E. (2022). PyZMQ: Python bindings for ZeroMQ.
https://github.com/zeromq/pyzmq

Engström, J. (2020). Paramiko: Python SSH2 library. https://github.com/paramiko/paramiko

Newville, M., Stensitzki, T., Allen, D. B. & Ingargiola, A. (2014). LMFIT: Non-linear
least-square minimization and curve-fitting for Python. *Zenodo*.
https://doi.org/10.5281/zenodo.11813

Rivers, M. (2012). pyepics: Python interface to EPICS Channel Access.
*Proceedings of the 14th International Conference on Accelerator and Large Experimental
Physics Control Systems (ICALEPCS 2013)*, San Francisco, CA.

The Qt Company (2022). Qt Framework (version 6). https://www.qt.io

[*Add suitcase.jsonl / event-model reference if published*]
[*Add MongoDB reference if needed by journal style*]
