// Minimal inline icon set (Lucide-style strokes) so the UI has no icon-font dependency.
const PATHS: Record<string, string> = {
  library: "M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  project: "M4 4h6l2 2h8v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2zM8 12h8M8 16h5",
  tag: "M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8zM7.5 7.5h.01",
  jobs: "M12 2v4M12 18v4M4.9 4.9l2.8 2.8M16.3 16.3l2.8 2.8M2 12h4M18 12h4M4.9 19.1l2.8-2.8M16.3 7.7l2.8-2.8",
  plug: "M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0zM12 18v4",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3",
  cube: "M21 16V8a2 2 0 0 0-1-1.7l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.7l7 4a2 2 0 0 0 2 0l7-4a2 2 0 0 0 1-1.7zM3.3 7 12 12l8.7-5M12 22V12",
  bone: "M17 10c.7-.7 1.7-1 2.5-.6a2 2 0 0 0 .5-3.4 2 2 0 0 0-3.4.5c.4.8.1 1.8-.6 2.5l-6.5 6.5c-.7.7-1.7 1-2.5.6a2 2 0 0 0-.5 3.4 2 2 0 0 0 3.4-.5c-.4-.8-.1-1.8.6-2.5z",
  film: "M4 4h16v16H4zM4 8h16M4 16h16M8 4v16M16 4v16",
  sphere: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM3 12c0 2.2 4 4 9 4s9-1.8 9-4M12 3c-2.5 2.5-3.5 5.5-3.5 9s1 6.5 3.5 9",
  image: "M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM8.5 10a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3zM21 15l-5-5L5 21",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
  nodes: "M5 3h4v4H5zM15 10h4v4h-4zM5 17h4v4H5zM9 5h2a2 2 0 0 1 2 2v8a2 2 0 0 0 2 2M13 12h2",
  file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6",
  layers: "M12 2 2 7l10 5 10-5zM2 17l10 5 10-5M2 12l10 5 10-5",
  puzzle: "M19.4 13a2.5 2.5 0 1 0 0-2H18V7a1 1 0 0 0-1-1h-4V4.6a2.5 2.5 0 1 0-2 0V6H7a1 1 0 0 0-1 1v4h1.4a2.5 2.5 0 1 1 0 2H6v4a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1v-4z",
  music: "M9 18V5l12-2v13M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0zM21 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z",
  blueprint: "M4 4h16v16H4zM4 9h16M9 4v16",
  star: "M12 2l3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1z",
  download: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3",
  upload: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12",
  plus: "M12 5v14M5 12h14",
  x: "M18 6 6 18M6 6l12 12",
  check: "M20 6 9 17l-5-5",
  refresh: "M21 12a9 9 0 0 1-15.5 6.3L3 16M3 12A9 9 0 0 1 18.5 5.7L21 8M21 3v5h-5M3 21v-5h5",
  trash: "M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6",
  play: "M6 4l14 8-14 8z",
  pause: "M6 4h4v16H6zM14 4h4v16h-4z",
  camera: "M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2zM12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
  grid: "M3 3h18v18H3zM3 9h18M3 15h18M9 3v18M15 3v18",
  wire: "M12 2 2 7l10 5 10-5zM2 7v10l10 5V12M22 7v10l-10 5",
  rotate: "M21 2v6h-6M3 12a9 9 0 0 1 15-6.7L21 8M3 22v-6h6M21 12a9 9 0 0 1-15 6.7L3 16",
  edit: "M12 20h9M16.5 3.5a2.1 2.1 0 1 1 3 3L7 19l-4 1 1-4z",
  link: "M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.8 1.7M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7",
  filter: "M22 3H2l8 9.5V19l4 2v-8.5z",
  select: "M9 11l3 3L22 4M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11",
  external: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14 21 3",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
  back: "M19 12H5M12 19l-7-7 7-7",
  copy: "M20 9h-9a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2v-9a2 2 0 0 0-2-2zM5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1",
  sparkle: "M12 3l1.9 5.8L20 11l-6.1 2.2L12 19l-1.9-5.8L4 11l6.1-2.2z",
  unreal: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zM9 7v7.5c0 1 .8 1.5 1.5 1.5S12 15.5 12 15v-5l3 3v-6",
  blender: "M12 14a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM12 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM4 11h5M2 7l7 4",
  menu: "M3 6h18M3 12h18M3 18h18",
};

export function Icon({ name, size = 18, stroke = 1.8, className, style }: { name: string; size?: number; stroke?: number; className?: string; style?: React.CSSProperties }) {
  const d = PATHS[name] ?? PATHS.cube;
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={stroke} strokeLinecap="round" strokeLinejoin="round" className={className} style={style} aria-hidden>
      {d.split("M").filter(Boolean).map((seg, i) => (
        <path key={i} d={`M${seg}`} />
      ))}
    </svg>
  );
}

export const KIND_ICON: Record<string, string> = {
  model: "cube",
  skeletal_model: "bone",
  animation: "film",
  material: "sphere",
  texture: "image",
  hdri: "sun",
  node_group: "nodes",
  scene: "layers",
  blueprint: "blueprint",
  unreal_asset: "unreal",
  addon: "puzzle",
  audio: "music",
  other: "file",
};
