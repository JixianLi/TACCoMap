import { useEffect, useMemo, useRef, useState } from "react";
import { observer } from "mobx-react-lite";
import * as d3 from "d3";
import { feature, mesh } from "topojson-client";
import type { GeometryCollection, Topology } from "topojson-specification";
import worldTopology from "world-atlas/countries-110m.json";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Typography from "@mui/material/Typography";
import { useTheme } from "@mui/material/styles";
import { Panel } from "@core/Panels/Panel";
import { useScenario } from "@core/ScenarioManager/ScenarioManager";
import type { CollaborationMapGlobalContext } from "../CollaborationMapGlobalContext";
import { countRows } from "../MapData";
import InstitutionTooltip from "./InstitutionTooltip";

// The poles are left out: no institution lies beyond 46°S or 70°N, and Mercator stretches them without bound.
const MAP_BOUNDS: [[number, number], [number, number]] = [[-180, -58], [180, 78]];
// The view the map opens on: the contiguous United States (west, north, east, south).
const US_BOUNDS: [[number, number], [number, number]] = [[-125, 50], [-66, 24]];
const MAX_ZOOM = 60;
const MIN_RADIUS = 1.5;
const MAX_RADIUS = 22;
const HUB_RADIUS = 9;
const MAX_ARC_WIDTH = 6;

const topology = worldTopology as unknown as Topology<{ countries: GeometryCollection }>;
const land = feature(topology, topology.objects.countries);
const borders = mesh(topology, topology.objects.countries, (left, right) => left !== right);

function starPath(radius: number): string {
    const points = d3.range(10).map(index => {
        const angle = (index * Math.PI) / 5 - Math.PI / 2;
        const length = index % 2 === 0 ? radius : radius * 0.45;
        return [Math.cos(angle) * length, Math.sin(angle) * length];
    });
    return `M${points.map(point => point.join(",")).join("L")}Z`;
}

function boundsTransform(projection: d3.GeoProjection, bounds: [[number, number], [number, number]],
                         width: number, height: number): d3.ZoomTransform {
    const [x0, y0] = projection(bounds[0])!;
    const [x1, y1] = projection(bounds[1])!;
    const scale = Math.min(MAX_ZOOM, 0.92 / Math.max((x1 - x0) / width, (y1 - y0) / height));
    return d3.zoomIdentity.translate(width / 2, height / 2).scale(scale).translate(-(x0 + x1) / 2, -(y0 + y1) / 2);
}

export default observer(function MapPanel() {
    const context = useScenario().globalContext as CollaborationMapGlobalContext;
    const tokens = useTheme().tokens;
    // Elements held in state through callback refs, not useRef: Panel can rebuild its content's DOM (WebUVisBox wraps it
    // in a ThemeProvider in some display modes), and the observer and zoom must follow the element that exists now.
    const [container, setContainer] = useState<HTMLDivElement | null>(null);
    const [svgElement, setSvgElement] = useState<SVGSVGElement | null>(null);
    const zoomRef = useRef<d3.ZoomBehavior<SVGSVGElement, unknown> | null>(null);
    const [size, setSize] = useState({ width: 0, height: 0 });
    const [transform, setTransform] = useState(d3.zoomIdentity);

    useEffect(() => {
        if (!container) return;
        const observer = new ResizeObserver(([entry]) =>
            setSize({ width: entry.contentRect.width, height: entry.contentRect.height }));
        observer.observe(container);
        return () => observer.disconnect();
    }, [container]);

    const { width, height } = size;
    const projection = useMemo(() => d3.geoMercator().fitSize([Math.max(width, 1), Math.max(height, 1)],
        { type: "MultiPoint", coordinates: MAP_BOUNDS }), [width, height]);
    const path = useMemo(() => d3.geoPath(projection), [projection]);

    const zoomTo = (bounds: [[number, number], [number, number]] | null, animate = true) => {
        if (!svgElement || !zoomRef.current || width === 0) return;
        const target = bounds ? boundsTransform(projection, bounds, width, height) : d3.zoomIdentity;
        const selection = d3.select(svgElement);
        (animate ? selection.transition().duration(600) : selection).call(zoomRef.current.transform as any, target);
    };

    useEffect(() => {
        const svg = svgElement;
        if (!svg || width === 0) return;
        const [[x0, y0], [x1, y1]] = [projection(MAP_BOUNDS[0])!, projection(MAP_BOUNDS[1])!];
        const zoom = d3.zoom<SVGSVGElement, unknown>()
            .scaleExtent([1, MAX_ZOOM])
            .extent([[0, 0], [width, height]])
            .translateExtent([[x0, y1], [x1, y0]])
            .on("zoom", event => setTransform(event.transform));
        zoomRef.current = zoom;
        d3.select(svg).call(zoom).on("dblclick.zoom", null);
        zoomTo(US_BOUNDS, false);
        return () => { d3.select(svg).on(".zoom", null); };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [svgElement, projection, width, height]);

    const selected = context.selectedInstitution;
    useEffect(() => {
        if (!selected || selected.longitude === null || selected.latitude === null) return;
        const pad = 6;
        zoomTo([[selected.longitude - pad, selected.latitude + pad / 2], [selected.longitude + pad, selected.latitude - pad / 2]]);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [context.focusRequest]);

    const radius = d3.scaleSqrt().domain([0, context.maxCountAllYears]).range([0, MAX_RADIUS]);
    const accent = tokens.accentOnSurface;
    const hubInstitution = context.hubInstitutions.find(institution => institution.latitude !== null);
    const hubPoint = hubInstitution ? projection([hubInstitution.longitude!, hubInstitution.latitude!]) : null;
    const screen = (longitude: number, latitude: number) => transform.apply(projection([longitude, latitude])!);

    const selectedCount = selected ? countRows(selected.counts, context.filter) : 0;
    const arcWidth = d3.scaleSqrt().domain([0, context.maxCountAllYears]).range([1, MAX_ARC_WIDTH]);
    const arcPath = selected && hubInstitution && selected.longitude !== null && !context.hubIds.has(selected.id)
        ? path({ type: "LineString", coordinates: [[hubInstitution.longitude!, hubInstitution.latitude!], [selected.longitude, selected.latitude!]] })
        : null;

    const hovered = context.hoveredInstitution;
    const hoveredPoint = hovered && hovered.longitude !== null ? screen(hovered.longitude, hovered.latitude!) : null;
    const highlightIds = new Set([context.selectedInstitutionId, context.hoveredInstitutionId]);

    return (
        <Panel panelName="Map" scene>
            <Box ref={setContainer} sx={{ position: "relative", width: 1, flex: 1, minHeight: 0, overflow: "hidden",
                                          bgcolor: tokens.canvas }}>
                <svg ref={setSvgElement} width={width} height={height} role="img"
                     aria-label="Institutions collaborating with TACC on a Mercator world map"
                     style={{ display: "block", cursor: "grab", touchAction: "none" }}
                     onClick={event => { if (event.target === event.currentTarget || (event.target as SVGElement).dataset.land) context.selectInstitution(null); }}>
                    <g transform={transform.toString()}>
                        <path data-land="1" d={path(land) ?? undefined} fill={tokens.surface} />
                        <path d={path(borders) ?? undefined} fill="none" stroke={tokens.border} strokeWidth={0.75}
                              vectorEffect="non-scaling-stroke" pointerEvents="none" />
                        {arcPath && (
                            <path d={arcPath} fill="none" stroke={accent} strokeOpacity={0.85} strokeLinecap="round"
                                  strokeWidth={arcWidth(selectedCount)} vectorEffect="non-scaling-stroke" pointerEvents="none" />
                        )}
                    </g>
                    <g>
                        {context.mappedInstitutionCounts.map(({ institution, count }) => {
                            const [x, y] = screen(institution.longitude!, institution.latitude!);
                            if (x < -MAX_RADIUS || y < -MAX_RADIUS || x > width + MAX_RADIUS || y > height + MAX_RADIUS) return null;
                            const highlighted = highlightIds.has(institution.id);
                            return (
                                <circle key={institution.id} cx={x} cy={y} r={Math.max(MIN_RADIUS, radius(count))}
                                        fill={highlighted ? accent : tokens.text} fillOpacity={highlighted ? 0.9 : 0.28}
                                        stroke={highlighted ? accent : tokens.text} strokeOpacity={highlighted ? 1 : 0.55}
                                        strokeWidth={highlighted ? 1.5 : 0.6} style={{ cursor: "pointer" }}
                                        onPointerEnter={() => context.setHoveredInstitution(institution.id)}
                                        onPointerLeave={() => context.setHoveredInstitution(null)}
                                        onClick={() => context.selectInstitution(institution.id)} />
                            );
                        })}
                    </g>
                    {hubPoint && hubInstitution && (() => {
                        const [x, y] = transform.apply(hubPoint);
                        return (
                            <path d={starPath(HUB_RADIUS)} transform={`translate(${x},${y})`} fill={accent}
                                  stroke={tokens.canvas} strokeWidth={1.5} style={{ cursor: "pointer" }}
                                  onPointerEnter={() => context.setHoveredInstitution(hubInstitution.id)}
                                  onPointerLeave={() => context.setHoveredInstitution(null)}
                                  onClick={() => context.selectInstitution(hubInstitution.id)} />
                        );
                    })()}
                </svg>
                {hovered && hoveredPoint && <InstitutionTooltip institution={hovered} x={hoveredPoint[0]} y={hoveredPoint[1]}
                                                                containerWidth={width} containerHeight={height} />}
                <Box sx={{ position: "absolute", top: 8, left: 8, display: "flex", gap: 0.5 }}>
                    <Button size="small" variant="outlined" sx={{ bgcolor: tokens.surface }} onClick={() => zoomTo(US_BOUNDS)}>US</Button>
                    <Button size="small" variant="outlined" sx={{ bgcolor: tokens.surface }} onClick={() => zoomTo(null)}>World</Button>
                </Box>
                <SizeLegend maxCount={context.maxCountAllYears} radius={radius} />
                <Typography variant="caption" sx={{ position: "absolute", left: 8, bottom: 6, color: "text.secondary",
                                                    bgcolor: tokens.surface, px: 0.75, borderRadius: 1 }}>
                    {context.loadError ?? (context.data
                        ? `${context.mappedInstitutionCounts.length.toLocaleString()} institutions shown · ` +
                          `${context.unmappedInstitutionCount.toLocaleString()} without coordinates, not mapped`
                        : "Loading…")}
                </Typography>
            </Box>
        </Panel>
    );
});

function SizeLegend({ maxCount, radius }: { maxCount: number; radius: d3.ScalePower<number, number> }) {
    const tokens = useTheme().tokens;
    const values = [...new Set([maxCount, Math.round(maxCount / 4), Math.max(1, Math.round(maxCount / 25))])]
        .filter(value => value > 0);
    const largest = radius(values[0]);
    return (
        <Box sx={{ position: "absolute", right: 8, bottom: 6, bgcolor: tokens.surface, borderRadius: 1, px: 1, py: 0.5 }}>
            <Typography variant="caption" sx={{ display: "block", color: "text.secondary" }}>Collaborations</Typography>
            <svg width={largest * 2 + 60} height={largest * 2 + 10} viewBox={`0 -6 ${largest * 2 + 60} ${largest * 2 + 10}`}>
                {values.map(value => {
                    const r = Math.max(MIN_RADIUS, radius(value));
                    return (
                        <g key={value}>
                            <circle cx={largest + 1} cy={largest * 2 + 2 - r} r={r} fill="none" stroke={tokens.textSecondary} />
                            <line x1={largest + 1} x2={largest * 2 + 8} y1={largest * 2 + 2 - 2 * r} y2={largest * 2 + 2 - 2 * r}
                                  stroke={tokens.textMuted} strokeDasharray="2 2" />
                            <text x={largest * 2 + 10} y={largest * 2 + 2 - 2 * r} dy="0.35em" fontSize={10}
                                  fill={tokens.textSecondary}>{value.toLocaleString()}</text>
                        </g>
                    );
                })}
            </svg>
        </Box>
    );
}
