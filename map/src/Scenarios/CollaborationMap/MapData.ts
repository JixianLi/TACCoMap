// The map aggregate written by harvest/build_map.py (SCHEMA.md, "Map aggregate") and the counting rules over it.

// [year or null when undated, index into kinds, relationship mask, number of collaborations]
export type CountRow = [number | null, number, number, number];

export interface MapInstitution {
    id: string;
    name: string;
    city: string | null;
    country_code: string | null;
    institution_type: string;
    latitude: number | null;
    longitude: number | null;
    counts: CountRow[];
}

export interface MapAggregate {
    generated_at: string;
    kinds: string[];
    relationship_types: string[];
    direct_relationship_types: string[];
    hub_institution_ids: string[];
    collaboration_counts: CountRow[];
    institutions: MapInstitution[];
}

export interface CountFilter {
    // Bit i set <=> relationship_types[i] is shown; a collaboration counts when any of its types is shown.
    relationshipMask: number;
    // Inclusive; null ignores the year. Undated collaborations count only when the range is the whole span.
    yearRange: [number, number] | null;
    includeUndated: boolean;
}

export const FIRST_YEAR = 2001;

export const RELATIONSHIP_TYPE_LABELS: Record<string, string> = {
    staff_coauthor: "TACC staff co-authored",
    resource_acknowledgment: "Acknowledges TACC resources",
    tacc_lead_award: "TACC-led award",
    tacc_partner_award: "TACC partner award",
    tacc_mention_award: "Award mentioning TACC",
    subaward: "Subaward",
    tacc_hosted_data: "Data hosted at TACC",
    tacc_resource_allocation: "Allocation on TACC systems",
};

export const INSTITUTION_TYPE_LABELS: Record<string, string> = {
    educational: "Educational", research: "Research", government: "Government", commercial: "Commercial",
    nonprofit: "Nonprofit", healthcare: "Healthcare", archive: "Archive", other: "Other",
};

export async function loadMapAggregate(url: string): Promise<MapAggregate> {
    const response = await fetch(url);
    if (!response.ok) {
        throw new Error(`${url}: HTTP ${response.status}. Run harvest/build.py to write it.`);
    }
    return response.json();
}

export function rowPasses(row: CountRow, filter: CountFilter): boolean {
    const [year, , mask] = row;
    if ((mask & filter.relationshipMask) === 0) return false;
    if (filter.yearRange === null) return true;
    if (year === null) return filter.includeUndated;
    return year >= filter.yearRange[0] && year <= filter.yearRange[1];
}

export function countRows(rows: CountRow[], filter: CountFilter): number {
    let total = 0;
    for (const row of rows) {
        if (rowPasses(row, filter)) total += row[3];
    }
    return total;
}

export function countsByRelationshipType(rows: CountRow[], filter: CountFilter, typeCount: number): number[] {
    // A collaboration with several types is counted under each, so these can sum to more than the total.
    const counts = new Array(typeCount).fill(0);
    for (const row of rows) {
        if (!rowPasses(row, filter)) continue;
        for (let index = 0; index < typeCount; index++) {
            if (row[2] & (1 << index)) counts[index] += row[3];
        }
    }
    return counts;
}

export function countsByYear(rows: CountRow[], filter: CountFilter, years: number[]): number[] {
    const counts = new Array(years.length).fill(0);
    for (const row of rows) {
        const year = row[0];
        if (year === null || !rowPasses(row, { ...filter, yearRange: null })) continue;
        const index = year - years[0];
        if (index >= 0 && index < years.length) counts[index] += row[3];
    }
    return counts;
}

export function yearSpan(rows: CountRow[], filter: CountFilter): [number, number] | null {
    let first = Infinity;
    let last = -Infinity;
    for (const row of rows) {
        if (row[0] === null || !rowPasses(row, filter)) continue;
        first = Math.min(first, row[0]);
        last = Math.max(last, row[0]);
    }
    return first === Infinity ? null : [first, last];
}

// map_records.json (harvest/build_map.py, SCHEMA.md "Map records"): one row per collaboration, as a positional array
// [id, kind index, year or null, relationship mask, title, url, [[institution id, people], ...], participants truncated].
export type MapRecordRow = [string, number, number | null, number, string | null, string | null, [string, number][], boolean];

export interface MapRecord {
    id: string;
    kindIndex: number;
    year: number | null;
    relationshipMask: number;
    title: string | null;
    url: string | null;
    // Number of people from each institution on this collaboration, most first; 0 when the source names the
    // institution but no person under it.
    institutionPeople: [string, number][];
    participantsTruncated: boolean;
}

export async function loadMapRecords(url: string): Promise<MapRecord[]> {
    const response = await fetch(url);
    if (!response.ok) {
        throw new Error(`${url}: HTTP ${response.status}. Run harvest/build.py to write it.`);
    }
    const payload: { records: MapRecordRow[] } = await response.json();
    return payload.records.map(([id, kindIndex, year, relationshipMask, title, url, institutionPeople, participantsTruncated]) =>
        ({ id, kindIndex, year, relationshipMask, title, url, institutionPeople, participantsTruncated }));
}

export function recordPasses(record: MapRecord, filter: CountFilter): boolean {
    return rowPasses([record.year, record.kindIndex, record.relationshipMask, 1], filter);
}
