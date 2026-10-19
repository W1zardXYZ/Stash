import { useMemo, useRef, useState } from "react";

interface GNode { name: string; type: string; label?: string; location: [number, number]; inputs?: Record<string, unknown>; image?: string; group?: string }
interface GLink { from: string; from_socket: string; to: string; to_socket: string }
export interface Graph { nodes: GNode[]; links: GLink[]; procedural?: boolean }

const W = 170;
const COLORS: Record<string, string> = {
  Tex: "#7c5cff", Bsdf: "#3ecf8e", Output: "#f0506e", Group: "#f5a524", Math: "#4fa3ff", Mix: "#4fa3ff", Geometry: "#2ec4c4", Mesh: "#2ec4c4",
};

function colorFor(type: string) {
  for (const [k, v] of Object.entries(COLORS)) if (type.includes(k)) return v;
  return "#6f7785";
}

function prettyType(t: string) {
  return t.replace(/^(ShaderNode|GeometryNode|FunctionNode|NodeGroup|CompositorNode)/, "").replace(/([a-z])([A-Z])/g, "$1 $2") || t;
}

/** Read-only view of a Blender node tree (material or geometry nodes) serialized by the extractor. */
export default function NodeGraph({ graph }: { graph: Graph }) {
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const drag = useRef<{ x: number; y: number } | null>(null);

  const layout = useMemo(() => {
    const pos: Record<string, { x: number; y: number; h: number; ins: string[] }> = {};
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of graph.nodes) {
      const ins = Array.from(new Set(graph.links.filter((l) => l.to === n.name).map((l) => l.to_socket)));
      const h = 34 + Math.max(ins.length, 1) * 16 + (n.image ? 16 : 0);
      const x = n.location[0];
      const y = -n.location[1];
      pos[n.name] = { x, y, h, ins };
      minX = Math.min(minX, x); minY = Math.min(minY, y); maxX = Math.max(maxX, x + W); maxY = Math.max(maxY, y + h);
    }
    return { pos, box: { minX, minY, w: maxX - minX || 1, h: maxY - minY || 1 } };
  }, [graph]);

  if (!graph.nodes.length) return <div className="muted">Empty node tree</div>;
  const { pos, box } = layout;
  const pad = 40;
  const vb = `${box.minX - pad} ${box.minY - pad} ${box.w + pad * 2} ${box.h + pad * 2}`;

  return (
    <svg
      className="graph"
      viewBox={vb}
      onWheel={(e) => setView((v) => ({ ...v, k: Math.min(4, Math.max(0.3, v.k * (e.deltaY < 0 ? 1.1 : 0.9))) }))}
      onMouseDown={(e) => (drag.current = { x: e.clientX, y: e.clientY })}
      onMouseUp={() => (drag.current = null)}
      onMouseLeave={() => (drag.current = null)}
      onMouseMove={(e) => {
        if (!drag.current) return;
        const scale = (box.w + pad * 2) / (e.currentTarget.clientWidth || 1) / view.k;
        const dx = (e.clientX - drag.current.x) * scale, dy = (e.clientY - drag.current.y) * scale;
        drag.current = { x: e.clientX, y: e.clientY };
        setView((v) => ({ ...v, x: v.x + dx, y: v.y + dy }));
      }}
    >
      <g transform={`translate(${view.x} ${view.y}) translate(${box.minX + box.w / 2} ${box.minY + box.h / 2}) scale(${view.k}) translate(${-(box.minX + box.w / 2)} ${-(box.minY + box.h / 2)})`}>
        {graph.links.map((l, i) => {
          const a = pos[l.from], b = pos[l.to];
          if (!a || !b) return null;
          const x1 = a.x + W, y1 = a.y + 22;
          const idx = Math.max(0, b.ins.indexOf(l.to_socket));
          const x2 = b.x, y2 = b.y + 38 + idx * 16;
          const dx = Math.max(40, Math.abs(x2 - x1) / 2);
          return <path key={i} className="link" d={`M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`} />;
        })}
        {graph.nodes.map((n) => {
          const p = pos[n.name];
          const c = colorFor(n.type);
          return (
            <g key={n.name} className="node" transform={`translate(${p.x} ${p.y})`}>
              <rect className="body" width={W} height={p.h} rx={6} />
              <rect width={W} height={22} rx={6} fill={c} opacity={0.85} />
              <text className="hdr" x={8} y={15} fill="#fff">{(n.label || prettyType(n.type)).slice(0, 24)}</text>
              {p.ins.map((s, i) => (
                <g key={s}>
                  <circle cx={0} cy={38 + i * 16} r={3.5} fill={c} />
                  <text x={8} y={42 + i * 16} opacity={0.8}>{s.slice(0, 26)}</text>
                </g>
              ))}
              {n.image && <text x={8} y={p.h - 8} opacity={0.65}>🖼 {n.image.slice(0, 22)}</text>}
              <circle cx={W} cy={22} r={3.5} fill={c} />
            </g>
          );
        })}
      </g>
    </svg>
  );
}
