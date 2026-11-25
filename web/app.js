import {
  TABS, TAB_BY_KEY, COLLABORATION_TAB_KEYS, RELATIONSHIP_TYPE_LABELS, PAGE_SIZE,
  buildIndex, prepareRecords, facetUniverse, applyFilters, sortItems, paginate,
  parseHash, buildListHash, buildDetailHash, identifierLinks, entityIdUrl, orcidUrl, openalexUrl, rorUrl,
  personTimeline, institutionCollaborations, childInstitutions, parentInstitutionId, tabKeyForCollaborationId,
  tabKeysForCollaborationIds, formatMoney, metaCountForTab, metaSources, toList, resourceLabel, emptyFilters,
  isHttpUrl, TEXT_COLLATOR, personDisplayName,
} from './data.js';

const PARTICIPANT_DISPLAY_LIMIT = 200;
const RELATED_LIST_DISPLAY_LIMIT = 200;
const SEARCH_DEBOUNCE_MILLISECONDS = 200;

const state = {
  meta: null,
  files: {},
  institutionIndex: new Map(),
  peopleIndex: new Map(),
  collaborationIndex: new Map(),
  listStates: {},
  currentTabKey: null,
  renderedListHash: null,
  filterCache: null,
  sortCache: null,
  detailRenderToken: 0,
};

const elements = {
  tabs: document.getElementById('tabs'),
  filters: document.getElementById('filters'),
  results: document.getElementById('results'),
  drawer: document.getElementById('drawer'),
  drawerContent: document.getElementById('drawer-content'),
  drawerBackdrop: document.getElementById('drawer-backdrop'),
  footer: document.getElementById('footer'),
};

const countryNames = (() => {
  try { return new Intl.DisplayNames(['en'], { type: 'region' }); } catch { return null; }
})();

// ---------- small helpers ----------

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[character]));
}

function formatNumber(value) {
  return value == null ? '' : Number(value).toLocaleString('en-US');
}

function countryLabel(code) {
  if (!code) return '';
  try { return `${countryNames?.of(code) || code} (${code})`; } catch { return code; }
}

function facetValueLabel(facetKey, value) {
  if (facetKey === 'relationship_types') return RELATIONSHIP_TYPE_LABELS[value] || value;
  if (facetKey === 'country_codes' || facetKey === 'country_code') return countryLabel(value);
  return value;
}

function debounce(callback, milliseconds) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => callback(...args), milliseconds);
  };
}

function externalLink(url, text) {
  if (!isHttpUrl(url)) return escapeHtml(text);
  return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(text)}</a>`;
}

function relationshipBadges(types) {
  return (types || []).map((type) =>
    `<span class="badge relationship relationship-${escapeHtml(type)}" title="${escapeHtml(type)}">${escapeHtml(RELATIONSHIP_TYPE_LABELS[type] || type)}</span>`).join(' ');
}

function institutionTypeBadge(type) {
  if (!type) return '<span class="muted">unknown</span>';
  return `<span class="badge institution-type institution-type-${escapeHtml(type)}">${escapeHtml(type)}</span>`;
}

const INSTITUTION_STATUS_LABELS = { missing: 'unknown' };

function statusBadge(status) {
  const label = INSTITUTION_STATUS_LABELS[status] || status || 'unknown';
  return `<span class="badge status-${escapeHtml(status || 'unknown')}">${escapeHtml(label)}</span>`;
}

function evidenceBadge(evidence) {
  if (!evidence || typeof evidence !== 'object') return '<span class="muted">—</span>';
  const confidence = evidence.confidence || 'unknown';
  return `<span class="evidence" title="${escapeHtml(evidence.detail || '')}">${escapeHtml(evidence.method || 'unknown')} `
    + `<span class="badge confidence-${escapeHtml(confidence)}">${escapeHtml(confidence)}</span></span>`;
}

// harvest/build_awards.py places award participants the funder does not (NSF co-PIs, PIs refined to TACC) from
// dated evidence; institution_name_as_listed then still holds only what the source listed.
const INFERRED_INSTITUTION_METHOD_LABELS = {
  orcid_employment: 'ORCID employment in the award period',
  openalex_author_affiliation_years: 'OpenAlex affiliations in the award period',
  usaspending_subaward: 'USAspending subaward under this award',
  cross_award_dated: 'same person on an overlapping award',
};

function institutionNotes(participant, canonicalName) {
  const evidence = participant.institution_evidence || {};
  const inferredLabel = INFERRED_INSTITUTION_METHOD_LABELS[evidence.method];
  const listedName = participant.institution_name_as_listed;
  const notes = [];
  if (inferredLabel) {
    notes.push(`<div class="muted small"><span class="badge" title="${escapeHtml(evidence.detail || '')}">inferred</span> from ${escapeHtml(inferredLabel)}</div>`);
  }
  if (listedName && canonicalName && listedName !== canonicalName) notes.push(`<div class="muted small">as listed: ${escapeHtml(listedName)}</div>`);
  return notes.join('');
}

function flagIcons(record) {
  const icons = [];
  if (record.has_missing_institution) icons.push('<span class="flag flag-missing" title="Has participant(s) with unknown institution">⚠</span>');
  if (record.has_unresolved_institution) icons.push('<span class="flag flag-unresolved" title="Has participant(s) with unresolved (free-text) institution">?</span>');
  return icons.join('');
}

function institutionName(institutionId, fallbackName) {
  return state.institutionIndex.get(institutionId)?.name || fallbackName || institutionId;
}

function institutionLink(institutionId, fallbackName) {
  if (!institutionId) return escapeHtml(fallbackName || '');
  return `<a href="${buildDetailHash('institution', institutionId)}">${escapeHtml(institutionName(institutionId, fallbackName))}</a>`;
}

function personLink(personId, name) {
  if (!personId) return escapeHtml(name || '');
  return `<a href="${buildDetailHash('person', personId)}">${escapeHtml(name || personDisplayName(state.peopleIndex.get(personId) || {}) || personId)}</a>`;
}

function participantNameCell(participant) {
  const displayName = participant.person_display_name || participant.name_as_listed;
  const listedName = participant.name_as_listed;
  return `${personLink(participant.person_id, displayName)}${listedName && listedName !== displayName ? `<div class="muted small">as listed: ${escapeHtml(listedName)}</div>` : ''}`;
}

function collaborationLink(collaboration) {
  return `<a href="${buildDetailHash('collaboration', collaboration.id)}">${escapeHtml(collaboration.title || collaboration.id)}</a>`;
}

// ---------- data loading ----------

async function fetchJson(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

function loadInstitutions() {
  return loadFile('institutions');
}

function loadFile(tabKey) {
  const existing = state.files[tabKey];
  if (existing?.promise) return existing.promise;
  const tab = TAB_BY_KEY[tabKey];
  const fileState = { status: 'loading', records: [], items: [], universe: {}, error: null, promise: null };
  state.files[tabKey] = fileState;
  renderTabs();
  fileState.promise = (async () => {
    try {
      const recordsPromise = fetchJson(`data/${tab.file}`);
      if (tab.recordType === 'collaboration') await loadInstitutions().catch(() => null);
      const records = await recordsPromise;
      if (!Array.isArray(records)) throw new Error(`data/${tab.file} is not a JSON array`);
      fileState.records = records;
      fileState.items = prepareRecords(records, tab, state.institutionIndex);
      fileState.universe = facetUniverse(fileState.items, tab);
      if (tab.recordType === 'institution') state.institutionIndex = buildIndex(records);
      else if (tab.recordType === 'person') state.peopleIndex = buildIndex(records);
      else for (const record of records) state.collaborationIndex.set(record.id, record);
      fileState.status = 'loaded';
    } catch (error) {
      fileState.status = 'error';
      fileState.error = error;
      console.error(error);
    }
    renderTabs();
    return fileState;
  })();
  return fileState.promise;
}

// ---------- tabs ----------

function renderTabs() {
  elements.tabs.innerHTML = TABS.map((tab) => {
    const fileState = state.files[tab.key];
    let countText;
    if (fileState?.status === 'loaded') countText = formatNumber(fileState.records.length);
    else if (fileState?.status === 'error') countText = '!';
    else {
      const metaCount = metaCountForTab(state.meta, tab);
      countText = metaCount != null ? formatNumber(metaCount) : (fileState?.status === 'loading' ? '…' : '–');
    }
    const isActive = tab.key === state.currentTabKey;
    return `<a class="tab${isActive ? ' active' : ''}" href="${buildListHash(tab.key, getListState(tab.key))}">`
      + `${escapeHtml(tab.label)} <span class="tab-count">${countText}</span></a>`;
  }).join('');
}

function getListState(tabKey) {
  if (!state.listStates[tabKey]) {
    state.listStates[tabKey] = { filters: emptyFilters(), sort: { ...TAB_BY_KEY[tabKey].defaultSort }, page: 1 };
  }
  return state.listStates[tabKey];
}

// ---------- list view ----------

async function showList(tabKey, listState) {
  state.currentTabKey = tabKey;
  if (listState) state.listStates[tabKey] = listState;
  const hash = buildListHash(tabKey, getListState(tabKey));
  renderTabs();
  if (state.renderedListHash === hash && state.files[tabKey]?.status === 'loaded') return;
  state.renderedListHash = hash;
  const tab = TAB_BY_KEY[tabKey];
  elements.filters.innerHTML = '';
  elements.results.innerHTML = `<p class="loading">Loading ${escapeHtml(tab.file)}…</p>`;
  const fileState = await loadFile(tabKey);
  if (state.currentTabKey !== tabKey) return;
  if (fileState.status === 'error') {
    elements.results.innerHTML = `<div class="error"><p>Could not load <code>data/${escapeHtml(tab.file)}</code>: ${escapeHtml(fileState.error.message)}</p>`
      + '<p>Run <code>python3 harvest/build.py</code> to produce the data files.</p></div>';
    return;
  }
  renderFilterPanel(tab);
  refreshResults();
}

function currentTab() {
  return TAB_BY_KEY[state.currentTabKey];
}

function computeCurrentView() {
  const tab = currentTab();
  const fileState = state.files[tab.key];
  const listState = getListState(tab.key);
  const filtersKey = `${tab.key}|${JSON.stringify(listState.filters)}`;
  if (state.filterCache?.key !== filtersKey || state.filterCache.items !== fileState.items) {
    state.filterCache = { key: filtersKey, items: fileState.items, result: applyFilters(fileState.items, tab, listState.filters) };
    state.sortCache = null;
  }
  const sortKey = `${filtersKey}|${listState.sort.key}|${listState.sort.direction}`;
  if (state.sortCache?.key !== sortKey) {
    state.sortCache = { key: sortKey, sorted: sortItems(state.filterCache.result.matched, tab, listState.sort) };
  }
  return { result: state.filterCache.result, sorted: state.sortCache.sorted };
}

function refreshResults() {
  const tab = currentTab();
  const { result, sorted } = computeCurrentView();
  updateFacetCounts(tab, result);
  renderResultsTable(tab, sorted);
}

function onListStateChanged() {
  const tabKey = state.currentTabKey;
  const hash = buildListHash(tabKey, getListState(tabKey));
  state.renderedListHash = hash;
  history.replaceState(null, '', hash);
  renderTabs();
  refreshResults();
}

function renderFilterPanel(tab) {
  const { filters } = getListState(tab.key);
  const yearInputs = tab.recordType === 'collaboration' ? `
    <fieldset class="filter-group">
      <legend>Year</legend>
      <div class="year-range">
        <input type="number" id="filter-year-min" placeholder="from" value="${filters.yearMin ?? ''}" aria-label="Year from">
        <span>–</span>
        <input type="number" id="filter-year-max" placeholder="to" value="${filters.yearMax ?? ''}" aria-label="Year to">
      </div>
    </fieldset>` : '';
  const flagControls = tab.flags.map((flag) => `
    <label class="flag-control">
      <span>${escapeHtml(flag.label)} <span class="count" data-flag-count="${flag.key}"></span></span>
      <select data-flag="${flag.key}">
        <option value="">any</option>
        <option value="only"${filters.flags[flag.key] === 'only' ? ' selected' : ''}>only these</option>
        <option value="exclude"${filters.flags[flag.key] === 'exclude' ? ' selected' : ''}>exclude these</option>
      </select>
    </label>`).join('');
  const facetGroups = tab.facets.map((facet) =>
    `<fieldset class="filter-group facet" data-facet-group="${facet.key}"><legend>${escapeHtml(facet.label)}</legend><div class="facet-values"></div></fieldset>`).join('');

  elements.filters.innerHTML = `
    <div class="filter-header"><h2>Filters</h2><button type="button" id="reset-filters">Reset</button></div>
    <input type="search" id="filter-text" placeholder="Search title, names, institutions…" value="${escapeHtml(filters.text)}" aria-label="Search">
    ${yearInputs}
    ${tab.flags.length ? `<fieldset class="filter-group"><legend>Flags</legend>${flagControls}</fieldset>` : ''}
    ${facetGroups}`;

  const listState = getListState(tab.key);
  const onTextInput = debounce((value) => {
    listState.filters.text = value;
    listState.page = 1;
    onListStateChanged();
  }, SEARCH_DEBOUNCE_MILLISECONDS);
  elements.filters.querySelector('#filter-text').addEventListener('input', (event) => onTextInput(event.target.value));

  const onYearInput = debounce(() => {
    const parse = (text) => (text === '' ? null : Number.parseInt(text, 10));
    listState.filters.yearMin = parse(elements.filters.querySelector('#filter-year-min').value);
    listState.filters.yearMax = parse(elements.filters.querySelector('#filter-year-max').value);
    listState.page = 1;
    onListStateChanged();
  }, SEARCH_DEBOUNCE_MILLISECONDS);
  for (const input of elements.filters.querySelectorAll('.year-range input')) input.addEventListener('input', onYearInput);

  elements.filters.querySelector('#reset-filters').addEventListener('click', () => {
    listState.filters = emptyFilters();
    listState.page = 1;
    renderFilterPanel(tab);
    onListStateChanged();
  });
}

// One delegated listener survives re-renders of the filter panel.
elements.filters.addEventListener('change', (event) => {
  const target = event.target;
  const listState = getListState(state.currentTabKey);
  if (target.matches('select[data-flag]')) {
    if (target.value) listState.filters.flags[target.dataset.flag] = target.value;
    else delete listState.filters.flags[target.dataset.flag];
  } else if (target.matches('input[data-facet]')) {
    const facetKey = target.dataset.facet;
    const selected = new Set(listState.filters.facets[facetKey] || []);
    if (target.checked) selected.add(target.value); else selected.delete(target.value);
    if (selected.size) listState.filters.facets[facetKey] = [...selected]; else delete listState.filters.facets[facetKey];
  } else {
    return;
  }
  listState.page = 1;
  onListStateChanged();
});

function updateFacetCounts(tab, result) {
  const fileState = state.files[tab.key];
  const { filters } = getListState(tab.key);
  for (const facet of tab.facets) {
    const group = elements.filters.querySelector(`[data-facet-group="${facet.key}"]`);
    if (!group) continue;
    const allValues = [...fileState.universe[facet.key].keys()];
    const selected = new Set(filters.facets[facet.key] || []);
    for (const value of selected) if (!fileState.universe[facet.key].has(value)) allValues.push(value);
    group.hidden = allValues.length <= 1 && selected.size === 0;
    const liveCounts = result.facetCounts[facet.key];
    allValues.sort((left, right) => (liveCounts.get(right) || 0) - (liveCounts.get(left) || 0)
      || TEXT_COLLATOR.compare(String(facetValueLabel(facet.key, left)), String(facetValueLabel(facet.key, right))));
    const scrollTop = group.querySelector('.facet-values')?.scrollTop || 0;
    group.querySelector('.facet-values').innerHTML = allValues.map((value) => {
      const count = liveCounts.get(value) || 0;
      const isSelected = selected.has(value);
      return `<label class="facet-value${count === 0 && !isSelected ? ' zero' : ''}">`
        + `<input type="checkbox" data-facet="${escapeHtml(facet.key)}" value="${escapeHtml(value)}"${isSelected ? ' checked' : ''}>`
        + `<span class="facet-label">${escapeHtml(facetValueLabel(facet.key, value))}</span><span class="count">${formatNumber(count)}</span></label>`;
    }).join('');
    group.querySelector('.facet-values').scrollTop = scrollTop;
  }
  for (const flag of tab.flags) {
    const countElement = elements.filters.querySelector(`[data-flag-count="${flag.key}"]`);
    if (countElement) countElement.textContent = `(${formatNumber(result.flagCounts[flag.key])})`;
  }
}

function renderCell(column, record) {
  const value = column.value(record);
  switch (column.type) {
    case 'flags': return flagIcons(record);
    case 'title': {
      const detailType = currentTab().recordType;
      const href = buildDetailHash(detailType, record.id);
      const kindBadge = record.kind === 'subaward' ? ' <span class="badge kind">subaward</span>' : '';
      return `<a href="${href}">${escapeHtml(value || record.id)}</a>${kindBadge}`;
    }
    case 'badges': return relationshipBadges(record.relationship_types);
    case 'number': return escapeHtml(value == null ? '' : formatNumber(value));
    case 'money': return escapeHtml(formatMoney(value));
    case 'institution_type': return institutionTypeBadge(value);
    default: return escapeHtml(value ?? '');
  }
}

function renderResultsTable(tab, sorted) {
  const listState = getListState(tab.key);
  const { pageItems, currentPage, pageCount, start } = paginate(sorted, listState.page, PAGE_SIZE);
  listState.page = currentPage;
  const total = state.files[tab.key].records.length;
  const headerCells = tab.columns.map((column) => {
    const isSorted = listState.sort.key === column.key;
    const arrow = isSorted ? (listState.sort.direction === 'asc' ? ' ▲' : ' ▼') : '';
    const title = column.type === 'flags' ? ' title="⚠ unknown institution, ? unresolved institution"' : '';
    return `<th class="sortable col-${column.key}${isSorted ? ' sorted' : ''}" data-sort-key="${column.key}"${title}>${escapeHtml(column.label || '⚑')}${arrow}</th>`;
  }).join('');
  const rows = pageItems.map((item) => {
    const detailType = tab.recordType;
    return `<tr data-href="${buildDetailHash(detailType, item.record.id)}">`
      + tab.columns.map((column) => `<td class="col-${column.key}">${renderCell(column, item.record)}</td>`).join('') + '</tr>';
  }).join('');
  const rangeText = sorted.length
    ? `${formatNumber(start + 1)}–${formatNumber(start + pageItems.length)} of ${formatNumber(sorted.length)}`
    : '0 results';
  elements.results.innerHTML = `
    <div class="results-header">
      <span><strong>${formatNumber(sorted.length)}</strong> of ${formatNumber(total)} ${escapeHtml(tab.label.toLowerCase())} match</span>
      ${paginationControls(currentPage, pageCount, rangeText)}
    </div>
    <div class="table-wrapper"><table class="results-table"><thead><tr>${headerCells}</tr></thead><tbody>${rows || `<tr><td colspan="${tab.columns.length}" class="muted">No matching records.</td></tr>`}</tbody></table></div>
    <div class="results-footer">${paginationControls(currentPage, pageCount, rangeText)}</div>`;
}

function paginationControls(currentPage, pageCount, rangeText) {
  return `<span class="pagination">
    <button type="button" data-page="1"${currentPage <= 1 ? ' disabled' : ''}>«</button>
    <button type="button" data-page="${currentPage - 1}"${currentPage <= 1 ? ' disabled' : ''}>‹</button>
    <span>Page <input type="number" class="page-input" min="1" max="${pageCount}" value="${currentPage}" aria-label="Page"> of ${formatNumber(pageCount)}</span>
    <button type="button" data-page="${currentPage + 1}"${currentPage >= pageCount ? ' disabled' : ''}>›</button>
    <button type="button" data-page="${pageCount}"${currentPage >= pageCount ? ' disabled' : ''}>»</button>
    <span class="muted">${rangeText}</span>
  </span>`;
}

function goToPage(page) {
  const listState = getListState(state.currentTabKey);
  listState.page = page;
  onListStateChanged();
  elements.results.querySelector('.table-wrapper')?.scrollIntoView({ block: 'start' });
}

elements.results.addEventListener('click', (event) => {
  const sortHeader = event.target.closest('th[data-sort-key]');
  if (sortHeader) {
    const tab = currentTab();
    const column = tab.columns.find((candidate) => candidate.key === sortHeader.dataset.sortKey);
    const listState = getListState(tab.key);
    if (listState.sort.key === column.key) {
      listState.sort = { key: column.key, direction: listState.sort.direction === 'asc' ? 'desc' : 'asc' };
    } else {
      const numericLike = ['number', 'money', 'flags', 'year'].includes(column.type);
      listState.sort = { key: column.key, direction: numericLike ? 'desc' : 'asc' };
    }
    listState.page = 1;
    onListStateChanged();
    return;
  }
  const pageButton = event.target.closest('button[data-page]');
  if (pageButton) { goToPage(Number(pageButton.dataset.page)); return; }
  if (event.target.closest('a, input, button')) return;
  const row = event.target.closest('tr[data-href]');
  if (row) navigateTo(row.dataset.href);
});

elements.results.addEventListener('change', (event) => {
  if (event.target.matches('.page-input')) goToPage(Number.parseInt(event.target.value, 10) || 1);
});

// ---------- navigation ----------

function navigateTo(hash) {
  location.hash = hash;
}

// Closing pushes the list route instead of history.back(), so it always lands on the list even after
// following several detail-to-detail links; the browser back button still walks through those details.
function closeDrawer() {
  if (elements.drawer.hidden) return;
  navigateTo(buildListHash(state.currentTabKey, getListState(state.currentTabKey)));
}

elements.drawerBackdrop.addEventListener('click', closeDrawer);
document.getElementById('drawer-close').addEventListener('click', closeDrawer);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeDrawer();
});

function onRouteChange() {
  const route = parseHash(location.hash);
  if (route.view === 'list') {
    hideDrawer();
    showList(route.tabKey, { filters: route.filters, sort: route.sort, page: route.page });
    return;
  }
  if (!state.currentTabKey) {
    const backgroundTabKey = route.detailType === 'person' ? 'people'
      : route.detailType === 'institution' ? 'institutions'
        : (tabKeyForCollaborationId(route.id) || TABS[0].key);
    showList(backgroundTabKey);
  }
  showDetail(route.detailType, route.id);
}

function hideDrawer() {
  elements.drawer.hidden = true;
  elements.drawerBackdrop.hidden = true;
  document.body.classList.remove('drawer-open');
  state.detailRenderToken += 1;
}

// ---------- detail views ----------

async function showDetail(detailType, id) {
  elements.drawer.hidden = false;
  elements.drawerBackdrop.hidden = false;
  document.body.classList.add('drawer-open');
  const renderToken = ++state.detailRenderToken;
  const isCurrent = () => renderToken === state.detailRenderToken;
  elements.drawerContent.innerHTML = '<p class="loading">Loading…</p>';
  elements.drawer.scrollTop = 0;
  try {
    if (detailType === 'collaboration') await renderCollaborationDetail(id, isCurrent);
    else if (detailType === 'person') await renderPersonDetail(id, isCurrent);
    else await renderInstitutionDetail(id, isCurrent);
  } catch (error) {
    console.error(error);
    if (isCurrent()) elements.drawerContent.innerHTML = `<p class="error">Failed to render: ${escapeHtml(error.message)}</p>`;
  }
}

function setDrawerContent(html, isCurrent) {
  if (!isCurrent()) return false;
  elements.drawerContent.innerHTML = html;
  return true;
}

function fieldRows(record, excludedKeys) {
  return Object.entries(record)
    .filter(([key]) => !excludedKeys.includes(key))
    .map(([key, value]) => `<tr><th>${escapeHtml(key)}</th><td>${formatFieldValue(key, value)}</td></tr>`)
    .join('');
}

function formatFieldValue(key, value) {
  if (value == null || value === '') return '<span class="muted">—</span>';
  if (key === 'relationship_types') return relationshipBadges(value);
  if (key === 'institution_ids') return value.map((institutionId) => institutionLink(institutionId)).join(', ') || '<span class="muted">—</span>';
  if (key === 'institution_types') return value.map(institutionTypeBadge).join(' ') || '<span class="muted">—</span>';
  if (key === 'country_codes') return value.map(countryLabel).map(escapeHtml).join(', ') || '<span class="muted">—</span>';
  if (key === 'amount_usd') return escapeHtml(formatMoney(value));
  if (key === 'url') return externalLink(value, value);
  if (key === 'description') return `<div class="description">${escapeHtml(value)}</div>`;
  if (key === 'collaboration_ids') return `${formatNumber(value.length)} ids`;
  if (key === 'resources') return escapeHtml(toList(value).map(resourceLabel).join(', '));
  if (typeof value === 'boolean') return value ? '<strong>yes</strong>' : 'no';
  if (Array.isArray(value) && value.every((entry) => typeof entry !== 'object')) return escapeHtml(value.join(', ')) || '<span class="muted">—</span>';
  if (typeof value === 'object') return `<pre>${escapeHtml(JSON.stringify(value, null, 1))}</pre>`;
  return escapeHtml(value);
}

function sourcesSection(sources) {
  if (!sources?.length) return '<h3>Sources</h3><p class="muted">No provenance recorded.</p>';
  return `<h3>Sources</h3><table class="compact"><thead><tr><th>Source</th><th>Record</th><th>Retrieved</th></tr></thead><tbody>${
    sources.map((source) => `<tr><td>${escapeHtml(source.source)}</td><td>${externalLink(source.url, source.source_record_id || source.url || '')}</td><td>${escapeHtml(source.retrieved_at || '')}</td></tr>`).join('')
  }</tbody></table>`;
}

function participantsTable(participants, showAll) {
  const shown = showAll ? participants : participants.slice(0, PARTICIPANT_DISPLAY_LIMIT);
  const rows = shown.map((participant) => {
    const institution = state.institutionIndex.get(participant.institution_id);
    const additional = (participant.additional_institution_ids || []).map((institutionId) => institutionLink(institutionId)).join('; ');
    const listedName = participant.institution_name_as_listed;
    const canonicalName = institution?.name;
    const institutionCell = participant.institution_id
      ? `${institutionLink(participant.institution_id, listedName)}${institutionNotes(participant, canonicalName)}`
      : `<span class="muted">${escapeHtml(listedName || 'none')}</span>${participant.institution_match_method === 'bulk_affiliation_guard' ? '<div class="muted small">removed: affiliation applied to most of the author list</div>' : ''}`;
    return `<tr class="status-row-${escapeHtml(participant.institution_status)}">
      <td>${participantNameCell(participant)}</td>
      <td>${escapeHtml(participant.role || '')}</td>
      <td>${institutionCell}</td>
      <td>${participant.institution_id ? institutionTypeBadge(institution?.institution_type) : ''}</td>
      <td>${statusBadge(participant.institution_status)}</td>
      <td>${evidenceBadge(participant.institution_evidence)}</td>
      <td>${additional}</td>
    </tr>`;
  }).join('');
  const moreButton = !showAll && participants.length > shown.length
    ? `<button type="button" id="show-all-participants">Show all ${formatNumber(participants.length)} participants</button>` : '';
  return `<table class="compact participants"><thead><tr><th>Name</th><th>Role</th><th>Institution on this record</th><th>Type</th><th>Status</th><th>Evidence</th><th>Additional institutions</th></tr></thead><tbody>${rows}</tbody></table>${moreButton}`;
}

async function renderCollaborationDetail(id, isCurrent) {
  const tabKey = tabKeyForCollaborationId(id);
  if (tabKey) await loadFile(tabKey);
  await loadInstitutions().catch(() => null);
  const record = state.collaborationIndex.get(id);
  if (!record) {
    setDrawerContent(`<p class="error">Collaboration <code>${escapeHtml(id)}</code> not found.</p>`, isCurrent);
    return;
  }
  const participants = record.participants || [];
  const statusCounts = participants.reduce((counts, participant) => {
    counts[participant.institution_status] = (counts[participant.institution_status] || 0) + 1;
    return counts;
  }, {});
  const identifierRows = identifierLinks(record)
    .map((link) => `<tr><th>${escapeHtml(link.key)}</th><td>${externalLink(link.url, link.value)}</td></tr>`).join('');
  const html = `
    <p class="detail-kind">${escapeHtml(record.kind || '')} ${relationshipBadges(record.relationship_types)} ${flagIcons(record)}</p>
    <h2>${escapeHtml(record.title || record.id)}</h2>
    <p class="muted">${escapeHtml(record.id)}</p>
    <h3>Identifiers</h3>
    ${identifierRows ? `<table class="compact fields">${identifierRows}</table>` : '<p class="muted">None.</p>'}
    <h3>Participants (${formatNumber(participants.length)}${record.participants_truncated ? ', truncated by source' : ''})</h3>
    <p class="small">${Object.entries(statusCounts).map(([status, count]) => `${statusBadge(status)} ${formatNumber(count)}`).join(' &nbsp; ')}</p>
    <div id="participants-container">${participantsTable(participants, false)}</div>
    ${sourcesSection(record.sources)}
    <h3>All fields</h3>
    <table class="compact fields">${fieldRows(record, ['participants', 'identifiers', 'sources', 'title'])}</table>`;
  if (!setDrawerContent(html, isCurrent)) return;
  elements.drawerContent.querySelector('#show-all-participants')?.addEventListener('click', () => {
    elements.drawerContent.querySelector('#participants-container').innerHTML = participantsTable(participants, true);
  });
}

function findPersonInLoadedCollaborations(personId) {
  const collaborationIds = [];
  let name = null;
  for (const collaboration of state.collaborationIndex.values()) {
    const participation = (collaboration.participants || []).find((participant) => participant.person_id === personId);
    if (participation) {
      collaborationIds.push(collaboration.id);
      name = name || participation.name_as_listed;
    }
  }
  return collaborationIds.length ? { id: personId, name, collaboration_ids: collaborationIds, sources: [], partial: true } : null;
}

function personIdentifierLinks(person) {
  const links = [];
  const ownLink = entityIdUrl(person.id);
  links.push(`<tr><th>id</th><td>${externalLink(ownLink, person.id)}</td></tr>`);
  if (person.orcid) links.push(`<tr><th>orcid</th><td>${externalLink(orcidUrl(person.orcid), person.orcid)}</td></tr>`);
  if (person.openalex_author_id) links.push(`<tr><th>openalex</th><td>${externalLink(openalexUrl(person.openalex_author_id), person.openalex_author_id)}</td></tr>`);
  for (const [key, value] of Object.entries(person.other_ids || {})) {
    links.push(`<tr><th>${escapeHtml(key)}</th><td>${escapeHtml(typeof value === 'object' ? JSON.stringify(value) : value)}</td></tr>`);
  }
  const nameVariants = toList(person.name_variants);
  if (nameVariants.length) links.push(`<tr><th>name variants</th><td>${escapeHtml(nameVariants.join('; '))}</td></tr>`);
  const mergedIds = toList(person.merged_ids);
  if (mergedIds.length) {
    links.push(`<tr><th>merged ids</th><td>${mergedIds.map((mergedId) => externalLink(entityIdUrl(mergedId), mergedId)).join('<br>')}</td></tr>`);
  }
  return links.join('');
}

async function renderPersonDetail(id, isCurrent) {
  setDrawerContent('<p class="loading">Loading people…</p>', isCurrent);
  await loadFile('people');
  let person = state.peopleIndex.get(id);
  const neededTabKeys = person ? tabKeysForCollaborationIds(person.collaboration_ids) : COLLABORATION_TAB_KEYS;
  setDrawerContent(`<p class="loading">Loading ${escapeHtml(neededTabKeys.join(', '))}…</p>`, isCurrent);
  await Promise.all([loadInstitutions().catch(() => null), ...neededTabKeys.map(loadFile)]);
  if (!person) person = findPersonInLoadedCollaborations(id);
  if (!person) {
    setDrawerContent(`<p class="error">Person <code>${escapeHtml(id)}</code> not found.</p>`, isCurrent);
    return;
  }
  const { entries, missingIds } = personTimeline(person, (collaborationId) => state.collaborationIndex.get(collaborationId));
  const timelineRows = entries.map(({ collaboration, participations }) => {
    const shownParticipations = participations.length ? participations : [{}];
    return shownParticipations.map((participation, position) => `<tr>
      ${position === 0 ? `<td rowspan="${shownParticipations.length}">${escapeHtml(collaboration.year ?? '—')}</td>
      <td rowspan="${shownParticipations.length}">${collaborationLink(collaboration)}<div class="small">${escapeHtml(collaboration.kind || '')} ${relationshipBadges(collaboration.relationship_types)}</div></td>` : ''}
      <td>${escapeHtml(participation.role || '')}${participation.name_as_listed && participation.name_as_listed !== personDisplayName(person) ? `<div class="muted small">as ${escapeHtml(participation.name_as_listed)}</div>` : ''}</td>
      <td>${participation.institution_id ? institutionLink(participation.institution_id, participation.institution_name_as_listed) : `<span class="muted">${escapeHtml(participation.institution_name_as_listed || 'none')}</span>`}
        ${participation.institution_id ? institutionNotes(participation, institutionName(participation.institution_id)) : ''}
        ${(participation.additional_institution_ids || []).length ? `<div class="small">also: ${participation.additional_institution_ids.map((institutionId) => institutionLink(institutionId)).join('; ')}</div>` : ''}</td>
      <td>${participation.institution_id ? institutionTypeBadge(state.institutionIndex.get(participation.institution_id)?.institution_type) : ''}</td>
      <td>${statusBadge(participation.institution_status)}</td>
    </tr>`).join('');
  }).join('');
  const html = `
    <p class="detail-kind">person</p>
    <h2>${escapeHtml(personDisplayName(person) || person.id)}</h2>
    ${person.partial ? '<p class="warning">Not in people.json; reconstructed from loaded collaborations.</p>' : ''}
    <table class="compact fields">${personIdentifierLinks(person)}
      <tr><th>collaborations</th><td>${formatNumber(person.collaboration_count ?? person.collaboration_ids?.length ?? 0)}</td></tr></table>
    <h3>Collaboration timeline</h3>
    <p class="small muted">Institution is the one listed on each collaboration, or for award participants the funder does not place, inferred from dated evidence in the award period (marked inferred). It is not a tracked personal location.</p>
    ${missingIds.length ? `<p class="warning">${formatNumber(missingIds.length)} collaboration id(s) not found in loaded data.</p>` : ''}
    <table class="compact timeline"><thead><tr><th>Year</th><th>Collaboration</th><th>Role</th><th>Institution</th><th>Type</th><th>Status</th></tr></thead><tbody>${timelineRows}</tbody></table>
    ${sourcesSection(person.sources)}`;
  setDrawerContent(html, isCurrent);
}

async function renderInstitutionDetail(id, isCurrent) {
  await loadInstitutions();
  const institution = state.institutionIndex.get(id);
  if (!institution) {
    setDrawerContent(`<p class="error">Institution <code>${escapeHtml(id)}</code> not found in institutions.json.</p>`, isCurrent);
    return;
  }
  const parentId = parentInstitutionId(institution);
  const children = childInstitutions(institution, state.files.institutions.records);
  const latitudeLongitude = institution.latitude != null && institution.longitude != null
    ? escapeHtml(`${institution.latitude}, ${institution.longitude}`) : '<span class="muted">unknown</span>';
  const headerHtml = `
    <p class="detail-kind">institution ${institutionTypeBadge(institution.institution_type)}</p>
    <h2>${escapeHtml(institution.name || institution.id)}</h2>
    <table class="compact fields">
      <tr><th>id</th><td>${externalLink(entityIdUrl(institution.id), institution.id)}</td></tr>
      <tr><th>type</th><td>${institutionTypeBadge(institution.institution_type)}</td></tr>
      <tr><th>type reason</th><td>${escapeHtml(institution.institution_type_reason || '') || '<span class="muted">—</span>'}</td></tr>
      <tr><th>website</th><td>${institution.website ? externalLink(institution.website, institution.website) : '<span class="muted">—</span>'}</td></tr>
      <tr><th>source types</th><td>${escapeHtml(toList(institution.source_types).join(', ')) || '<span class="muted">—</span>'}</td></tr>
      <tr><th>ror</th><td>${institution.ror ? externalLink(rorUrl(institution.ror), institution.ror) : '<span class="muted">—</span>'}</td></tr>
      <tr><th>openalex</th><td>${institution.openalex_id ? externalLink(openalexUrl(institution.openalex_id), institution.openalex_id) : '<span class="muted">—</span>'}</td></tr>
      <tr><th>country</th><td>${escapeHtml(countryLabel(institution.country_code)) || '<span class="muted">—</span>'}</td></tr>
      <tr><th>city</th><td>${escapeHtml(institution.city || '') || '<span class="muted">—</span>'}</td></tr>
      <tr><th>lat / lng</th><td>${latitudeLongitude}</td></tr>
      <tr><th>parent</th><td>${parentId ? (state.institutionIndex.has(parentId) ? institutionLink(parentId) : externalLink(rorUrl(parentId), parentId)) : '<span class="muted">—</span>'}</td></tr>
      ${children.length ? `<tr><th>children</th><td>${children.map((child) => institutionLink(child.id)).join('; ')}</td></tr>` : ''}
      <tr><th>collaborations</th><td>${formatNumber(institution.collaboration_count ?? 0)}</td></tr>
    </table>
    ${sourcesSection(institution.sources)}
    <h3>Collaborations</h3>`;
  if (!setDrawerContent(`${headerHtml}<p class="loading">Loading all collaboration files (papers may be large)…</p>`, isCurrent)) return;
  await Promise.all(COLLABORATION_TAB_KEYS.map(loadFile));
  const entries = institutionCollaborations(id, state.collaborationIndex.values());
  const failedFiles = COLLABORATION_TAB_KEYS.filter((tabKey) => state.files[tabKey]?.status === 'error');
  const renderRows = (limit) => entries.slice(0, limit).map(({ collaboration, participants }) => `<tr>
      <td>${escapeHtml(collaboration.year ?? '—')}</td>
      <td>${collaborationLink(collaboration)}<div class="small">${escapeHtml(collaboration.kind || '')} ${relationshipBadges(collaboration.relationship_types)}</div></td>
      <td>${participants.slice(0, 6).map((participant) => personLink(participant.person_id, participant.person_display_name || participant.name_as_listed)).join(', ')}${participants.length > 6 ? ` <span class="muted">+${participants.length - 6}</span>` : ''}</td>
    </tr>`).join('');
  const listHtml = (limit) => `
    ${failedFiles.length ? `<p class="warning">Could not load: ${escapeHtml(failedFiles.join(', '))}</p>` : ''}
    <p class="small">${formatNumber(entries.length)} collaborations in loaded data list this institution.</p>
    <table class="compact"><thead><tr><th>Year</th><th>Collaboration</th><th>Participants listed here</th></tr></thead><tbody>${renderRows(limit)}</tbody></table>
    ${entries.length > limit ? `<button type="button" id="show-all-institution-collaborations">Show all ${formatNumber(entries.length)}</button>` : ''}`;
  if (!setDrawerContent(`${headerHtml}<div id="institution-collaborations">${listHtml(RELATED_LIST_DISPLAY_LIMIT)}</div>`, isCurrent)) return;
  elements.drawerContent.querySelector('#show-all-institution-collaborations')?.addEventListener('click', () => {
    elements.drawerContent.querySelector('#institution-collaborations').innerHTML = listHtml(entries.length);
  });
}

// ---------- footer / startup ----------

// GeoNames (CC BY 4.0) requires attribution for the coordinates ROR passes on; shown even when meta.json fails.
const FOOTER_NOTICE_HTML = `
  <span>Geographic coordinates © <a href="https://www.geonames.org/">GeoNames</a>, <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>, via <a href="https://ror.org/">ROR</a>.</span>
  <span>The data comes from the public sources listed here, under each source's own terms; this project claims no copyright in it. Code: MIT.</span>
  <span>Not an official product of TACC or The University of Texas at Austin; no endorsement by them or by any data provider is implied. Corrections and removal: see the project README.</span>`;

function renderFooter() {
  const meta = state.meta;
  if (!meta) {
    elements.footer.innerHTML = `<span class="muted">meta.json not available.</span>${FOOTER_NOTICE_HTML}`;
    return;
  }
  const sources = metaSources(meta);
  elements.footer.innerHTML = `
    <span>Data generated ${escapeHtml(meta.generated_at || 'unknown')}</span>
    ${sources.length ? `<span>Sources: ${sources.map((source) => `<span class="footer-source" title="${escapeHtml(source.detail || '')}">${escapeHtml(source.name)}${source.retrievedAt ? ` <span class="muted">(${escapeHtml(source.retrievedAt)})</span>` : ''}</span>`).join(', ')}</span>` : ''}${FOOTER_NOTICE_HTML}`;
}

async function start() {
  window.addEventListener('hashchange', onRouteChange);
  fetchJson('data/meta.json')
    .then((meta) => { state.meta = meta; })
    .catch((error) => console.warn(error))
    .finally(() => { renderFooter(); renderTabs(); });
  loadInstitutions();
  onRouteChange();
}

start();
