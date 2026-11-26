import type React from "react";
import MapPanel from "./Views/MapPanel";
import FiltersPanel from "./Views/FiltersPanel";
import DetailsPanel from "./Views/DetailsPanel";
import TimelinePanel from "./Views/TimelinePanel";

export function collaborationMapPanelMappingFunction(viewName: string): React.ReactNode {
    switch (viewName) {
        case "Map": return <MapPanel />;
        case "Filters": return <FiltersPanel />;
        case "Details": return <DetailsPanel />;
        case "Timeline": return <TimelinePanel />;
        default: throw new Error(`Unknown view: ${viewName}`);
    }
}
