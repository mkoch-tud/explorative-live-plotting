const $ = id => document.getElementById(id);
let bootstrap = {sources: [], registry: {aggregations: [], plot_types: []}, expression_variables: []};
let config = null;
let liveTimer = null;
let renderSequence = 0;
let queryRevision = 0;
let queryDirty = true;
let pathSuggestionTimer = null;
let pathSuggestionSequence = 0;
let pathSuggestions = [];
let pathSuggestionIndex = -1;
let systemStatsTimer = null;
let filterControlSequence = 0;
const filterSampleCache = new WeakMap();
const filterTimeValueCache = new WeakMap();
const STD_COLORS = ['#375E97', '#FB6542', '#c1195c', '#37975e'];
let workspaces = [];
let activeWorkspaceId = null;
let workspaceNumber = 0;
let workspaceSwitchSequence = 0;

const escapeHtml = value => String(value).replace(
  /[&<>"]/g,
  character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[character]),
);
const option = (values, selected, empty = false) => (
  (empty ? '<option value="">— none —</option>' : '')
  + values.map(value => `<option value="${escapeHtml(value)}" ${value === selected ? 'selected' : ''}>${escapeHtml(value)}</option>`).join('')
);
const source = name => bootstrap.sources.find(item => item.name === name);
const columns = name => (source(name)?.columns ?? []).map(item => item.name);
const columnMetadata = (sourceName, column) => (source(sourceName)?.columns ?? []).find(item => item.name === column);
const temporalColumn = (sourceName, column) => /^(Date|Datetime)/.test(columnMetadata(sourceName, column)?.dtype ?? '');
const filterConditions = items => (items ?? []).flatMap(item => (
  isFilterGroup(item) ? filterConditions(item.filters) : [item]
));
const scalar = value => {
  const text = String(value).trim();
  if (text === '') return '';
  const parsed = Number(text);
  return Number.isFinite(parsed) ? parsed : text;
};

function message(text, error = false) {
  $('message').textContent = text;
  $('message').className = error ? 'error' : '';
}
const previewMarkup = previews => previews.map(item => `<div class="preview"><strong>${escapeHtml(item.filename)}</strong><img alt="${escapeHtml(item.filename)}" src="${item.image}"></div>`).join('');
function updatePreviews(previews) {
  if (!Array.isArray(previews)) return;
  $('previews').innerHTML = previewMarkup(previews);
}

function apiFetch(path, options = {}, workspaceId = activeWorkspaceId) {
  const headers = new Headers(options.headers ?? {});
  if (workspaceId) headers.set('X-ELP-Workspace', workspaceId);
  return window.fetch(path, {...options, headers});
}

const workspaceById = id => workspaces.find(item => item.id === id);
const newWorkspaceId = () => {
  const random = globalThis.crypto?.randomUUID?.().replaceAll('-', '') ?? `${Date.now()}${Math.random().toString(16).slice(2)}`;
  return `workspace_${random}`.slice(0, 64);
};

function sourceDraft() {
  return {
    name: $('source-name').value, path: $('source-path').value,
    format: $('source-format').value, separator: $('source-separator').value,
    filterExpression: $('source-filter-expression').value,
    options: $('source-options').value,
  };
}

function applySourceDraft(draft = {}) {
  $('source-name').value = draft.name ?? '';
  $('source-path').value = draft.path ?? '';
  $('source-format').value = draft.format ?? 'auto';
  $('source-separator').value = draft.separator ?? '';
  $('source-filter-expression').value = draft.filterExpression ?? '';
  $('source-options').value = draft.options ?? '{}';
  renderPathSuggestions([]); $('source-path-resolved').textContent = '';
}

function snapshotActiveWorkspace() {
  const workspace = workspaceById(activeWorkspaceId);
  if (!workspace || !config) return;
  workspace.config = collect();
  workspace.bootstrap = bootstrap;
  workspace.previewHtml = $('previews').innerHTML;
  workspace.message = {text: $('message').textContent, error: $('message').classList.contains('error')};
  workspace.queryDirty = queryDirty;
  workspace.sourceDraft = sourceDraft();
}

function renderWorkspaceTabs() {
  const root = $('workspace-tabs'); root.innerHTML = '';
  for (const workspace of workspaces) {
    const tab = document.createElement('div');
    tab.className = `workspace-tab${workspace.id === activeWorkspaceId ? ' active' : ''}`;
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', String(workspace.id === activeWorkspaceId));
    const dirty = workspace.queryDirty ? '<span class="workspace-dirty" title="Not rendered">●</span>' : '';
    tab.innerHTML = `<button class="workspace-tab-name" type="button" title="Double-click to rename">${dirty}${escapeHtml(workspace.title)}</button><button class="workspace-tab-close" type="button" aria-label="Close ${escapeHtml(workspace.title)}" ${workspaces.length === 1 ? 'disabled' : ''}>×</button>`;
    tab.querySelector('.workspace-tab-name').onclick = () => switchWorkspace(workspace.id);
    tab.querySelector('.workspace-tab-name').ondblclick = event => {
      event.preventDefault();
      const title = window.prompt('Workspace name', workspace.title)?.trim();
      if (title) { workspace.title = title.slice(0, 80); renderWorkspaceTabs(); }
    };
    tab.querySelector('.workspace-tab-close').onclick = event => {
      event.stopPropagation(); closeWorkspace(workspace.id);
    };
    root.appendChild(tab);
  }
  root.querySelector('.workspace-tab.active')?.scrollIntoView({block: 'nearest', inline: 'nearest'});
}

async function initializeWorkspace(workspace) {
  if (workspace.initialized) return true;
  const response = await apiFetch('/api/bootstrap', {}, workspace.id);
  if (!response.ok) { await apiError(response); return false; }
  workspace.bootstrap = await response.json();
  workspace.config = workspace.bootstrap.default_config;
  workspace.previewHtml = '';
  workspace.message = {text: 'Ready. Add a source and layer, then render.', error: false};
  workspace.queryDirty = true;
  workspace.sourceDraft = {};
  workspace.initialized = true;
  return true;
}

async function switchWorkspace(identifier) {
  if (identifier === activeWorkspaceId) return;
  snapshotActiveWorkspace();
  clearTimeout(liveTimer); clearTimeout(pathSuggestionTimer);
  renderSequence += 1; queryRevision += 1; pathSuggestionSequence += 1;
  const sequence = ++workspaceSwitchSequence;
  activeWorkspaceId = identifier; renderWorkspaceTabs();
  const workspace = workspaceById(identifier);
  if (!workspace || !await initializeWorkspace(workspace)) return;
  if (sequence !== workspaceSwitchSequence || identifier !== activeWorkspaceId) return;
  bootstrap = workspace.bootstrap;
  apply(workspace.config);
  applySourceDraft(workspace.sourceDraft);
  $('previews').innerHTML = workspace.previewHtml ?? '';
  queryDirty = workspace.queryDirty ?? true;
  message(workspace.message?.text ?? 'Ready.', workspace.message?.error ?? false);
  renderExpressionSymbols(); renderSources(); renderWorkspaceTabs();
}

async function addWorkspace() {
  const workspace = {
    id: newWorkspaceId(), title: `Workspace ${++workspaceNumber}`,
    initialized: false, queryDirty: true,
  };
  workspaces.push(workspace); renderWorkspaceTabs();
  await switchWorkspace(workspace.id);
}

async function closeWorkspace(identifier) {
  if (workspaces.length <= 1) return;
  const index = workspaces.findIndex(item => item.id === identifier);
  if (index < 0) return;
  if (identifier === activeWorkspaceId) snapshotActiveWorkspace();
  workspaces.splice(index, 1); renderWorkspaceTabs();
  apiFetch(`/api/workspaces/${encodeURIComponent(identifier)}`, {method: 'DELETE'}, identifier).catch(() => {});
  if (identifier === activeWorkspaceId) {
    activeWorkspaceId = null;
    await switchWorkspace(workspaces[Math.min(index, workspaces.length - 1)].id);
  }
}
function numberValue(id) { return $(id).value === '' ? null : Number($(id).value); }
function scalarValue(id) { return $(id).value.trim() === '' ? null : scalar($(id).value); }
function csvNumbers(id) { return $(id).value.split(',').map(x => x.trim()).filter(Boolean).map(Number); }
function csvText(id) { return $(id).value.split(',').map(x => x.trim()).filter(Boolean); }

const colorPalette = selected => `<span class="color-control"><input class="color" type="color" value="${escapeHtml(selected)}"><span class="standard-colors" aria-label="Standard colors">${STD_COLORS.map(color => `<button type="button" class="standard-color ${color.toLowerCase() === selected.toLowerCase() ? 'active' : ''}" data-color="${color}" title="${color}" aria-label="Use ${color}" style="--swatch:${color}"></button>`).join('')}</span></span>`;
const info = text => `<span class="info" tabindex="0" title="${escapeHtml(text)}" data-tip="${escapeHtml(text)}" aria-label="${escapeHtml(text)}">i</span>`;
const groupingMode = layer => layer.time_bin ? 'group_by_dynamic' : layer.aggregation !== 'none' ? 'group_by' : 'none';

function formatBytes(value) {
  if (value === null || value === undefined) return '—';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let amount = Number(value); let index = 0;
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
  return `${amount.toFixed(index < 2 ? 0 : 1)} ${units[index]}`;
}

function renderPathSuggestions(items, selected = 0) {
  const root = $('source-path-suggestions');
  pathSuggestions = items;
  pathSuggestionIndex = items.length ? Math.max(0, Math.min(selected, items.length - 1)) : -1;
  root.innerHTML = items.map((item, index) => (
    `<button type="button" class="path-suggestion ${index === pathSuggestionIndex ? 'active' : ''}" role="option" aria-selected="${index === pathSuggestionIndex}" data-index="${index}"><span>${escapeHtml(item.label)}</span><span class="path-suggestion-kind">${escapeHtml(item.kind)}</span></button>`
  )).join('');
  root.hidden = !items.length;
  $('source-path').setAttribute('aria-expanded', String(Boolean(items.length)));
  root.querySelectorAll('.path-suggestion').forEach(button => {
    button.onmousedown = event => {
      event.preventDefault();
      acceptPathSuggestion(Number(button.dataset.index));
    };
  });
}

function selectPathSuggestion(index) {
  if (!pathSuggestions.length) return;
  pathSuggestionIndex = (index + pathSuggestions.length) % pathSuggestions.length;
  $('source-path-suggestions').querySelectorAll('.path-suggestion').forEach((button, itemIndex) => {
    const active = itemIndex === pathSuggestionIndex;
    button.classList.toggle('active', active); button.setAttribute('aria-selected', String(active));
    if (active) button.scrollIntoView({block: 'nearest'});
  });
}

function acceptPathSuggestion(index = pathSuggestionIndex) {
  const item = pathSuggestions[index];
  if (!item) return;
  $('source-path').value = item.value;
  renderPathSuggestions([]);
  $('source-path').focus();
  updatePathSuggestions().catch(() => {});
}

async function updatePathSuggestions() {
  const value = $('source-path').value;
  const sequence = ++pathSuggestionSequence;
  if (!value) {
    renderPathSuggestions([]);
    $('source-path-resolved').textContent = '';
    return;
  }
  const response = await apiFetch(`/api/path-suggestions?value=${encodeURIComponent(value)}`);
  if (!response.ok) return;
  const result = await response.json();
  if (sequence !== pathSuggestionSequence || value !== $('source-path').value) return;
  renderPathSuggestions(result.suggestions ?? []);
  const resolved = $('source-path-resolved');
  resolved.textContent = result.error ? result.error : result.resolved_path ? `Resolved: ${result.resolved_path}` : '';
  resolved.className = result.error ? 'path-resolution error' : 'path-resolution';
}

function schedulePathSuggestions() {
  clearTimeout(pathSuggestionTimer);
  pathSuggestionTimer = setTimeout(() => updatePathSuggestions().catch(() => {}), 150);
}

async function updateSystemStats() {
  const response = await apiFetch('/api/system-stats');
  if (!response.ok) throw new Error('System metrics unavailable');
  const stats = await response.json();
  const cpu = stats.cpu_percent === null ? '—' : `${stats.cpu_percent.toFixed(1)}%`;
  const ram = stats.ram_percent === null ? '—' : `${stats.ram_percent.toFixed(1)}%`;
  $('system-stats').textContent = `CPU ${cpu} · RAM ${formatBytes(stats.ram_used_bytes)} / ${formatBytes(stats.ram_total_bytes)} (${ram})`;
}

function setSystemStatsEnabled(enabled) {
  clearInterval(systemStatsTimer); systemStatsTimer = null;
  $('system-stats').hidden = !enabled;
  if (!enabled) return;
  $('system-stats').textContent = 'Loading CPU/RAM…';
  updateSystemStats().catch(error => { $('system-stats').textContent = error.message; });
  systemStatsTimer = setInterval(() => {
    updateSystemStats().catch(error => { $('system-stats').textContent = error.message; });
  }, 10000);
}

function setupPanelResizer() {
  const workspace = $('workspace'); const resizer = $('panel-resizer');
  const defaultWidth = 680; let dragging = false;
  const bounds = () => ({min: 500, max: Math.max(500, window.innerWidth - 360)});
  const setWidth = (requested, persist = true) => {
    const {min, max} = bounds(); const width = Math.max(min, Math.min(max, requested));
    workspace.style.setProperty('--config-panel-width', `${width}px`);
    resizer.setAttribute('aria-valuemin', String(min)); resizer.setAttribute('aria-valuemax', String(max));
    resizer.setAttribute('aria-valuenow', String(Math.round(width)));
    if (persist) { try { localStorage.setItem('elp-config-panel-width', String(width)); } catch {} }
  };
  let saved = defaultWidth;
  try { saved = Number(localStorage.getItem('elp-config-panel-width')) || defaultWidth; } catch {}
  setWidth(saved, false);
  resizer.onpointerdown = event => {
    if (window.innerWidth <= 1000) return;
    dragging = true; resizer.setPointerCapture(event.pointerId);
    document.body.classList.add('resizing-panel'); event.preventDefault();
  };
  resizer.onpointermove = event => { if (dragging) setWidth(event.clientX); };
  const stop = event => {
    if (!dragging) return;
    dragging = false; document.body.classList.remove('resizing-panel');
    if (resizer.hasPointerCapture(event.pointerId)) resizer.releasePointerCapture(event.pointerId);
  };
  resizer.onpointerup = stop; resizer.onpointercancel = stop;
  resizer.ondblclick = () => setWidth(defaultWidth);
  resizer.onkeydown = event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home'].includes(event.key)) return;
    const current = Number(resizer.getAttribute('aria-valuenow')) || defaultWidth;
    setWidth(event.key === 'Home' ? defaultWidth : current + (event.key === 'ArrowLeft' ? -20 : 20));
    event.preventDefault();
  };
  window.addEventListener('resize', () => {
    const current = Number(resizer.getAttribute('aria-valuenow')) || defaultWidth;
    setWidth(current, false);
  });
}

function ensureConfig() {
  config.figure ??= {};
  config.axes ??= {};
  config.legend ??= {};
  config.broken_y_axis ??= {enabled: false, gap: 0.1, ranges: []};
  config.broken_y_axis.ranges ??= [];
  config.layers ??= [];
  config.annotations ??= [];
  config.stages ??= {enabled: false, overlay_alpha: 0.25, steps: []};
  config.stages.steps ??= [];
  config.annotations.forEach((item, index) => {
    item.id ??= `annotation-${Date.now()}-${index}`;
    item.enabled ??= true;
    item.label ??= item.text || item.id;
    item.text ??= '';
    item.show_in_legend ??= false;
    item.legend_label ??= item.label;
    item.text_background_enabled ??= false;
    item.text_background_color ??= '#ffffff';
    item.inference ??= {x: 'manual', y: 'manual', layer_ids: []};
    item.inference.x ??= 'manual'; item.inference.y ??= 'manual';
    item.inference.layer_ids ??= [];
    item.fontsize ??= config.figure.font_size ?? 12;
    item.step ??= 1;
  });
}

function renderExpressionSymbols() {
  $('expression-symbols').innerHTML = (bootstrap.expression_variables ?? [])
    .map(value => `<option value="${escapeHtml(value)}"></option>`).join('');
}

function newLayer() {
  const index = config.layers.length;
  return {
    id: `layer-${Date.now()}-${index}`, enabled: true, label: `Layer ${index + 1}`,
    source: bootstrap.sources[0]?.name ?? '', plot_type: 'line', x_column: '',
    y_column: '', group_column: '', aggregation: 'none', aggregation_options: {},
    time_bin: null, filter_logic: 'and', required_filters: [], filters: [],
    base_filter_expression: '', filter_expression: '',
    sort: 'x_ascending', limit: null, result_limit: null,
    result_y_min: null, result_y_max: null,
    stacked: false, secondary_y: false, fix_x_values: false,
    style: {color: STD_COLORS[index % STD_COLORS.length], alpha: 1, linewidth: 1.5, linestyle: '-', marker: 'none', markersize: 4},
    options: {},
  };
}

function newAnnotation(kind = 'text') {
  const number = config.annotations.length + 1;
  return {
    id: `annotation-${Date.now()}-${config.annotations.length}`, enabled: true, kind,
    label: `Annotation ${number}`, text: kind === 'text' ? `Annotation ${number}` : '',
    show_in_legend: false, legend_label: `Annotation ${number}`,
    text_background_enabled: false, text_background_color: '#ffffff',
    inference: {x: 'manual', y: 'manual', layer_ids: []},
    x: 0, y: 0, x1: 0, x2: 1,
    y1: 0, y2: 1, color: '#666666', alpha: 0.6, linestyle: '--',
    fontsize: config.figure?.font_size ?? 12, step: 1,
  };
}

function renderBrokenYRanges() {
  const root = $('broken-y-ranges'); root.innerHTML = '';
  (config.broken_y_axis.ranges ?? []).forEach((range, index) => {
    const row = document.createElement('div'); row.className = 'broken-y-range';
    row.innerHTML = `<label>Range ${index + 1} min<input class="min" type="number" step="any" value="${escapeHtml(range.min ?? '')}"></label><label>Range ${index + 1} max<input class="max" type="number" step="any" value="${escapeHtml(range.max ?? '')}"></label><button class="remove" type="button">×</button>`;
    row.querySelector('.min').oninput = event => { range.min = scalar(event.target.value); changed(false); };
    row.querySelector('.max').oninput = event => { range.max = scalar(event.target.value); changed(false); };
    row.querySelector('.remove').onclick = () => { config.broken_y_axis.ranges.splice(index, 1); renderBrokenYRanges(); updateBrokenYAxisState(); changed(false); };
    root.appendChild(row);
  });
}

function updateBrokenYAxisState() {
  const enabled = $('broken-y-enabled').checked;
  $('broken-y-gap').disabled = !enabled;
  $('add-broken-y-range').disabled = !enabled;
  $('ymin').disabled = enabled; $('ymax').disabled = enabled;
  $('broken-y-ranges').querySelectorAll('input,button').forEach(item => { item.disabled = !enabled; });
}

function renderSources() {
  $('sources').innerHTML = '';
  $('source-count').textContent = `${bootstrap.sources.length} registered`;
  for (const item of bootstrap.sources) {
    const div = document.createElement('div');
    div.className = 'source';
    const resolved = item.resolved_path && item.resolved_path !== item.path
      ? `<small>Resolved</small><code>${escapeHtml(item.resolved_path)}</code>` : '';
    const sourceFilter = item.filter_expression
      ? `<small>Global filter</small><code>${escapeHtml(item.filter_expression)}</code>` : '';
    div.innerHTML = `<div class="source-head"><strong>${escapeHtml(item.name)}</strong><button class="remove">Remove</button></div><small>${escapeHtml(item.format)} · ${item.columns.length} columns</small><code>${escapeHtml(item.path)}</code>${resolved}${sourceFilter}`;
    div.querySelector('button').onclick = async () => {
      const workspaceId = activeWorkspaceId;
      const currentWorkspace = workspaceById(workspaceId);
      if (currentWorkspace) currentWorkspace.config = collect();
      const response = await apiFetch(`/api/sources/${encodeURIComponent(item.name)}`, {method: 'DELETE'}, workspaceId);
      if (!response.ok) return apiError(response, workspaceId);
      const workspace = workspaceById(workspaceId);
      if (!workspace) return;
      workspace.bootstrap.sources = workspace.bootstrap.sources.filter(x => x.name !== item.name);
      workspace.config.layers = workspace.config.layers.filter(x => x.source !== item.name);
      if (workspaceId !== activeWorkspaceId) return;
      bootstrap = workspace.bootstrap; config = workspace.config;
      reconcileStages(); renderSources(); renderLayers(); renderStages(); changed(true);
    };
    $('sources').appendChild(div);
  }
}

const newFilter = layer => ({column: columns(layer.source)[0] ?? '', operator: 'eq', value: ''});
const newFilterGroup = layer => ({type: 'group', logic: 'and', filters: [newFilter(layer)]});
const isFilterGroup = item => item?.type === 'group';
const filterOperatorOptions = selected => [
  ['eq', 'equals (=)'], ['ne', 'not equal (≠)'], ['gt', 'greater than (>)'],
  ['ge', 'minimum / at or after (≥)'], ['lt', 'less than (<)'],
  ['le', 'maximum / at or before (≤)'], ['between', 'between (inclusive)'],
  ['in', 'in comma-separated list'], ['not_in', 'not in comma-separated list'],
  ['is_null', 'is null'], ['is_not_null', 'is not null'],
].map(([value, label]) => `<option value="${value}" ${value === selected ? 'selected' : ''}>${escapeHtml(label)}</option>`).join('');

function renderTimeValueSelect(select, status, item, state) {
  const current = String(item.value ?? '');
  const scrollTop = select.scrollTop;
  select.innerHTML = `<option value="">— select an existing time —</option>${state.values.map(value => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join('')}`;
  if (state.values.some(value => String(value) === current)) select.value = current;
  select.scrollTop = scrollTop;
  status.textContent = state.loading
    ? `Loaded ${state.values.length}; loading more…`
    : state.complete ? `${state.values.length} distinct time value(s)`
      : `${state.values.length} loaded; scroll to load more`;
}

async function loadTimeValuePage(layer, item, select, status) {
  let state = filterTimeValueCache.get(item);
  if (!state || state.source !== layer.source || state.column !== item.column) {
    state = {source: layer.source, column: item.column, values: [], offset: 0, complete: false, loading: false, pending: null};
    filterTimeValueCache.set(item, state);
  }
  if (state.loading) {
    await state.pending; renderTimeValueSelect(select, status, item, state); return;
  }
  if (state.complete) return;
  state.loading = true; renderTimeValueSelect(select, status, item, state);
  state.pending = (async () => {
    try {
      const response = await apiFetch(`/api/sources/${encodeURIComponent(layer.source)}/column-values?column=${encodeURIComponent(item.column)}&offset=${state.offset}&limit=250`);
      if (!response.ok) { await apiError(response); return; }
      const result = await response.json();
      state.values.push(...(result.values ?? []));
      state.offset = result.next_offset ?? state.offset;
      state.complete = !result.has_more;
    } catch (error) {
      message(`Could not load time values: ${error.message}`, true);
    } finally {
      state.loading = false;
    }
  })();
  await state.pending; state.pending = null;
  renderTimeValueSelect(select, status, item, state);
}

function filterRow(layer, item, siblings, index) {
  if (isFilterGroup(item)) return filterGroup(layer, item, siblings, index);
  const div = document.createElement('div');
  div.className = 'filter-condition';
  const cols = columns(layer.source);
  const controlId = `filter-values-${++filterControlSequence}`;
  const noValue = ['is_null', 'is_not_null'].includes(item.operator);
  const between = item.operator === 'between';
  const isTemporal = temporalColumn(layer.source, item.column);
  const temporalEquals = isTemporal && item.operator === 'eq';
  const timeHint = isTemporal ? 'ISO date/time, e.g. 2026-04-01T00:00:00+00:00' : 'Value';
  const samples = filterSampleCache.get(item)?.values ?? [];
  const inputs = noValue ? '<span class="filter-no-value">No value needed</span>' : between
    ? `<span class="filter-range"><input class="filter-value" list="${controlId}" aria-label="Inclusive minimum" placeholder="Minimum · ${timeHint}" value="${escapeHtml(item.value ?? item.min ?? '')}"><input class="filter-value2" list="${controlId}" aria-label="Inclusive maximum" placeholder="Maximum · ${timeHint}" value="${escapeHtml(item.value2 ?? item.max ?? '')}"></span>`
    : temporalEquals
      ? `<span class="filter-value-cell"><input class="filter-value" list="${controlId}" placeholder="${escapeHtml(timeHint)}" value="${escapeHtml(item.value ?? '')}"><details class="time-value-picker"><summary>Choose an existing time value</summary><select class="time-value-select" size="8" aria-label="Existing time values"></select><small class="time-value-status">Open to load values</small></details></span>`
      : `<input class="filter-value" list="${controlId}" placeholder="${escapeHtml(timeHint)}" value="${escapeHtml(item.value ?? '')}">`;
  const sampleText = samples.length
    ? `<div class="filter-samples"><small>Min · intermediate values · max</small>${samples.map(value => `<button type="button" class="sample-value" data-value="${escapeHtml(value)}">${escapeHtml(value)}</button>`).join('')}${between ? '<button type="button" class="use-sample-range">Use min–max</button>' : ''}</div>` : '';
  div.innerHTML = `<div class="filter"><select class="filter-column" aria-label="Filter column">${option(cols, item.column)}</select><select class="filter-operator" aria-label="Filter operator">${filterOperatorOptions(item.operator)}</select>${inputs}<button class="query-filter-values" type="button" title="Read only this column and return min, max, and three values in between">Query 5 values</button><button class="remove" type="button" title="Remove filter">×</button></div><datalist id="${controlId}">${samples.map(value => `<option value="${escapeHtml(value)}"></option>`).join('')}</datalist>${sampleText}`;
  const column = div.querySelector('.filter-column');
  const operator = div.querySelector('.filter-operator');
  const value = div.querySelector('.filter-value');
  const value2 = div.querySelector('.filter-value2');
  column.onchange = () => { item.column = column.value; filterSampleCache.delete(item); filterTimeValueCache.delete(item); renderLayers(); changed(true); };
  operator.onchange = () => {
    item.operator = operator.value;
    if (item.operator === 'between') item.value2 ??= '';
    renderLayers(); changed(true);
  };
  if (value) value.oninput = () => { item.value = value.value; delete item.min; changed(true); };
  if (value2) value2.oninput = () => { item.value2 = value2.value; delete item.max; changed(true); };
  const timePicker = div.querySelector('.time-value-picker');
  if (timePicker) {
    const select = div.querySelector('.time-value-select');
    const status = div.querySelector('.time-value-status');
    const state = filterTimeValueCache.get(item);
    if (state) renderTimeValueSelect(select, status, item, state);
    timePicker.ontoggle = () => { if (timePicker.open) loadTimeValuePage(layer, item, select, status); };
    select.onscroll = () => {
      if (select.scrollTop + select.clientHeight >= select.scrollHeight - 20) {
        loadTimeValuePage(layer, item, select, status);
      }
    };
    select.onchange = () => {
      if (!select.value) return;
      item.value = select.value; value.value = select.value; delete item.min; changed(true);
    };
  }
  div.querySelector('.remove').onclick = () => { siblings.splice(index, 1); renderLayers(); changed(true); };
  div.querySelector('.query-filter-values').onclick = async event => {
    const button = event.currentTarget; button.disabled = true; button.textContent = 'Querying…';
    const response = await apiFetch(`/api/sources/${encodeURIComponent(layer.source)}/column-excerpt?column=${encodeURIComponent(item.column)}&intermediate=3`);
    if (!response.ok) { await apiError(response); button.disabled = false; button.textContent = 'Query 5 values'; return; }
    const result = await response.json(); filterSampleCache.set(item, result); renderLayers();
    message(`Loaded ${result.values.length} representative value(s) for ${item.column}; only that column was projected.`);
  };
  div.querySelectorAll('.sample-value').forEach(button => {
    button.onclick = () => {
      item.value = button.dataset.value; delete item.min;
      renderLayers(); changed(true);
    };
  });
  const useRange = div.querySelector('.use-sample-range');
  if (useRange) useRange.onclick = () => {
    item.value = samples[0] ?? ''; item.value2 = samples[samples.length - 1] ?? '';
    delete item.min; delete item.max;
    renderLayers(); changed(true);
  };
  return div;
}

function filterGroup(layer, group, siblings, index) {
  group.filters ??= [];
  const div = document.createElement('div'); div.className = 'filter-group';
  div.innerHTML = `<div class="filter-group-head"><strong>Nested group</strong><label>Match<select class="group-logic">${option(['and', 'or'], group.logic ?? 'and')}</select></label><button class="add-condition" type="button">+ condition</button><button class="add-group" type="button">+ nested group</button><button class="remove" type="button">×</button></div><div class="filter-group-items"></div>`;
  div.querySelector('.group-logic').onchange = event => { group.logic = event.target.value; changed(true); };
  div.querySelector('.add-condition').onclick = () => { group.filters.push(newFilter(layer)); renderLayers(); changed(true); };
  div.querySelector('.add-group').onclick = () => { group.filters.push(newFilterGroup(layer)); renderLayers(); changed(true); };
  div.querySelector('.remove').onclick = () => { siblings.splice(index, 1); renderLayers(); changed(true); };
  const root = div.querySelector('.filter-group-items');
  group.filters.forEach((child, childIndex) => root.appendChild(filterRow(layer, child, group.filters, childIndex)));
  return div;
}

function renderLayers() {
  const root = $('layers');
  const expandedAdvanced = new Set(
    [...root.querySelectorAll('.layer-advanced[open]')]
      .map(details => details.closest('.layer')?.dataset.layerId),
  );
  root.innerHTML = '';
  $('layer-count').textContent = `${config.layers.length} configured`;
  config.layers.forEach((layer, index) => {
    layer.style ??= {}; layer.filters ??= []; layer.required_filters ??= [];
    layer.base_filter_expression ??= ''; layer.filter_expression ??= '';
    layer.aggregation_options ??= {}; layer.options ??= {};
    layer.style.color ??= STD_COLORS[index % STD_COLORS.length];
    const card = document.createElement('div');
    card.className = 'layer';
    card.dataset.layerId = layer.id;
    const names = bootstrap.sources.map(x => x.name);
    const cols = columns(layer.source);
    if (cols.length) {
      if (!cols.includes(layer.x_column)) layer.x_column = cols[0] ?? '';
      if (!cols.includes(layer.y_column)) layer.y_column = cols[1] ?? cols[0] ?? '';
      if (layer.group_column && !cols.includes(layer.group_column)) layer.group_column = '';
    }
    const selectedColor = layer.style.color;
    const grouping = groupingMode(layer);
    const xLabel = grouping === 'group_by_dynamic' ? 'Time column (X)' : grouping === 'group_by' ? 'Group-by column (X)' : 'X column';
    const groupingText = grouping === 'group_by_dynamic' ? 'group_by_dynamic bins the Time/X column by Every; the function aggregates Y values in each time bin.' : grouping === 'group_by' ? 'group_by uses each distinct X value as a group; the function aggregates Y values within that group.' : 'None plots row-level X and Y values without aggregation.';
    const aggregations = bootstrap.registry.aggregations.filter(value => value !== 'none');
    const groupingOptions = `<option value="none" ${grouping === 'none' ? 'selected' : ''}>none (raw rows)</option><option value="group_by" ${grouping === 'group_by' ? 'selected' : ''}>group_by</option><option value="group_by_dynamic" ${grouping === 'group_by_dynamic' ? 'selected' : ''}>group_by_dynamic</option>`;
    card.innerHTML = `<div class="layer-head"><input class="enabled" type="checkbox" ${layer.enabled ? 'checked' : ''}><input class="label" value="${escapeHtml(layer.label)}"><button class="remove">×</button></div><div class="grid plot-basics"><label>Source<select class="source-select">${option(names, layer.source)}</select></label><label>Plot type<select class="plot-type">${option(bootstrap.registry.plot_types, layer.plot_type)}</select></label><label>Color${colorPalette(selectedColor)}</label></div><div class="grid axis-columns"><label><span>${xLabel} ${info('X supplies the horizontal values. With group_by it is the grouping key; with group_by_dynamic it must be a Date or Datetime column.')}</span><select class="x-column">${option(cols, layer.x_column, true)}</select></label><label><span>Y/value column ${info('The selected aggregation function is applied to this column. count and relative_count count rows and therefore ignore Y.')}</span><select class="y-column">${option(cols, layer.y_column, true)}</select></label></div><div class="grouping-panel"><div class="grid grouping-grid"><label><span>Grouping method ${info('none plots raw rows; group_by combines equal X values; group_by_dynamic creates regular time bins from the Time/X column.')}</span><select class="grouping-method">${groupingOptions}</select></label><label class="time-every" ${grouping === 'group_by_dynamic' ? '' : 'hidden'}><span>Every ${info('Width of each time bin, for example 1s, 1m, 5m, 1h, 1d, 1w, or 1mo.')}</span><input class="time-bin" list="time-bins" placeholder="1m" value="${escapeHtml(layer.time_bin ?? '1m')}"></label><label><span>Aggregate Y with ${info('The function is applied to Y inside every X group or time bin. count and relative_count operate on rows instead.')}</span><select class="aggregation" ${grouping === 'none' ? 'disabled' : ''}>${option(aggregations, layer.aggregation === 'none' ? 'sum' : layer.aggregation)}</select></label></div><small>${groupingText}</small></div><div class="grid"><label><span>Split series by / color ${info('Optional categorical column. Each distinct value becomes a separate plotted series and legend entry; when aggregating, it is an additional grouping key.')}</span><select class="group-column">${option(cols, layer.group_column, true)}</select></label><label>Sort<select class="sort">${option(['none', 'x_ascending', 'x_descending', 'y_ascending', 'y_descending'], layer.sort)}</select></label><label>Input row limit<input class="limit" type="number" min="1" value="${layer.limit ?? ''}"></label><label><span>Result limit ${info('Applied after filtering, aggregation, and sorting. Use this for ranked top-N plots; Input row limit instead bounds raw data loading.')}</span><input class="result-limit" type="number" min="1" value="${layer.result_limit ?? ''}"></label><label>Result Y min<input class="result-y-min" type="number" step="any" value="${layer.result_y_min ?? ''}"></label><label>Result Y max<input class="result-y-max" type="number" step="any" value="${layer.result_y_max ?? ''}"></label><label>Opacity<input class="alpha" type="number" min="0" max="1" step="0.05" value="${layer.style.alpha ?? 1}"></label><label>Marker<select class="marker">${option(['none', 'o', 's', '^', 'v', 'D', 'x', '+', '*'], layer.style.marker ?? 'none')}</select></label><label>Line width<input class="linewidth" type="number" step=".1" value="${layer.style.linewidth ?? 1.5}"></label></div><div class="checks"><label><input class="stacked" type="checkbox" ${layer.stacked ? 'checked' : ''}> Stacked</label><label><input class="secondary" type="checkbox" ${layer.secondary_y ? 'checked' : ''}> Secondary y</label><label><input class="fix-x-values" type="checkbox" ${layer.fix_x_values ? 'checked' : ''}> Fix shared X values from this layer ${info('This layer defines the ordered X domain after its filters, aggregation, sorting, and result limit. Every other layer is filtered and aligned to that domain.')}</label></div><label><span>Aggregation options (JSON) ${info('quantile, relative_count, and relative_value accept built-in options. See the examples below and the README for the complete list.')}</span><textarea class="aggregation-options">${escapeHtml(JSON.stringify(layer.aggregation_options))}</textarea></label><small>Examples: {"quantile":0.95}; {"scale":"percent"} for relative_count; or {"denominator":"total","scale":"percent"} for relative_value.</small><label><span>Plot options (JSON) ${info('Renderer-specific settings. Styling such as color, opacity, marker, and line width uses the controls above.')}</span><textarea class="plot-options">${escapeHtml(JSON.stringify(layer.options))}</textarea></label><small>Examples: histogram {"bins":50,"density":true}; step {"where":"pre"}; hexbin {"gridsize":40}.</small><div class="filter-editor"><div class="filter-editor-head"><strong>Filters</strong>${info('Structured filters and the layer expression use Root match. When set, the base expression is required and is applied before a relative_count denominator is calculated.') }<label>Root match<select class="filter-logic">${option(['and', 'or'], layer.filter_logic)}</select></label><button class="add-filter" type="button">+ condition</button><button class="add-filter-group" type="button">+ nested group</button></div><label>Layer base Polars expression<input class="base-filter-expression" list="expression-symbols" value="${escapeHtml(layer.base_filter_expression)}" placeholder="const.IS_SYN"><small>Defines base rows for this layer and is included in the relative_count denominator.</small></label><label>Layer Polars expression<input class="filter-expression" list="expression-symbols" value="${escapeHtml(layer.filter_expression)}" placeholder="pl.col('is_irregular_syn')"><small>Combined with the structured filters using Root match; affects the numerator/plotted rows.</small></label><div class="filters"></div></div>`;
    const groupLabel = card.querySelector('.group-column').closest('label');
    card.querySelector('.grouping-grid').appendChild(groupLabel);
    const groupingPanel = card.querySelector('.grouping-panel');
    const advanced = document.createElement('details');
    advanced.className = 'layer-advanced';
    advanced.open = expandedAdvanced.has(String(layer.id));
    const advancedStatus = [];
    const filterCount = filterConditions(layer.filters).length;
    if (filterCount) advancedStatus.push(`${filterCount} filter${filterCount === 1 ? '' : 's'}`);
    if (layer.base_filter_expression || layer.filter_expression) advancedStatus.push('Polars expression');
    if (layer.limit || layer.result_limit) advancedStatus.push('limited');
    if (layer.secondary_y) advancedStatus.push('secondary Y');
    if (layer.stacked) advancedStatus.push('stacked');
    const advancedSummary = document.createElement('summary');
    advancedSummary.innerHTML = `<span>Filters, limits & styling</span><span class="section-meta">${advancedStatus.join(' · ') || 'Optional'}</span>`;
    const advancedBody = document.createElement('div');
    advancedBody.className = 'layer-advanced-body';
    while (groupingPanel.nextSibling) advancedBody.appendChild(groupingPanel.nextSibling);
    advanced.append(advancedSummary, advancedBody);
    card.appendChild(advanced);
    card.querySelector('.marker').closest('label').insertAdjacentHTML(
      'afterend',
      `<label>Marker size<input class="markersize" type="number" min="0" step=".5" value="${escapeHtml(layer.style.markersize ?? 4)}"></label>`,
    );
    const selectedLineStyle = layer.style.linestyle ?? '-';
    const lineStyles = [['-', 'solid'], ['--', 'dashed'], ['-.', 'dash-dot'], [':', 'dotted'], ['none', 'no line']];
    card.querySelector('.linewidth').closest('label').insertAdjacentHTML(
      'afterend',
      `<label>Line style<select class="linestyle">${lineStyles.map(([value, label]) => `<option value="${value}" ${value === selectedLineStyle ? 'selected' : ''}>${label}</option>`).join('')}</select></label>`,
    );
    const q = selector => card.querySelector(selector);
    q('.enabled').onchange = event => { layer.enabled = event.target.checked; renderStages(); changed(true); };
    q('.label').oninput = event => { layer.label = event.target.value; changed(false); };
    q('.remove').onclick = () => { config.layers.splice(index, 1); reconcileStages(); renderLayers(); renderStages(); changed(false); };
    q('.source-select').onchange = event => {
      layer.source = event.target.value; layer.x_column = ''; layer.y_column = '';
      layer.group_column = ''; layer.time_bin = null; layer.required_filters = []; layer.filters = [];
      layer.base_filter_expression = ''; layer.filter_expression = '';
      renderLayers(); changed(true);
    };
    for (const [selector, key, presentation] of [
      ['.plot-type', 'plot_type', true],
      ['.x-column', 'x_column', false], ['.y-column', 'y_column', false],
      ['.group-column', 'group_column', false], ['.sort', 'sort', false],
      ['.filter-logic', 'filter_logic', false],
    ]) q(selector).onchange = event => { layer[key] = event.target.value; changed(!presentation); };
    q('.grouping-method').onchange = event => {
      if (event.target.value === 'none') { layer.aggregation = 'none'; layer.time_bin = null; }
      else {
        if (layer.aggregation === 'none') layer.aggregation = 'sum';
        layer.time_bin = event.target.value === 'group_by_dynamic' ? (layer.time_bin || '1m') : null;
      }
      renderLayers(); changed(true);
    };
    q('.aggregation').onchange = event => { layer.aggregation = event.target.value; changed(true); };
    q('.time-bin').onchange = event => { layer.time_bin = event.target.value.trim() || '1m'; changed(true); };
    q('.limit').oninput = event => { layer.limit = event.target.value === '' ? null : Number(event.target.value); changed(true); };
    q('.result-limit').oninput = event => { layer.result_limit = event.target.value === '' ? null : Number(event.target.value); changed(true); };
    q('.result-y-min').oninput = event => { layer.result_y_min = event.target.value === '' ? null : Number(event.target.value); changed(true); };
    q('.result-y-max').oninput = event => { layer.result_y_max = event.target.value === '' ? null : Number(event.target.value); changed(true); };
    q('.color').oninput = event => {
      layer.style.color = event.target.value;
      card.querySelectorAll('.standard-color').forEach(button => button.classList.toggle('active', button.dataset.color.toLowerCase() === event.target.value.toLowerCase()));
      changed(false);
    };
    card.querySelectorAll('.standard-color').forEach(button => {
      button.onclick = () => {
        layer.style.color = button.dataset.color; q('.color').value = button.dataset.color;
        card.querySelectorAll('.standard-color').forEach(item => item.classList.toggle('active', item === button));
        changed(false);
      };
    });
    q('.alpha').oninput = event => { layer.style.alpha = Number(event.target.value); changed(false); };
    q('.marker').onchange = event => { layer.style.marker = event.target.value; changed(false); };
    q('.markersize').oninput = event => { layer.style.markersize = Number(event.target.value); changed(false); };
    q('.linewidth').oninput = event => { layer.style.linewidth = Number(event.target.value); changed(false); };
    q('.linestyle').onchange = event => { layer.style.linestyle = event.target.value; changed(false); };
    q('.stacked').onchange = event => { layer.stacked = event.target.checked; changed(false); };
    q('.secondary').onchange = event => { layer.secondary_y = event.target.checked; changed(false); };
    q('.fix-x-values').onchange = event => {
      config.layers.forEach(item => { item.fix_x_values = item === layer && event.target.checked; });
      renderLayers(); changed(true);
    };
    q('.aggregation-options').onchange = event => {
      try { layer.aggregation_options = JSON.parse(event.target.value); changed(true); }
      catch { message('Invalid aggregation options JSON', true); }
    };
    q('.plot-options').onchange = event => {
      try { layer.options = JSON.parse(event.target.value); changed(false); }
      catch { message('Invalid plot options JSON', true); }
    };
    q('.base-filter-expression').oninput = event => { layer.base_filter_expression = event.target.value; changed(true); };
    q('.filter-expression').oninput = event => { layer.filter_expression = event.target.value; changed(true); };
    const filters = q('.filters');
    layer.filters.forEach((item, filterIndex) => filters.appendChild(filterRow(layer, item, layer.filters, filterIndex)));
    q('.add-filter').onclick = () => {
      layer.filters.push(newFilter(layer));
      renderLayers(); changed(true);
    };
    q('.add-filter-group').onclick = () => {
      layer.filters.push(newFilterGroup(layer));
      renderLayers(); changed(true);
    };
    root.appendChild(card);
  });
}

const annotationCoordinates = kind => ({vline: ['x'], hline: ['y'], vspan: ['x1', 'x2'], hspan: ['y1', 'y2'], text: ['x', 'y']}[kind] ?? ['x', 'y']);
const coordinateEditor = (item, field) => {
  const mode = field === 'x' ? item.inference?.x : field === 'y' ? item.inference?.y : 'manual';
  return `<label>${field}<span class="coordinate"><button type="button" data-nudge="${field}" data-direction="-1" ${mode !== 'manual' ? 'disabled' : ''}>−</button><input data-coordinate="${field}" value="${escapeHtml(item[field] ?? '')}" ${mode !== 'manual' ? 'disabled' : ''}><button type="button" data-nudge="${field}" data-direction="1" ${mode !== 'manual' ? 'disabled' : ''}>+</button></span></label>`;
};
const inferenceOptions = (axis, selected) => {
  const values = axis === 'x' ? [
    ['manual', 'manual X'], ['min_x', 'minimum X'], ['max_x', 'maximum X'],
    ['x_at_min_y', 'X at minimum Y'], ['x_at_max_y', 'X at maximum Y'],
  ] : [
    ['manual', 'manual Y'], ['min_y', 'minimum Y'], ['max_y', 'maximum Y'],
    ['y_at_min_x', 'Y at minimum X'], ['y_at_max_x', 'Y at maximum X'],
  ];
  return values.map(([value, label]) => `<option value="${value}" ${value === selected ? 'selected' : ''}>${label}</option>`).join('');
};
function syncAnnotationJson() { $('annotations').value = JSON.stringify(config.annotations, null, 2); }

function renderAnnotations() {
  const root = $('annotation-editors'); root.innerHTML = '';
  $('annotation-count').textContent = `${config.annotations.length} configured`;
  config.annotations.forEach((item, index) => {
    const card = document.createElement('div'); card.className = 'annotation';
    const coordinateFields = annotationCoordinates(item.kind).map(field => coordinateEditor(item, field)).join('');
    const selectedLayers = new Set(item.inference?.layer_ids ?? []);
    const layerOptions = config.layers.map(layer => `<option value="${escapeHtml(layer.id)}" ${selectedLayers.has(layer.id) ? 'selected' : ''}>${escapeHtml(layer.label || layer.id)}</option>`).join('');
    const hasX = ['text', 'vline'].includes(item.kind); const hasY = ['text', 'hline'].includes(item.kind);
    card.innerHTML = `<div class="annotation-head"><input class="enabled" type="checkbox" ${item.enabled !== false ? 'checked' : ''}><label>ID<input class="annotation-id" value="${escapeHtml(item.id)}"></label><label>Annotation label<input class="annotation-label" value="${escapeHtml(item.label ?? '')}"></label><button class="remove">×</button></div><div class="grid"><label>Kind<select class="kind">${option(['text', 'vline', 'hline', 'vspan', 'hspan'], item.kind)}</select></label><label>Displayed text<input class="text" value="${escapeHtml(item.text ?? '')}" placeholder="Text drawn on the plot"></label>${coordinateFields}${hasX ? `<label>Infer X<select class="infer-x">${inferenceOptions('x', item.inference?.x ?? 'manual')}</select></label>` : ''}${hasY ? `<label>Infer Y<select class="infer-y">${inferenceOptions('y', item.inference?.y ?? 'manual')}</select></label>` : ''}<label>Inference layers<select class="inference-layers" multiple size="${Math.min(4, Math.max(2, config.layers.length))}">${layerOptions}</select><small>Ctrl/Cmd-click to select multiple layers.</small></label><label><span>Nudge step ${info('The amount added or subtracted from a numeric coordinate each time you click its − or + button. For example, a step of 1000000 moves Y by one million per click.')}</span><input class="step" value="${escapeHtml(item.step ?? 1)}"></label><label>Font size<span class="coordinate"><button type="button" class="font-down">−</button><input class="fontsize" type="number" min="1" value="${item.fontsize ?? 10}"><button type="button" class="font-up">+</button></span></label><label>Color<input class="color" type="color" value="${item.color ?? '#666666'}"></label><label><span><input class="text-background-enabled" type="checkbox" ${item.text_background_enabled ? 'checked' : ''}> Text background</span><input class="text-background-color" type="color" value="${item.text_background_color ?? '#ffffff'}"></label><label>Opacity<input class="alpha" type="number" min="0" max="1" step="0.05" value="${item.alpha ?? 0.6}"></label><label>Line style<select class="linestyle">${option(['-', '--', '-.', ':'], item.linestyle ?? '--')}</select></label><label class="legend-annotation"><span><input class="show-in-legend" type="checkbox" ${item.show_in_legend ? 'checked' : ''}> Add to legend</span><input class="legend-label" value="${escapeHtml(item.legend_label ?? '')}" placeholder="Legend label"></label></div>`;
    const q = selector => card.querySelector(selector);
    q('.enabled').onchange = event => { item.enabled = event.target.checked; renderStages(); syncAnnotationJson(); changed(false); };
    q('.annotation-id').onchange = event => {
      const oldId = item.id; item.id = event.target.value.trim() || oldId;
      for (const step of config.stages.steps ?? []) for (const element of step.elements ?? []) if (element.id === `annotation:${oldId}`) element.id = `annotation:${item.id}`;
      renderStages(); syncAnnotationJson(); changed(false);
    };
    q('.annotation-label').oninput = event => { item.label = event.target.value; syncAnnotationJson(); renderStages(); changed(false); };
    q('.text').oninput = event => { item.text = event.target.value; syncAnnotationJson(); changed(false); };
    q('.remove').onclick = () => { config.annotations.splice(index, 1); reconcileStages(); renderAnnotations(); renderStages(); changed(false); };
    q('.kind').onchange = event => { item.kind = event.target.value; renderAnnotations(); changed(false); };
    q('.step').onchange = event => { item.step = scalar(event.target.value); syncAnnotationJson(); };
    q('.fontsize').onchange = event => { item.fontsize = Number(event.target.value); syncAnnotationJson(); changed(false); };
    q('.font-down').onclick = () => { item.fontsize = Math.max(1, Number(item.fontsize ?? 10) - 1); renderAnnotations(); changed(false); };
    q('.font-up').onclick = () => { item.fontsize = Number(item.fontsize ?? 10) + 1; renderAnnotations(); changed(false); };
    q('.color').oninput = event => { item.color = event.target.value; item.text_color = event.target.value; syncAnnotationJson(); changed(false); };
    q('.text-background-enabled').onchange = event => { item.text_background_enabled = event.target.checked; syncAnnotationJson(); changed(false); };
    q('.text-background-color').oninput = event => { item.text_background_color = event.target.value; syncAnnotationJson(); changed(false); };
    q('.alpha').oninput = event => { item.alpha = Number(event.target.value); syncAnnotationJson(); changed(false); };
    q('.linestyle').onchange = event => { item.linestyle = event.target.value; syncAnnotationJson(); changed(false); };
    q('.show-in-legend').onchange = event => { item.show_in_legend = event.target.checked; syncAnnotationJson(); changed(false); };
    q('.legend-label').oninput = event => { item.legend_label = event.target.value; syncAnnotationJson(); changed(false); };
    if (q('.infer-x')) q('.infer-x').onchange = event => { item.inference.x = event.target.value; renderAnnotations(); changed(false); };
    if (q('.infer-y')) q('.infer-y').onchange = event => { item.inference.y = event.target.value; renderAnnotations(); changed(false); };
    q('.inference-layers').onchange = event => { item.inference.layer_ids = [...event.target.selectedOptions].map(option => option.value); syncAnnotationJson(); changed(false); };
    card.querySelectorAll('[data-coordinate]').forEach(input => {
      input.onchange = () => { item[input.dataset.coordinate] = scalar(input.value); syncAnnotationJson(); changed(false); };
    });
    card.querySelectorAll('[data-nudge]').forEach(button => {
      button.onclick = () => {
        const field = button.dataset.nudge; const current = Number(item[field]); const step = Number(item.step ?? 1);
        if (!Number.isFinite(current) || !Number.isFinite(step)) return message('Nudge buttons require numeric coordinates and step.', true);
        item[field] = current + Number(button.dataset.direction) * step;
        renderAnnotations(); changed(false);
      };
    });
    root.appendChild(card);
  });
  syncAnnotationJson();
}

function stageElements() {
  return [
    ...config.layers.map(item => ({id: `layer:${item.id}`, label: `Layer: ${item.label}`, layer: true})),
    ...config.annotations.map(item => ({id: `annotation:${item.id}`, label: `Annotation: ${item.label || item.id}`, layer: false})),
  ];
}
function reconcileStages() {
  const valid = new Set(stageElements().map(item => item.id));
  config.stages.steps = (config.stages.steps ?? []).map(step => ({...step, elements: (step.elements ?? []).filter(item => valid.has(item.id))})).filter(step => step.elements.length);
}
function generateDefaultStages(render = true) {
  const ordered = [
    ...config.layers.filter(item => item.enabled !== false).map(item => `layer:${item.id}`),
    ...config.annotations.filter(item => item.enabled !== false).map(item => `annotation:${item.id}`),
  ];
  config.stages.enabled = true;
  config.stages.steps = ordered.map((current, index) => ({
    id: `stage-${Date.now()}-${index}`, label: `Stage ${index + 1}`,
    elements: ordered.slice(0, index + 1).map(id => ({id, overlay: id.startsWith('layer:') && id !== current})),
  }));
  if (render) { $('stages-enabled').checked = true; renderStages(); changed(false); }
}
function renderStages() {
  const root = $('stages'); root.innerHTML = '';
  $('stage-count').textContent = config.stages.enabled
    ? `${config.stages.steps.length} configured`
    : 'Off';
  const available = stageElements();
  (config.stages.steps ?? []).forEach((step, index) => {
    const card = document.createElement('div'); card.className = 'stage';
    card.innerHTML = `<div class="stage-head"><input class="label" value="${escapeHtml(step.label ?? `Stage ${index + 1}`)}"><button class="remove">×</button></div><div class="stage-elements"></div>`;
    card.querySelector('.label').oninput = event => { step.label = event.target.value; changed(false); };
    card.querySelector('.remove').onclick = () => { config.stages.steps.splice(index, 1); renderStages(); changed(false); };
    const elements = card.querySelector('.stage-elements');
    for (const element of available) {
      const selected = (step.elements ?? []).find(item => item.id === element.id);
      const row = document.createElement('div'); row.className = 'stage-element';
      row.innerHTML = `<span>${escapeHtml(element.label)}</span><label><input class="include" type="checkbox" ${selected ? 'checked' : ''}> Include</label><label><input class="overlay" type="checkbox" ${selected?.overlay ? 'checked' : ''} ${!selected || !element.layer ? 'disabled' : ''}> Overlay</label>`;
      row.querySelector('.include').onchange = event => {
        step.elements ??= [];
        if (event.target.checked) step.elements.push({id: element.id, overlay: false});
        else step.elements = step.elements.filter(item => item.id !== element.id);
        renderStages();
        if (step.elements.length) changed(false);
        else message('Select at least one element for this stage.', true);
      };
      row.querySelector('.overlay').onchange = event => {
        const item = step.elements.find(candidate => candidate.id === element.id);
        if (item) item.overlay = event.target.checked;
        changed(false);
      };
      elements.appendChild(row);
    }
    root.appendChild(card);
  });
}

function collect() {
  const result = structuredClone(config);
  result.config_module = $('config-module').value.trim() || null;
  result.sources = bootstrap.sources.map(({name, path, format, options, filter_expression}) => ({name, path, format, options, filter_expression: filter_expression ?? null}));
  result.filename = $('filename').value;
  result.figure = {width: Number($('width').value), height: Number($('height').value), dpi: 150, font_family: $('mono').checked ? 'monospace' : 'default', font_size: Number($('font-size').value)};
  result.axes = {
    xlabel: $('xlabel').value, ylabel: $('ylabel').value, secondary_ylabel: $('secondary-ylabel').value,
    xscale: $('xscale').value, yscale: $('yscale').value, secondary_yscale: $('secondary-yscale').value,
    xmin: scalarValue('xmin'), xmax: scalarValue('xmax'), ymin: numberValue('ymin'), ymax: numberValue('ymax'),
    secondary_ymin: numberValue('secondary-ymin'), secondary_ymax: numberValue('secondary-ymax'), x_grid: $('x-grid').checked, y_grid: $('y-grid').checked,
    secondary_y_grid: $('secondary-grid').checked, grid_alpha: Number($('grid-opacity').value), major_x_ticks: $('major-x').checked,
    minor_x_ticks: $('minor-x').checked, minor_y_ticks: $('minor-y').checked,
    secondary_minor_y_ticks: $('secondary-minor-y').checked, custom_x_ticks: csvNumbers('xticks'), custom_x_tick_labels: csvText('xtick-labels'),
    custom_y_ticks: csvNumbers('yticks'), custom_y_tick_labels: csvText('ytick-labels'),
    y_tick_min: numberValue('y-tick-min'), y_tick_max: numberValue('y-tick-max'),
    y_tick_step: numberValue('y-tick-step'),
    custom_secondary_y_ticks: csvNumbers('secondary-yticks'), custom_secondary_y_tick_labels: csvText('secondary-ytick-labels'),
    secondary_y_tick_min: numberValue('secondary-y-tick-min'), secondary_y_tick_max: numberValue('secondary-y-tick-max'),
    secondary_y_tick_step: numberValue('secondary-y-tick-step'),
    x_value_ticks: $('x-value-ticks').checked,
    x_value_tick_interval: Number($('x-value-tick-interval').value),
    x_datetime_format: $('x-datetime-format').value,
    x_tick_rotation: Number($('rotation').value), x_tick_horizontal_alignment: $('tick-ha').value,
    x_tick_vertical_alignment: $('tick-va').value, x_engineering: $('x-engineering').checked,
    y_engineering: $('y-engineering').checked, secondary_y_engineering: $('secondary-y-engineering').checked,
    label_font_size_override: $('label-font-override').checked, label_font_size: Number($('label-font-size').value),
    tick_font_size_override: $('tick-font-override').checked, tick_font_size: Number($('tick-font-size').value),
  };
  result.legend = {
    enabled: $('legend').checked, loc: $('legend-loc').value,
    ncols: Number($('legend-ncols').value),
    bbox_enabled: $('legend-bbox-enabled').checked,
    bbox_x: Number($('legend-bbox-x').value), bbox_y: Number($('legend-bbox-y').value),
    handlelength: Number($('legend-handlelength').value),
    columnspacing: Number($('legend-columnspacing').value),
    handletextpad: Number($('legend-handletextpad').value),
    opacity: Number($('legend-opacity').value),
    font_size_override: $('legend-font-override').checked,
    font_size: Number($('legend-font-size').value),
  };
  result.broken_y_axis.enabled = $('broken-y-enabled').checked;
  result.broken_y_axis.gap = Number($('broken-y-gap').value);
  result.stages.enabled = $('stages-enabled').checked;
  result.stages.overlay_alpha = Number($('overlay-alpha').value);
  result.export_formats = ['png', 'pdf', 'json'].filter(x => $(x).checked);
  return result;
}

function updateFontControlState() {
  $('label-font-size').disabled = !$('label-font-override').checked;
  $('tick-font-size').disabled = !$('tick-font-override').checked;
  $('legend-font-size').disabled = !$('legend-font-override').checked;
}
function updateLegendBboxState() {
  $('legend-bbox-x').disabled = !$('legend-bbox-enabled').checked;
  $('legend-bbox-y').disabled = !$('legend-bbox-enabled').checked;
}
function updateXValueTickState() {
  $('x-value-tick-interval').disabled = !$('x-value-ticks').checked;
}
function apply(next) {
  queryRevision += 1; queryDirty = true;
  config = structuredClone(next); ensureConfig();
  $('config-module').value = config.config_module ?? bootstrap.config_module ?? '';
  $('filename').value = config.filename ?? 'explorative-plot';
  $('width').value = config.figure.width ?? 5.6; $('height').value = config.figure.height ?? 2.8;
  $('font-size').value = config.figure.font_size ?? 12; $('mono').checked = config.figure.font_family === 'monospace';
  const axes = config.axes;
  $('xlabel').value = axes.xlabel ?? ''; $('ylabel').value = axes.ylabel ?? '';
  $('secondary-ylabel').value = axes.secondary_ylabel ?? ''; $('xscale').value = axes.xscale ?? 'linear';
  $('yscale').value = axes.yscale ?? 'linear';
  $('secondary-yscale').value = axes.secondary_yscale ?? 'linear';
  for (const id of ['xmin', 'xmax', 'ymin', 'ymax']) $(id).value = axes[id] ?? '';
  $('secondary-ymin').value = axes.secondary_ymin ?? ''; $('secondary-ymax').value = axes.secondary_ymax ?? '';
  $('x-grid').checked = axes.x_grid ?? false; $('y-grid').checked = axes.y_grid ?? true;
  $('secondary-grid').checked = axes.secondary_y_grid ?? false; $('major-x').checked = axes.major_x_ticks ?? true;
  $('grid-opacity').value = axes.grid_alpha ?? 0.5;
  $('minor-x').checked = axes.minor_x_ticks ?? false;
  $('minor-y').checked = axes.minor_y_ticks ?? false;
  $('secondary-minor-y').checked = axes.secondary_minor_y_ticks ?? false;
  $('x-engineering').checked = axes.x_engineering ?? false;
  $('y-engineering').checked = axes.y_engineering ?? false; $('secondary-y-engineering').checked = axes.secondary_y_engineering ?? false;
  $('label-font-override').checked = axes.label_font_size_override ?? false;
  $('label-font-size').value = axes.label_font_size ?? config.figure.font_size ?? 12;
  $('tick-font-override').checked = axes.tick_font_size_override ?? false;
  $('tick-font-size').value = axes.tick_font_size ?? config.figure.font_size ?? 12;
  $('legend-font-override').checked = config.legend.font_size_override ?? false;
  $('legend-font-size').value = config.legend.font_size ?? config.figure.font_size ?? 12;
  $('legend-loc').value = config.legend.loc ?? 'upper center';
  $('legend-ncols').value = config.legend.ncols ?? 1;
  $('legend-bbox-enabled').checked = config.legend.bbox_enabled ?? true;
  $('legend-bbox-x').value = config.legend.bbox_x ?? 0.5;
  $('legend-bbox-y').value = config.legend.bbox_y ?? 1.2;
  $('legend-handlelength').value = config.legend.handlelength ?? 1.5;
  $('legend-columnspacing').value = config.legend.columnspacing ?? 0.8;
  $('legend-handletextpad').value = config.legend.handletextpad ?? 0.5;
  $('legend-opacity').value = config.legend.opacity ?? 0.8;
  $('broken-y-enabled').checked = config.broken_y_axis.enabled ?? false;
  $('broken-y-gap').value = config.broken_y_axis.gap ?? 0.1;
  $('xticks').value = (axes.custom_x_ticks ?? []).join(', '); $('xtick-labels').value = (axes.custom_x_tick_labels ?? []).join(', ');
  $('yticks').value = (axes.custom_y_ticks ?? []).join(', '); $('ytick-labels').value = (axes.custom_y_tick_labels ?? []).join(', ');
  $('y-tick-min').value = axes.y_tick_min ?? ''; $('y-tick-max').value = axes.y_tick_max ?? '';
  $('y-tick-step').value = axes.y_tick_step ?? '';
  $('secondary-yticks').value = (axes.custom_secondary_y_ticks ?? []).join(', ');
  $('secondary-ytick-labels').value = (axes.custom_secondary_y_tick_labels ?? []).join(', ');
  $('secondary-y-tick-min').value = axes.secondary_y_tick_min ?? '';
  $('secondary-y-tick-max').value = axes.secondary_y_tick_max ?? '';
  $('secondary-y-tick-step').value = axes.secondary_y_tick_step ?? '';
  $('x-value-ticks').checked = axes.x_value_ticks ?? false;
  $('x-value-tick-interval').value = axes.x_value_tick_interval ?? 1;
  $('x-datetime-format').value = axes.x_datetime_format ?? '';
  $('rotation').value = axes.x_tick_rotation ?? 0; $('tick-ha').value = axes.x_tick_horizontal_alignment ?? 'center';
  $('tick-va').value = axes.x_tick_vertical_alignment ?? 'top'; $('legend').checked = config.legend.enabled ?? true;
  $('stages-enabled').checked = config.stages.enabled ?? false; $('overlay-alpha').value = config.stages.overlay_alpha ?? 0.25;
  for (const format of ['png', 'pdf', 'json']) $(format).checked = (config.export_formats ?? []).includes(format);
  renderBrokenYRanges(); updateBrokenYAxisState(); updateXValueTickState();
  updateFontControlState(); updateLegendBboxState(); renderLayers(); renderAnnotations(); renderStages();
}

function changed(queryChanged = true) {
  if (queryChanged) {
    queryRevision += 1; queryDirty = true; clearTimeout(liveTimer);
    const workspace = workspaceById(activeWorkspaceId);
    if (workspace) workspace.queryDirty = true;
    renderWorkspaceTabs();
    message('Query configuration changed; render to update.'); return;
  }
  if (queryDirty) return message('Style changed; finish the query settings and click Render.');
  if (!$('live-update').checked) return message('Style changed; render to update.');
  if (!$('previews').children.length) return message('Style changed; render once to enable live updates.');
  message('Updating preview…');
  if (!config.layers.some(layer => layer.enabled !== false)) return;
  clearTimeout(liveTimer); liveTimer = setTimeout(() => post('/api/render', false, true), 300);
}
async function apiError(response, workspaceId = activeWorkspaceId) {
  let body; try { body = await response.json(); } catch { body = {error: response.statusText}; }
  const text = body.error ?? response.statusText;
  const workspace = workspaceById(workspaceId);
  if (workspace) workspace.message = {text, error: true};
  if (workspaceId === activeWorkspaceId) message(text, true);
  return text;
}
async function applyConfigModule(showMessage = true) {
  const workspaceId = activeWorkspaceId;
  const currentWorkspace = workspaceById(workspaceId);
  if (currentWorkspace) currentWorkspace.config = collect();
  const requested = $('config-module').value.trim() || null;
  if (requested === (bootstrap.config_module ?? null)) return true;
  const response = await apiFetch('/api/config-module', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({module: requested})}, workspaceId);
  if (!response.ok) { await apiError(response, workspaceId); return false; }
  const result = await response.json(); const workspace = workspaceById(workspaceId);
  if (!workspace) return false;
  workspace.bootstrap.config_module = result.config_module;
  workspace.bootstrap.sources = result.sources;
  workspace.bootstrap.expression_variables = result.expression_variables ?? [];
  workspace.config.config_module = result.config_module; workspace.queryDirty = true;
  if (workspaceId !== activeWorkspaceId) return false;
  bootstrap = workspace.bootstrap; config = workspace.config; renderExpressionSymbols();
  queryRevision += 1; queryDirty = true; clearTimeout(liveTimer);
  renderSources(); renderLayers(); schedulePathSuggestions();
  if (showMessage) message(`Config module applied: ${result.config_module ?? '(none)'}.`);
  return true;
}
async function post(path, download = false, live = false) {
  const workspaceId = activeWorkspaceId;
  if (live && queryDirty) return;
  if (!await applyConfigModule(false)) return;
  if (workspaceId !== activeWorkspaceId) return;
  let body; try { body = collect(); } catch (error) { return message(error.message, true); }
  const sequence = ++renderSequence; const revision = queryRevision;
  if (!live) message('Working…');
  const response = await apiFetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}, workspaceId);
  if (!response.ok) return apiError(response, workspaceId);
  if (download) {
    const blob = await response.blob(); const link = document.createElement('a');
    const disposition = response.headers.get('content-disposition') ?? '';
    const match = disposition.match(/filename="?([^";]+)"?/);
    link.href = URL.createObjectURL(blob); link.download = match?.[1] ?? body.filename;
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 0);
    message('Download prepared.'); return;
  }
  const result = await response.json();
  const workspace = workspaceById(workspaceId);
  if (workspace) {
    workspace.config = body;
    workspace.previewHtml = previewMarkup(result.previews ?? []);
    workspace.queryDirty = false;
    workspace.message = {
      text: result.files ? `Saved: ${result.files.join(', ')}` : 'Rendered.', error: false,
    };
  }
  if (sequence !== renderSequence || revision !== queryRevision) return;
  queryDirty = false;
  const activeWorkspace = workspaceById(activeWorkspaceId);
  if (activeWorkspace) activeWorkspace.queryDirty = false;
  renderWorkspaceTabs();
  updatePreviews(result.previews);
  const summaries = (result.cache ?? []).map(item => `${item.layer}: ${item.rows} rows, ${item.grouping}${item.every ? ` every ${item.every}` : ''}`);
  message(result.files ? `Saved: ${result.files.join(', ')}` : `Rendered. ${summaries.join('; ')}`);
}

function bindPresentationControls() {
  const inputIds = ['width', 'height', 'font-size', 'xlabel', 'ylabel', 'secondary-ylabel', 'xmin', 'xmax', 'ymin', 'ymax', 'secondary-ymin', 'secondary-ymax', 'xticks', 'xtick-labels', 'yticks', 'ytick-labels', 'y-tick-min', 'y-tick-max', 'y-tick-step', 'secondary-yticks', 'secondary-ytick-labels', 'secondary-y-tick-min', 'secondary-y-tick-max', 'secondary-y-tick-step', 'x-value-tick-interval', 'x-datetime-format', 'rotation', 'grid-opacity', 'label-font-size', 'tick-font-size', 'legend-font-size', 'legend-ncols', 'legend-bbox-x', 'legend-bbox-y', 'legend-handlelength', 'legend-columnspacing', 'legend-handletextpad', 'legend-opacity', 'broken-y-gap'];
  const changeIds = ['xscale', 'yscale', 'secondary-yscale', 'tick-ha', 'tick-va', 'major-x', 'minor-x', 'minor-y', 'secondary-minor-y', 'x-engineering', 'y-engineering', 'secondary-y-engineering', 'x-grid', 'y-grid', 'secondary-grid', 'legend', 'legend-loc', 'mono'];
  for (const id of inputIds) $(id).oninput = () => changed(false);
  for (const id of changeIds) $(id).onchange = () => changed(false);
  for (const id of ['label-font-override', 'tick-font-override', 'legend-font-override']) {
    $(id).onchange = () => { updateFontControlState(); changed(false); };
  }
  $('legend-bbox-enabled').onchange = () => { updateLegendBboxState(); changed(false); };
  $('x-value-ticks').onchange = () => { updateXValueTickState(); changed(false); };
  $('broken-y-enabled').onchange = event => {
    config.broken_y_axis.enabled = event.target.checked;
    if (event.target.checked && config.broken_y_axis.ranges.length < 2) {
      config.broken_y_axis.ranges = [{min: 0, max: 1}, {min: 10, max: 20}];
      renderBrokenYRanges();
    }
    if (event.target.checked) { $('ymin').value = ''; $('ymax').value = ''; }
    updateBrokenYAxisState(); changed(false);
  };
  $('add-broken-y-range').onclick = () => {
    const previous = config.broken_y_axis.ranges.at(-1);
    const lower = previous ? Number(previous.max) + 1 : 0;
    config.broken_y_axis.ranges.push({min: lower, max: lower + 1});
    renderBrokenYRanges(); updateBrokenYAxisState(); changed(false);
  };
}

$('apply-config-module').onclick = () => applyConfigModule();
$('add-workspace').onclick = () => addWorkspace();
$('source-path').oninput = schedulePathSuggestions;
$('source-path').onfocus = schedulePathSuggestions;
$('source-path').onkeydown = event => {
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    if (!pathSuggestions.length) return;
    event.preventDefault();
    selectPathSuggestion(pathSuggestionIndex + (event.key === 'ArrowDown' ? 1 : -1));
  } else if (event.key === 'Tab') {
    event.preventDefault(); clearTimeout(pathSuggestionTimer);
    updatePathSuggestions().then(() => acceptPathSuggestion()).catch(() => {});
  } else if (event.key === 'Enter' && pathSuggestions.length) {
    event.preventDefault(); acceptPathSuggestion();
  } else if (event.key === 'Escape') {
    renderPathSuggestions([]);
  }
};
$('source-path').onblur = () => setTimeout(() => renderPathSuggestions([]), 120);
$('show-system-stats').onchange = event => setSystemStatsEnabled(event.target.checked);
$('add-source').onclick = async () => {
  const workspaceId = activeWorkspaceId;
  if (!await applyConfigModule(false)) return;
  if (workspaceId !== activeWorkspaceId) return;
  const submitted = {name: $('source-name').value, path: $('source-path').value, format: $('source-format').value, separator: $('source-separator').value, filterExpression: $('source-filter-expression').value, optionsText: $('source-options').value};
  let options; try { options = JSON.parse(submitted.optionsText || '{}'); } catch { return message('Invalid source options JSON', true); }
  message(`Inferring ${submitted.name || 'source'} schema from the first 100 rows in the background…`);
  const response = await apiFetch('/api/sources', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: submitted.name, path: submitted.path, format: submitted.format, separator: submitted.separator, filter_expression: submitted.filterExpression, options})}, workspaceId);
  if (!response.ok) return apiError(response, workspaceId);
  const added = await response.json(); const workspace = workspaceById(workspaceId);
  if (!workspace) return;
  workspace.bootstrap.sources = workspace.bootstrap.sources.filter(item => item.name !== added.name); workspace.bootstrap.sources.push(added);
  workspace.queryDirty = true;
  if (workspaceId !== activeWorkspaceId) return;
  bootstrap = workspace.bootstrap;
  $('source-name').value = ''; $('source-path').value = ''; $('source-format').value = 'auto'; $('source-separator').value = ''; $('source-filter-expression').value = ''; $('source-options').value = '{}';
  renderPathSuggestions([]); $('source-path-resolved').textContent = '';
  renderSources(); renderLayers(); message('Lazy source registered.');
};
$('add-layer').onclick = () => { config.layers.push(newLayer()); renderLayers(); renderStages(); changed(true); };
$('add-annotation').onclick = () => { config.annotations.push(newAnnotation()); renderAnnotations(); renderStages(); changed(false); };
$('annotations').onchange = event => {
  try { config.annotations = JSON.parse(event.target.value); ensureConfig(); reconcileStages(); renderAnnotations(); renderStages(); changed(false); }
  catch (error) { message(`Invalid annotation JSON: ${error.message}`, true); }
};
$('stages-enabled').onchange = event => {
  config.stages.enabled = event.target.checked;
  if (config.stages.enabled && !config.stages.steps.length) generateDefaultStages(false);
  renderStages(); changed(false);
};
$('overlay-alpha').oninput = event => { config.stages.overlay_alpha = Number(event.target.value); changed(false); };
$('generate-stages').onclick = () => generateDefaultStages(true);
$('add-stage').onclick = () => {
  const elements = stageElements();
  config.stages.steps.push({id: `stage-${Date.now()}`, label: `Stage ${config.stages.steps.length + 1}`, elements: elements.length ? [{id: elements[0].id, overlay: false}] : []});
  config.stages.enabled = true; $('stages-enabled').checked = true; renderStages(); changed(false);
};
$('render').onclick = () => post('/api/render'); $('save').onclick = () => post('/api/save');
$('download').onclick = () => post('/api/download', true); $('export-code').onclick = () => post('/api/export-code', true);
$('clear-cache').onclick = async () => { await apiFetch('/api/cache/clear', {method: 'POST'}); message('Cache cleared.'); };
$('load-config').onchange = async event => {
  const workspaceId = activeWorkspaceId;
  const workspace = workspaceById(workspaceId);
  if (!workspace) return;
  const targetBootstrap = workspace.bootstrap;
  let loaded; try { loaded = JSON.parse(await event.target.files[0].text()); }
  catch (error) { return message(`Invalid configuration: ${error.message}`, true); }
  const migrationResponse = await apiFetch('/api/migrate-config', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(loaded)}, workspaceId);
  if (!migrationResponse.ok) return apiError(migrationResponse, workspaceId);
  const migration = await migrationResponse.json(); loaded = migration.config;
  const issues = [...(migration.warnings ?? [])];
  if (!Array.isArray(loaded.sources)) { issues.push('sources: expected an array; no sources were loaded'); loaded.sources = []; }
  if (!Array.isArray(loaded.layers)) { issues.push('layers: expected an array; no layers were loaded'); loaded.layers = []; }
  let module = loaded.config_module ?? null;
  let response = await apiFetch('/api/config-module', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({module, sources: []})}, workspaceId);
  if (!response.ok && module !== null) {
    const body = await response.json().catch(() => ({error: response.statusText}));
    issues.push(`config_module: ${body.error ?? response.statusText}; continued without it`);
    module = null;
    response = await apiFetch('/api/config-module', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({module, sources: []})}, workspaceId);
  }
  if (!response.ok) return apiError(response, workspaceId);
  const result = await response.json(); targetBootstrap.config_module = result.config_module; targetBootstrap.sources = [];
  targetBootstrap.expression_variables = result.expression_variables ?? [];
  const loadedSources = [];
  for (const sourceSpec of loaded.sources) {
    const sourceResponse = await apiFetch('/api/sources', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(sourceSpec)}, workspaceId);
    if (!sourceResponse.ok) {
      const body = await sourceResponse.json().catch(() => ({error: sourceResponse.statusText}));
      issues.push(`source ${sourceSpec.name ?? '(unnamed)'}: ${body.error ?? sourceResponse.statusText}`);
      continue;
    }
    const added = await sourceResponse.json(); targetBootstrap.sources.push(added);
    loadedSources.push(sourceSpec);
  }
  loaded.config_module = targetBootstrap.config_module; loaded.sources = loadedSources;
  const sourceNames = new Set(targetBootstrap.sources.map(item => item.name));
  loaded.layers = loaded.layers.filter((layer, index) => {
    if (!layer || typeof layer !== 'object' || Array.isArray(layer)) {
      issues.push(`layer ${index + 1}: expected an object; layer skipped`); return false;
    }
    if (!sourceNames.has(layer.source)) {
      layer.enabled = false;
      issues.push(`layer ${layer.label ?? layer.id ?? '(unnamed)'}: source ${layer.source ?? '(none)'} was not loaded; layer disabled`);
      return true;
    }
    const metadata = targetBootstrap.sources.find(item => item.name === layer.source);
    const layerColumns = new Set((metadata?.columns ?? []).map(item => item.name));
    const invalid = [];
    if (!targetBootstrap.registry.plot_types.includes(layer.plot_type ?? 'line')) invalid.push(`unknown plot type ${layer.plot_type}`);
    if (!targetBootstrap.registry.aggregations.includes(layer.aggregation ?? 'none')) invalid.push(`unknown aggregation ${layer.aggregation}`);
    if (!['histogram', 'box', 'violin'].includes(layer.plot_type ?? 'line') && !layer.x_column) invalid.push('missing X-column selection');
    if (!['count', 'relative_count'].includes(layer.aggregation ?? 'none') && !layer.y_column) invalid.push('missing Y-column selection');
    if (layer.x_column && !layerColumns.has(layer.x_column)) invalid.push(`missing X column ${layer.x_column}`);
    if (layer.y_column && !layerColumns.has(layer.y_column)) invalid.push(`missing Y column ${layer.y_column}`);
    if (layer.group_column && !layerColumns.has(layer.group_column)) invalid.push(`missing split column ${layer.group_column}`);
    for (const filter of filterConditions([...(layer.required_filters ?? []), ...(layer.filters ?? [])])) {
      if (filter?.column && !layerColumns.has(filter.column)) invalid.push(`missing filter column ${filter.column}`);
    }
    if (invalid.length) {
      layer.enabled = false;
      issues.push(`layer ${layer.label ?? layer.id ?? index + 1}: ${invalid.join(', ')}; layer disabled`);
    }
    return true;
  });
  workspace.config = loaded; workspace.queryDirty = true;
  workspace.message = {text: issues.length ? `Configuration loaded with issues: ${issues.join(' · ')}` : 'Configuration loaded.', error: Boolean(issues.length)};
  if (workspaceId === activeWorkspaceId) {
    bootstrap = targetBootstrap; renderExpressionSymbols(); renderSources(); apply(loaded);
    message(workspace.message.text, workspace.message.error); renderWorkspaceTabs();
  }
};

bindPresentationControls();
setupPanelResizer();
(async () => {
  await addWorkspace();
  message('Ready. Query changes render on demand; style changes update live from cache.');
})().catch(error => message(error.message, true));
