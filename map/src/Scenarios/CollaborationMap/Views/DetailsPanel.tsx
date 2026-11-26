import { observer } from "mobx-react-lite";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Link from "@mui/material/Link";
import Typography from "@mui/material/Typography";
import { Panel } from "@core/Panels/Panel";
import { useScenario } from "@core/ScenarioManager/ScenarioManager";
import type { CollaborationMapGlobalContext } from "../CollaborationMapGlobalContext";
import {
    INSTITUTION_TYPE_LABELS, RELATIONSHIP_TYPE_LABELS, countRows, countsByRelationshipType, yearSpan,
} from "../MapData";
import { placeLabel } from "./InstitutionTooltip";
import { CollaborationSection, RecordDetail } from "./CollaborationList";

const RANKING_LENGTH = 15;

export default observer(function DetailsPanel() {
    const context = useScenario().globalContext as CollaborationMapGlobalContext;
    const data = context.data;
    if (!data) return <Panel panelName="Details" />;
    const selected = context.selectedInstitution;
    const filter = context.filter;

    if (!selected) {
        const ranking = context.institutionCounts.slice(0, RANKING_LENGTH);
        const top = ranking[0]?.count ?? 1;
        return (
            <Panel panelName="Details">
                <Box sx={{ width: 1 }}>
                    <Typography variant="caption" sx={{ display: "block", color: "text.secondary", mb: 1 }}>
                        Top institutions under the current filters (TACC and UT Austin excluded). Click one, or a circle on the map.
                    </Typography>
                    {ranking.map(({ institution, count }) => (
                        <Box key={institution.id} onClick={() => context.selectInstitution(institution.id, true)}
                             onPointerEnter={() => context.setHoveredInstitution(institution.id)}
                             onPointerLeave={() => context.setHoveredInstitution(null)}
                             sx={{ cursor: "pointer", mb: 0.75, "&:hover .bar": { bgcolor: theme => theme.tokens.accentOnSurface } }}>
                            <Box sx={{ display: "flex", justifyContent: "space-between", gap: 1 }}>
                                <Typography variant="body2" noWrap>{institution.name}</Typography>
                                <Typography variant="body2">{count.toLocaleString()}</Typography>
                            </Box>
                            <Box className="bar" sx={{ height: 4, borderRadius: 2, bgcolor: theme => theme.tokens.textSecondary, width: `${(count / top) * 100}%` }} />
                        </Box>
                    ))}
                </Box>
            </Panel>
        );
    }

    const relationshipTypes = data.relationship_types;
    const byType = countsByRelationshipType(selected.counts, filter, relationshipTypes.length);
    const span = yearSpan(selected.counts, filter);
    const total = countRows(selected.counts, filter);
    const browserUrl = context.browserUrl(selected.id);
    const selectedRecord = context.selectedRecord;
    return (
        <Panel panelName="Details" appbarContent={<Button size="small" onClick={() => context.selectInstitution(null)}>Clear</Button>}>
            <Box sx={{ width: 1 }}>
                <Typography variant="subtitle1" sx={{ fontWeight: 600, lineHeight: 1.3 }}>{selected.name}</Typography>
                <Typography variant="caption" sx={{ display: "block", color: "text.secondary" }}>
                    {[placeLabel(selected) || "Location unknown, not on the map", INSTITUTION_TYPE_LABELS[selected.institution_type]]
                        .filter(Boolean).join(" · ")}
                </Typography>
                {context.hubIds.has(selected.id) && (
                    <Typography variant="caption" sx={{ display: "block", color: "text.secondary" }}>Drawn as the TACC hub.</Typography>
                )}
                {selectedRecord ? <RecordDetail record={selectedRecord} /> : <>
                <Typography variant="body2" sx={{ mt: 1 }}>
                    <b>{total.toLocaleString()}</b> collaborations under the current filters{span ? `, ${span[0]}–${span[1]}` : ""}
                </Typography>
                <Box sx={{ mt: 1 }}>
                    {relationshipTypes.map((relationshipType, index) => byType[index] > 0 && (
                        <Box key={relationshipType} sx={{ display: "flex", justifyContent: "space-between", gap: 1 }}>
                            <Typography variant="body2" sx={{ color: "text.secondary" }}>{RELATIONSHIP_TYPE_LABELS[relationshipType]}</Typography>
                            <Typography variant="body2">{byType[index].toLocaleString()}</Typography>
                        </Box>
                    ))}
                </Box>
                <Typography variant="caption" sx={{ display: "block", color: "text.secondary", mt: 0.5 }}>
                    A record with several types counts under each.
                </Typography>
                {browserUrl && (
                    <Link href={browserUrl} target="_blank" rel="noopener noreferrer"
                          variant="body2" sx={{ display: "inline-block", mt: 1.5 }}>
                        Open the records in the browser
                    </Link>
                )}
                <CollaborationSection key={selected.id} />
                </>}
            </Box>
        </Panel>
    );
});
