import { useEffect, useRef, useState } from "react";
import { observer } from "mobx-react-lite";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Link from "@mui/material/Link";
import Typography from "@mui/material/Typography";
import { useScenario } from "@core/ScenarioManager/ScenarioManager";
import type { CollaborationMapGlobalContext } from "../CollaborationMapGlobalContext";
import { RELATIONSHIP_TYPE_LABELS, type MapRecord } from "../MapData";
import { placeLabel } from "./InstitutionTooltip";

const KIND_LABELS: Record<string, string> = {
    paper: "Paper", award: "Award", subaward: "Subaward", dataset: "Dataset", allocation: "Allocation",
};
const PAGE_LENGTH = 50;
const PARTNER_PREVIEW_LENGTH = 10;

const sectionTitle = { display: "block", fontWeight: 600, mt: 2, mb: 0.5 };
const clickableRow = { cursor: "pointer", py: 0.5, borderRadius: 1, "&:hover": { bgcolor: "action.hover" } };

// Records with the same title, kind and year shown as one row: some sources publish a DOI per part of one piece of
// work (HuBMAP: one per tissue section, all titled after the donor). The records stay separate everywhere else.
function groupSameTitle(records: MapRecord[]): MapRecord[][] {
    const groups = new Map<string, MapRecord[]>();
    for (const record of records) {
        const key = record.title ? `${record.kindIndex}|${record.year}|${record.title.trim().toLowerCase()}` : record.id;
        const group = groups.get(key);
        if (group) group.push(record); else groups.set(key, [record]);
    }
    return [...groups.values()];
}

const RecordRow = observer(function RecordRow({ record, caption }: { record: MapRecord; caption?: string }) {
    const context = useScenario().globalContext as CollaborationMapGlobalContext;
    return (
        <Box sx={clickableRow} onClick={() => context.selectRecord(record.id)}>
            <Typography variant="caption" sx={{ display: "block", color: "text.secondary" }}>
                {caption ?? [record.year ?? "undated", kindLabel(context, record), `${record.institutionPeople.length} institutions`].join(" · ")}
            </Typography>
            {!caption && <Typography variant="body2" sx={{ lineHeight: 1.3 }}>{record.title ?? "(untitled)"}</Typography>}
        </Box>
    );
});

const RecordGroupRow = observer(function RecordGroupRow({ group }: { group: MapRecord[] }) {
    const context = useScenario().globalContext as CollaborationMapGlobalContext;
    const [expanded, setExpanded] = useState(false);
    const first = group[0];
    return (
        <Box>
            <Box sx={clickableRow} onClick={() => setExpanded(!expanded)}>
                <Typography variant="caption" sx={{ display: "block", color: "text.secondary" }}>
                    {[first.year ?? "undated", kindLabel(context, first), `${group.length} records with this title`].join(" · ")}
                    {expanded ? " ▾" : " ▸"}
                </Typography>
                <Typography variant="body2" sx={{ lineHeight: 1.3 }}>{first.title}</Typography>
            </Box>
            {expanded && (
                <Box sx={{ pl: 2 }}>
                    {group.map(record => (
                        <RecordRow key={record.id} record={record}
                                   caption={`${record.id.replace(/^[a-z]+:(doi:)?/, "")} · ${record.institutionPeople.length} institutions`} />
                    ))}
                </Box>
            )}
        </Box>
    );
});

function kindLabel(context: CollaborationMapGlobalContext, record: MapRecord): string {
    return KIND_LABELS[context.data?.kinds[record.kindIndex] ?? ""] ?? "";
}

// The selected institution's partners and collaborations (no collaboration selected).
export const CollaborationSection = observer(function CollaborationSection() {
    const context = useScenario().globalContext as CollaborationMapGlobalContext;
    const [shownCount, setShownCount] = useState(PAGE_LENGTH);
    if (context.recordsError) return <Typography variant="caption" sx={{ color: "error.main" }}>{context.recordsError}</Typography>;
    if (!context.records) return <Typography variant="caption" sx={{ display: "block", mt: 2, color: "text.secondary" }}>Loading collaborations…</Typography>;

    const records = context.selectedInstitutionRecords;
    const groups = groupSameTitle(records);
    const partners = context.partnerEdges;
    const partnerPeople = partners.reduce((sum, edge) => sum + edge.people, 0);
    return (
        <Box>
            <Typography variant="body2" sx={sectionTitle}>
                Partners: {partners.length.toLocaleString()} institutions, {partnerPeople.toLocaleString()} people
            </Typography>
            <Typography variant="caption" sx={{ display: "block", color: "text.secondary", mb: 0.5 }}>
                Institutions on the same collaborations (TACC and UT Austin left out), by people from each; a line on the map for each.
            </Typography>
            {partners.slice(0, PARTNER_PREVIEW_LENGTH).map(edge => (
                <Box key={edge.institution.id} sx={{ ...clickableRow, display: "flex", justifyContent: "space-between", gap: 1 }}
                     onClick={() => context.selectInstitution(edge.institution.id, true)}
                     onPointerEnter={() => context.setHoveredInstitution(edge.institution.id)}
                     onPointerLeave={() => context.setHoveredInstitution(null)}>
                    <Typography variant="body2" noWrap>{edge.institution.name}</Typography>
                    <Typography variant="body2" sx={{ flex: "none" }}>
                        {edge.people.toLocaleString()} people · {edge.sharedCollaborations.toLocaleString()}
                    </Typography>
                </Box>
            ))}

            <Typography variant="body2" sx={sectionTitle}>Collaborations ({records.length.toLocaleString()})</Typography>
            <Typography variant="caption" sx={{ display: "block", color: "text.secondary", mb: 0.5 }}>
                Newest first. Select one to see the institutions on it.
            </Typography>
            {groups.slice(0, shownCount).map(group => group.length === 1
                ? <RecordRow key={group[0].id} record={group[0]} />
                : <RecordGroupRow key={group[0].id} group={group} />)}
            {groups.length > shownCount && (
                <Button size="small" onClick={() => setShownCount(shownCount + PAGE_LENGTH)}>
                    Show {Math.min(PAGE_LENGTH, groups.length - shownCount)} more of {(groups.length - shownCount).toLocaleString()}
                </Button>
            )}
        </Box>
    );
});

// One collaboration: what it is, and every institution on it by people.
export const RecordDetail = observer(function RecordDetail({ record }: { record: MapRecord }) {
    const context = useScenario().globalContext as CollaborationMapGlobalContext;
    const relationshipTypes = context.data?.relationship_types ?? [];
    const types = relationshipTypes.filter((_, index) => record.relationshipMask & (1 << index))
        .map(relationshipType => RELATIONSHIP_TYPE_LABELS[relationshipType]);
    const people = record.institutionPeople.reduce((sum, [, count]) => sum + count, 0);
    // The panel keeps the list's scroll position; start each collaboration at its title.
    const topRef = useRef<HTMLDivElement>(null);
    useEffect(() => { topRef.current?.scrollIntoView({ block: "start" }); }, [record.id]);
    return (
        <Box ref={topRef}>
            <Button size="small" sx={{ mt: 1, ml: -1 }} onClick={() => context.selectRecord(null)}>← All collaborations</Button>
            <Typography variant="caption" sx={{ display: "block", color: "text.secondary" }}>
                {[record.year ?? "undated", kindLabel(context, record), ...types].join(" · ")}
            </Typography>
            <Typography variant="body2" sx={{ fontWeight: 600, lineHeight: 1.3 }}>{record.title ?? "(untitled)"}</Typography>
            {record.url && (
                <Link href={record.url} target="_blank" rel="noopener noreferrer" variant="body2">Source record</Link>
            )}
            <Typography variant="body2" sx={sectionTitle}>
                {record.institutionPeople.length} institutions, {people.toLocaleString()} people
            </Typography>
            {record.participantsTruncated && (
                <Typography variant="caption" sx={{ display: "block", color: "text.secondary", mb: 0.5 }}>
                    Author list truncated at the source: these counts are a lower bound.
                </Typography>
            )}
            {record.institutionPeople.map(([institutionId, count]) => {
                const institution = context.institutionById(institutionId);
                if (!institution) return null;
                const isSelected = institutionId === context.selectedInstitutionId;
                return (
                    <Box key={institutionId} sx={{ ...clickableRow, display: "flex", justifyContent: "space-between", gap: 1 }}
                         onClick={() => !isSelected && context.selectInstitution(institutionId, true)}
                         onPointerEnter={() => context.setHoveredInstitution(institutionId)}
                         onPointerLeave={() => context.setHoveredInstitution(null)}>
                        <Box sx={{ minWidth: 0 }}>
                            <Typography variant="body2" noWrap sx={{ fontWeight: isSelected ? 600 : 400 }}>{institution.name}</Typography>
                            <Typography variant="caption" noWrap sx={{ display: "block", color: "text.secondary" }}>
                                {placeLabel(institution) || "location unknown, not on the map"}
                            </Typography>
                        </Box>
                        <Typography variant="body2" sx={{ flex: "none" }}>
                            {count > 0 ? `${count} ${count === 1 ? "person" : "people"}` : "listed, no people"}
                        </Typography>
                    </Box>
                );
            })}
        </Box>
    );
});
