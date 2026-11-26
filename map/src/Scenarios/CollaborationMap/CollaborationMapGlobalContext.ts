import { makeAutoObservable, observable, runInAction } from "mobx";
import type { GlobalContext } from "@core/Types/GlobalContext";
import {
    FIRST_YEAR, countRows, loadMapAggregate, loadMapRecords, recordPasses, type CountFilter, type MapAggregate,
    type MapInstitution, type MapRecord,
} from "./MapData";

export interface InstitutionCount {
    institution: MapInstitution;
    count: number;
    // Same filters but every year: fixes the size scale so brushing the timeline does not rescale the circles.
    countAllYears: number;
}

// A line from the selected institution to another one: the people from that institution (on the selected
// collaboration, or summed over the selected institution's collaborations), and how many collaborations they share.
export interface PartnerEdge {
    institution: MapInstitution;
    people: number;
    sharedCollaborations: number;
}

export class CollaborationMapGlobalContext implements GlobalContext {
    dataUrl = "data/map_institutions.json";
    recordsUrl = "data/map_records.json";
    records: MapRecord[] | null = null;
    recordsLoading = false;
    recordsError: string | null = null;
    selectedRecordId: string | null = null;
    // The record browser is not published; only a dev session links to a local copy (CLAUDE.md "Web UI").
    browserBaseUrl: string | null = import.meta.env.DEV ? "http://localhost:8000/" : null;
    data: MapAggregate | null = null;
    loadError: string | null = null;

    enabledRelationshipTypes = new Set<string>();
    enabledInstitutionTypes = new Set<string>();
    yearRange: [number, number] = [FIRST_YEAR, FIRST_YEAR];
    selectedInstitutionId: string | null = null;
    hoveredInstitutionId: string | null = null;
    // Bumped to ask the map to zoom to the selection (a search pick) without zooming on every map click.
    focusRequest = 0;

    constructor() {
        makeAutoObservable(this, { data: observable.ref, records: observable.ref });
    }

    initialize(globalData: any): void {
        this.dataUrl = globalData?.data_url ?? this.dataUrl;
        this.recordsUrl = globalData?.records_url ?? this.recordsUrl;
    }

    async asyncInitialize(): Promise<void> {
        try {
            const data = await loadMapAggregate(this.dataUrl);
            runInAction(() => {
                this.data = data;
                this.enabledRelationshipTypes = new Set(data.direct_relationship_types);
                this.enabledInstitutionTypes = new Set(data.institutions.map(institution => institution.institution_type));
                this.yearRange = [this.years[0], this.years[this.years.length - 1]];
            });
        } catch (error) {
            runInAction(() => { this.loadError = String(error); });
        }
    }

    toObject(): any {
        return { data_url: this.dataUrl, records_url: this.recordsUrl };
    }

    get years(): number[] {
        if (!this.data) return [FIRST_YEAR];
        let last = FIRST_YEAR;
        for (const row of this.data.collaboration_counts) {
            if (row[0] !== null) last = Math.max(last, row[0]);
        }
        return Array.from({ length: last - FIRST_YEAR + 1 }, (_, index) => FIRST_YEAR + index);
    }

    get isFullYearRange(): boolean {
        const years = this.years;
        return this.yearRange[0] === years[0] && this.yearRange[1] === years[years.length - 1];
    }

    get relationshipMask(): number {
        if (!this.data) return 0;
        return this.data.relationship_types.reduce(
            (mask, relationshipType, index) => this.enabledRelationshipTypes.has(relationshipType) ? mask | (1 << index) : mask, 0);
    }

    get filter(): CountFilter {
        return { relationshipMask: this.relationshipMask, yearRange: this.yearRange, includeUndated: this.isFullYearRange };
    }

    get filterAllYears(): CountFilter {
        return { relationshipMask: this.relationshipMask, yearRange: null, includeUndated: true };
    }

    get hubIds(): Set<string> {
        return new Set(this.data?.hub_institution_ids ?? []);
    }

    get hubInstitutions(): MapInstitution[] {
        return this.data?.institutions.filter(institution => this.hubIds.has(institution.id)) ?? [];
    }

    // Collaborator institutions passing the filters, largest first (so smaller circles are drawn on top).
    get institutionCounts(): InstitutionCount[] {
        if (!this.data) return [];
        const filter = this.filter;
        const filterAllYears = this.filterAllYears;
        const result: InstitutionCount[] = [];
        for (const institution of this.data.institutions) {
            if (this.hubIds.has(institution.id) || !this.enabledInstitutionTypes.has(institution.institution_type)) continue;
            const count = countRows(institution.counts, filter);
            if (count > 0) result.push({ institution, count, countAllYears: countRows(institution.counts, filterAllYears) });
        }
        return result.sort((left, right) => right.count - left.count);
    }

    get mappedInstitutionCounts(): InstitutionCount[] {
        return this.institutionCounts.filter(entry => entry.institution.latitude !== null && entry.institution.longitude !== null);
    }

    get unmappedInstitutionCount(): number {
        return this.institutionCounts.length - this.mappedInstitutionCounts.length;
    }

    get maxCountAllYears(): number {
        return this.institutionCounts.reduce((max, entry) => Math.max(max, entry.countAllYears), 1);
    }

    get selectedInstitution(): MapInstitution | null {
        return this.data?.institutions.find(institution => institution.id === this.selectedInstitutionId) ?? null;
    }

    get hoveredInstitution(): MapInstitution | null {
        return this.data?.institutions.find(institution => institution.id === this.hoveredInstitutionId) ?? null;
    }

    toggleRelationshipType(relationshipType: string): void {
        const next = new Set(this.enabledRelationshipTypes);
        if (next.has(relationshipType)) next.delete(relationshipType); else next.add(relationshipType);
        this.enabledRelationshipTypes = next;
    }

    setRelationshipTypes(relationshipTypes: string[], enabled: boolean): void {
        const next = new Set(this.enabledRelationshipTypes);
        relationshipTypes.forEach(relationshipType => enabled ? next.add(relationshipType) : next.delete(relationshipType));
        this.enabledRelationshipTypes = next;
    }

    toggleInstitutionType(institutionType: string): void {
        const next = new Set(this.enabledInstitutionTypes);
        if (next.has(institutionType)) next.delete(institutionType); else next.add(institutionType);
        this.enabledInstitutionTypes = next;
    }

    setYearRange(yearRange: [number, number]): void {
        this.yearRange = yearRange;
    }

    selectInstitution(institutionId: string | null, focus = false): void {
        this.selectedInstitutionId = institutionId;
        this.selectedRecordId = null;
        if (focus && institutionId) this.focusRequest += 1;
        if (institutionId) void this.ensureRecordsLoaded();
    }

    selectRecord(recordId: string | null): void {
        this.selectedRecordId = recordId;
    }

    // Loaded on the first selection, not with the page: it is several times the size of the counts.
    async ensureRecordsLoaded(): Promise<void> {
        if (this.records || this.recordsLoading) return;
        this.recordsLoading = true;
        try {
            const records = await loadMapRecords(this.recordsUrl);
            runInAction(() => { this.records = records; });
        } catch (error) {
            runInAction(() => { this.recordsError = String(error); });
        } finally {
            runInAction(() => { this.recordsLoading = false; });
        }
    }

    get recordsByInstitution(): Map<string, MapRecord[]> {
        const index = new Map<string, MapRecord[]>();
        for (const record of this.records ?? []) {
            for (const [institutionId] of record.institutionPeople) {
                const list = index.get(institutionId);
                if (list) list.push(record); else index.set(institutionId, [record]);
            }
        }
        return index;
    }

    institutionById(institutionId: string): MapInstitution | undefined {
        return this.institutionIndex.get(institutionId);
    }

    get institutionIndex(): Map<string, MapInstitution> {
        return new Map((this.data?.institutions ?? []).map(institution => [institution.id, institution]));
    }

    // The selected institution's collaborations under the current filters, newest first.
    get selectedInstitutionRecords(): MapRecord[] {
        if (!this.selectedInstitutionId) return [];
        const filter = this.filter;
        return (this.recordsByInstitution.get(this.selectedInstitutionId) ?? [])
            .filter(record => recordPasses(record, filter))
            .sort((left, right) => (right.year ?? 0) - (left.year ?? 0) || (left.title ?? "").localeCompare(right.title ?? ""));
    }

    get selectedRecord(): MapRecord | null {
        return this.records?.find(record => record.id === this.selectedRecordId) ?? null;
    }

    // With a collaboration selected: every other institution on it (the hub too, as one line to Austin). Otherwise:
    // every institution sharing at least one of the selected institution's collaborations, people summed (the hub
    // left out: the arc to Austin stands for it).
    get partnerEdges(): PartnerEdge[] {
        const selectedId = this.selectedInstitutionId;
        if (!selectedId) return [];
        const records = this.selectedRecord ? [this.selectedRecord] : this.selectedInstitutionRecords;
        const totals = new Map<string, PartnerEdge>();
        for (const record of records) {
            for (const [institutionId, people] of record.institutionPeople) {
                if (institutionId === selectedId || (!this.selectedRecord && this.hubIds.has(institutionId))) continue;
                const institution = this.institutionById(institutionId);
                if (!institution) continue;
                const edge = totals.get(institutionId) ?? { institution, people: 0, sharedCollaborations: 0 };
                edge.people += people;
                edge.sharedCollaborations += 1;
                totals.set(institutionId, edge);
            }
        }
        return [...totals.values()].sort((left, right) => right.people - left.people);
    }

    setHoveredInstitution(institutionId: string | null): void {
        this.hoveredInstitutionId = institutionId;
    }

    browserUrl(institutionId: string): string | null {
        if (this.browserBaseUrl === null) return null;
        return `${this.browserBaseUrl}#/institution/${encodeURIComponent(institutionId)}`;
    }
}
