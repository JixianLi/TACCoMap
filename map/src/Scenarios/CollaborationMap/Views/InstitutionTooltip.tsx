import { observer } from "mobx-react-lite";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";
import { useTheme } from "@mui/material/styles";
import { useScenario } from "@core/ScenarioManager/ScenarioManager";
import type { CollaborationMapGlobalContext } from "../CollaborationMapGlobalContext";
import {
    INSTITUTION_TYPE_LABELS, RELATIONSHIP_TYPE_LABELS, countRows, countsByRelationshipType, yearSpan, type MapInstitution,
} from "../MapData";

const TOOLTIP_WIDTH = 260;
const countryNames = new Intl.DisplayNames(["en"], { type: "region" });

export function placeLabel(institution: MapInstitution): string {
    const country = institution.country_code ? countryNames.of(institution.country_code) : null;
    return [institution.city, country].filter(Boolean).join(", ");
}

export default observer(function InstitutionTooltip({ institution, x, y, containerWidth, containerHeight }: {
    institution: MapInstitution; x: number; y: number; containerWidth: number; containerHeight: number;
}) {
    const context = useScenario().globalContext as CollaborationMapGlobalContext;
    const tokens = useTheme().tokens;
    const relationshipTypes = context.data?.relationship_types ?? [];
    const filter = context.filter;
    const total = countRows(institution.counts, filter);
    const byType = countsByRelationshipType(institution.counts, filter, relationshipTypes.length);
    const span = yearSpan(institution.counts, filter);
    const flipLeft = x + 16 + TOOLTIP_WIDTH > containerWidth;
    const isHub = context.hubIds.has(institution.id);
    return (
        <Box sx={{ position: "absolute", pointerEvents: "none", width: TOOLTIP_WIDTH, zIndex: 2,
                   left: flipLeft ? x - 16 - TOOLTIP_WIDTH : x + 16, top: Math.min(Math.max(8, y - 20), containerHeight - 160),
                   bgcolor: tokens.surfaceRaised, border: `1px solid ${tokens.border}`, borderRadius: 1.5,
                   boxShadow: tokens.shadowRaised, p: 1.25 }}>
            <Typography variant="body2" sx={{ fontWeight: 600, lineHeight: 1.3 }}>{institution.name}</Typography>
            <Typography variant="caption" sx={{ display: "block", color: "text.secondary" }}>
                {[placeLabel(institution), INSTITUTION_TYPE_LABELS[institution.institution_type]].filter(Boolean).join(" · ")}
                {isHub && " · drawn as the TACC hub"}
            </Typography>
            <Typography variant="body2" sx={{ mt: 0.75 }}>
                <b>{total.toLocaleString()}</b> collaborations{span ? `, ${span[0]}–${span[1]}` : ""}
            </Typography>
            {relationshipTypes.map((relationshipType, index) => byType[index] > 0 && (
                <Box key={relationshipType} sx={{ display: "flex", justifyContent: "space-between", gap: 1 }}>
                    <Typography variant="caption" sx={{ color: "text.secondary" }}>{RELATIONSHIP_TYPE_LABELS[relationshipType]}</Typography>
                    <Typography variant="caption">{byType[index].toLocaleString()}</Typography>
                </Box>
            ))}
        </Box>
    );
});
