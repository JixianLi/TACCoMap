import { scenarioRegistry } from "@core/Scenarios/ScenarioRegistry";
import { CollaborationMapGlobalContext } from "./CollaborationMapGlobalContext";
import { collaborationMapPanelMappingFunction } from "./collaborationMapPanelMappingFunction";
import COLLABORATION_MAP_CONFIG from "./CollaborationMapConfig.json";

scenarioRegistry.register({
    name: COLLABORATION_MAP_CONFIG.name,
    description: COLLABORATION_MAP_CONFIG.description,
    createGlobalContext: () => new CollaborationMapGlobalContext(),
    panelMapping: collaborationMapPanelMappingFunction,
    defaultConfig: COLLABORATION_MAP_CONFIG,
});

export { COLLABORATION_MAP_CONFIG };
