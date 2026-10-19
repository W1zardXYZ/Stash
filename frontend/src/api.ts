export type Kind =
  | "model"
  | "skeletal_model"
  | "animation"
  | "material"
  | "texture"
  | "hdri"
  | "node_group"
  | "scene"
  | "blueprint"
  | "unreal_asset"
  | "addon"
  | "audio"
  | "other";

export interface AssetBrief {
  id: number;
  uid: string;
  name: string;
  kind: Kind;
  format: string;
  status: string;
  favorite: boolean;
  rating: number;
  size: number;
  poly_count: number;
  is_rigged: boolean;
  is_animated: boolean;
  is_procedural: boolean;
  has_textures: boolean;
  thumbnail: string | null;
  has_preview: boolean;
  source_id: number | null;
  parent_id: number | null;
  source_path: string;
  tags: string[];
  updated_at: string | null;
  ue_class: string | null;
  child_count: number | null;
  score?: number;
}

export interface AssetFile {
  id: number;
  role: string;
  name: string;
  format: string;
  size: number;
  label: string;
  storage: string;
  path: string | null;
  url: string;
  meta: Record<string, unknown>;
}

export interface Job {
  id: number;
  type: string;
  status: "queued" | "running" | "done" | "error" | "cancelled";
  progress: number;
  message: string;
  payload: Record<string, any>;
  result: Record<string, any>;
  asset_id: number | null;
  source_id: number | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  log?: string;
}

export interface AssetDetail extends AssetBrief {
  description: string;
  meta: Record<string, any>;
  files: AssetFile[];
  preview: AssetFile | null;
  links: { relation: string; direction: "in" | "out"; asset: AssetBrief | null }[];
  children: AssetBrief[];
  parent: AssetBrief | null;
  source: { id: number; name: string; kind: string; path: string } | null;
  projects: { id: number; name: string; color: string }[];
  targets: string[];
  exports: AssetFile[];
  jobs: Job[];
  created_at: string | null;
  pack: { name: string; description: string; keywords: string[] } | null;
}

export interface PackInfo {
  name: string;
  count: number;
  description: string;
  keywords: string[];
  meta: { by?: string; model?: string; [k: string]: any };
  updated_at: string | null;
  cover: string | null;
}

export interface PackDetail extends PackInfo {
  kinds: Record<string, number>;
  formats: Record<string, number>;
  tags: string[];
  auto_tags: { tag: string; weight: number }[];
  folders: { path: string; count: number }[];
  names: string[];
  sources: { id: number; name: string }[];
  ue_project: string | null;
  samples: (AssetBrief & { auto_tags: string[]; description: string })[];
  gallery: string[];
}

export const packUrl = (name: string) => `/api/packs/${name.split("/").map(encodeURIComponent).join("/")}`;

export interface Source {
  id: number;
  name: string;
  kind: string;
  path: string;
  mode: string;
  options: Record<string, any>;
  status: string;
  last_scan_at: string | null;
  meta: Record<string, any>;
  asset_count: number;
}

export interface Project {
  id: number;
  name: string;
  description: string;
  color: string;
  count: number;
  covers: (string | null)[];
}

export interface TagInfo {
  id: number;
  name: string;
  color: string;
  count: number;
}

export interface Facets {
  kinds: Record<string, number>;
  formats: Record<string, number>;
  tags: { name: string; color: string; count: number }[];
  ue_classes: Record<string, number>;
  packs: Record<string, number>;
  flags: Record<string, number>;
}

export interface Status {
  version: string;
  blender: boolean;
  blender_path: string | null;
  workers: number;
  assets: number;
  sources: number;
  jobs: Record<string, number>;
  auth: boolean;
  ai: boolean;
  local_ai: { enabled: boolean; model?: string; downloaded?: boolean; embedded?: number };
  browse_roots: string[];
  data_dir: string;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, credentials: "same-origin", headers: {} };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
  }
  const res = await fetch(url, init);
  if (res.status === 401 && !url.startsWith("/api/auth")) {
    window.dispatchEvent(new CustomEvent("stash:unauthorized"));
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, msg);
  }
  return res.json() as Promise<T>;
}

export const api = {
  get: <T>(url: string) => request<T>("GET", url),
  post: <T>(url: string, body?: unknown) => request<T>("POST", url, body ?? {}),
  patch: <T>(url: string, body: unknown) => request<T>("PATCH", url, body),
  put: <T>(url: string, body: unknown) => request<T>("PUT", url, body),
  del: <T>(url: string) => request<T>("DELETE", url),
};

export function qs(params: Record<string, string | number | boolean | undefined | null | string[]>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0)) continue;
    p.set(k, Array.isArray(v) ? v.join(",") : String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

let statusPromise: Promise<Status> | null = null;
/** Server status, fetched once per page load (capabilities like Blender/AI don't change at runtime). */
export function getStatus(): Promise<Status> {
  statusPromise ??= api.get<Status>("/api/status").catch((e) => {
    statusPromise = null;
    throw e;
  });
  return statusPromise;
}

/** Poll a job until it finishes. */
export async function waitForJob(id: number, onUpdate?: (j: Job) => void, intervalMs = 1000): Promise<Job> {
  for (;;) {
    const j = await api.get<Job>(`/api/jobs/${id}`);
    onUpdate?.(j);
    if (j.status === "done" || j.status === "error" || j.status === "cancelled") return j;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

export const KIND_LABELS: Record<string, string> = {
  model: "Model",
  skeletal_model: "Rigged model",
  animation: "Animation",
  material: "Material",
  texture: "Texture",
  hdri: "HDRI",
  node_group: "Node group",
  scene: "Scene / file",
  blueprint: "Blueprint",
  unreal_asset: "Unreal asset",
  addon: "Blender add-on",
  audio: "Audio",
  other: "Other",
};

export const TARGET_LABELS: Record<string, { label: string; hint: string }> = {
  original: { label: "Original files", hint: "Untouched source file(s)" },
  blend: { label: "Blender (.blend)", hint: "Best for Blender – keeps procedural materials, textures packed" },
  unreal: { label: "Unreal package", hint: "FBX (cm, no leaf bones) + textures + manifest for the Stash Unreal importer" },
  unreal_native: { label: "Unreal native (.uasset)", hint: "Package + all dependencies laid out as Content/… – copy into a project" },
  glb: { label: "glTF binary (.glb)", hint: "Web, Godot, three.js, most tools" },
  gltf: { label: "glTF (separate)", hint: ".gltf + .bin + textures" },
  fbx: { label: "FBX", hint: "Generic FBX with embedded textures" },
  fbx_unity: { label: "FBX for Unity", hint: "Axis/scale tuned for Unity" },
  obj: { label: "OBJ + MTL", hint: "Static meshes only" },
  usdz: { label: "USDZ", hint: "Apple AR / Omniverse" },
  usd: { label: "USD", hint: "Universal Scene Description" },
  stl: { label: "STL", hint: "3D printing" },
  ply: { label: "PLY", hint: "Point clouds / scans" },
  abc: { label: "Alembic", hint: "Baked animation cache" },
  png: { label: "PNG", hint: "" },
  jpg: { label: "JPEG", hint: "" },
  tga: { label: "TGA", hint: "" },
  tif: { label: "TIFF", hint: "" },
};

export function formatBytes(n: number): string {
  if (!n) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

export function formatCount(n: number): string {
  if (!n) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}
