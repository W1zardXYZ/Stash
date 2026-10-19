import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AssetBrief, Job, Status, TagInfo, api, formatBytes, qs } from "../api";
import { Icon } from "../components/Icon";
import { AssetCard, CopyField, Modal, Progress, useToast } from "../components/ui";

// ---------------------------------------------------------------- tags
export function TagsPage() {
  const toast = useToast();
  const [tags, setTags] = useState<TagInfo[]>([]);
  const [filter, setFilter] = useState("");
  const load = useCallback(() => api.get<TagInfo[]>("/api/tags").then(setTags), []);
  useEffect(() => { load(); }, [load]);
  const act = async (fn: () => Promise<unknown>) => {
    try { await fn(); load(); } catch (e: any) { toast(e.message, "error"); }
  };
  const shown = tags.filter((t) => t.name.includes(filter.toLowerCase()));
  return (
    <div className="page">
      <div className="page-head">
        <h1>Tags</h1>
        <span className="sub">{tags.length} tags</span>
        <span className="grow" />
        <input className="input" style={{ width: 240 }} placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} />
      </div>
      <table className="table panel">
        <thead><tr><th style={{ width: 40 }}>Color</th><th>Tag</th><th style={{ width: 100 }}>Assets</th><th style={{ width: 260 }} /></tr></thead>
        <tbody>
          {shown.map((t) => (
            <tr key={t.id}>
              <td><input type="color" value={t.color || "#6f7785"} onChange={(e) => act(() => api.patch(`/api/tags/${t.id}`, { color: e.target.value }))} style={{ width: 28, height: 24, border: 0, background: "none" }} /></td>
              <td><Link to={`/?tags=${encodeURIComponent(t.name)}`}><span className="tag">{t.name}</span></Link></td>
              <td>{t.count}</td>
              <td className="row">
                <button className="btn sm" onClick={() => { const n = prompt("Rename tag", t.name); n && n !== t.name && act(() => api.patch(`/api/tags/${t.id}`, { name: n })); }}><Icon name="edit" size={13} />Rename</button>
                <button className="btn sm" onClick={() => { const n = prompt(`Merge "${t.name}" into which tag?`); n && act(() => api.patch(`/api/tags/${t.id}`, { merge_into: n })); }}>Merge</button>
                <button className="btn sm ghost icon danger" title="Delete tag" onClick={() => confirm(`Delete tag "${t.name}"?`) && act(() => api.del(`/api/tags/${t.id}`))}><Icon name="trash" size={13} /></button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!tags.length && <div className="empty">No tags yet. Tag assets from their detail page or with bulk selection in the library.</div>}
    </div>
  );
}

// ---------------------------------------------------------------- jobs
const JOB_LABEL: Record<string, string> = {
  scan_source: "Scan source",
  extract_blend: "Extract .blend",
  preview: "Preview / thumbnail",
  thumbnail: "Texture thumbnail",
  export: "Export",
  package_project: "Project package",
};

export function JobsPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [filter, setFilter] = useState("");
  const [open, setOpen] = useState<Job | null>(null);
  const load = useCallback(() => {
    api.get<{ items: Job[]; counts: Record<string, number> }>(`/api/jobs${qs({ status: filter, limit: 300 })}`).then((r) => { setJobs(r.items); setCounts(r.counts); });
  }, [filter]);
  useEffect(() => {
    load();
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [load]);
  return (
    <div className="page">
      <div className="page-head">
        <h1>Jobs</h1>
        <div className="seg">
          {["", "running", "queued", "error", "done"].map((s) => (
            <button key={s} className={filter === s ? "on" : ""} onClick={() => setFilter(s)}>{s || "all"}{s && counts[s] ? ` (${counts[s]})` : ""}</button>
          ))}
        </div>
        <span className="grow" />
        <button className="btn" onClick={async () => { await api.del("/api/jobs"); load(); }}><Icon name="trash" size={14} />Clear finished</button>
      </div>
      <table className="table panel">
        <thead><tr><th style={{ width: 60 }}>#</th><th>Job</th><th style={{ width: 110 }}>Status</th><th style={{ width: 220 }}>Progress</th><th style={{ width: 170 }}>Created</th><th style={{ width: 110 }} /></tr></thead>
        <tbody>
          {jobs.map((j) => (
            <tr key={j.id}>
              <td className="muted">{j.id}</td>
              <td>
                <div>{JOB_LABEL[j.type] ?? j.type} {j.payload?.target && <span className="kind">{j.payload.target}</span>}</div>
                <div className="small muted">
                  {j.asset_id && <Link to={`/asset/${j.asset_id}`}>asset #{j.asset_id}</Link>}
                  {j.source_id && <Link to={`/?source=${j.source_id}`}>source #{j.source_id}</Link>}
                  {j.message && <span className="ellipsis" style={{ marginLeft: 8, color: j.status === "error" ? "var(--err)" : undefined }}>{j.message.slice(0, 140)}</span>}
                </div>
              </td>
              <td><span className={`status ${j.status}`}>{j.status}</span></td>
              <td>{j.status === "running" ? <Progress value={j.progress} /> : j.status === "done" ? <span className="muted small">{dur(j)}</span> : null}</td>
              <td className="small muted">{j.created_at ? new Date(j.created_at).toLocaleString() : ""}</td>
              <td className="row">
                <button className="btn sm ghost" onClick={async () => setOpen(await api.get<Job>(`/api/jobs/${j.id}`))}>Log</button>
                {(j.status === "queued" || j.status === "running") && <button className="btn sm ghost icon" title="Cancel" onClick={() => api.post(`/api/jobs/${j.id}/cancel`).then(load)}><Icon name="x" size={13} /></button>}
                {(j.status === "error" || j.status === "cancelled") && <button className="btn sm ghost icon" title="Retry" onClick={() => api.post(`/api/jobs/${j.id}/retry`).then(load)}><Icon name="refresh" size={13} /></button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!jobs.length && <div className="empty">No jobs.</div>}
      {open && (
        <Modal title={`Job #${open.id} – ${JOB_LABEL[open.type] ?? open.type}`} onClose={() => setOpen(null)} width={900}>
          <div className="small muted" style={{ marginBottom: 8 }}>{open.status} {open.message}</div>
          {open.result && Object.keys(open.result).length > 0 && <pre className="log">{JSON.stringify(open.result, null, 2)}</pre>}
          <pre className="log">{open.log || "(no log)"}</pre>
        </Modal>
      )}
    </div>
  );
}

function dur(j: Job) {
  if (!j.started_at || !j.finished_at) return "";
  const s = (new Date(j.finished_at).getTime() - new Date(j.started_at).getTime()) / 1000;
  return s < 60 ? `${s.toFixed(1)}s` : `${(s / 60).toFixed(1)}min`;
}

// ---------------------------------------------------------------- integrations / add-ons
interface Storage { store_bytes: number; database_bytes: number; export_cache_cap_bytes: number; by_role: Record<string, number> }

export function IntegrationsPage() {
  const toast = useToast();
  const [status, setStatus] = useState<Status | null>(null);
  const [storage, setStorage] = useState<Storage | null>(null);
  const [addons, setAddons] = useState<AssetBrief[]>([]);
  const origin = __DEV_API_ORIGIN__ ?? window.location.origin;
  useEffect(() => {
    api.get<Status>("/api/status").then(setStatus);
    api.get<Storage>("/api/maintenance/storage").then(setStorage).catch(() => undefined);
    api.get<{ items: AssetBrief[] }>(`/api/assets${qs({ kind: "addon", per_page: 200, sort: "name", order: "asc" })}`).then((r) => setAddons(r.items));
  }, []);
  return (
    <div className="page">
      <div className="page-head">
        <h1>Integrations</h1>
        <span className="sub">Pull assets straight into Blender and Unreal – and push from them</span>
      </div>
      <div className="cards" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(420px, 1fr))" }}>
        <div className="panel panel-pad col">
          <div className="row"><Icon name="blender" size={22} /><strong className="grow">Blender add-on</strong><a className="btn sm primary" href="/api/integrations/blender-addon.zip"><Icon name="download" size={13} />Download</a></div>
          <p className="muted small" style={{ margin: 0 }}>
            Browse/search the library in Blender's sidebar (N panel → Stash), append assets (procedural materials included), apply PBR sets to the selection,
            and push selected objects or the whole file back to Stash. Install via Preferences → Get Extensions → Install from Disk (Blender 4.2+).
          </p>
          <span className="small">Server URL to enter in the add-on preferences:</span>
          <CopyField value={origin} />
        </div>
        <div className="panel panel-pad col">
          <div className="row"><Icon name="unreal" size={22} /><strong className="grow">Unreal Engine plugin</strong><a className="btn sm primary" href="/api/integrations/unreal-plugin.zip"><Icon name="download" size={13} />Download</a></div>
          <p className="muted small" style={{ margin: 0 }}>
            Editor plugin (Python, UE 5.1+). <b>Export:</b> right-click assets in the Content Browser → Stash → Push to Stash (FBX/glTF + textures + thumbnails, matched to the scanned .uasset).
            <b> Import:</b> Tools → Stash → Import from Stash (FBX/glTF import + automatic material instances) or drop native packages into Content.
          </p>
          <span className="small">Unzip <code>StashBridge</code> into <code>YourProject/Plugins/</code> and restart the editor (it enables the Python plugin). Then <b>Stash → Edit settings…</b> opens <code>Config/StashBridge.json</code> – set <code>server_url</code> to:</span>
          <CopyField value={origin} />
        </div>
        <div className="panel panel-pad col">
          <div className="row"><Icon name="puzzle" size={22} /><strong className="grow">Blender extension repository</strong></div>
          <p className="muted small" style={{ margin: 0 }}>
            Every Blender extension (zip with <code>blender_manifest.toml</code>) found in your sources is served as a remote repository.
            In Blender: Preferences → Get Extensions → Repositories → + → Add Remote Repository{status?.auth ? " (enable “Authentication” and use your API token)" : ""}:
          </p>
          <CopyField value={`${origin}/api/blender/extensions/index.json`} />
        </div>
        <div className="panel panel-pad col">
          <div className="row"><Icon name="sparkle" size={22} /><strong className="grow">Pack descriptions with a local LLM</strong><a className="btn sm primary" href="/api/integrations/stash_describe.py"><Icon name="download" size={13} />Script</a></div>
          <p className="muted small" style={{ margin: 0 }}>
            Runs on the computer with LM Studio (Python 3, nothing to install). It sends each undescribed pack's summary and a few previews to the loaded model
            and saves a description + keywords to the pack (see <b>Packs</b>). <code>--engine clip</code> summarises the on-device CLIP tags instead, no LLM needed.
          </p>
          <CopyField value={`python3 stash_describe.py --stash ${origin}${status?.auth ? " --token YOUR_PASSWORD" : ""} packs`} />
        </div>
        <div className="panel panel-pad col">
          <div className="row"><Icon name="settings" size={22} /><strong className="grow">Server</strong></div>
          {status && (
            <dl className="kv">
              <dt>Version</dt><dd>{status.version}</dd>
              <dt>Blender</dt><dd>{status.blender ? <span className="status ok">{status.blender_path}</span> : <span className="status error">not available</span>}</dd>
              <dt>Workers</dt><dd>{status.workers}</dd>
              <dt>Assets</dt><dd>{status.assets.toLocaleString()}</dd>
              <dt>Data folder</dt><dd className="mono">{status.data_dir}</dd>
              <dt>Auth</dt><dd>{status.auth ? "password / token required" : "open (set STASH_PASSWORD to protect)"}</dd>
            </dl>
          )}
          {storage && (
            <dl className="kv">
              <dt>Stash data</dt><dd>{formatBytes(storage.store_bytes)} files · {formatBytes(storage.database_bytes)} database</dd>
              <dt>Export cache</dt><dd>{formatBytes(storage.by_role.export ?? 0)} of {storage.export_cache_cap_bytes ? formatBytes(storage.export_cache_cap_bytes) : "unlimited"}</dd>
              <dt>Previews</dt><dd>{formatBytes((storage.by_role.preview ?? 0) + (storage.by_role.thumbnail ?? 0))}</dd>
            </dl>
          )}
          <div className="row wrap">
            <button className="btn sm" onClick={async () => {
              const r = await api.post<{ gc: { removed: number; freed_bytes: number }; export_cache: { removed: number; freed_bytes: number } }>("/api/maintenance/cleanup");
              toast(`Freed ${formatBytes(r.gc.freed_bytes + r.export_cache.freed_bytes)} (${r.gc.removed + r.export_cache.removed} files)`, "ok");
              api.get<Storage>("/api/maintenance/storage").then(setStorage);
            }}>Clean up storage</button>
            <button className="btn sm" onClick={() => api.post("/api/maintenance/reindex")}>Rebuild search index</button>
          </div>
        </div>
      </div>

      <div className="page-head" style={{ marginTop: 30 }}>
        <h1 style={{ fontSize: 17 }}>Blender add-ons in your library</h1>
        <span className="sub">{addons.length} found</span>
      </div>
      {addons.length ? (
        <div className="grid" style={{ ["--card" as any]: "190px" }}>
          {addons.map((a) => <AssetCard key={a.id} asset={a} onClick={() => (window.location.href = `/asset/${a.id}`)} />)}
        </div>
      ) : (
        <div className="muted">No add-ons indexed yet – add the folder where you keep add-on zips as a source.</div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- login
export function Login({ onDone }: { onDone: () => void }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  const submit = async () => {
    try {
      await api.post("/api/auth/login", { password: pw });
      onDone();
    } catch (e: any) {
      setErr(e.message);
    }
  };
  return (
    <div className="login">
      <div className="panel">
        <div className="row" style={{ marginBottom: 18 }}><img src="/favicon.svg" width={32} alt="" /><strong style={{ fontSize: 18 }}>Stash</strong></div>
        <label className="field"><span>Password</span><input className="input" type="password" autoFocus value={pw} onChange={(e) => setPw(e.target.value)} onKeyDown={(e) => e.key === "Enter" && submit()} /></label>
        {err && <div className="small" style={{ color: "var(--err)", marginBottom: 10 }}>{err}</div>}
        <button className="btn primary" style={{ width: "100%", justifyContent: "center" }} onClick={submit}>Sign in</button>
      </div>
    </div>
  );
}

export function UploadOverlay() {
  return <div className="dropzone">Drop files or folders to upload into Stash</div>;
}

export { Modal };
