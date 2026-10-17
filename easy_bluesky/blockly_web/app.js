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
    { kind: 'category', name: 'Plans', colour: EB_COLOURS.plan, contents: [
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
  try {
    return gen.workspaceToCode(workspace);
  } catch (e) {
    return '# code generation error: ' + e;
  }
}

let timer = null;
function notify() {
  if (!EB.bridge) return;
  clearTimeout(timer);
  timer = setTimeout(() => {
    EB.bridge.onChanged(JSON.stringify(Blockly.serialization.workspaces.save(workspace)), generateBody());
  }, 120);
}
workspace.addChangeListener((e) => {
  if (e.isUiEvent) return;
  notify();
});

// ── API called from Qt ───────────────────────────────────────────────────────
EB.setDevices = function (json) {
  const d = JSON.parse(json);
  EB.devices = { motors: d.motors || [], all: d.all || [], plans: d.plans || [] };
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
        if (/^(DEV\d*|MOTOR|PLAN)$/.test(k) && typeof o.fields[k] === 'string') EB.extraNames.add(o.fields[k]);
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
