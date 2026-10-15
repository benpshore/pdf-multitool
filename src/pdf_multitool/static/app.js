/* Local PDF Workbench. No libraries, external requests, or HTML from job data. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = { files: [], selected: new Set(), engines: {}, ready: false, uploading: false, busy: false, deleting: false, job: null, jobOperation: null, textMode: 'clipboard', previews: new Map(), maxFiles: 200, maxBytes: 50 * 1024 * 1024, pollTimer: null };
  const operations = {
    merge: { label: 'Merge PDFs & images', description: 'Combine selected PDFs and supported images in the order shown. Images become pages without OCR.', engine: 'pymupdf', pages: false },
    imagepdf: { label: 'Images → PDF', description: 'Make one PDF from selected JPEG, PNG or JPEG2000 images. EXIF orientation is applied; no OCR is performed. Animated images are rejected.', engine: 'pillow', pages: false },
    split: { label: 'Split PDFs', description: 'Split every page into a PDF, or extract the page groups you choose.', engine: 'pymupdf', pages: true },
    rotate: { label: 'Rotate pages', description: 'Turn the chosen pages clockwise. A new PDF is made for each file.', engine: 'pymupdf', pages: true },
    render: { label: 'Render images', description: 'Turn PDF pages into crisp images with the local Poppler engine.', engine: 'poppler', pages: true },
    text: { label: 'Extract & copy text', description: 'Pull selectable text out of each PDF, ready for your clipboard.', engine: 'poppler', pages: true },
    html: { label: 'Convert HTML to PDF', description: 'Turn inline HTML and CSS into PDFs with local WeasyPrint. All resource URLs are blocked.', engine: 'weasyprint', pages: false },
    compress: { label: 'Compress PDFs', description: 'Make smaller copies with local compression. Originals stay untouched.', engine: 'ghostscript', pages: false },
    docling: { label: 'Convert with Docling', description: 'Convert document structure with locally installed Docling models.', engine: 'docling', pages: false },
    identify: { label: 'Analyze documents', description: 'Identify text layers, scan candidates, headings, fonts, forms and signature fields. Counts describe observed content, not completeness.', engine: 'pymupdf', pages: false },
    pdfhtml: { label: 'Build HTML reader', description: 'Extract text and raster images into a self-contained HTML reader with linked heading navigation and page boundaries.', engine: 'pymupdf', pages: false },
    rename: { label: 'Name document copies', description: 'Use a strongly supported document title as the filename. Keep the original filename when the evidence is uncertain.', engine: 'pymupdf', pages: false },
    clean: { label: 'Clean PDF structure', description: 'Rebuild content streams, remove unreachable objects and compress streams. Deskew and dewarping are not available yet.', engine: 'pymupdf', pages: false },
    repaginate: { label: 'Reflow & repaginate', description: 'Reconstruct flowing content through HTML and WeasyPrint. Complex scientific layout may differ.', engine: 'weasyprint', pages: false },
    forms: { label: 'Inspect / fill / create forms', description: 'Inspect fields, fill explicit values (radio buttons use export values), or create bounded fields from local JSON. Nothing is inferred or signed.', engine: 'pymupdf', pages: false },
    signatures: { label: 'Inspect signatures', description: 'Report signature fields and whether values are present. Cryptographic validation and digital signing are not implemented.', engine: 'pymupdf', pages: false },
    redact: { label: 'Remove selected text', description: 'Permanently remove matched content from a new PDF copy. The original stays available.', engine: 'pymupdf', pages: true },
    unlock: { label: 'Create unlocked copy', description: 'Create an unencrypted copy using the document password supplied below.', engine: 'pymupdf', pages: false },
    protect: { label: 'Protect PDF', description: 'Create an AES-256 password-protected copy.', engine: 'pymupdf', pages: false },
    markdown: { label: 'Export Markdown', description: 'Create structured native-text Markdown with headings, source page labels and companion images. Use a ZIP bundle to keep image links intact.', engine: 'pymupdf', pages: false },
    textlayout: { label: 'Export structured text', description: 'Extract text in conservative geometric reading order, with source page boundaries and paragraph joins. No missing symbols are invented.', engine: 'pymupdf', pages: false },
    bundle: { label: 'Package results', description: 'Package completed result files, preserving companion-image paths.', engine: 'pymupdf', pages: false }
  };
  const additional = {};
  const advancedControls = new Map();

  function installCapabilities(catalog) {
    for (const [name, spec] of Object.entries(catalog?.operations || {})) {
      additional[name] = spec;
      operations[name] = spec;
      const option = node('option', '', spec.label);
      option.value = name;
      $('operation').append(option);
    }
    $('capability-gaps').replaceChildren();
    for (const gap of catalog?.gaps || []) {
      $('capability-gaps').append(node('li', '', `${gap.feature}: ${gap.status}. ${gap.reason}`));
    }
  }

  function renderAdvancedOptions(op) {
    const container = $('advanced-options');
    container.replaceChildren();
    advancedControls.clear();
    for (const [name, spec] of Object.entries(additional[op]?.options || {})) {
      const field = node('div', 'field');
      const label = node('label', spec.type === 'boolean' ? 'check-label' : 'field-label', spec.label);
      const input = node(spec.enum ? 'select' : spec.multiline ? 'textarea' : 'input');
      input.id = `option-${name}`;
      label.setAttribute('for', input.id);
      if (spec.enum) {
        for (const value of spec.enum) {
          const option = node('option', '', value);
          option.value = value;
          input.append(option);
        }
      } else if (spec.type === 'boolean') {
        input.type = 'checkbox';
        input.checked = spec.default;
      } else if (spec.type === 'number' || spec.type === 'integer') {
        input.type = 'number';
        input.min = spec.minimum ?? '';
        input.max = spec.maximum ?? '';
        input.step = spec.type === 'integer' ? '1' : 'any';
      } else {
        if (!spec.multiline) input.type = 'text';
        if (spec.maxLength) input.maxLength = spec.maxLength;
        if (spec.multiline) input.rows = 8;
      }
      input.value = String(spec.default);
      input.addEventListener('change', refreshControls);
      if (spec.type === 'boolean') {
        label.textContent = '';
        label.append(input, node('span', '', spec.label));
        field.append(label);
      } else field.append(label, input);
      container.append(field);
      advancedControls.set(name, { input, spec });
    }
  }
  let token = '';
  try {
    const fragmentToken = new URLSearchParams(window.location.hash.slice(1)).get('token');
    token = fragmentToken || sessionStorage.getItem('pdf-workbench-token') || '';
    if (fragmentToken) {
      if (sessionStorage.getItem('pdf-workbench-token') !== fragmentToken) sessionStorage.removeItem('pdf-workbench-job');
      sessionStorage.setItem('pdf-workbench-token', fragmentToken);
      history.replaceState(null, '', window.location.pathname + window.location.search);
    }
  } catch (_) {
    token = new URLSearchParams(window.location.hash.slice(1)).get('token') || token;
    if (window.location.hash) history.replaceState(null, '', window.location.pathname + window.location.search);
  }

  function notice(message, type = '') {
    $('notice').textContent = message;
    $('notice').className = `notice ${type}`.trim();
    $('notice').hidden = !message;
  }
  function size(bytes) {
    if (!Number.isFinite(Number(bytes))) return '';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = String(text);
    return el;
  }
  function button(text, className, label, action) {
    const el = node('button', className, text);
    el.type = 'button';
    if (label) { el.setAttribute('aria-label', label); el.title = label; }
    el.addEventListener('click', action);
    return el;
  }
  function apiURL(path) {
    const url = new URL(path, window.location.href);
    if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/')) throw new Error('The server returned an unsafe download address.');
    return url;
  }
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set('X-PDF-Token', token);
    const response = await fetch(apiURL(path), { ...options, headers, credentials: 'same-origin', cache: 'no-store', redirect: 'error' });
    if (!response.ok) {
      let message = `Request failed (${response.status}).`;
      try {
        const error = await response.json();
        message = typeof error.error === 'string' ? error.error : typeof error.message === 'string' ? error.message : message;
      } catch (_) { /* HTTP status remains useful when no JSON error exists. */ }
      if (response.status === 401 || response.status === 403) message = 'This workspace needs its launch token. Reopen the full launch URL from the terminal.';
      const error = new Error(message); error.status = response.status; throw error;
    }
    return response;
  }
  async function json(path, options) { const response = await api(path, options); return response.status === 204 ? {} : response.json(); }
  function rememberJob() {
    try {
      // Keep completed results recoverable after Back or reload; store only job metadata.
      if (state.job?.id) sessionStorage.setItem('pdf-workbench-job', JSON.stringify({ id: state.job.id, operation: state.jobOperation, textMode: state.textMode, bundleSourceId: state.bundleSource?.id || null }));
      else sessionStorage.removeItem('pdf-workbench-job');
    } catch (_) { /* Reload recovery is optional when browser storage is blocked. */ }
  }
  function filePath(file, suffix) { return `/api/files/${encodeURIComponent(file.id)}/${suffix}`; }
  function selectedFiles() { return state.files.filter(file => state.selected.has(file.id)); }
  function blocked() { return state.uploading || state.busy || state.deleting; }
  function engineAvailable(name) {
    const value = state.engines[name];
    if (typeof value === 'boolean') return value;
    return Boolean(value && (value.available === true || value.enabled === true));
  }
  function operationAvailable(op) {
    if (op === 'render_native' && advancedControls.has('renderer')) return engineAvailable(advancedControls.get('renderer').input.value);
    if (op === 'imagepdf') return engineAvailable('pymupdf') && engineAvailable('pillow');
    if (Object.prototype.hasOwnProperty.call(state.engines, op)) return engineAvailable(op);
    return engineAvailable(operations[op].engine);
  }
  function effectiveOperation() {
    const op = $('operation').value;
    return op === 'text' && ['markdown', 'textlayout', 'pdfhtml'].includes($('text-export').value) ? $('text-export').value : op;
  }
  function refreshControls() {
    const count = state.selected.size;
    const op = $('operation').value;
    const unavailable = !operationAvailable(effectiveOperation());
    $('file-count').textContent = state.files.length;
    $('total-size').textContent = state.files.length ? `${size(state.files.reduce((sum, file) => sum + Number(file.size || 0), 0))} total` : 'Nothing added yet';
    $('select-all').disabled = blocked() || !state.files.length;
    $('select-all').checked = Boolean(state.files.length && count === state.files.length);
    $('select-all').indeterminate = count > 0 && count < state.files.length;
    $('selection-label').textContent = count ? `${count} selected` : 'Select all';
    $('remove-selected').disabled = blocked() || !count;
    $('choose-files').disabled = blocked() || !state.ready || state.files.length >= state.maxFiles;
    $('choose-folder').disabled = $('choose-files').disabled;
    $('drop-zone').setAttribute('aria-disabled', String($('choose-files').disabled));
    $('operation').disabled = blocked();
    for (const input of $('operation-options').querySelectorAll('input,select,textarea')) input.disabled = blocked();
    const requiresFiles = additional[op]?.inputs !== 0;
    const rasterAccepted = !advancedControls.has('allow_rasterize') || advancedControls.get('allow_rasterize').input.checked;
    $('run-button').disabled = !state.ready || blocked() || (requiresFiles && !count) || unavailable || !rasterAccepted || (op === 'merge' && count < 2) || (op === 'compress' && !$('allow-lossy').checked);
    let hint = `${count} selected file${count === 1 ? '' : 's'} · New files only`;
    if (!state.ready) hint = 'Connecting to the local workspace…';
    else if (state.uploading) hint = 'Adding files to the workspace…';
    else if (state.busy) hint = 'Working locally. You can cancel below.';
    else if (unavailable) hint = 'This engine is unavailable. See About & engines below.';
    else if (!rasterAccepted) hint = 'Confirm rasterization and possible content loss to continue.';
    else if (!requiresFiles) hint = 'Create a new document from the text above.';
    else if (!count) hint = 'Add and select files to begin.';
    else if (op === 'merge' && count < 2) hint = 'Select at least two PDFs or images to merge.';
    else if (op === 'compress' && !$('allow-lossy').checked) hint = 'Confirm PDF rewriting and possible quality loss to continue.';
    $('run-hint').textContent = hint;
  }
  function refreshOperation() {
    const op = $('operation').value;
    const effective = effectiveOperation();
    renderAdvancedOptions(op);
    $('operation-description').textContent = operations[effective].description + (effective !== op ? ' Exports the whole document.' : '');
    $('run-label').textContent = effective === 'text' && $('text-export').value !== 'clipboard' ? 'Extract text' : operations[effective].label;
    $('pages-field').hidden = !operations[effective].pages;
    $('rotation-field').hidden = op !== 'rotate';
    $('dpi-field').hidden = !['render', 'imagepdf', 'merge'].includes(op);
    $('compression-field').hidden = op !== 'compress';
    $('ocr-field').hidden = op !== 'docling';
    $('text-export-field').hidden = op !== 'text';
    $('password-field').hidden = ['html', 'imagepdf', 'create'].includes(op);
    $('new-password-field').hidden = op !== 'protect';
    $('forms-field').hidden = op !== 'forms';
    $('redaction-field').hidden = op !== 'redact';
    $('structured-field').hidden = !['markdown', 'textlayout'].includes(op) && !(op === 'text' && ['markdown', 'textlayout'].includes($('text-export').value));
    $('pages').placeholder = op === 'split' ? 'Every page, or 1-3; 4; 5-7' : 'All pages, or 1-3, 5, 8-10';
    $('pages-help').textContent = op === 'split' ? 'Leave blank for one PDF per page. Separate output groups with semicolons.' : 'Page numbers start at 1. Applied to each selected PDF.';
    refreshControls();
  }
  function refreshFormMode() {
    const mode = $('form-mode').value;
    $('form-values-field').hidden = mode === 'inspect';
    $('flatten-forms-label').hidden = mode === 'inspect';
    $('form-values-label').textContent = mode === 'create' ? 'Field definitions · JSON array' : 'Field values · JSON object';
    $('form-values').placeholder = mode === 'create' ? '[{"page":1,"name":"Name","type":"text","rect":[40,60,260,90]}]' : '{"Name":"Example","Agreed":true,"Delivery":"Email"}';
    $('forms-help').textContent = mode === 'create' ? 'Types: text, checkbox, combobox, listbox. Use one-based pages and unrotated PDF-point rectangles. Choice fields need choices. Values are optional; signatures and inferred answers are excluded.' : mode === 'fill' ? 'Map existing field names to explicit values. Checkboxes use true/false; radios use a listed export value. Unsupported group/choice layouts fail clearly.' : 'Inspect field names, types, choices and radio export values. This does not change the PDF.';
  }
  function renderEngines() {
    const list = $('engine-list');
    list.replaceChildren();
    let available = 0;
    const entries = Object.entries(state.engines);
    for (const [name, engine] of entries) {
      const ok = engineAvailable(name);
      if (ok) available += 1;
      const row = node('li');
      const details = node('div');
      details.append(node('span', '', name));
      if (engine && typeof engine === 'object' && engine.reason) details.append(node('span', 'engine-reason', engine.reason));
      if (engine && typeof engine === 'object' && engine.provenance) details.append(node('span', 'engine-reason', typeof engine.provenance === 'string' ? engine.provenance : JSON.stringify(engine.provenance)));
      const version = engine && typeof engine === 'object' && engine.version ? ` · ${engine.version}` : '';
      row.append(details, node('span', ok ? 'available' : 'unavailable', ok ? `Ready${version}` : 'Unavailable'));
      list.append(row);
    }
    $('engine-summary').textContent = entries.length ? `${available} / ${entries.length} ready` : 'No engines reported';
    for (const option of $('operation').options) {
      const ok = operationAvailable(option.value);
      option.disabled = !ok;
      const names = { merge: 'Merge PDFs & images', split: 'Split / extract pages', rotate: 'Rotate pages', render: 'Render to images', text: 'Extract text', html: 'HTML → PDF', compress: 'Compress PDF', docling: 'Convert with Docling' };
      option.textContent = (names[option.value] || operations[option.value].label) + (ok ? '' : ' · unavailable');
    }
    if ($('operation').selectedOptions[0]?.disabled) {
      const availableOption = [...$('operation').options].find(option => !option.disabled);
      if (availableOption) $('operation').value = availableOption.value;
    }
    refreshOperation();
  }
  function clearPreview(id) {
    const preview = state.previews.get(id);
    if (preview?.url) URL.revokeObjectURL(preview.url);
    state.previews.delete(id);
  }
  function renderFiles() {
    const list = $('file-list');
    list.replaceChildren();
    if (!state.files.length) {
      const empty = node('div', 'empty-files');
      const glyph = node('span', 'empty-glyph', '▤'); glyph.setAttribute('aria-hidden', 'true');
      empty.append(glyph, node('p', '', 'A little less paperwork.'), node('span', '', 'Add a document or a whole stack to get started.'));
      list.append(empty);
    }
    let order = 0;
    state.files.forEach((file, index) => {
      const row = node('div', 'file-row');
      const main = node('div', 'file-main');
      const check = node('input'); check.type = 'checkbox'; check.checked = state.selected.has(file.id); check.disabled = blocked(); check.setAttribute('aria-label', `Select ${file.name}`);
      check.addEventListener('change', () => { if (check.checked) state.selected.add(file.id); else state.selected.delete(file.id); renderFiles(); });
      const info = node('div', 'file-info');
      const name = node('span', 'file-name', file.name); name.title = file.name;
      const meta = node('div', 'file-meta');
      if (check.checked) meta.append(node('span', '', `#${++order}`));
      meta.append(node('span', '', size(file.size)));
      const previewButton = button(state.previews.has(file.id) ? 'Hide preview' : 'Preview', 'text-button', `Toggle preview of ${file.name}`, () => togglePreview(file.id));
      previewButton.setAttribute('aria-expanded', String(state.previews.has(file.id)));
      meta.append(previewButton, button('Original ↗', 'text-button', `Open original ${file.name} in a new tab`, () => openOriginal(file)));
      if (file.member_path) meta.append(node('span', '', file.member_path));
      if (file.source_archive?.url) meta.append(button('Source archive ↓', 'text-button', 'Download the original source archive', () => downloadResult({url: file.source_archive.url, name: file.source_archive.name})));
      info.append(name, meta);
      const move = node('div', 'file-order');
      const up = button('↑', 'order-button', `Move ${file.name} up`, () => moveFile(file.id, -1)); up.disabled = blocked() || index === 0;
      const down = button('↓', 'order-button', `Move ${file.name} down`, () => moveFile(file.id, 1)); down.disabled = blocked() || index === state.files.length - 1;
      move.append(up, down);
      const remove = button('×', 'file-remove', `Remove ${file.name} from workspace`, () => removeFiles([file.id])); remove.disabled = blocked();
      const selectArea = node('label', 'file-select'); selectArea.append(check);
      const actions = node('div', 'file-actions'); actions.append(move, remove);
      main.append(selectArea, node('span', 'file-badge', /\.html?$/i.test(file.name) ? 'HTML' : /\.(jpe?g|png|jp2|jpx|j2k)$/i.test(file.name) ? 'IMG' : 'PDF'), info, actions);
      row.append(main);
      const preview = state.previews.get(file.id);
      if (preview) {
        const container = node('div', 'preview');
        if (preview.url) { const img = node('img'); img.src = preview.url; img.alt = `First page of ${file.name}`; container.append(img); }
        container.append(node('span', '', preview.message || 'First page · Preview only'));
        row.append(container);
      }
      list.append(row);
    });
    refreshControls();
  }
  function moveFile(id, direction) {
    if (blocked()) return;
    const index = state.files.findIndex(file => file.id === id);
    const next = index + direction;
    if (index < 0 || next < 0 || next >= state.files.length) return;
    [state.files[index], state.files[next]] = [state.files[next], state.files[index]];
    renderFiles();
    const buttons = $('file-list').children[next]?.querySelectorAll('.order-button');
    buttons?.[direction < 0 ? 0 : 1]?.focus();
  }
  async function togglePreview(id) {
    if (state.previews.has(id)) { clearPreview(id); renderFiles(); return; }
    const file = state.files.find(item => item.id === id);
    if (!file) return;
    const item = { message: 'Loading preview…' };
    state.previews.set(id, item); renderFiles();
    try {
      const response = await api(filePath(file, 'preview'));
      const blob = await response.blob();
      if (!blob.type.startsWith('image/')) throw new Error('Preview is unavailable for this file.');
      if (state.previews.get(id) !== item) return;
      item.url = URL.createObjectURL(blob); item.message = 'First page · Preview only';
    } catch (error) { if (state.previews.get(id) === item) item.message = error.message; }
    renderFiles();
  }
  async function openOriginal(file) {
    // Create the tab during the tap so Safari does not block an asynchronous popup.
    const tab = window.open('about:blank', '_blank');
    if (!tab) { notice('Your browser blocked the new tab. Allow pop-ups for this workspace to view originals.', 'error'); return; }
    tab.opener = null;
    tab.document.title = 'Opening original…';
    tab.document.body.textContent = 'Loading the original from this workspace…';
    try {
      const source = await (await api(filePath(file, 'original'))).blob();
      // Never run uploaded HTML in this origin; show its original source as plain text.
      const suffix = file.name.toLowerCase().split('.').pop();
      const type = ({pdf:'application/pdf', jpg:'image/jpeg', jpeg:'image/jpeg', png:'image/png', jp2:'image/jp2', jpx:'image/jpx', j2k:'application/octet-stream'})[suffix] || 'text/plain;charset=utf-8';
      const blob = new Blob([source], { type });
      const url = URL.createObjectURL(blob);
      if (!tab.closed) tab.location.replace(url);
      setTimeout(() => URL.revokeObjectURL(url), 5 * 60 * 1000);
    } catch (error) { tab.close(); notice(error.message, 'error'); }
  }
  async function removeFiles(ids) {
    if (blocked() || !ids.length) return;
    state.deleting = true; renderFiles();
    const errors = [];
    for (const id of ids) {
      try {
        await api(`/api/files/${encodeURIComponent(id)}`, { method: 'DELETE' });
        state.files = state.files.filter(file => file.id !== id); state.selected.delete(id); clearPreview(id);
      } catch (error) { errors.push(error.message); }
    }
    state.deleting = false; renderFiles();
    if (errors.length) notice(`${errors.length} file${errors.length === 1 ? '' : 's'} could not be removed. ${errors[0]}`, 'error');
  }
  async function addFiles(files) {
    if (blocked() || !state.ready) return;
    const source = Array.from(files);
    const pdfs = source.filter(file => /\.(pdf|html?|zip|tar|gz|tgz|jpe?g|png|jp2|jpx|j2k)$/i.test(file.name));
    const ignored = source.length - pdfs.length;
    if (!pdfs.length) { notice('Choose PDF, HTML, JPEG, PNG, JPEG2000 or supported archives.', 'error'); return; }
    const remaining = Math.max(0, state.maxFiles - state.files.length);
    const oversize = pdfs.filter(file => file.size > state.maxBytes);
    const eligible = pdfs.filter(file => file.size <= state.maxBytes && file.size > 0);
    const empty = pdfs.filter(file => file.size === 0).length;
    const queue = eligible.slice(0, remaining);
    const skipped = [];
    if (ignored) skipped.push(`${ignored} unsupported file${ignored === 1 ? '' : 's'}`);
    if (oversize.length) skipped.push(`${oversize.length} over ${size(state.maxBytes)}`);
    if (empty) skipped.push(`${empty} empty file${empty === 1 ? '' : 's'}`);
    if (eligible.length > remaining) skipped.push(`${eligible.length - remaining} over the ${state.maxFiles}-file limit`);
    if (!queue.length) { notice(`No files added. Skipped ${skipped.join(', ')}.`, 'error'); return; }
    state.uploading = true; renderFiles();
    const errors = [], archiveWarnings = [];
    let added = 0;
    for (let i = 0; i < queue.length; i++) {
      const file = queue[i];
      notice(`Adding ${i + 1} of ${queue.length}: ${file.name}`);
      try {
        const uploaded = await json('/api/files', { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name) }, body: file });
        const imported = Array.isArray(uploaded.files) ? uploaded.files : [uploaded];
        if (!imported.length || imported.some(item => !item.id)) throw new Error('The server did not return supported documents.');
        for (const item of imported) { state.files.push(item); state.selected.add(item.id); added += 1; }
        archiveWarnings.push(...(uploaded.warnings || [])); renderFiles();
      } catch (error) { errors.push(`${file.name}: ${error.message}`); }
    }
    state.uploading = false; renderFiles();
    let message = `${added} file${added === 1 ? '' : 's'} added.`;
    if (skipped.length) message += ` Skipped ${skipped.join(', ')}.`;
    if (errors.length) message += ` ${errors.length} failed. ${errors[0]}`;
    if (archiveWarnings.length) message += ` ${archiveWarnings.join(' ')}`;
    notice(message, errors.length || skipped.length ? '' : 'success');
    $('file-input').value = ''; $('folder-input').value = '';
  }
  async function droppedFiles(dataTransfer) {
    const items = Array.from(dataTransfer.items || []);
    if (!items.some(item => item.webkitGetAsEntry?.()?.isDirectory)) return Array.from(dataTransfer.files);
    const result = [];
    const entries = items.map(item => item.webkitGetAsEntry?.()).filter(Boolean);
    async function visit(entry) {
      if (result.length >= state.maxFiles + 1) return;
      if (entry.isFile) {
        const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
        if (/\.(pdf|html?|zip|tar|gz|tgz|jpe?g|png|jp2|jpx|j2k)$/i.test(file.name)) result.push(file);
      } else if (entry.isDirectory) {
        const reader = entry.createReader();
        let batch;
        do {
          batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
          for (const child of batch) await visit(child);
        } while (batch.length && result.length < state.maxFiles + 1);
      }
    }
    for (const entry of entries) await visit(entry);
    return result;
  }
  function validatePages(value, op) {
    if (!value) return true;
    const part = '\\d+(?:\\s*-\\s*\\d+)?';
    const group = `${part}(?:\\s*,\\s*${part})*`;
    const pattern = op === 'split' ? `^\\s*${group}(?:\\s*;\\s*${group})*\\s*$` : `^\\s*${group}\\s*$`;
    if (!new RegExp(pattern).test(value) || value.match(/\d+/g).some(number => Number(number) < 1)) return false;
    return value.split(/[;,]/).every(range => { const ends = range.split('-').map(Number); return ends.length < 2 || ends[0] <= ends[1]; });
  }
  async function startJob() {
    if ($('run-button').disabled || blocked()) return;
    const operation = effectiveOperation();
    const pages = operations[operation].pages ? $('pages').value.trim() : '';
    if (!validatePages(pages, operation)) { notice(operation === 'split' ? 'Use page groups such as 1-3; 4; 5-7. Page numbers must start at 1.' : 'Use page numbers such as 1-3, 5, 8-10. Page numbers must start at 1.', 'error'); $('pages').focus(); return; }
    state.busy = true; state.job = null; rememberJob(); state.jobOperation = operation; state.textMode = $('text-export').value;
    state.bundleSource = null;
    $('bundle-controls').hidden = true;
    notice(''); renderFiles();
    $('output-panel').hidden = false; $('results').replaceChildren(); $('job-warnings').replaceChildren(); $('job-errors').replaceChildren(); $('job-warnings').hidden = true; $('job-errors').hidden = true; $('text-result').hidden = true; $('extracted-text').value = '';
    $('job-status').classList.remove('is-error'); $('job-message').textContent = 'Starting…'; $('job-percent').textContent = ''; $('job-progress').value = 0; $('cancel-job').hidden = true;
    const options = additional[operation] ? (operations[operation].pages ? { pages } : {}) : { pages, rotation: Number($('rotation').value), dpi: Number($('dpi').value), compress_profile: $('compress-profile').value, allow_lossy: $('allow-lossy').checked, ocr: $('ocr').checked };
    for (const [name, { input, spec }] of advancedControls) {
      options[name] = spec.type === 'boolean' ? input.checked : ['integer', 'number'].includes(spec.type) ? Number(input.value) : input.value;
    }
    if (['markdown', 'textlayout'].includes(operation)) options.skip_repeated_margins = $('skip-margins').checked;
    let rawForm = null;
    try {
      if (operation !== 'create' && $('document-password').value) options.password = $('document-password').value;
      if (operation === 'protect') {
        options.new_password = $('new-password').value;
        if (new TextEncoder().encode(options.new_password).length < 8) throw new Error('Use a new password of at least 8 UTF-8 bytes.');
      }
      if (operation === 'forms' && $('form-mode').value !== 'inspect') {
        const mode = $('form-mode').value;
        if (!$('form-values').value.trim()) throw new Error(mode === 'create' ? 'Enter field definitions as a JSON array.' : 'Enter field values as a JSON object.');
        const values = JSON.parse($('form-values').value);
        if (mode === 'create' ? !Array.isArray(values) : !values || Array.isArray(values) || typeof values !== 'object') throw new Error(mode === 'create' ? 'Creation needs a JSON array.' : 'Filling needs a JSON object.');
        rawForm = { key: mode === 'create' ? 'create_fields' : 'fields', source: $('form-values').value };
        options.flatten = $('flatten-forms').checked;
      }
      if (operation === 'redact') { options.redact_text = $('redact-text').value; if (!options.redact_text) throw new Error('Enter the exact text to remove.'); }
      // The raw form value has passed JSON.parse and a root-type check above.
      // Retain its exact nested JSON so the server can reject duplicate keys,
      // rather than silently collapsing them in the browser first.
      const encodedOptions = JSON.stringify(options);
      const optionsBody = rawForm ? encodedOptions.slice(0, -1) + ',' + JSON.stringify(rawForm.key) + ':' + rawForm.source + '}' : encodedOptions;
      const body = '{"operation":' + JSON.stringify(operation) + ',"files":' + JSON.stringify(additional[operation]?.inputs === 0 ? [] : selectedFiles().map(file => file.id)) + ',"options":' + optionsBody + '}';
      state.job = await json('/api/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
      $('document-password').value = ''; $('new-password').value = '';
      if (operation === 'forms') $('form-values').value = '';
      if (!state.job.id) throw new Error('The server did not return a job ID.');
      rememberJob();
      $('cancel-job').hidden = false; $('cancel-job').disabled = false;
      $('output-panel').scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'nearest' });
      await pollJob();
    } catch (error) {
      state.busy = false; rememberJob(); $('cancel-job').hidden = true; $('job-status').classList.add('is-error'); $('job-message').textContent = error.message; renderFiles();
    }
  }
  function listMessages(id, messages, format) {
    const list = $(id); list.replaceChildren();
    for (const message of messages || []) list.append(node('li', '', format ? format(message) : typeof message === 'string' ? message : JSON.stringify(message)));
    list.hidden = !list.children.length;
  }
  async function pollJob() {
    if (!state.job?.id || !state.busy) return;
    try {
      const job = await json(`/api/jobs/${encodeURIComponent(state.job.id)}`);
      state.job = job;
      let progress = Number(job.progress) * 100;
      if (!Number.isFinite(progress)) progress = 0;
      progress = Math.max(0, Math.min(100, progress));
      $('job-message').textContent = job.message || ({ queued: 'Waiting to start…', running: 'Working…', completed: 'Done. Your files are ready.', partial: 'Finished with some errors.', failed: 'The job could not be completed.', cancelled: 'Job cancelled.' }[job.state] || 'Working…');
      $('job-percent').textContent = `${Math.round(progress)}%`; $('job-progress').value = progress;
      listMessages('job-warnings', job.warnings);
      listMessages('job-errors', job.errors, error => typeof error === 'string' ? error : `${error.name ? `${error.name}: ` : ''}${error.error || error.message || 'Could not process this file.'}`);
      if (['completed', 'partial', 'failed', 'cancelled'].includes(job.state)) {
        $('cancel-job').hidden = true;
        $('job-status').classList.toggle('is-error', job.state === 'failed');
        if (job.state === 'completed') { $('job-progress').value = 100; $('job-percent').textContent = '100%'; }
        await finishJob(job);
        state.busy = false; rememberJob(); renderFiles();
        return;
      }
    } catch (error) {
      if ([401, 403, 404].includes(error.status)) {
        state.job = null; state.busy = false; rememberJob(); $('cancel-job').hidden = true; $('job-status').classList.add('is-error');
        $('job-message').textContent = error.status === 404 ? 'This job is no longer available. Select your files and run the tool again.' : error.message;
        renderFiles(); return;
      }
      $('job-message').textContent = `Connection interrupted. Retrying… ${error.message}`;
    }
    state.pollTimer = window.setTimeout(pollJob, 900);
  }
  async function cancelJob() {
    if (!state.job?.id || !state.busy || $('cancel-job').disabled) return;
    $('cancel-job').disabled = true;
    try { await api(`/api/jobs/${encodeURIComponent(state.job.id)}/cancel`, { method: 'POST' }); $('job-message').textContent = 'Cancellation requested. Waiting for the current step to stop…'; }
    catch (error) { notice(error.message, 'error'); $('cancel-job').disabled = false; }
  }
  function saveBlob(blob, name) {
    const download = new Blob([blob], { type: 'application/octet-stream' });
    const url = URL.createObjectURL(download);
    const link = node('a'); link.href = url; link.download = name || 'download'; link.target = '_blank'; link.rel = 'noopener noreferrer';
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5 * 60 * 1000);
  }
  async function downloadResult(result, trigger) {
    if (trigger) trigger.disabled = true;
    try { const blob = await (await api(result.url)).blob(); saveBlob(blob, result.name); }
    catch (error) { notice(error.message, 'error'); }
    finally { if (trigger) trigger.disabled = false; }
  }
  async function finishJob(job) {
    const ownResults = Array.isArray(job.results) ? job.results : [];
    const results = state.jobOperation === 'bundle' && state.bundleSource ? [...state.bundleSource.results, ...ownResults] : ownResults;
    $('results').replaceChildren();
    for (const result of results) {
      const card = node('div', 'result-card');
      const info = node('div', 'result-info');
      const name = node('span', 'result-name', result.name || 'Result'); name.title = result.name || 'Result';
      info.append(name, node('span', 'result-size', size(result.size)));
      const download = button('Download ↓', 'button small', `Download ${result.name || 'result'}`, () => downloadResult(result, download));
      card.append(info, download); $('results').append(card);
    }
    $('bundle-controls').hidden = !results.length || (state.jobOperation === 'bundle' && !state.bundleSource);
    if (!['text', 'textlayout', 'markdown'].includes(state.jobOperation) || !results.length) return;
    try {
      const pieces = [];
      const textResults = results.filter(item => /\.(txt|md)$/i.test(item.name));
      for (const result of textResults) {
        const text = typeof result.text === 'string' ? result.text : await (await api(result.url)).text();
        pieces.push(textResults.length > 1 ? `=== ${result.name || 'Document'} ===\n\n${text}` : text);
      }
      $('extracted-text').value = pieces.join('\n\n'); $('text-result').hidden = false;
      if (!pieces.join('').trim()) { notice('No selectable text was found. Scanned PDFs may need the Docling OCR tool.'); return; }
      if (state.textMode === 'clipboard') await copyText(false);
      else if (state.textMode === 'view') $('text-result').open = true;
      else if (state.textMode === 'download') {
        // One combined file avoids Safari's restriction on multiple automatic downloads.
        saveBlob(new Blob([$('extracted-text').value], { type: 'text/plain;charset=utf-8' }), results.length === 1 ? results[0].name : 'extracted-text.txt');
      }
    } catch (error) { notice(`Files are ready, but the text view could not load. ${error.message}`, 'error'); }
  }
  async function startBundle(format) {
    if (blocked()) return;
    const source = state.bundleSource || state.job;
    if (!source?.results?.length) return;
    state.bundleSource = source;
    state.busy = true; state.jobOperation = 'bundle'; renderFiles();
    $('bundle-controls').hidden = true; $('job-message').textContent = 'Packaging completed results…';
    try {
      state.job = await json('/api/bundles', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({job:source.id, format})});
      rememberJob(); $('cancel-job').hidden = false; $('cancel-job').disabled = false;
      await pollJob();
    } catch (error) { state.job = source; state.busy = false; $('bundle-controls').hidden = false; notice(error.message, 'error'); rememberJob(); renderFiles(); }
  }
  async function copyText(userGesture = true) {
    const textarea = $('extracted-text');
    if (!textarea.value) return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(textarea.value);
      notice('Text copied to your clipboard.', 'success');
      $('copy-text').textContent = 'Copied ✓'; setTimeout(() => { $('copy-text').textContent = 'Copy text'; }, 1800);
    } catch (_) {
      if (userGesture) {
        textarea.focus(); textarea.select(); textarea.setSelectionRange(0, textarea.value.length);
        try { if (document.execCommand('copy')) { notice('Text copied to your clipboard.', 'success'); return; } } catch (_) { /* The selectable text is the final fallback. */ }
      }
      notice(userGesture ? 'Select the text below, then use Copy from your browser or keyboard.' : 'Your text is ready. Tap Copy text below to copy it, or select it manually.');
      $('text-result').hidden = false;
      $('text-result').open = true;
    }
  }
  function setupTheme() {
    let theme;
    try { theme = localStorage.getItem('pdf-workbench-theme'); } catch (_) { /* Storage is optional. */ }
    if (!['light', 'dark'].includes(theme)) theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    document.documentElement.dataset.theme = theme;
    $('theme-toggle').setAttribute('aria-label', `Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`);
    $('theme-toggle').addEventListener('click', () => {
      const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      $('theme-toggle').setAttribute('aria-label', `Switch to ${next === 'dark' ? 'light' : 'dark'} theme`);
      try { localStorage.setItem('pdf-workbench-theme', next); } catch (_) { /* Storage is optional. */ }
    });
  }
  function setupEvents() {
    $('choose-files').addEventListener('click', event => { event.stopPropagation(); $('file-input').click(); });
    $('choose-folder').addEventListener('click', event => { event.stopPropagation(); $('folder-input').click(); });
    $('choose-folder').hidden = !('webkitdirectory' in $('folder-input'));
    $('file-input').addEventListener('change', event => addFiles(event.target.files));
    $('folder-input').addEventListener('change', event => addFiles(event.target.files));
    $('drop-zone').addEventListener('click', event => { if (!event.target.closest('button') && !$('choose-files').disabled) $('file-input').click(); });
    $('drop-zone').addEventListener('keydown', event => { if ((event.key === 'Enter' || event.key === ' ') && event.target === $('drop-zone')) { event.preventDefault(); if (!$('choose-files').disabled) $('file-input').click(); } });
    document.addEventListener('dragover', event => { event.preventDefault(); });
    document.addEventListener('drop', event => { event.preventDefault(); });
    $('drop-zone').addEventListener('dragover', event => { event.preventDefault(); if (!blocked()) $('drop-zone').classList.add('dragover'); });
    $('drop-zone').addEventListener('dragleave', event => { if (!$('drop-zone').contains(event.relatedTarget)) $('drop-zone').classList.remove('dragover'); });
    $('drop-zone').addEventListener('drop', async event => { event.preventDefault(); $('drop-zone').classList.remove('dragover'); if (blocked()) return; try { await addFiles(await droppedFiles(event.dataTransfer)); } catch (error) { notice(`Could not read dropped files. ${error.message}`, 'error'); } });
    $('select-all').addEventListener('change', event => { state.selected = new Set(event.target.checked ? state.files.map(file => file.id) : []); renderFiles(); });
    $('remove-selected').addEventListener('click', () => removeFiles([...state.selected]));
    $('operation').addEventListener('change', refreshOperation);
    $('form-mode').addEventListener('change', refreshFormMode);
    refreshFormMode();
    $('text-export').addEventListener('change', refreshOperation);
    $('compress-profile').addEventListener('change', () => { $('allow-lossy').checked = false; refreshControls(); });
    $('allow-lossy').addEventListener('change', refreshControls);
    $('run-button').addEventListener('click', startJob);
    $('cancel-job').addEventListener('click', cancelJob);
    $('bundle-zip').addEventListener('click', () => startBundle('zip'));
    $('bundle-tar').addEventListener('click', () => startBundle('tar.gz'));
    $('copy-text').addEventListener('click', () => copyText(true));
    $('select-text').addEventListener('click', () => { $('extracted-text').focus(); $('extracted-text').select(); $('extracted-text').setSelectionRange(0, $('extracted-text').value.length); });
  }
  async function init() {
    setupTheme(); setupEvents(); refreshOperation();
    if (!token) { notice('Open the full launch URL from the terminal to unlock this workspace. Its private token is kept in this browser tab.', 'error'); $('run-hint').textContent = 'Launch token required.'; $('engine-summary').textContent = 'Not connected'; return; }
    try {
      const [status, files] = await Promise.all([json('/api/status'), json('/api/files')]);
      state.engines = status.engines || {};
      installCapabilities(status.capabilities);
      const limits = status.limits || {};
      const fileLimit = Number(limits.max_files || limits.files || 200);
      state.maxFiles = Number.isFinite(fileLimit) && fileLimit > 0 ? Math.min(200, Math.floor(fileLimit)) : 200;
      const byteLimit = Number(limits.max_file_bytes || limits.max_upload_bytes || 50 * 1024 * 1024);
      state.maxBytes = Number.isFinite(byteLimit) && byteLimit > 0 ? Math.min(50 * 1024 * 1024, byteLimit) : 50 * 1024 * 1024;
      $('limits-label').textContent = `Up to ${state.maxFiles} files · ${size(state.maxBytes)} each`;
      state.files = Array.isArray(files.files) ? files.files : []; state.selected = new Set(state.files.map(file => file.id)); state.ready = true;
      renderEngines(); renderFiles();
      let saved;
      try { saved = JSON.parse(sessionStorage.getItem('pdf-workbench-job') || 'null'); } catch (_) { /* Ignore incomplete local state. */ }
      if (saved && typeof saved.id === 'string' && Object.prototype.hasOwnProperty.call(operations, saved.operation)) {
        // Restoring results must not repeat clipboard writes or automatic downloads.
        state.job = { id: saved.id }; state.jobOperation = saved.operation; state.textMode = 'view'; state.busy = true;
        // Packaging is an output action, not an option in the tool selector.
        if (saved.operation !== 'bundle') $('operation').value = saved.operation;
        if (saved.operation === 'bundle' && typeof saved.bundleSourceId === 'string') {
          try { state.bundleSource = await json(`/api/jobs/${encodeURIComponent(saved.bundleSourceId)}`); }
          catch (_) { notice('The original results could not be restored. The package job is still available.'); }
        }
        refreshOperation(); renderFiles();
        $('output-panel').hidden = false; $('cancel-job').hidden = false; $('cancel-job').disabled = false; $('job-message').textContent = 'Reconnecting to your job…';
        await pollJob();
      }
    } catch (error) { notice(`Could not connect to the workspace. ${error.message}`, 'error'); $('run-hint').textContent = 'Check the server, then reload this page.'; $('engine-summary').textContent = 'Not connected'; }
  }
  init();
})();
