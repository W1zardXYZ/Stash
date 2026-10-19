import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { AssetBrief, AssetDetail, AssetFile, Job, KIND_LABELS, Project, TARGET_LABELS, api, formatBytes, getStatus, waitForJob } from "../api";
import { Icon, KIND_ICON } from "../components/Icon";
import NodeGraph from "../components/NodeGraph";
import Viewer, { ViewerSource } from "../components/Viewer";
import { CopyField, KindBadge, MiniCard, Progress, Stars, TagInput, useToast } from "../components/ui";

const RELATION_LABEL: Record<string, [string, string]> = {
  uses_material: ["Materials", "Used by"],
  uses_texture: ["Textures", "Used by materials"],
  uses_skeleton: ["Skeleton", "Meshes on this skeleton"],
  animates: ["Animates", "Animations"],
  depends_on: ["Depends on", "Referenced by"],
};
const WEB_IMAGES = /\.(png|jpe?g|webp|gif|bmp|svg)$/i;
const WEB_MODELS = new Set(["glb", "gltf", "fbx", "obj", "stl", "ply", "dae", "3mf", "usdz"]);

function viewerSource(a: AssetDetail, related: AssetDetail | null): ViewerSource | null {
  const src = a.files.find((f) => f.role === "source");
  if (a.kind === "hdri" && src && /\.(hdr|exr|jpe?g|png)$/i.test(src.name)) return { type: "hdri", url: src.url, format: src.format };
  if (a.kind === "material" && a.format === "pbr") {
    const maps: Record<string, string> = {};
    a.files.filter((f) => f.role === "texture" && f.label).forEach((f) => (maps[f.label] = f.url));
    if (Object.keys(maps).length) return { type: "pbr", maps };
  }
  if (a.kind === "animation" && !a.preview && related?.preview) return { type: "model", url: related.preview.url, format: "glb" };
  if (a.preview) {
    // a linked .gltf/.obj must load via the folder-relative route so its textures resolve
    const url = a.preview.storage === "source" ? `/api/assets/${a.id}/raw/${encodeURIComponent(a.preview.name)}` : a.preview.url;
    return { type: "model", url, format: a.preview.format || "glb" };
  }
  const ueExport = a.files.find((f) => f.role === "ue_export");
  const model = ueExport ?? src;
  if (model && WEB_MODELS.has(model.format) && ["model", "skeletal_model", "animation", "scene", "unreal_asset"].includes(a.kind)) {
    const mtl = a.files.find((f) => f.role === "sidecar" && f.format === "mtl");
    return {
      type: "model",
      url: `/api/assets/${a.id}/raw/${encodeURIComponent(model.name)}`,
      format: model.format,
      mtlUrl: mtl ? `/api/assets/${a.id}/raw/${encodeURIComponent(mtl.name)}` : undefined,
    };
  }
  return null;
}

export default function AssetPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const [a, setA] = useState<AssetDetail | null>(null);
  const [related, setRelated] = useState<AssetDetail | null>(null);
  const [tab, setTab] = useState("overview");
  const [projects, setProjects] = useState<Project[]>([]);
  const [allTags, setAllTags] = useState<string[]>([]);
  const [editingName, setEditingName] = useState(false);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [aiEnabled, setAiEnabled] = useState(false);
  const [similar, setSimilar] = useState<AssetBrief[] | null>(null);
  const [aiBusy, setAiBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const d = await api.get<AssetDetail>(`/api/assets/${id}`);
      setA(d);
      setName(d.name);
      setDesc(d.description);
      // animations extracted from a .blend play on the rig they animate
      const target = d.kind === "animation" && !d.preview ? d.links.find((l) => l.relation === "animates" && l.direction === "out")?.asset : null;
      setRelated(target ? await api.get<AssetDetail>(`/api/assets/${target.id}`) : null);
    } catch (e: any) {
      toast(e.message, "error");
    }
  }, [id, toast]);

  useEffect(() => {
    setA(null);
    setSimilar(null);
    setTab("overview");
    load();
  }, [load]);
  useEffect(() => {
    api.get<Project[]>("/api/projects").then(setProjects).catch(() => undefined);
    api.get<{ name: string }[]>("/api/tags").then((t) => setAllTags(t.map((x) => x.name))).catch(() => undefined);
    getStatus().then((s) => setAiEnabled(s.ai)).catch(() => undefined);
  }, []);

  const source = useMemo(() => (a ? viewerSource(a, related) : null), [a, related]);

  if (!a) return <div className="page muted">Loading…</div>;

  const patch = async (body: Partial<AssetDetail>) => {
    try {
      await api.patch(`/api/assets/${a.id}`, body);
      setA({ ...a, ...body });
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const capture = async (dataUrl: string) => {
    try {
      const r = await api.post<{ thumbnail: string }>(`/api/assets/${a.id}/thumbnail`, { data_url: dataUrl });
      setA({ ...a, thumbnail: r.thumbnail });
      toast("Thumbnail updated", "ok");
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const aiTag = async () => {
    setAiBusy(true);
    try {
      const r = await api.post<{ jobs: number[] }>("/api/ai/tag", { ids: [a.id] });
      const job = await waitForJob(r.jobs[0]);
      if (job.status === "done") {
        toast(`AI: ${job.result.category}`, "ok");
        load();
      } else toast(`AI tagging failed: ${job.message}`, "error");
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setAiBusy(false);
    }
  };

  const reprocess = async () => {
    const r = await api.post<{ jobs: number[] }>(`/api/assets/${a.id}/reprocess`);
    toast(r.jobs.length ? "Re-processing queued" : "Nothing to re-process", r.jobs.length ? "ok" : "info");
  };

  const remove = async () => {
    if (!confirm("Remove this asset from the library? Files on disk are not touched and it won't come back on rescans.")) return;
    await api.del(`/api/assets/${a.id}`);
    navigate(-1);
  };

  const meta = a.meta || {};
  const insp = meta.inspect || {};
  const stats = meta.stats || {};
  const graph = meta.graph;
  const linksOut = a.links.filter((l) => l.direction === "out" && l.asset);
  const linksIn = a.links.filter((l) => l.direction === "in" && l.asset);
  const animations: { name: string; duration?: number }[] = insp.animations || [];
  const clipName = a.kind === "animation" ? a.name : undefined;
  const src = a.files.find((f) => f.role === "source");

  return (
    <div className="detail">
      <div className="stage">
        {source ? (
          <Viewer key={a.id} source={source} initialClip={clipName} onCapture={capture} />
        ) : (a.kind === "texture" || a.kind === "hdri") && src && WEB_IMAGES.test(src.name) ? (
          <div className="image-stage"><img src={src.url} alt={a.name} /></div>
        ) : a.thumbnail ? (
          <div className="image-stage"><img src={a.thumbnail} alt={a.name} style={{ maxWidth: 512 }} /></div>
        ) : (
          <div className="image-stage"><Icon name={KIND_ICON[a.kind]} size={120} stroke={0.8} /></div>
        )}
      </div>

      <aside className="info">
        <div className="info-head">
          <div className="crumbs">
            <a onClick={() => navigate(-1)} style={{ cursor: "pointer" }}><Icon name="back" size={13} /></a>
            {a.source && <Link to={`/?source=${a.source.id}`}>{a.source.name}</Link>}
            {a.parent && (<><span>›</span><Link to={`/asset/${a.parent.id}`}>{a.parent.name}</Link></>)}
          </div>
          {editingName ? (
            <input
              className="input" style={{ margin: "8px 0", fontSize: 17 }} autoFocus value={name}
              onChange={(e) => setName(e.target.value)}
              onBlur={() => { setEditingName(false); name !== a.name && patch({ name }); }}
              onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
            />
          ) : (
            <h2 onDoubleClick={() => setEditingName(true)} title="Double-click to rename">{a.name}</h2>
          )}
          <div className="row wrap" style={{ gap: 8 }}>
            <KindBadge kind={a.kind} />
            {a.ue_class && <span className="kind">{a.ue_class}</span>}
            <span className="muted small">{a.format.toUpperCase()}</span>
            {a.size > 0 && <span className="muted small">{formatBytes(a.size)}</span>}
            {a.status !== "ok" && <span className="kind" style={{ color: "var(--warn)" }}>{a.status}</span>}
            <span className="grow" />
            <button className={`btn sm icon ${a.favorite ? "on" : ""}`} title="Favorite" onClick={() => patch({ favorite: !a.favorite })}><Icon name="star" size={14} /></button>
            <button className="btn sm icon" title="Re-process (extract / preview / thumbnail)" onClick={reprocess}><Icon name="refresh" size={14} /></button>
            <button className="btn sm icon danger" title="Remove from library" onClick={remove}><Icon name="trash" size={14} /></button>
          </div>
        </div>

        <div className="tabs">
          {[
            ["overview", "Overview"],
            ["export", "Export"],
            ["related", `Related${a.children.length + linksOut.length + linksIn.length ? ` (${a.children.length + linksOut.length + linksIn.length})` : ""}`],
            ["files", `Files (${a.files.length})`],
            ...(graph ? [["graph", "Nodes"]] : []),
          ].map(([k, label]) => (
            <button key={k} className={tab === k ? "on" : ""} onClick={() => {
              setTab(k);
              if (k === "related" && similar === null) api.get<{ items: AssetBrief[] }>(`/api/assets/${a.id}/similar`).then((r) => setSimilar(r.items)).catch(() => setSimilar([]));
            }}>{label}</button>
          ))}
        </div>

        {tab === "overview" && (
          <>
            <div className="section">
              <h5>
                Tags
                {aiEnabled && (
                  <button className="btn sm ghost" style={{ marginLeft: "auto" }} disabled={aiBusy} onClick={aiTag} title="Let Claude look at the thumbnail and metadata and suggest a category, tags and description">
                    <Icon name="sparkle" size={13} />{aiBusy ? "Thinking…" : "AI tag"}
                  </button>
                )}
              </h5>
              {meta.category && <div className="small muted" style={{ marginBottom: 6 }}>Category: <b>{meta.category}</b></div>}
              {(meta.auto_tags ?? []).filter((s: { tag: string }) => !a.tags.includes(s.tag)).length > 0 && (
                <div className="chips" style={{ marginBottom: 8 }} title="Suggested by the on-device AI from the thumbnail - click to add">
                  <span className="small muted"><Icon name="sparkle" size={12} /> Suggested:</span>
                  {(meta.auto_tags as { tag: string; score: number }[]).filter((s) => !a.tags.includes(s.tag)).map((s) => (
                    <button key={s.tag} className="tag" style={{ cursor: "pointer", borderStyle: "dashed" }} onClick={async () => { const r = await api.post<{ tags: string[] }>(`/api/assets/${a.id}/tags`, { tags: [s.tag] }); setA({ ...a, tags: r.tags }); }}>
                      + {s.tag}
                    </button>
                  ))}
                </div>
              )}
              <TagInput
                tags={a.tags}
                suggestions={allTags}
                onAdd={async (t) => { const r = await api.post<{ tags: string[] }>(`/api/assets/${a.id}/tags`, { tags: t }); setA({ ...a, tags: r.tags }); }}
                onRemove={async (t) => { const r = await api.del<{ tags: string[] }>(`/api/assets/${a.id}/tags/${encodeURIComponent(t)}`); setA({ ...a, tags: r.tags }); }}
              />
            </div>
            <div className="section">
              <h5>Projects</h5>
              <div className="chips" style={{ marginBottom: 8 }}>
                {a.projects.map((p) => (
                  <span key={p.id} className="tag">
                    <Link to={`/projects/${p.id}`}>{p.name}</Link>
                    <button onClick={async () => { await api.del(`/api/projects/${p.id}/assets/${a.id}`); load(); }}><Icon name="x" size={12} /></button>
                  </span>
                ))}
              </div>
              <select className="input" value="" onChange={async (e) => { if (!e.target.value) return; await api.post(`/api/projects/${e.target.value}/assets`, { ids: [a.id] }); load(); }}>
                <option value="">Add to project…</option>
                {projects.filter((p) => !a.projects.some((x) => x.id === p.id)).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </div>
            <div className="section">
              <h5>Rating</h5>
              <Stars value={a.rating} onChange={(v) => patch({ rating: v })} />
            </div>
            <div className="section">
              <h5>Notes</h5>
              <textarea className="input" value={desc} placeholder="Description, license, where it's from…" onChange={(e) => setDesc(e.target.value)} onBlur={() => desc !== a.description && patch({ description: desc })} />
            </div>
            <div className="section">
              <h5>Details</h5>
              <dl className="kv">
                <dt>Kind</dt>
                <dd>
                  <select className="input" style={{ height: 28 }} value={a.kind} onChange={(e) => patch({ kind: e.target.value as AssetBrief["kind"] })}>
                    {Object.entries(KIND_LABELS).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
                  </select>
                </dd>
                {a.poly_count > 0 && (<><dt>Triangles</dt><dd>{a.poly_count.toLocaleString()}</dd></>)}
                {(stats.vertices || insp.vertices) > 0 && (<><dt>Vertices</dt><dd>{(stats.vertices || insp.vertices).toLocaleString()}</dd></>)}
                {(stats.bones || insp.bones) > 0 && (<><dt>Bones</dt><dd>{stats.bones || insp.bones}</dd></>)}
                {stats.shape_keys > 0 && (<><dt>Shape keys</dt><dd>{stats.shape_keys}</dd></>)}
                {insp.morph_targets > 0 && (<><dt>Morph targets</dt><dd>{insp.morph_targets}</dd></>)}
                {stats.modifiers?.length > 0 && (<><dt>Modifiers</dt><dd>{stats.modifiers.join(", ")}</dd></>)}
                {meta.frame_range && (<><dt>Frames</dt><dd>{meta.frame_range[0]}–{meta.frame_range[1]} @ {meta.fps} fps</dd></>)}
                {meta.channels && (<><dt>Channels</dt><dd>{meta.channels.join(", ")}</dd></>)}
                {meta.resolution && (<><dt>Resolution</dt><dd>{meta.resolution}</dd></>)}
                {meta.image?.width && (<><dt>Image</dt><dd>{meta.image.width}×{meta.image.height} {meta.image.mode}</dd></>)}
                {meta.channel && (<><dt>Channel</dt><dd>{meta.channel}</dd></>)}
                {insp.materials?.length > 0 && (<><dt>Materials</dt><dd>{insp.materials.join(", ")}</dd></>)}
                {animations.length > 0 && (<><dt>Animations</dt><dd>{animations.map((x) => `${x.name}${x.duration ? ` (${x.duration}s)` : ""}`).join(", ")}</dd></>)}
                {meta.ue_package && (<><dt>Unreal path</dt><dd className="mono">{meta.ue_package}</dd></>)}
                {meta.ue_engine_version && (<><dt>Engine</dt><dd>UE {meta.ue_engine_version}</dd></>)}
                {meta.ue_project && (<><dt>UE project</dt><dd>{meta.ue_project}</dd></>)}
                {meta.sidecar?.name && (<><dt>Vendor name</dt><dd>{meta.sidecar.name}{meta.sidecar.id ? ` (${meta.sidecar.id})` : ""}</dd></>)}
                {meta.sidecar?.properties && Object.entries(meta.sidecar.properties as Record<string, unknown>).slice(0, 8).map(([k, v]) => (<Fragment key={k}><dt>{k}</dt><dd>{String(v)}</dd></Fragment>))}
                {meta.pack && (<><dt>Pack</dt><dd><Link to={`/?pack=${encodeURIComponent(meta.pack)}`}>{meta.pack}</Link>{a.pack?.description && <div className="muted small" style={{ marginTop: 3 }}>{a.pack.description}</div>}</dd></>)}
                {meta.blender_version && (<><dt>Blender</dt><dd>{meta.blender_version}</dd></>)}
                {meta.scene_stats && (<><dt>File contents</dt><dd>{Object.entries(meta.scene_stats).map(([k, v]) => `${v} ${k}`).join(" · ")}</dd></>)}
                {meta.addon && (
                  <>
                    <dt>Add-on</dt><dd>{meta.addon.name} {meta.addon.version}</dd>
                    <dt>Type</dt><dd>{meta.addon.addon_type === "extension" ? "Extension (4.2+)" : "Legacy add-on"}</dd>
                    {meta.addon.author && (<><dt>Author</dt><dd>{meta.addon.author}</dd></>)}
                    {meta.addon.blender_version_min && (<><dt>Blender ≥</dt><dd>{meta.addon.blender_version_min}</dd></>)}
                    {meta.addon.category && (<><dt>Category</dt><dd>{meta.addon.category}</dd></>)}
                  </>
                )}
                <dt>Path</dt><dd className="mono small">{a.source_path}{meta.ue_class ? "" : ""}</dd>
                <dt>Asset ID</dt><dd className="mono small">{a.uid}</dd>
              </dl>
              {meta.extract_pending && <div className="callout warn" style={{ marginTop: 10 }}>{meta.extract_pending}</div>}
              {meta.extract_errors?.length > 0 && <div className="callout warn" style={{ marginTop: 10 }}>{meta.extract_errors.slice(0, 5).join("\n")}</div>}
            </div>
          </>
        )}

        {tab === "export" && <ExportPanel asset={a} onDone={load} />}

        {tab === "related" && (
          <div className="section">
            {a.children.length > 0 && <RelGroup title={`Contains (${a.children.length})`} items={a.children} />}
            {groupLinks(linksOut, 0).map(([title, items]) => <RelGroup key={`o${title}`} title={title} items={items} />)}
            {groupLinks(linksIn, 1).map(([title, items]) => <RelGroup key={`i${title}`} title={title} items={items} />)}
            {similar && similar.length > 0 && <RelGroup title="Visually similar" items={similar} />}
            {a.children.length + linksOut.length + linksIn.length === 0 && !similar?.length && <div className="muted">No related assets.</div>}
          </div>
        )}

        {tab === "files" && (
          <div className="section">
            {a.files.map((f) => <FileRow key={f.id} f={f} asset={a} onThumb={load} />)}
          </div>
        )}

        {tab === "graph" && graph && (
          <div className="section">
            <h5>{meta.tree_type ? meta.tree_type.replace("NodeTree", "") + " nodes" : "Shader nodes"} · {graph.nodes.length} nodes {graph.procedural ? "· procedural" : ""}</h5>
            <NodeGraph graph={graph} />
            <p className="muted small">Read-only view of the node tree. Export as .blend to get the fully editable material.</p>
          </div>
        )}
      </aside>
    </div>
  );
}

function groupLinks(links: AssetDetail["links"], idx: 0 | 1): [string, AssetBrief[]][] {
  const by: Record<string, AssetBrief[]> = {};
  for (const l of links) {
    const title = RELATION_LABEL[l.relation]?.[idx] ?? l.relation;
    (by[title] ||= []).push(l.asset!);
  }
  return Object.entries(by);
}

function RelGroup({ title, items }: { title: string; items: AssetBrief[] }) {
  const navigate = useNavigate();
  return (
    <div style={{ marginBottom: 16 }}>
      <h5>{title}</h5>
      <div className="mini-grid">
        {items.slice(0, 200).map((c) => <MiniCard key={c.id} asset={c} note={KIND_LABELS[c.kind]} onClick={() => navigate(`/asset/${c.id}`)} />)}
      </div>
    </div>
  );
}

function FileRow({ f, asset, onThumb }: { f: AssetFile; asset: AssetDetail; onThumb: () => void }) {
  const isImg = /\.(png|jpe?g|webp|gif)$/i.test(f.name);
  return (
    <div className="file-row">
      <span className="kind" style={{ minWidth: 70, textAlign: "center" }}>{f.role}{f.label ? `:${f.label}` : ""}</span>
      <span className="ellipsis grow" title={f.path ?? f.name}>{f.name}</span>
      <span className="muted small">{formatBytes(f.size)}</span>
      {isImg && asset.thumbnail !== f.url && (
        <button className="btn sm ghost icon" title="Use as thumbnail" onClick={async () => { await api.post(`/api/assets/${asset.id}/thumbnail/${f.id}`); onThumb(); }}>
          <Icon name="image" size={13} />
        </button>
      )}
      <a className="btn sm ghost icon" href={`${f.url}?download=1`} title="Download"><Icon name="download" size={13} /></a>
    </div>
  );
}

function ExportPanel({ asset, onDone }: { asset: AssetDetail; onDone: () => void }) {
  const toast = useToast();
  const [jobs, setJobs] = useState<Record<string, Job>>({});
  const [ready, setReady] = useState<Record<string, AssetFile>>(() => Object.fromEntries(asset.exports.map((f) => [f.label, f])));

  const run = async (target: string, force = false) => {
    try {
      const r = await api.post<{ file?: AssetFile; job_id?: number }>(`/api/assets/${asset.id}/export`, { target, force });
      if (r.file) {
        setReady((x) => ({ ...x, [target]: r.file! }));
        window.location.href = `${r.file.url}?download=1`;
        return;
      }
      const job = await waitForJob(r.job_id!, (j) => setJobs((x) => ({ ...x, [target]: j })));
      if (job.status === "done" && job.result.file_id) {
        const url = `/api/files/${job.result.file_id}/${encodeURIComponent(job.result.name)}`;
        setReady((x) => ({ ...x, [target]: { id: job.result.file_id, url, name: job.result.name, size: job.result.size } as AssetFile }));
        window.location.href = `${url}?download=1`;
        onDone();
      } else if (job.status === "error") {
        toast(`Export failed: ${job.message}`, "error");
      }
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const send = async (app: "blender" | "unreal") => {
    try {
      await api.post("/api/deliveries", { app, asset_ids: [asset.id] });
      toast(`Sent to ${app === "blender" ? "Blender" : "Unreal"} – it imports on the next poll (or click Receive in the plugin)`, "ok");
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  if (!asset.targets.length) return <div className="section muted">No export targets available for this asset.</div>;

  return (
    <div className="section">
      <h5>Send to an open app</h5>
      <div className="row" style={{ marginBottom: 16 }}>
        <button className="btn" onClick={() => send("blender")}><Icon name="blender" size={15} />Send to Blender</button>
        <button className="btn" onClick={() => send("unreal")}><Icon name="unreal" size={15} />Send to Unreal</button>
      </div>
      <h5>Download as</h5>
      {asset.targets.map((t) => {
        const j = jobs[t];
        const busy = j && (j.status === "queued" || j.status === "running");
        const file = ready[t];
        return (
          <div key={t} className="target">
            <div className="grow">
              <div className="t-name">{TARGET_LABELS[t]?.label ?? t}</div>
              <div className="t-hint">{busy ? (j.status === "queued" ? "Queued…" : j.message || "Converting…") : j?.status === "error" ? <span style={{ color: "var(--err)" }}>{j.message.slice(0, 160)}</span> : TARGET_LABELS[t]?.hint}</div>
              {busy && <Progress value={j.progress || 0.05} />}
            </div>
            {file && !busy && <a className="btn sm" href={`${file.url}?download=1`} title={file.name}><Icon name="download" size={13} />{formatBytes(file.size)}</a>}
            <button className="btn sm primary" disabled={busy} onClick={() => run(t, !!file)}>{file ? <Icon name="refresh" size={13} /> : <Icon name="download" size={13} />}</button>
          </div>
        );
      })}
      <h5 style={{ marginTop: 18 }}>Pull from Blender / Unreal</h5>
      <p className="muted small">Install the Stash Blender add-on or Unreal plugin (see Integrations) and search for this asset, or paste its ID:</p>
      <CopyField value={asset.uid} />
    </div>
  );
}
