/* EasyBluesky custom Blockly blocks + Python generators.
 *
 * Statement blocks generate `yield from ...` lines (bluesky plan stubs).
 * Device names come from EB.devices (set by the Qt side); unknown names that
 * appear in a loaded workspace are kept in EB.extraNames so dropdowns accept them.
 */
const EB = {
  devices: { motors: [], all: [], plans: [] },
  extraNames: new Set(),
};

// Colours — Scratch-like category palette
const EB_COLOURS = {
  motion: '#4C97FF', action: '#9966FF', plan: '#59C059',
  loop: '#FFAB19', stats: '#00A6A6', python: '#6B6B6B', scan: '#0FBD8C',
};

function ebOptions(list) {
  return function () {
    const names = new Set(list());
    EB.extraNames.forEach((n) => names.add(n));
    const arr = Array.from(names).filter((n) => n !== '').sort();
    if (arr.length === 0) arr.push('motor');
    return arr.map((n) => [n, n]);
  };
}
const motorOptions = ebOptions(() => EB.devices.motors);
// Device dropdowns also offer the plan's own device parameters (e.g. the `detectors` list)
const deviceOptions = function () {
  const lists = new Set(EB.devices.listParams || []);
  const params = EB.devices.paramNames || [];
  const base = ebOptions(() => EB.devices.all)().filter((o) => !params.includes(o[1]));
  const extra = params.map((n) => [lists.has(n) ? `${n}  (all in list)` : `${n}  (plan parameter)`, n]);
  return extra.concat(base);
};
const isListParam = (name) => (EB.devices.listParams || []).includes(name);
const planOptions = ebOptions(() => EB.devices.plans);

const PLUS_SVG = 'data:image/svg+xml;utf8,' + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><circle cx="10" cy="10" r="9" fill="rgba(255,255,255,0.35)"/>' +
  '<path d="M5 10h10M10 5v10" stroke="white" stroke-width="2.4" stroke-linecap="round"/></svg>');
const MINUS_SVG = 'data:image/svg+xml;utf8,' + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><circle cx="10" cy="10" r="9" fill="rgba(255,255,255,0.35)"/>' +
  '<path d="M5 10h10" stroke="white" stroke-width="2.4" stroke-linecap="round"/></svg>');

function safeId(block) { return block.id.replace(/\W/g, '').slice(0, 8).toLowerCase(); }

// ── Motion ────────────────────────────────────────────────────────────────────
Blockly.Blocks['eb_mv'] = {
  init() {
    this.appendValueInput('VALUE').setCheck(null)
      .appendField('move').appendField(new Blockly.FieldDropdown(motorOptions), 'DEV').appendField('to');
    this.setInputsInline(true);
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setColour(EB_COLOURS.motion);
    this.setTooltip('Move a motor to an absolute position.\nyield from bps.mv(motor, position)');
  },
};

Blockly.Blocks['eb_mvr'] = {
  init() {
    this.appendValueInput('VALUE').setCheck(null)
      .appendField('move').appendField(new Blockly.FieldDropdown(motorOptions), 'DEV').appendField('by');
    this.setInputsInline(true);
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setColour(EB_COLOURS.motion);
    this.setTooltip('Move a motor by a relative amount.\nyield from bps.mvr(motor, delta)');
  },
};

Blockly.Blocks['eb_mv_multi'] = {
  init() {
    this.n_ = 2;
    this.appendDummyInput('HEAD')
      .appendField('move together')
      .appendField(new Blockly.FieldImage(PLUS_SVG, 18, 18, '+', () => this.addRow_()))
      .appendField(new Blockly.FieldImage(MINUS_SVG, 18, 18, '-', () => this.removeRow_()));
    for (let i = 0; i < this.n_; i++) this.addRowInput_(i);
    this.setInputsInline(false);
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setColour(EB_COLOURS.motion);
    this.setTooltip('Move several motors simultaneously (e.g. sx and sy as a pair).\n' +
      'yield from bps.mv(m1, p1, m2, p2, ...)\nUse + / - to add or remove motors.');
  },
  addRowInput_(i) {
    this.appendValueInput('POS' + i).setCheck(null)
      .appendField(new Blockly.FieldDropdown(motorOptions), 'DEV' + i).appendField('→');
  },
  addRow_() { this.addRowInput_(this.n_); this.n_++; },
  removeRow_() {
    if (this.n_ <= 1) return;
    this.n_--; this.removeInput('POS' + this.n_);
  },
  saveExtraState() { return { n: this.n_ }; },
  loadExtraState(s) {
    const n = (s && s.n) || 2;
    while (this.n_ < n) this.addRow_();
    while (this.n_ > n) this.removeRow_();
  },
};

Blockly.Blocks['eb_set_attr'] = {
  init() {
    this.appendValueInput('VALUE').setCheck(null)
      .appendField('set').appendField(new Blockly.FieldDropdown(deviceOptions), 'DEV')
      .appendField('.').appendField(new Blockly.FieldTextInput('cam.acquire_time'), 'ATTR').appendField('to');
    this.setInputsInline(true);
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setColour(EB_COLOURS.motion);
    this.setTooltip('Set a device attribute (e.g. Pil300K . cam.acquire_time).\nyield from bps.mv(device.attr, value)');
  },
};

// ── Actions ───────────────────────────────────────────────────────────────────
Blockly.Blocks['eb_sleep'] = {
  init() {
    this.appendValueInput('SECONDS').setCheck(null).appendField('wait');
    this.appendDummyInput().appendField('seconds');
    this.setInputsInline(true);
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setColour(EB_COLOURS.action);
    this.setTooltip('yield from bps.sleep(seconds)');
  },
};

function simpleDeviceBlock(type, label, colour, tip) {
  Blockly.Blocks[type] = {
    init() {
      this.appendDummyInput().appendField(label)
        .appendField(new Blockly.FieldDropdown(deviceOptions), 'DEV');
      this.setPreviousStatement(true); this.setNextStatement(true);
      this.setColour(colour); this.setTooltip(tip);
    },
  };
}
// open/close shutter: no longer in the toolbox (use "move shutter to 'open'"); kept so old workspaces still load
simpleDeviceBlock('eb_open_shutter', 'open shutter', EB_COLOURS.action, "yield from bps.mv(shutter, 'open')");
simpleDeviceBlock('eb_close_shutter', 'close shutter', EB_COLOURS.action, "yield from bps.mv(shutter, 'closed')");
simpleDeviceBlock('eb_stage', 'stage', EB_COLOURS.action, 'Arm a device for acquisition.\nyield from bps.stage(device)');
simpleDeviceBlock('eb_unstage', 'unstage', EB_COLOURS.action, 'Disarm a device.\nyield from bps.unstage(device)');

Blockly.Blocks['eb_run'] = {
  init() {
    this.appendDummyInput().appendField('run');
    this.appendStatementInput('DO');
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setColour(EB_COLOURS.action);
    this.setTooltip('Everything inside is recorded as ONE run: open run, the blocks, close run.\n' +
      'Put trigger & read blocks inside. Scan blocks open their own run — do not put them inside.\n' +
      'yield from bps.open_run(md=md) ... yield from bps.close_run()');
  },
};
// separate open/close blocks: superseded by "run" (kept so old workspaces still load)
Blockly.Blocks['eb_open_run'] = {
  init() {
    this.appendDummyInput().appendField('open run');
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setColour(EB_COLOURS.action);
    this.setTooltip('Start a new run (one start document); trigger & read blocks then add events to it.\n' +
      'Pair with "close run". Scan blocks open their own run — do not put them inside.\n' +
      'yield from bps.open_run(md=md)');
  },
};
Blockly.Blocks['eb_close_run'] = {
  init() {
    this.appendDummyInput().appendField('close run');
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setColour(EB_COLOURS.action);
    this.setTooltip('Finish the current run (stop document).\nyield from bps.close_run()');
  },
};

Blockly.Blocks['eb_trigger_read'] = {
  init() {
    this.appendDummyInput().appendField('trigger & read')
      .appendField(new Blockly.FieldTextInput('[det]'), 'DETS');
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setColour(EB_COLOURS.action);
    this.setTooltip('Trigger detectors and read them into the data stream.\n' +
      'yield from bps.trigger_and_read([det1, det2])');
  },
};

// ── Plans ─────────────────────────────────────────────────────────────────────
Blockly.Blocks['eb_plan'] = {
  init() {
    this.appendDummyInput().appendField('run plan')
      .appendField(new Blockly.FieldDropdown(planOptions), 'PLAN')
      .appendField('args').appendField(new Blockly.FieldTextInput(''), 'ARGS');
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setColour(EB_COLOURS.plan);
    this.setTooltip('Run another registered plan.\nyield from plan_name(args)\n' +
      'args: e.g.  [det], sx, 0, 10, 11   or   num=11, delay=0.5');
  },
};

// ── Loops ─────────────────────────────────────────────────────────────────────
Blockly.Blocks['eb_foreach'] = {
  init() {
    this.appendDummyInput('HEAD').appendField('for each')
      .appendField(new Blockly.FieldTextInput('_sx, _sy'), 'VARS')
      .appendField('in')
      .appendField(new Blockly.FieldDropdown([['list', 'list'], ['range', 'range'], ['CSV file', 'csv']],
        (v) => { this.showMode_(v); return v; }), 'MODE');
    this.appendDummyInput('LIST').appendField(new Blockly.FieldTextInput('(0, 0), (5, -2.5), (10, -5)'), 'VALUES');
    this.appendDummyInput('RANGE')
      .appendField('start').appendField(new Blockly.FieldNumber(0), 'START')
      .appendField('stop').appendField(new Blockly.FieldNumber(1), 'STOP')
      .appendField('N').appendField(new Blockly.FieldNumber(10, 2, 100000, 1), 'NUM');
    this.appendDummyInput('CSV')
      .appendField('file').appendField(new Blockly.FieldTextInput('data.csv'), 'FILE')
      .appendField('columns').appendField(new Blockly.FieldTextInput('0, 1'), 'COLS');
    this.appendDummyInput('MOTORS').appendField('move motors')
      .appendField(new Blockly.FieldTextInput('sx, sy'), 'MOTORS')
      .appendField('(optional)');
    this.appendStatementInput('DO').appendField('do');
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setColour(EB_COLOURS.loop);
    this.setTooltip('Loop over a list, a range, or CSV column(s).\n' +
      'Several variables (e.g. _sx, _sy) unpack tuples / multi-column CSV rows.\n' +
      'If motors are given (same count as variables) they are moved together each iteration.');
    this.showMode_('list');
  },
  showMode_(mode) {
    this.getInput('LIST').setVisible(mode === 'list');
    this.getInput('RANGE').setVisible(mode === 'range');
    this.getInput('CSV').setVisible(mode === 'csv');
    if (this.rendered) this.queueRender();
  },
  saveExtraState() { return { mode: this.getFieldValue('MODE') }; },
  loadExtraState(s) { this.showMode_((s && s.mode) || 'list'); },
};

// ── Statistics ────────────────────────────────────────────────────────────────
Blockly.Blocks['eb_stats'] = {
  init() {
    this.appendDummyInput().appendField('statistics   x')
      .appendField(new Blockly.FieldTextInput('fipi2_setpoint'), 'X')
      .appendField('y').appendField(new Blockly.FieldTextInput('sbpmTot'), 'Y')
      .appendField('variables').appendField(new Blockly.FieldTextInput('stat'), 'PREFIX');
    this.appendDummyInput().appendField('move to')
      .appendField(new Blockly.FieldDropdown([['nothing', 'none'], ['CEN', 'cen'], ['COM', 'com'],
        ['MAX', 'max'], ['MIN', 'min']]), 'MOVE')
      .appendField(new Blockly.FieldDropdown(motorOptions), 'MOTOR')
      .appendField('afterwards');
    this.appendStatementInput('DO').appendField('scan');
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setColour(EB_COLOURS.stats);
    this.setTooltip('Wrap scan block(s): compute CEN / COM / MAX / MIN / FWHM of y vs x.\n' +
      'Results are available to later blocks via the "statistic" reporter\n' +
      '(<prefix>_cen, _com, _max, _max_y, _min, _min_y, _fwhm; None if not computable).\n' +
      'Optionally move a motor to the chosen statistic after the scan.\n' +
      'x must be the event-data field name, e.g. fipi2_setpoint.');
  },
};

Blockly.Blocks['eb_stat_value'] = {
  init() {
    this.appendDummyInput()
      .appendField(new Blockly.FieldDropdown([['CEN', 'cen'], ['COM', 'com'], ['MAX position', 'max'],
        ['MAX value', 'max_y'], ['MIN position', 'min'], ['MIN value', 'min_y'], ['FWHM', 'fwhm']]), 'STAT')
      .appendField('of')
      .appendField(new Blockly.FieldTextInput('stat'), 'PREFIX');
    this.setOutput(true, null);
    this.setColour(EB_COLOURS.stats);
    this.setTooltip('A statistic computed by a Statistics block (same variable prefix).');
  },
};

// ── Devices & Python ──────────────────────────────────────────────────────────
Blockly.Blocks['eb_device'] = {
  init() {
    this.appendDummyInput().appendField(new Blockly.FieldDropdown(deviceOptions), 'DEV');
    this.setOutput(true, null);
    this.setColour(EB_COLOURS.motion);
    this.setTooltip('A device object (e.g. to pass to another plan).');
  },
};

// Multi-line code field: clicking opens a Qt code dialog (Blockly 13 core has no
// multiline field).  The block shows the first non-blank line.
class CodeField extends Blockly.FieldTextInput {
  showEditor_() {
    if (EB.bridge) EB.bridge.editCode(this.getSourceBlock().id, this.getValue() || '');
  }
  getDisplayText_() {
    const lines = String(this.getValue() || '').split('\n').filter((l) => l.trim());
    let t = lines.length ? lines[0].trim() : '...';
    if (t.length > 42) t = t.slice(0, 40) + '…';
    return lines.length > 1 ? t + '  ⏎' : t;
  }
}

Blockly.Blocks['eb_python'] = {
  init() {
    this.appendDummyInput().appendField('python')
      .appendField(new CodeField('yield from bps.null()'), 'CODE');
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setColour(EB_COLOURS.python);
    this.setTooltip('Arbitrary Python plan code. Use "yield from bps.*" / "yield from bp.*".');
  },
};

Blockly.Blocks['eb_python_expr'] = {
  init() {
    this.appendDummyInput().appendField('py')
      .appendField(new Blockly.FieldTextInput('mono.position'), 'CODE');
    this.setOutput(true, null);
    this.setColour(EB_COLOURS.python);
    this.setTooltip('A Python expression, e.g. mono.position or np.linspace(0, 1, 5)');
  },
};

// ── Generators ────────────────────────────────────────────────────────────────
const gen = python.pythonGenerator;
const ORD = python.Order;
gen.INDENT = '    ';  // PEP 8 indentation in generated plans

function val(block, name, generator, dflt) {
  const v = generator.valueToCode(block, name, ORD.NONE);
  return v || dflt;
}
function splitCsv(text) {
  return String(text || '').split(',').map((s) => s.trim()).filter((s) => s);
}
function ident(text, dflt) {
  const t = String(text || '').trim().replace(/\W/g, '_');
  return (!t || /^\d/.test(t)) ? dflt : t;
}

gen.forBlock['eb_mv'] = (b, g) =>
  `yield from bps.mv(${b.getFieldValue('DEV')}, ${val(b, 'VALUE', g, '0')})\n`;
gen.forBlock['eb_mvr'] = (b, g) =>
  `yield from bps.mvr(${b.getFieldValue('DEV')}, ${val(b, 'VALUE', g, '0')})\n`;
gen.forBlock['eb_mv_multi'] = (b, g) => {
  const parts = [];
  for (let i = 0; i < b.n_; i++) parts.push(`${b.getFieldValue('DEV' + i)}, ${val(b, 'POS' + i, g, '0')}`);
  return `yield from bps.mv(${parts.join(', ')})\n`;
};
gen.forBlock['eb_set_attr'] = (b, g) => {
  const d = b.getFieldValue('DEV'), a = b.getFieldValue('ATTR'), v = val(b, 'VALUE', g, '0');
  if (isListParam(d)) return `yield from bps.mv(*[x for _d in ${d} for x in (_d.${a}, ${v})])\n`;
  return `yield from bps.mv(${d}.${a}, ${v})\n`;
};
gen.forBlock['eb_sleep'] = (b, g) => `yield from bps.sleep(${val(b, 'SECONDS', g, '1')})\n`;
gen.forBlock['eb_open_shutter'] = (b) => `yield from bps.mv(${b.getFieldValue('DEV')}, 'open')\n`;
gen.forBlock['eb_close_shutter'] = (b) => `yield from bps.mv(${b.getFieldValue('DEV')}, 'closed')\n`;
gen.forBlock['eb_stage'] = (b) => {
  const d = b.getFieldValue('DEV');
  return isListParam(d) ? `yield from bps.stage_all(*${d})\n` : `yield from bps.stage(${d})\n`;
};
gen.forBlock['eb_unstage'] = (b) => {
  const d = b.getFieldValue('DEV');
  return isListParam(d) ? `yield from bps.unstage_all(*${d})\n` : `yield from bps.unstage(${d})\n`;
};
gen.forBlock['eb_run'] = (b, g) => {
  note(b, 'run');
  const body = g.statementToCode(b, 'DO')
    .split('\n').map((ln) => (ln.startsWith(gen.INDENT) ? ln.slice(gen.INDENT.length) : ln)).join('\n');
  return 'yield from bps.open_run(md=md)\n' + body + (body && !body.endsWith('\n') ? '\n' : '') +
    'yield from bps.close_run()\n';
};
gen.forBlock['eb_open_run'] = (b) => { note(b, 'open run'); return 'yield from bps.open_run(md=md)\n'; };
gen.forBlock['eb_close_run'] = (b) => { note(b, 'close run'); return 'yield from bps.close_run()\n'; };
gen.forBlock['eb_trigger_read'] = (b) => `yield from bps.trigger_and_read(${b.getFieldValue('DETS') || '[]'})\n`;
gen.forBlock['eb_plan'] = (b) => {
  note(b, `plan ${b.getFieldValue('PLAN')}(${b.getFieldValue('ARGS')})`);
  return `yield from ${b.getFieldValue('PLAN')}(${b.getFieldValue('ARGS')})\n`;
};
gen.forBlock['eb_device'] = (b) => [b.getFieldValue('DEV'), ORD.ATOMIC];
gen.forBlock['eb_python'] = (b) => {
  const code = b.getFieldValue('CODE') || 'pass';
  return code + '\n';
};
gen.forBlock['eb_python_expr'] = (b) => [b.getFieldValue('CODE') || 'None', ORD.ATOMIC];
gen.forBlock['eb_stat_value'] = (b) =>
  [`${ident(b.getFieldValue('PREFIX'), 'stat')}_${b.getFieldValue('STAT')}`, ORD.ATOMIC];

// Step notes for the generated docstring (reset by generateBody in app.js).
EB.summary = [];
function note(block, text) {
  let depth = 0;
  for (let p = block.getSurroundParent(); p; p = p.getSurroundParent()) depth++;
  EB.summary.push('  '.repeat(depth) + text);
}

function bodyOrPass(code) { return code && code.trim() ? code : gen.INDENT + 'pass\n'; }

gen.forBlock['eb_foreach'] = (b, g) => {
  const vars = b.getFieldValue('VARS') || '_val';
  const mode = b.getFieldValue('MODE');
  let iterable;
  if (mode === 'range') {
    iterable = `np.linspace(${b.getFieldValue('START')}, ${b.getFieldValue('STOP')}, ${b.getFieldValue('NUM')})`;
  } else if (mode === 'csv') {
    const file = b.getFieldValue('FILE') || 'data.csv';
    const cols = splitCsv(b.getFieldValue('COLS')); if (!cols.length) cols.push('0');
    const idx = cols.map((c) => parseInt(c, 10));
    if (idx.every((n, i) => !Number.isNaN(n) && String(n) === cols[i])) {
      iterable = `np.loadtxt(r'${file}', delimiter=',', usecols=[${idx.join(', ')}]` +
        (idx.length > 1 ? ', ndmin=2)' : ')');
    } else {
      iterable = `zip(*[np.genfromtxt(r'${file}', delimiter=',', names=True)[c] for c in [${cols.map((c) => `'${c}'`).join(', ')}]])`;
    }
  } else {
    iterable = `[${b.getFieldValue('VALUES')}]`;
  }
  let code = `for ${vars} in ${iterable}:\n`;
  const motors = splitCsv(b.getFieldValue('MOTORS'));
  const names = splitCsv(vars);
  note(b, `loop: for ${vars} in ${iterable}` + (motors.length ? `, moving ${motors.join(', ')}` : ''));
  if (motors.length && motors.length === names.length) {
    code += gen.INDENT + `yield from bps.mv(${motors.map((m, i) => `${m}, ${names[i]}`).join(', ')})\n`;
  } else if (motors.length === 1 && names.length === 1) {
    code += gen.INDENT + `yield from bps.mv(${motors[0]}, ${names[0]})\n`;
  }
  code += bodyOrPass(g.statementToCode(b, 'DO'));
  return code;
};

gen.forBlock['eb_stats'] = (b, g) => {
  const x = b.getFieldValue('X') || 'x';
  const y = b.getFieldValue('Y') || 'y';
  const pre = ident(b.getFieldValue('PREFIX'), 'stat');
  const id = safeId(b);
  {
    const mv = b.getFieldValue('MOVE'), mm = b.getFieldValue('MOTOR');
    note(b, `statistics: ${y} vs ${x} -> ${pre}_cen, ${pre}_com, ${pre}_max, ${pre}_min, ${pre}_fwhm` +
      (mv !== 'none' && mm ? `; then move ${mm} to ${mv.toUpperCase()}` : ''));
  }
  const ps = `_ps_${id}`;
  const fn = `_stats_${id}`;
  // Variables assigned inside the nested function must be declared nonlocal.
  const names = b.workspace.getVariableMap().getAllVariables().map((v) => g.getVariableName(v.getId()));
  let body = g.statementToCode(b, 'DO');
  body = body && body.trim() ? body : gen.INDENT + 'yield from bps.null()\n';
  const nl = names.length ? gen.INDENT + `nonlocal ${names.join(', ')}\n` : '';
  let code = `${ps} = PeakStats('${x}', '${y}')\n` +
    `@bpp.subs_decorator(${ps})\n` +
    `def ${fn}():\n${nl}${body}` +
    `yield from ${fn}()\n` +
    `${pre}_cen = ${ps}.cen\n` +
    `${pre}_com = ${ps}.com\n` +
    `${pre}_max, ${pre}_max_y = ${ps}.max if ${ps}.max else (None, None)\n` +
    `${pre}_min, ${pre}_min_y = ${ps}.min if ${ps}.min else (None, None)\n` +
    `${pre}_fwhm = getattr(${ps}, 'fwhm', None)\n` +
    `print(f'[stats] ${y}: cen={${pre}_cen} com={${pre}_com} max={${pre}_max} min={${pre}_min} fwhm={${pre}_fwhm}')\n`;
  const move = b.getFieldValue('MOVE');
  const motor = b.getFieldValue('MOTOR');
  if (move !== 'none' && motor) {
    code += `if ${pre}_${move} is not None:\n` +
      gen.INDENT + `yield from bps.mv(${motor}, ${pre}_${move})\n` +
      `else:\n` +
      gen.INDENT + `print('[stats] ${move} not found - ${motor} not moved')\n`;
  }
  return code;
};


// ── Scan blocks (bluesky built-in plans) ─────────────────────────────────────
// All forward md=md so the queue-injected metadata (exp_dir, scan_num, ...) reaches the run.
Blockly.Blocks['eb_scan'] = {
  init() {
    this.appendEndRowInput().appendField(new Blockly.FieldDropdown(
      [['scan', 'scan'], ['relative scan', 'rel_scan']]), 'KIND')
      .appendField(new Blockly.FieldDropdown(motorOptions), 'MOTOR');
    this.appendValueInput('START').appendField('from');
    this.appendValueInput('STOP').appendField('to');
    this.appendValueInput('NUM').appendField('points');
    this.appendEndRowInput().appendField('detectors').appendField(new Blockly.FieldTextInput('[det]'), 'DETS');
    this.setInputsInline(true);
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setColour(EB_COLOURS.scan);
    this.setTooltip('1-D scan of one motor.\nyield from bp.scan(detectors, motor, start, stop, num, md=md)\n' +
      '"relative scan" uses positions relative to the starting point (bp.rel_scan).');
  },
};

Blockly.Blocks['eb_count'] = {
  init() {
    this.appendEndRowInput().appendField('count').appendField(new Blockly.FieldTextInput('[det]'), 'DETS');
    this.appendValueInput('NUM').appendField('times');
    this.appendValueInput('DELAY').appendField('delay');
    this.appendDummyInput().appendField('s');
    this.setInputsInline(true);
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setColour(EB_COLOURS.scan);
    this.setTooltip('Take N readings of the detectors.\nyield from bp.count(detectors, num=N, delay=s, md=md)');
  },
};

Blockly.Blocks['eb_grid_scan'] = {
  init() {
    this.appendEndRowInput().appendField(new Blockly.FieldDropdown(
      [['grid scan', 'grid_scan'], ['relative grid scan', 'rel_grid_scan']]), 'KIND')
      .appendField('detectors').appendField(new Blockly.FieldTextInput('[det]'), 'DETS');
    this.appendValueInput('START1').appendField(new Blockly.FieldDropdown(motorOptions), 'MOTOR1').appendField('from');
    this.appendValueInput('STOP1').appendField('to');
    this.appendValueInput('NUM1').appendField('points');
    this.appendEndRowInput();
    this.appendValueInput('START2').appendField(new Blockly.FieldDropdown(motorOptions), 'MOTOR2').appendField('from');
    this.appendValueInput('STOP2').appendField('to');
    this.appendValueInput('NUM2').appendField('points');
    this.appendEndRowInput().appendField(new Blockly.FieldCheckbox('FALSE'), 'SNAKE').appendField('snake (alternate direction of the inner axis)');
    this.setInputsInline(true);
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setColour(EB_COLOURS.scan);
    this.setTooltip('2-D mesh scan: the first motor is the outer (slow) axis.\n' +
      'yield from bp.grid_scan(detectors, m1, s1, e1, n1, m2, s2, e2, n2, snake_axes=..., md=md)');
  },
};

gen.forBlock['eb_scan'] = (b, g) => {
  const kind = b.getFieldValue('KIND'), dets = b.getFieldValue('DETS') || '[]', motor = b.getFieldValue('MOTOR');
  const start = val(b, 'START', g, '0'), stop = val(b, 'STOP', g, '1'), num = val(b, 'NUM', g, '11');
  note(b, `${kind}: ${motor} from ${start} to ${stop}, ${num} points; detectors ${dets}`);
  return `yield from bp.${kind}(${dets}, ${motor}, ${start}, ${stop}, ${num}, md=md)\n`;
};
gen.forBlock['eb_count'] = (b, g) => {
  const dets = b.getFieldValue('DETS') || '[]', num = val(b, 'NUM', g, '1'), delay = val(b, 'DELAY', g, '0');
  note(b, `count: ${num} reading(s), delay ${delay} s; detectors ${dets}`);
  return `yield from bp.count(${dets}, num=${num}, delay=${delay}, md=md)\n`;
};
gen.forBlock['eb_grid_scan'] = (b, g) => {
  const kind = b.getFieldValue('KIND'), dets = b.getFieldValue('DETS') || '[]';
  const m1 = b.getFieldValue('MOTOR1'), m2 = b.getFieldValue('MOTOR2');
  const s1 = val(b, 'START1', g, '0'), e1 = val(b, 'STOP1', g, '1'), n1 = val(b, 'NUM1', g, '5');
  const s2 = val(b, 'START2', g, '0'), e2 = val(b, 'STOP2', g, '1'), n2 = val(b, 'NUM2', g, '5');
  const snake = b.getFieldValue('SNAKE') === 'TRUE';
  note(b, `${kind}: ${m1} ${s1} to ${e1} (${n1} points) x ${m2} ${s2} to ${e2} (${n2} points)` +
    `${snake ? ', snake' : ''}; detectors ${dets}`);
  return `yield from bp.${kind}(${dets}, ${m1}, ${s1}, ${e1}, ${n1}, ${m2}, ${s2}, ${e2}, ${n2}, ` +
    `snake_axes=${snake ? 'True' : 'False'}, md=md)\n`;
};

// ── Run plan with the registered plan's own parameters ───────────────────────
// Parameter specs come from the RE Manager (EB.devices.planParams) and are stored in the
// block's extraState, so saved workspaces load even when the plan list is not available.
Blockly.Blocks['eb_plan_call'] = {
  init() {
    this.params_ = [];
    this.appendDummyInput('HEAD').appendField('run plan')
      .appendField(new Blockly.FieldDropdown(planOptions, (v) => { this.setPlan_(v); return v; }), 'PLAN');
    this.setInputsInline(false);
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setColour(EB_COLOURS.plan);
    this.setTooltip('Run a registered plan. Pick the plan, then plug values into its parameters.\n' +
      'Parameters left empty use the plan default.');
  },
  setPlan_(name) {
    const spec = EB.devices.planParams && EB.devices.planParams[name];
    if (spec) this.rebuild_(spec);
  },
  rebuild_(spec) {
    this.params_.forEach((p) => { if (this.getInput('ARG_' + p.name)) this.removeInput('ARG_' + p.name); });
    this.params_ = spec.filter((p) => p.name !== 'md' && !/VAR_/.test(p.kind || ''))
      .concat(spec.filter((p) => p.name === 'md').map((p) => ({ ...p, hidden: true })));
    this.params_.forEach((p) => {
      if (p.hidden) return;
      const label = p.name + (p.required ? ' *' : (p.default !== undefined && p.default !== '' ? ` (=${p.default})` : ''));
      const inp = this.appendValueInput('ARG_' + p.name).appendField(label);
      inp.setAlign(Blockly.inputs.Align.RIGHT);
    });
    this.hasMd_ = spec.some((p) => p.name === 'md');
  },
  saveExtraState() { return { params: this.params_.filter((p) => !p.hidden), md: !!this.hasMd_ }; },
  loadExtraState(s) {
    const spec = (s && s.params) || [];
    if (s && s.md) spec.push({ name: 'md', kind: 'KEYWORD_ONLY' });
    this.rebuild_(spec);
  },
};

gen.forBlock['eb_plan_call'] = (b, g) => {
  const pos = [];
  const kw = [];
  let gap = false;
  (b.params_ || []).forEach((p) => {
    if (p.hidden) return;
    const v = g.valueToCode(b, 'ARG_' + p.name, ORD.NONE);
    if (!v) { gap = true; return; }
    if (/POSITIONAL_ONLY/.test(p.kind || '') && !gap) pos.push(v);
    else kw.push(`${p.name}=${v}`);
  });
  if (b.hasMd_) kw.push('md=md');
  const args = pos.concat(kw).join(', ');
  note(b, `plan ${b.getFieldValue('PLAN')}(${args})`);
  return `yield from ${b.getFieldValue('PLAN')}(${args})\n`;
};
