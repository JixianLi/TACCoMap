import { useRef, useState } from "react";
import { observer } from "mobx-react-lite";
import * as d3 from "d3";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Typography from "@mui/material/Typography";
import { useTheme } from "@mui/material/styles";
import { Panel } from "@core/Panels/Panel";
import { useScenario } from "@core/ScenarioManager/ScenarioManager";
import type { CollaborationMapGlobalContext } from "../CollaborationMapGlobalContext";
import { countsByYear } from "../MapData";

// Drawn in a WIDTH × HEIGHT box stretched to the panel (preserveAspectRatio none); labels are HTML placed by percentage.
const WIDTH = 1000;
const HEIGHT = 100;
const BAR_GAP = 0.2;

export default observer(function TimelinePanel() {
    const context = useScenario().globalContext as CollaborationMapGlobalContext;
    const years = context.years;
    return (
        <Panel panelName="Timeline" appbarContent={
            context.data && !context.isFullYearRange && (
                <Button size="small" onClick={() => context.setYearRange([years[0], years[years.length - 1]])}>All years</Button>
            )}>
            {context.data && <TimelineChart />}
        </Panel>
    );
});

// Rendered inside <Panel>, so useTheme() returns the panel's own tokens: in some WebUVisBox display modes (neutral),
// Panel gives control panels a theme of their own.
const TimelineChart = observer(function TimelineChart() {
    const context = useScenario().globalContext as CollaborationMapGlobalContext;
    const tokens = useTheme().tokens;
    const svgRef = useRef<SVGSVGElement>(null);
    const [dragStartYear, setDragStartYear] = useState<number | null>(null);
    const [hoveredYear, setHoveredYear] = useState<number | null>(null);
    const data = context.data!;
    const years = context.years;
    const totals = countsByYear(data.collaboration_counts, context.filter, years);
    const selected = context.selectedInstitution;
    const selectedCounts = selected ? countsByYear(selected.counts, context.filter, years) : null;
    const x = d3.scaleBand<number>().domain(years).range([0, WIDTH]).paddingInner(BAR_GAP);
    const y = d3.scaleLinear().domain([0, Math.max(1, d3.max(totals) ?? 1)]).nice().range([HEIGHT, 0]);
    const [startYear, endYear] = context.yearRange;
    const inRange = (year: number) => year >= startYear && year <= endYear;
    const accent = tokens.accentOnSurface;

    const yearAt = (clientX: number) => {
        const bounds = svgRef.current!.getBoundingClientRect();
        const fraction = Math.max(0, Math.min(0.9999, (clientX - bounds.left) / bounds.width));
        return years[Math.floor(fraction * years.length)];
    };
    const percentLeft = (year: number) => `${((x(year)! + x.bandwidth() / 2) / WIDTH) * 100}%`;
    const readoutYear = hoveredYear;
    const undatedNote = context.isFullYearRange ? "" : " · undated records hidden while a range is set";

    return (
            <Box sx={{ width: 1, display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
                <Typography variant="body2" sx={{ mb: 0.5 }}>
                    {readoutYear !== null
                        ? `${readoutYear}: ${totals[readoutYear - years[0]].toLocaleString()} collaborations` +
                          (selectedCounts ? ` · ${selected!.name}: ${selectedCounts[readoutYear - years[0]].toLocaleString()}` : "")
                        : `${startYear}–${endYear} · drag to choose years`}
                </Typography>
                <Box sx={{ position: "relative", flex: 1, minHeight: 40 }}>
                    <svg ref={svgRef} viewBox={`0 0 ${WIDTH} ${HEIGHT}`} preserveAspectRatio="none" width="100%" height="100%"
                         role="img" aria-label="Collaborations per year" style={{ display: "block", cursor: "col-resize", touchAction: "none" }}
                         onPointerDown={event => {
                             event.currentTarget.setPointerCapture(event.pointerId);
                             const year = yearAt(event.clientX);
                             setDragStartYear(year);
                             context.setYearRange([year, year]);
                         }}
                         onPointerMove={event => {
                             const year = yearAt(event.clientX);
                             setHoveredYear(year);
                             if (dragStartYear !== null) context.setYearRange([Math.min(dragStartYear, year), Math.max(dragStartYear, year)]);
                         }}
                         onPointerUp={() => setDragStartYear(null)}
                         onPointerLeave={() => setHoveredYear(null)}>
                        {y.ticks(3).map(tick => (
                            <line key={tick} x1={0} x2={WIDTH} y1={y(tick)} y2={y(tick)} stroke={tokens.border}
                                  vectorEffect="non-scaling-stroke" />
                        ))}
                        {years.map((year, index) => (
                            <rect key={year} x={x(year)} width={x.bandwidth()} y={y(totals[index])} height={HEIGHT - y(totals[index])}
                                  fill={inRange(year) ? tokens.textSecondary : tokens.border} />
                        ))}
                        {selectedCounts && years.map((year, index) => selectedCounts[index] > 0 && (
                            <rect key={`selected-${year}`} x={x(year)} width={x.bandwidth()} y={y(selectedCounts[index])}
                                  height={HEIGHT - y(selectedCounts[index])} fill={accent} opacity={inRange(year) ? 1 : 0.35} />
                        ))}
                    </svg>
                </Box>
                <Box sx={{ position: "relative", height: 16 }}>
                    {years.filter(year => year % 5 === 0 || year === years[0] || year === years[years.length - 1]).map(year => (
                        <Typography key={year} variant="caption" sx={{ position: "absolute", top: 2, left: percentLeft(year),
                                                                       transform: "translateX(-50%)", color: "text.secondary", lineHeight: 1 }}>
                            {year}
                        </Typography>
                    ))}
                </Box>
                <Typography variant="caption" sx={{ color: "text.secondary", mt: 0.5 }}>
                    All institutions; follows the collaboration-type filters, not institution type.
                    Allocations are current ACCESS allocations only, so recent years are inflated{undatedNote}.
                </Typography>
            </Box>
    );
});
