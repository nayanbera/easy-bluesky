/* EasyBluesky Blockly host page: toolbox, workspace, Qt bridge. */

function num(n) { return { shadow: { type: 'math_number', fields: { NUM: n } } }; }

const TOOLBOX = {
  kind: 'categoryToolbox',
  contents: [
    { kind: 'category', name: 'Motion', colour: EB_COLOURS.motion, contents: [
      { kind: 'block', type: 'eb_mv', inputs: { VALUE: num(0) } },
      { kind: 'block', type: 'eb_mvr', inputs: { VALUE: num(0) } },
      { kind: 'block', type: 'eb_mv_multi', inputs: { POS0: num(0), POS1: num(0) } },
      { kind: 'block', type: 'eb_set_attr', inputs: { VALUE: num(1) } },
    ] },
    { kind: 'category', name: 'Actions', colour: EB_COLOURS.action, contents: [
      { kind: 'block', type: 'eb_sleep', inputs: { SECONDS: num(1) } },
      { kind: 'block', type: 'eb_open_shutter' },
      { kind: 'block', type: 'eb_close_shutter' },
      { kind: 'block', type: 'eb_stage' },
      { kind: 'block', type: 'eb_unstage' },
      { kind: 'block', type: 'eb_trigger_read' },
    ] },
    { kind: 'category', name: 'Scans', colour: EB_COLOURS.scan, contents: [
      { kind: 'block', type: 'eb_scan', inputs: { START: num(-1), STOP: num(1), NUM: num(21) } },
      { kind: 'block', type: 'eb_grid_scan', inputs: { START1: num(0), STOP1: num(1), NUM1: num(5),
                                                       START2: num(0), STOP2: num(1), NUM2: num(5) } },
      { kind: 'block', type: 'eb_count', inputs: { NUM: num(1), DELAY: num(0) } },
    ] },
    { kind: 'category', name: 'Plans', colour: EB_COLOURS.plan, contents: [
      { kind: 'block', type: 'eb_plan_call' },
      { kind: 'block', type: 'eb_plan' },
      { kind: 'block', type: 'eb_device' },
    ] },
    { kind: 'category', name: 'Statistics', colour: EB_COLOURS.stats, contents: [
      { kind: 'block', type: 'eb_stats' },
      { kind: 'block', type: 'eb_stat_value' },
    ] },
    { kind: 'sep' },
    { kind: 'category', name: 'Loops', colour: EB_COLOURS.loop, contents: [
      { kind: 'block', type: 'eb_foreach' },
      { kind: 'block', type: 'controls_repeat_ext', inputs: { TIMES: num(3) } },
      { kind: 'block', type: 'controls_whileUntil' },
      { kind: 'block', type: 'controls_for', inputs: { FROM: num(1), TO: num(10), BY: num(1) } },
      { kind: 'block', type: 'controls_forEach' },
      { kind: 'block', type: 'controls_flow_statements' },
    ] },
    { kind: 'category', name: 'Logic', colour: '#5C81A6', contents: [
      { kind: 'block', type: 'controls_if' },
      { kind: 'block', type: 'logic_compare' },
      { kind: 'block', type: 'logic_operation' },
      { kind: 'block', type: 'logic_negate' },
      { kind: 'block', type: 'logic_boolean' },
      { kind: 'block', type: 'logic_null' },
    ] },
    { kind: 'category', name: 'Math', colour: '#59C059', contents: [
      { kind: 'block', type: 'math_number' },
      { kind: 'block', type: 'math_arithmetic', inputs: { A: num(1), B: num(1) } },
      { kind: 'block', type: 'math_single', inputs: { NUM: num(9) } },
      { kind: 'block', type: 'math_round', inputs: { NUM: num(3.1) } },
      { kind: 'block', type: 'math_on_list' },
    ] },
    { kind: 'category', name: 'Text', colour: '#5CB1D6', contents: [
      { kind: 'block', type: 'text' },
      { kind: 'block', type: 'text_join' },
    ] },
    { kind: 'category', name: 'Lists', colour: '#745CA6', contents: [
      { kind: 'block', type: 'lists_create_with' },
      { kind: 'block', type: 'lists_length' },
      { kind: 'block', type: 'lists_getIndex' },
    ] },
    { kind: 'category', name: 'Variables', colour: '#FF8C1A', custom: 'VARIABLE' },
    { kind: 'sep' },
    { kind: 'category', name: 'Python', colour: EB_COLOURS.python, contents: [
      { kind: 'block', type: 'eb_python' },
      { kind: 'block', type: 'eb_python_expr' },
    ] },
  ],
};

function makeTheme(c) {
  return Blockly.Theme.defineTheme('eb_' + Date.now(), {
    base: Blockly.Themes.Zelos,
    componentStyles: {
      workspaceBackgroundColour: c.bg,
      toolboxBackgroundColour: c.panel,
      toolboxForegroundColour: c.fg,
      flyoutBackgroundColour: c.panel,
      flyoutForegroundColour: c.fg,
      flyoutOpacity: 1,
      scrollbarColour: c.scroll,
      scrollbarOpacity: 0.6,
      insertionMarkerColour: '#ffffff',
      insertionMarkerOpacity: 0.3,
      markerColour: '#ffffff',
      cursorColour: '#d0d0d0',
    },
  });
}

let colors = { bg: '#1e1e1e', panel: '#252526', fg: '#d4d4d4', scroll: '#555555' };
const workspace = Blockly.inject('blocklyDiv', {
  toolbox: TOOLBOX,
  renderer: 'zelos',
  theme: makeTheme(colors),
  grid: { spacing: 24, length: 2, colour: 'rgba(128,128,128,0.25)', snap: true },
  zoom: { controls: true, wheel: true, startScale: 0.9, maxScale: 2, minScale: 0.4 },
  trashcan: true,
  move: { scrollbars: true, drag: true, wheel: false },
  sounds: false,
  media: 'media/',
});

function generateBody() {
  EB.summary = [];
  try {
    return gen.workspaceToCode(workspace);
  } catch (e) {
    return '# code generation error: ' + e;
  }
}

// ── Validation: attach warning icons to suspicious blocks ───────────────────
const SCAN_X_SUFFIXES = ['', '_setpoint', '_user_setpoint', '_readback', '_user_readback'];

function validate() {
  const dev = EB.devices;
  const haveDevs = dev.all.length > 0;
  const all = workspace.getAllBlocks(false);
  const prefixes = new Set(all.filter((b) => b.type === 'eb_stats')
    .map((b) => ident(b.getFieldValue('PREFIX'), 'stat')));
  const msgs = [];
  all.forEach((b) => {
    if (!b.type.startsWith('eb_')) return;
    const w = [];
    if (haveDevs) {
      b.inputList.forEach((inp) => inp.fieldRow.forEach((f) => {
        const nm = f.name || '';
        if (!/^(DEV\d*|MOTOR\d*)$/.test(nm) || typeof f.getValue !== 'function') return;
        const v = f.getValue();
        if (!dev.all.includes(v)) w.push(`'${v}' is not in the loaded device list.`);
        else if (/^MOTOR/.test(nm) || /^eb_(mv|mvr|mv_multi)$/.test(b.type)) {
          if (!dev.motors.includes(v)) w.push(`'${v}' is not a movable device.`);
        }
      }));
    }
    if (b.type === 'eb_foreach') {
      const motors = splitCsv(b.getFieldValue('MOTORS'));
      const vars = splitCsv(b.getFieldValue('VARS'));
      if (motors.length && motors.length !== vars.length) {
        w.push(`${motors.length} motor(s) but ${vars.length} loop variable(s): they must match to be moved together.`);
      }
    }
    if (b.type === 'eb_stats') {
      const scans = b.getDescendants(false).filter((d) => d.type === 'eb_scan');
      if (!b.getInputTargetBlock('DO')) w.push('Put a scan block inside the Statistics block.');
      if (!(b.getFieldValue('Y') || '').trim()) w.push('Signal (y) is empty.');
      if (scans.length) {
        const m = scans[0].getFieldValue('MOTOR');
        const x = (b.getFieldValue('X') || '').trim();
        if (!SCAN_X_SUFFIXES.some((s) => x === m + s)) {
          w.push(`x '${x}' does not look like the scanned motor '${m}' (expected '${m}' or '${m}_setpoint').`);
        }
      }
    }
    if (b.type === 'eb_stat_value') {
      const p = ident(b.getFieldValue('PREFIX'), 'stat');
      if (!prefixes.has(p)) w.push(`No Statistics block uses the variable prefix '${p}'.`);
    }
    if (b.type === 'eb_plan_call') {
      const name = b.getFieldValue('PLAN');
      if (dev.plans.length && !dev.plans.includes(name)) w.push(`Plan '${name}' is not registered on the RE Manager.`);
      const missing = (b.params_ || []).filter((p) => p.required && !p.hidden && !b.getInputTargetBlock('ARG_' + p.name))
        .map((p) => p.name);
      if (missing.length) w.push('Missing required parameter(s): ' + missing.join(', '));
    }
    b.setWarningText(w.length ? w.join('\n') : null);
    w.forEach((m) => msgs.push(m));
  });
  return msgs;
}

let timer = null;
function notify() {
  if (!EB.bridge) return;
  clearTimeout(timer);
  timer = setTimeout(() => {
    EB.bridge.onChanged(JSON.stringify(Blockly.serialization.workspaces.save(workspace)), generateBody(),
      JSON.stringify(validate()), JSON.stringify(EB.summary));
  }, 120);
}
workspace.addChangeListener((e) => {
  if (e.isUiEvent) return;
  notify();
});

// ── API called from Qt ───────────────────────────────────────────────────────
EB.setDevices = function (json) {
  const d = JSON.parse(json);
  EB.devices = { motors: d.motors || [], all: d.all || [], plans: d.plans || [], planParams: d.planParams || {} };
  notify();
};
EB.setColors = function (json) {
  colors = JSON.parse(json);
  workspace.setTheme(makeTheme(colors));
};
function collectNames(o) {
  if (Array.isArray(o)) { o.forEach(collectNames); return; }
  if (o && typeof o === 'object') {
    if (o.fields) {
      for (const k of Object.keys(o.fields)) {
        if (/^(DEV\d*|MOTOR\d*|PLAN)$/.test(k) && typeof o.fields[k] === 'string') EB.extraNames.add(o.fields[k]);
      }
    }
    Object.values(o).forEach(collectNames);
  }
}
EB.load = function (json) {
  const data = JSON.parse(json);
  collectNames(data);
  workspace.clear();
  Blockly.serialization.workspaces.load(data, workspace);
  notify();
};
EB.setCode = function (id, code) {
  const b = workspace.getBlockById(id);
  if (b) b.setFieldValue(code, 'CODE');
};
EB.clear = function () { workspace.clear(); notify(); };
EB.save = function () { return JSON.stringify(Blockly.serialization.workspaces.save(workspace)); };
EB.generate = generateBody;
EB.resize = function () { Blockly.svgResize(workspace); };
window.addEventListener('resize', EB.resize);

new QWebChannel(qt.webChannelTransport, (channel) => {
  EB.bridge = channel.objects.bridge;
  EB.bridge.onReady();
  notify();
});
