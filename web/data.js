// Pure data logic for the TACCoMap browser: no DOM access, so it can be tested with node.

export const PAGE_SIZE = 50;

// One shared collator: localeCompare with options builds a new collator per call, which dominates large sorts.
export const TEXT_COLLATOR = new Intl.Collator(undefined, { sensitivity: 'base', numeric: true });

export const RELATIONSHIP_TYPE_LABELS = {
  staff_coauthor: 'Staff co-author',
  resource_acknowledgment: 'Resource acknowledgment',
  tacc_lead_award: 'TACC lead award',
  tacc_partner_award: 'TACC partner award',
  tacc_mention_award: 'TACC mention award',
  subaward: 'Subaward',
  tacc_hosted_data: 'TACC-hosted data',
  tacc_resource_allocation: 'TACC allocation',
  funding_opportunity: 'Funding opportunity',
};

export const INSTITUTION_TYPES = ['government', 'commercial', 'nonprofit', 'educational', 'research', 'healthcare', 'archive', 'other'];

const COLLABORATION_ID_PREFIX_TO_TAB = {
  paper: 'papers',
  award: 'awards',
  subaward: 'awards',
  dataset: 'datasets',
  allocation: 'allocations',
  opportunity: 'funding_opportunities',
};

const COLLABORATION_FACETS = [
  { key: 'relationship_types', label: 'Relationship type', values: (record) => record.relationship_types },
  { key: 'kind', label: 'Kind', values: (record) => [record.kind] },
  { key: 'institution_types', label: 'Institution type', values: (record) => record.institution_types },
  { key: 'country_codes', label: 'Country', values: (record) => record.country_codes },
  { key: 'source', label: 'Source', values: sourceNames },
];

const COLLABORATION_FLAGS = [
  { key: 'missing', label: 'Has unknown institution', test: (record) => record.has_missing_institution === true },
  { key: 'unresolved', label: 'Has unresolved institution', test: (record) => record.has_unresolved_institution === true },
];

const BASE_COLLABORATION_COLUMNS = [
  { key: 'flags', label: '', type: 'flags', value: (record) => (record.has_missing_institution ? 2 : 0) + (record.has_unresolved_institution ? 1 : 0) },
  { key: 'title', label: 'Title', type: 'title', value: (record) => record.title },
  { key: 'relationship_types', label: 'Relationship', type: 'badges', value: (record) => (record.relationship_types || []).join(',') },
  { key: 'year', label: 'Year', type: 'year', value: (record) => record.year },
];
const TRAILING_COLLABORATION_COLUMNS = [
  { key: 'participants', label: '# Participants', type: 'number', value: (record) => (record.participants || []).length },
  { key: 'institutions', label: '# Institutions', type: 'number', value: (record) => (record.institution_ids || []).length },
  { key: 'source', label: 'Source', type: 'text', value: (record) => sourceNames(record).join(', ') },
];

function collaborationColumns(extraColumns) {
  return [...BASE_COLLABORATION_COLUMNS, ...extraColumns, ...TRAILING_COLLABORATION_COLUMNS];
}

function extraFieldFacet(key, label) {
  return { key, label, values: (record) => toList(record[key]) };
}

export const TABS = [
  {
    key: 'papers', label: 'Papers', file: 'papers.json', recordType: 'collaboration',
    facets: COLLABORATION_FACETS, flags: COLLABORATION_FLAGS,
    columns: collaborationColumns([{ key: 'venue', label: 'Venue', type: 'text', value: (record) => record.venue }]),
    defaultSort: { key: 'year', direction: 'desc' },
  },
  {
    key: 'awards', label: 'Awards', file: 'awards.json', recordType: 'collaboration',
    facets: COLLABORATION_FACETS, flags: COLLABORATION_FLAGS,
    columns: collaborationColumns([
      { key: 'funder', label: 'Funder', type: 'text', value: (record) => record.funder },
      { key: 'amount_usd', label: 'Amount (USD)', type: 'money', value: (record) => record.amount_usd },
    ]),
    defaultSort: { key: 'year', direction: 'desc' },
  },
  {
    key: 'datasets', label: 'Datasets', file: 'datasets.json', recordType: 'collaboration',
    facets: [...COLLABORATION_FACETS, extraFieldFacet('resource_type', 'Resource type')], flags: COLLABORATION_FLAGS,
    columns: collaborationColumns([{ key: 'venue', label: 'Publisher', type: 'text', value: (record) => record.venue }]),
    defaultSort: { key: 'year', direction: 'desc' },
  },
  {
    key: 'allocations', label: 'Allocations', file: 'allocations.json', recordType: 'collaboration',
    facets: [
      ...COLLABORATION_FACETS,
      extraFieldFacet('allocation_type', 'Allocation type'),
      extraFieldFacet('field_of_science', 'Field of science'),
      extraFieldFacet('resources', 'Resource'),
    ],
    flags: COLLABORATION_FLAGS,
    columns: collaborationColumns([
      { key: 'field_of_science', label: 'Field of science', type: 'text', value: (record) => toList(record.field_of_science).join(', ') },
      { key: 'resources', label: 'Resources', type: 'text', value: (record) => toList(record.resources).map(resourceLabel).join(', ') },
    ]),
    defaultSort: { key: 'year', direction: 'desc' },
  },
  {
    key: 'funding_opportunities', label: 'Funding Opportunities', file: 'funding_opportunities.json', recordType: 'collaboration',
    facets: [...COLLABORATION_FACETS, extraFieldFacet('status', 'Status')], flags: COLLABORATION_FLAGS,
    columns: collaborationColumns([
      { key: 'status', label: 'Status', type: 'text', value: (record) => record.status },
      { key: 'end_date', label: 'Closes', type: 'text', value: (record) => record.end_date },
      { key: 'amount_usd', label: 'Amount (USD)', type: 'money', value: (record) => record.amount_usd },
    ]),
    defaultSort: { key: 'end_date', direction: 'desc' },
  },
  {
    key: 'people', label: 'People', file: 'people.json', recordType: 'person',
    facets: [{ key: 'source', label: 'Source', values: sourceNames }],
    flags: [{ key: 'no_orcid', label: 'No ORCID', test: (record) => !record.orcid }],
    columns: [
      { key: 'name', label: 'Name', type: 'title', value: personDisplayName },
      { key: 'orcid', label: 'ORCID', type: 'text', value: (record) => record.orcid },
      { key: 'collaboration_count', label: '# Collaborations', type: 'number', value: (record) => record.collaboration_count },
      { key: 'source', label: 'Source', type: 'text', value: (record) => sourceNames(record).join(', ') },
    ],
    defaultSort: { key: 'collaboration_count', direction: 'desc' },
  },
  {
    key: 'institutions', label: 'Institutions', file: 'institutions.json', recordType: 'institution',
    facets: [
      { key: 'institution_type', label: 'Institution type', values: (record) => [record.institution_type] },
      { key: 'institution_type_reason', label: 'Type reason', values: (record) => [record.institution_type_reason] },
      { key: 'country_code', label: 'Country', values: (record) => [record.country_code] },
      { key: 'source_types', label: 'Source-reported type', values: (record) => record.source_types },
      { key: 'source', label: 'Source', values: sourceNames },
    ],
    flags: [
      { key: 'unresolved_id', label: 'Name-only id (unresolved)', test: (record) => String(record.id).startsWith('name:') },
      { key: 'no_coordinates', label: 'No coordinates', test: (record) => record.latitude == null || record.longitude == null },
    ],
    columns: [
      { key: 'name', label: 'Name', type: 'title', value: (record) => record.name },
      { key: 'institution_type', label: 'Type', type: 'institution_type', value: (record) => record.institution_type },
      { key: 'institution_type_reason', label: 'Type reason', type: 'text', value: (record) => record.institution_type_reason },
      { key: 'country_code', label: 'Country', type: 'text', value: (record) => record.country_code },
      { key: 'city', label: 'City', type: 'text', value: (record) => record.city },
      { key: 'collaboration_count', label: '# Collaborations', type: 'number', value: (record) => record.collaboration_count },
    ],
    defaultSort: { key: 'collaboration_count', direction: 'desc' },
  },
];

export const TAB_BY_KEY = Object.fromEntries(TABS.map((tab) => [tab.key, tab]));
export const COLLABORATION_TAB_KEYS = TABS.filter((tab) => tab.recordType === 'collaboration').map((tab) => tab.key);

export function toList(value) {
  if (value == null || value === '') return [];
  return Array.isArray(value) ? value.filter((item) => item != null && item !== '') : [value];
}

// Allocation "resources" may be plain strings or objects such as {name, ...}.
export function resourceLabel(resource) {
  if (resource && typeof resource === 'object') return resource.name || resource.resource_name || JSON.stringify(resource);
  return String(resource);
}

export function personDisplayName(person) {
  return person.display_name || person.name;
}

export function sourceNames(record) {
  return [...new Set((record.sources || []).map((source) => source.source).filter(Boolean))];
}

export function normalizeText(text) {
  return String(text ?? '')
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase();
}

export function tabKeyForCollaborationId(collaborationId) {
  const prefix = String(collaborationId).split(':', 1)[0];
  return COLLABORATION_ID_PREFIX_TO_TAB[prefix] || null;
}

export function buildIndex(records) {
  const index = new Map();
  for (const record of records) index.set(record.id, record);
  return index;
}

function uniqueDefined(values) {
  return [...new Set(values.filter((value) => value != null && value !== ''))];
}

function collaborationSearchParts(record, institutionIndex) {
  const parts = [record.id, record.title, record.venue, record.funder, record.description ? record.description.slice(0, 500) : null];
  for (const value of Object.values(record.identifiers || {})) parts.push(value);
  for (const participant of record.participants || []) {
    parts.push(participant.name_as_listed, participant.person_display_name);
    // Affiliation text removed by the bulk-affiliation guard was ruled wrong for this person; keep it out of search.
    if (participant.institution_match_method !== 'bulk_affiliation_guard') parts.push(participant.institution_name_as_listed);
  }
  for (const institutionId of record.institution_ids || []) parts.push(institutionIndex?.get(institutionId)?.name);
  return parts;
}

function personSearchParts(record) {
  return [record.id, record.display_name, record.name, ...toList(record.name_variants), record.orcid, record.openalex_author_id, ...Object.values(record.other_ids || {})];
}

function institutionSearchParts(record) {
  return [record.id, record.name, record.ror, record.openalex_id, record.city, record.country_code, record.parent_ror];
}

/**
 * Precomputes everything filtering needs, once per file load.
 * Returns items of shape {record, searchText, facetValues: {facetKey: [value]}, flagValues: {flagKey: bool}}.
 */
export function prepareRecords(records, tab, institutionIndex) {
  return records.map((record) => {
    let searchParts;
    if (tab.recordType === 'collaboration') {
      searchParts = collaborationSearchParts(record, institutionIndex);
    } else if (tab.recordType === 'person') {
      searchParts = personSearchParts(record);
    } else {
      searchParts = institutionSearchParts(record);
    }
    const facetValues = {};
    for (const facet of tab.facets) {
      facetValues[facet.key] = uniqueDefined(toList(facet.values(record)).map((value) => (typeof value === 'object' ? resourceLabel(value) : String(value))));
    }
    const flagValues = {};
    for (const flag of tab.flags) flagValues[flag.key] = flag.test(record);
    return {
      record,
      searchText: normalizeText(searchParts.filter((part) => part != null && part !== '').join(' \u0001 ')),
      facetValues,
      flagValues,
    };
  });
}

/** All distinct facet values over the whole file with their total counts: {facetKey: Map(value -> count)}. */
export function facetUniverse(items, tab) {
  const universe = {};
  for (const facet of tab.facets) universe[facet.key] = new Map();
  for (const item of items) {
    for (const facet of tab.facets) {
      const counts = universe[facet.key];
      for (const value of item.facetValues[facet.key]) counts.set(value, (counts.get(value) || 0) + 1);
    }
  }
  return universe;
}

export function emptyFilters() {
  return { text: '', yearMin: null, yearMax: null, facets: {}, flags: {} };
}

export function searchTokens(text) {
  return normalizeText(text).split(/\s+/).filter(Boolean);
}

/**
 * Filters items and computes live facet counts in a single pass.
 *
 * Facet counts follow the usual faceted-search convention: the count shown for a value of facet F is the number of
 * records that match every active filter EXCEPT F's own selection, so ticking a box never zeroes its siblings.
 * An item that fails exactly one filter therefore still contributes to that one filter's counts.
 *
 * filters: {text, yearMin, yearMax, facets: {facetKey: [selected values]}, flags: {flagKey: 'only' | 'exclude'}}
 */
export function applyFilters(items, tab, filters) {
  const tokens = searchTokens(filters.text);
  const activeFacets = tab.facets.filter((facet) => (filters.facets[facet.key] || []).length > 0)
    .map((facet) => ({ key: facet.key, selected: new Set(filters.facets[facet.key]) }));
  const activeFlags = tab.flags.filter((flag) => filters.flags[flag.key] === 'only' || filters.flags[flag.key] === 'exclude')
    .map((flag) => ({ key: flag.key, mode: filters.flags[flag.key] }));
  const yearMin = filters.yearMin ?? null;
  const yearMax = filters.yearMax ?? null;
  const hasYearFilter = (yearMin != null || yearMax != null) && tab.recordType === 'collaboration';

  const facetCounts = {};
  for (const facet of tab.facets) facetCounts[facet.key] = new Map();
  const flagCounts = {};
  for (const flag of tab.flags) flagCounts[flag.key] = 0;

  const matched = [];
  for (const item of items) {
    let failedFilterKey = null;
    let failureCount = 0;

    if (hasYearFilter) {
      const year = item.record.year;
      if (year == null || (yearMin != null && year < yearMin) || (yearMax != null && year > yearMax)) {
        failureCount = 2;
      }
    }
    if (failureCount < 2 && tokens.length > 0) {
      for (const token of tokens) {
        if (!item.searchText.includes(token)) { failureCount = 2; break; }
      }
    }
    for (let indexFacet = 0; failureCount < 2 && indexFacet < activeFacets.length; indexFacet += 1) {
      const facet = activeFacets[indexFacet];
      if (!item.facetValues[facet.key].some((value) => facet.selected.has(value))) {
        failureCount += 1;
        failedFilterKey = `facet:${facet.key}`;
      }
    }
    for (let indexFlag = 0; failureCount < 2 && indexFlag < activeFlags.length; indexFlag += 1) {
      const flag = activeFlags[indexFlag];
      const hasFlag = item.flagValues[flag.key];
      if ((flag.mode === 'only') !== hasFlag) {
        failureCount += 1;
        failedFilterKey = `flag:${flag.key}`;
      }
    }

    if (failureCount === 0) {
      matched.push(item);
      for (const facet of tab.facets) addFacetCounts(facetCounts[facet.key], item.facetValues[facet.key]);
      for (const flag of tab.flags) if (item.flagValues[flag.key]) flagCounts[flag.key] += 1;
    } else if (failureCount === 1) {
      if (failedFilterKey.startsWith('facet:')) {
        const facetKey = failedFilterKey.slice('facet:'.length);
        addFacetCounts(facetCounts[facetKey], item.facetValues[facetKey]);
      } else {
        const flagKey = failedFilterKey.slice('flag:'.length);
        if (item.flagValues[flagKey]) flagCounts[flagKey] += 1;
      }
    }
  }
  return { matched, facetCounts, flagCounts };
}

function addFacetCounts(counts, values) {
  for (const value of values) counts.set(value, (counts.get(value) || 0) + 1);
}

/** Stable sort by a column's value; null/empty values always sort last regardless of direction. */
export function sortItems(items, tab, sort) {
  const column = tab.columns.find((candidate) => candidate.key === sort?.key);
  if (!column) return items.slice();
  const directionSign = sort.direction === 'asc' ? 1 : -1;
  const decorated = items.map((item, position) => ({ item, position, value: column.value(item.record) }));
  decorated.sort((left, right) => {
    const leftEmpty = left.value == null || left.value === '';
    const rightEmpty = right.value == null || right.value === '';
    if (leftEmpty || rightEmpty) {
      if (leftEmpty && rightEmpty) return left.position - right.position;
      return leftEmpty ? 1 : -1;
    }
    let comparison;
    if (typeof left.value === 'number' && typeof right.value === 'number') comparison = left.value - right.value;
    else comparison = TEXT_COLLATOR.compare(String(left.value), String(right.value));
    return comparison !== 0 ? comparison * directionSign : left.position - right.position;
  });
  return decorated.map((entry) => entry.item);
}

export function paginate(items, page, pageSize = PAGE_SIZE) {
  const pageCount = Math.max(1, Math.ceil(items.length / pageSize));
  const currentPage = Math.min(Math.max(1, page || 1), pageCount);
  const start = (currentPage - 1) * pageSize;
  return { pageItems: items.slice(start, start + pageSize), currentPage, pageCount, start };
}

// ---------- hash routing ----------

/**
 * Routes:
 *   #/<tabKey>?q=..&year_min=..&year_max=..&f.<facetKey>=v&flag.<flagKey>=only|exclude&sort=..&dir=..&page=..
 *   #/person/<id>, #/institution/<id>, #/collaboration/<id>
 */
export function parseHash(hash) {
  const withoutPrefix = String(hash || '').replace(/^#\/?/, '');
  const questionMarkPosition = withoutPrefix.indexOf('?');
  const path = questionMarkPosition >= 0 ? withoutPrefix.slice(0, questionMarkPosition) : withoutPrefix;
  const query = questionMarkPosition >= 0 ? withoutPrefix.slice(questionMarkPosition + 1) : '';
  const slashPosition = path.indexOf('/');
  const head = slashPosition >= 0 ? path.slice(0, slashPosition) : path;
  const rest = slashPosition >= 0 ? path.slice(slashPosition + 1) : '';

  if (['person', 'institution', 'collaboration'].includes(head) && rest) {
    return { view: 'detail', detailType: head, id: safeDecode(rest) };
  }
  const tabKey = TAB_BY_KEY[head] ? head : TABS[0].key;
  return { view: 'list', tabKey, ...listStateFromParams(TAB_BY_KEY[tabKey], new URLSearchParams(query)) };
}

function safeDecode(text) {
  try { return decodeURIComponent(text); } catch { return text; }
}

export function listStateFromParams(tab, params) {
  const filters = emptyFilters();
  filters.text = params.get('q') || '';
  filters.yearMin = parseOptionalInteger(params.get('year_min'));
  filters.yearMax = parseOptionalInteger(params.get('year_max'));
  for (const facet of tab.facets) {
    const selected = params.getAll(`f.${facet.key}`);
    if (selected.length) filters.facets[facet.key] = selected;
  }
  for (const flag of tab.flags) {
    const mode = params.get(`flag.${flag.key}`);
    if (mode === 'only' || mode === 'exclude') filters.flags[flag.key] = mode;
  }
  const sortKey = params.get('sort');
  const sort = sortKey && tab.columns.some((column) => column.key === sortKey)
    ? { key: sortKey, direction: params.get('dir') === 'asc' ? 'asc' : 'desc' }
    : { ...tab.defaultSort };
  const page = parseOptionalInteger(params.get('page')) || 1;
  return { filters, sort, page };
}

function parseOptionalInteger(text) {
  if (text == null || text === '') return null;
  const value = Number.parseInt(text, 10);
  return Number.isFinite(value) ? value : null;
}

export function buildListHash(tabKey, listState) {
  const tab = TAB_BY_KEY[tabKey];
  const params = new URLSearchParams();
  const { filters, sort, page } = listState;
  if (filters.text) params.set('q', filters.text);
  if (filters.yearMin != null) params.set('year_min', String(filters.yearMin));
  if (filters.yearMax != null) params.set('year_max', String(filters.yearMax));
  for (const facet of tab.facets) {
    for (const value of filters.facets[facet.key] || []) params.append(`f.${facet.key}`, value);
  }
  for (const flag of tab.flags) {
    if (filters.flags[flag.key]) params.set(`flag.${flag.key}`, filters.flags[flag.key]);
  }
  if (sort && (sort.key !== tab.defaultSort.key || sort.direction !== tab.defaultSort.direction)) {
    params.set('sort', sort.key);
    params.set('dir', sort.direction);
  }
  if (page && page > 1) params.set('page', String(page));
  const query = params.toString();
  return `#/${tabKey}${query ? `?${query}` : ''}`;
}

export function buildDetailHash(detailType, id) {
  return `#/${detailType}/${encodeURIComponent(id)}`;
}

// ---------- external links ----------

/** True only for absolute http(s) URLs; anything else (javascript:, data:, relative) must not become an href. */
export function isHttpUrl(url) {
  return /^https?:\/\/[^\s]/i.test(String(url ?? '').trim());
}

function stripUrlPrefix(value, prefixes) {
  let text = String(value).trim();
  for (const prefix of prefixes) {
    if (text.toLowerCase().startsWith(prefix)) text = text.slice(prefix.length);
  }
  return text;
}

export function doiUrl(doi) {
  return `https://doi.org/${stripUrlPrefix(doi, ['https://doi.org/', 'http://doi.org/', 'http://dx.doi.org/', 'https://dx.doi.org/', 'doi:'])}`;
}

export function openalexUrl(openalexId) {
  return `https://openalex.org/${stripUrlPrefix(openalexId, ['https://openalex.org/', 'openalex:'])}`;
}

export function rorUrl(rorId) {
  return `https://ror.org/${stripUrlPrefix(rorId, ['https://ror.org/', 'ror:'])}`;
}

export function orcidUrl(orcid) {
  return `https://orcid.org/${stripUrlPrefix(orcid, ['https://orcid.org/', 'http://orcid.org/', 'orcid:'])}`;
}

export function nsfAwardUrl(awardNumber) {
  return `https://www.nsf.gov/awardsearch/showAward?AWD_ID=${encodeURIComponent(String(awardNumber).trim())}`;
}

function isNsfRecord(record) {
  return String(record.id || '').startsWith('award:nsf:') || /\bnsf\b|national science foundation/i.test(record.funder || '');
}

/** [{key, value, url|null}] for a collaboration's identifiers dict. */
export function identifierLinks(record) {
  const links = [];
  for (const [key, rawValue] of Object.entries(record.identifiers || {})) {
    for (const value of toList(rawValue)) {
      links.push({ key, value: String(value), url: identifierUrl(key, value, record) });
    }
  }
  return links;
}

function identifierUrl(key, value, record) {
  const lowerKey = key.toLowerCase();
  if (lowerKey === 'doi') return doiUrl(value);
  if (lowerKey === 'openalex' || lowerKey === 'openalex_id') return openalexUrl(value);
  if (lowerKey === 'ror') return rorUrl(value);
  if (lowerKey === 'orcid') return orcidUrl(value);
  if (lowerKey.includes('nsf') || (lowerKey === 'award_number' && isNsfRecord(record))) return nsfAwardUrl(value);
  if (/^https?:\/\//i.test(String(value))) return String(value);
  return null;
}

/** External link for an entity id such as ror:..., openalex:I..., orcid:..., or null for name:/nsf: ids. */
export function entityIdUrl(id) {
  const text = String(id || '');
  if (text.startsWith('ror:')) return rorUrl(text);
  if (text.startsWith('orcid:')) return orcidUrl(text);
  if (text.startsWith('openalex:')) return openalexUrl(text);
  return null;
}

// ---------- person / institution views ----------

/** Which collaboration files must be loaded to resolve these ids. */
export function tabKeysForCollaborationIds(collaborationIds) {
  return [...new Set((collaborationIds || []).map(tabKeyForCollaborationId).filter(Boolean))];
}

/**
 * A person's collaborations, each with the participant entries for that person AS LISTED on that record
 * (institution is per-collaboration, never a tracked personal location). Sorted by year ascending, undated last.
 * Returns {entries: [{collaboration, participations}], missingIds: [ids not found in loaded data]}.
 */
export function personTimeline(person, collaborationLookup) {
  const entries = [];
  const missingIds = [];
  for (const collaborationId of person.collaboration_ids || []) {
    const collaboration = collaborationLookup(collaborationId);
    if (!collaboration) { missingIds.push(collaborationId); continue; }
    const participations = (collaboration.participants || []).filter((participant) => participant.person_id === person.id);
    entries.push({ collaboration, participations });
  }
  entries.sort((left, right) => compareYearsThenTitle(left.collaboration, right.collaboration));
  return { entries, missingIds };
}

function compareYearsThenTitle(left, right) {
  const leftYear = left.year ?? Number.POSITIVE_INFINITY;
  const rightYear = right.year ?? Number.POSITIVE_INFINITY;
  if (leftYear !== rightYear) return leftYear - rightYear;
  return TEXT_COLLATOR.compare(String(left.title || ''), String(right.title || ''));
}

/**
 * Collaborations that list this institution (primary or additional affiliation), each with the participants
 * listed at it. Sorted by year descending, undated last.
 */
export function institutionCollaborations(institutionId, collaborations) {
  const entries = [];
  for (const collaboration of collaborations) {
    if (!(collaboration.institution_ids || []).includes(institutionId)) continue;
    const participantsHere = (collaboration.participants || []).filter((participant) =>
      participant.institution_id === institutionId || (participant.additional_institution_ids || []).includes(institutionId));
    entries.push({ collaboration, participants: participantsHere });
  }
  entries.sort((left, right) => {
    const leftUndated = left.collaboration.year == null;
    const rightUndated = right.collaboration.year == null;
    if (leftUndated !== rightUndated) return leftUndated ? 1 : -1;
    return compareYearsThenTitle(right.collaboration, left.collaboration);
  });
  return entries;
}

/** Institutions whose parent_ror points at this institution. */
export function childInstitutions(institution, institutions) {
  if (!institution.ror) return [];
  const rorId = stripUrlPrefix(institution.ror, ['https://ror.org/', 'ror:']);
  return institutions.filter((candidate) =>
    candidate.parent_ror && stripUrlPrefix(candidate.parent_ror, ['https://ror.org/', 'ror:']) === rorId);
}

export function parentInstitutionId(institution) {
  if (!institution.parent_ror) return null;
  return `ror:${stripUrlPrefix(institution.parent_ror, ['https://ror.org/', 'ror:'])}`;
}

export function formatMoney(amount) {
  if (amount == null || amount === '' || Number.isNaN(Number(amount))) return '';
  return `$${Math.round(Number(amount)).toLocaleString('en-US')}`;
}

/** Record count for a tab from meta.json (build.py writes meta.counts keyed by output file name). */
export function metaCountForTab(meta, tab) {
  const count = meta?.counts?.[tab.file];
  return typeof count === 'number' ? count : null;
}

/** meta.sources as written by build.py: [{source, retrieved_at, query_description}] -> [{name, retrievedAt, detail}]. */
export function metaSources(meta) {
  return (meta?.sources || []).map((source) => ({
    name: source.source,
    retrievedAt: source.retrieved_at || null,
    detail: source.query_description || null,
  }));
}
