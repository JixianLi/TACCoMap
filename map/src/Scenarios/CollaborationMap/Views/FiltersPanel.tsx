import { observer } from "mobx-react-lite";
import Autocomplete from "@mui/material/Autocomplete";
import Box from "@mui/material/Box";
import Checkbox from "@mui/material/Checkbox";
import FormControlLabel from "@mui/material/FormControlLabel";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import { Panel } from "@core/Panels/Panel";
import { useScenario } from "@core/ScenarioManager/ScenarioManager";
import type { CollaborationMapGlobalContext } from "../CollaborationMapGlobalContext";
import { INSTITUTION_TYPE_LABELS, RELATIONSHIP_TYPE_LABELS, countsByRelationshipType, type MapInstitution } from "../MapData";
import { placeLabel } from "./InstitutionTooltip";

const GROUPS = [
    { label: "Direct collaboration", description: "TACC people on the record, or time on TACC systems", direct: true },
    { label: "Indirect", description: "Mentions, acknowledgments, hosted data, subawards", direct: false },
];

const checkboxRow = { m: 0, width: 1, "& .MuiFormControlLabel-label": { flex: 1, display: "flex", justifyContent: "space-between", gap: 1 } };

export default observer(function FiltersPanel() {
    const context = useScenario().globalContext as CollaborationMapGlobalContext;
    const data = context.data;
    if (!data) return <Panel panelName="Filters" />;
    const relationshipTypes = data.relationship_types;
    const recordCounts = countsByRelationshipType(data.collaboration_counts,
        { ...context.filter, relationshipMask: ~0 }, relationshipTypes.length);
    const institutionTypeCounts = new Map<string, number>();
    for (const institution of data.institutions) {
        if (context.hubIds.has(institution.id)) continue;
        institutionTypeCounts.set(institution.institution_type, (institutionTypeCounts.get(institution.institution_type) ?? 0) + 1);
    }
    const caption = { display: "block", color: "text.secondary", mb: 0.25 };

    return (
        <Panel panelName="Filters">
            <Box sx={{ width: 1, display: "flex", flexDirection: "column", gap: 1.5 }}>
                <Autocomplete<MapInstitution> size="small" options={data.institutions}
                              getOptionLabel={institution => institution.name}
                              renderOption={(props, institution) => (
                                  <li {...props} key={institution.id}>
                                      <Box>
                                          <Typography variant="body2">{institution.name}</Typography>
                                          <Typography variant="caption" sx={{ color: "text.secondary" }}>
                                              {placeLabel(institution) || "location unknown"}
                                          </Typography>
                                      </Box>
                                  </li>
                              )}
                              onChange={(_, institution) => institution && context.selectInstitution(institution.id, true)}
                              renderInput={params => <TextField {...params} placeholder="Find an institution" />} />
                {GROUPS.map(group => {
                    const members = relationshipTypes.filter(relationshipType =>
                        data.direct_relationship_types.includes(relationshipType) === group.direct);
                    const enabledCount = members.filter(member => context.enabledRelationshipTypes.has(member)).length;
                    return (
                        <Box key={group.label}>
                            <FormControlLabel sx={checkboxRow} label={<b>{group.label}</b>} control={
                                <Checkbox size="small" checked={enabledCount === members.length}
                                          indeterminate={enabledCount > 0 && enabledCount < members.length}
                                          onChange={(_, checked) => context.setRelationshipTypes(members, checked)} />} />
                            <Typography variant="caption" sx={{ ...caption, ml: 3.5 }}>{group.description}</Typography>
                            {members.map(member => (
                                <FormControlLabel key={member} sx={{ ...checkboxRow, pl: 2 }} control={
                                    <Checkbox size="small" checked={context.enabledRelationshipTypes.has(member)}
                                              onChange={() => context.toggleRelationshipType(member)} />}
                                    label={<>
                                        <span>{RELATIONSHIP_TYPE_LABELS[member]}</span>
                                        <Typography component="span" variant="caption" sx={{ color: "text.secondary" }}>
                                            {recordCounts[relationshipTypes.indexOf(member)].toLocaleString()}
                                        </Typography>
                                    </>} />
                            ))}
                        </Box>
                    );
                })}
                <Box>
                    <Typography variant="caption" sx={caption}>Institution type (institutions)</Typography>
                    {[...institutionTypeCounts.entries()].sort((left, right) => right[1] - left[1]).map(([institutionType, count]) => (
                        <FormControlLabel key={institutionType} sx={checkboxRow} control={
                            <Checkbox size="small" checked={context.enabledInstitutionTypes.has(institutionType)}
                                      onChange={() => context.toggleInstitutionType(institutionType)} />}
                            label={<>
                                <span>{INSTITUTION_TYPE_LABELS[institutionType] ?? institutionType}</span>
                                <Typography component="span" variant="caption" sx={{ color: "text.secondary" }}>
                                    {count.toLocaleString()}
                                </Typography>
                            </>} />
                    ))}
                </Box>
            </Box>
        </Panel>
    );
});
