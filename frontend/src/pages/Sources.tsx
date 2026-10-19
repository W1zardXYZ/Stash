import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Source, Status, api } from "../api";
import { Icon } from "../components/Icon";
import { Modal, useToast } from "../components/ui";

const KIND_INFO: Record<string, [string, string]> = {
  folder: ["Folder", "folder"],
  unreal_project: ["Unreal project", "unreal"],
  blender_file: [".blend file", "blender"],
  uploads: ["Uploads", "upload"],
  bundles: ["Pushed from DCC", "plug"],
};

export default function Sources() {
  const toast = useToast();
  const [sources, setSources] = useState<Source[]>([]);
  const [status, setStatus] = useState<Status | null>(null);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<Source | null>(null);

  const load = useCallback(() => {
    api.get<Source[]>("/api/sources").then(setSources).catch((e) => toast(e.message, "error"));
    api.get<Status>("/api/status").then(setStatus).catch(() => undefined);
  }, [toast]);

  useEffect(() => {
    load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [load]);

  const scan = async (s: Source) => {
    await api.post(`/api/sources/${s.id}/scan`);
    toast(`Scanning ${s.name}…`, "ok");
    load();
  };

  return (
    <div className="page">
      <div className="page-head">
        <h1>Sources</h1>
        <span className="sub">Folders, Unreal projects, Fab vault caches and .blend files that Stash indexes</span>
        <span className="grow" />
        <button className="btn primary" onClick={() => setAdding(true)}><Icon name="plus" size={15} />Add source</button>
      </div>
      {status && !status.blender && (
        <div className="callout warn" style={{ marginBottom: 16 }}>
          Blender isn't available on the server, so .blend extraction, format conversion and rendered thumbnails are disabled.
          Use the standard Docker image (it bundles Blender) or set <code>STASH_BLENDER</code>.
        </div>
      )}
      {sources.length === 0 ? (
        <div className="empty">
          <Icon name="folder" size={44} stroke={1.1} />
          <h3>No sources yet</h3>
          <p>Point Stash at a folder of downloads, an Unreal project (or the Epic Launcher <code>VaultCache</code>), or a .blend file.</p>
          <button className="btn primary" onClick={() => setAdding(true)}><Icon name="plus" size={15} />Add your first source</button>
        </div>
      ) : (
        <div className="cards">
          {sources.map((s) => {
            const [label, icon] = KIND_INFO[s.kind] ?? [s.kind, "folder"];
            const res = s.meta?.last_result;
            const projects = s.meta?.unreal_projects ? Object.keys(s.meta.unreal_projects) : [];
            return (
              <div key={s.id} className="panel src-card">
                <div className="row">
                  <Icon name={icon} size={20} />
                  <strong className="grow ellipsis">{s.name}</strong>
                  <span className={`status ${s.status}`}>{s.status}</span>
                </div>
                <div className="path">{s.path}</div>
                <div className="row wrap small muted">
                  <span className="kind">{label}</span>
                  <span className="kind">{s.mode === "import" ? "copied into Stash" : "linked in place"}</span>
                  <span>{s.asset_count.toLocaleString()} assets</span>
                  {s.last_scan_at && <span>scanned {new Date(s.last_scan_at).toLocaleString()}</span>}
                </div>
                {projects.length > 0 && <div className="small muted">Unreal: {projects.join(", ")}</div>}
                {res?.missing > 0 && <div className="small" style={{ color: "var(--warn)" }}>{res.missing} files missing since last scan</div>}
                <div className="row wrap" style={{ gap: 6 }}>
                  <Link className="btn sm" to={`/?source=${s.id}`}><Icon name="library" size={13} />Browse</Link>
                  <button className="btn sm" onClick={() => scan(s)} disabled={s.status === "scanning"}><Icon name="refresh" size={13} />Rescan</button>
                  <button className="btn sm" onClick={async () => { const r = await api.post<{ jobs: number }>(`/api/sources/${s.id}/extract-all`); toast(`${r.jobs} Blender jobs queued`, "ok"); }} title="Re-run Blender extraction and previews for everything">
                    <Icon name="blender" size={13} />Re-extract
                  </button>
                  <span className="grow" />
                  <button className="btn sm ghost icon" onClick={() => setEditing(s)} title="Settings"><Icon name="settings" size={14} /></button>
                  <button className="btn sm ghost icon danger" title="Remove source (files are not touched)" onClick={async () => {
                    if (!confirm(`Remove "${s.name}" and its ${s.asset_count} assets from the library? Files on disk are not touched.`)) return;
                    await api.del(`/api/sources/${s.id}`);
                    load();
                  }}><Icon name="trash" size={14} /></button>
                </div>
              </div>
            );
          })}
        </div>
      )}
      {adding && <AddSourceModal roots={status?.browse_roots ?? ["/"]} onClose={() => setAdding(false)} onAdded={() => { setAdding(false); load(); }} />}
      {editing && <SourceSettingsModal source={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />}
    </div>
  );
}

interface FsEntry { name: string; path: string; dir: boolean }

function FsBrowser({ value, onChange }: { value: string; onChange: (p: string) => void }) {
  const [cwd, setCwd] = useState<string | null>(null);
  const [entries, setEntries] = useState<FsEntry[]>([]);
  const [parent, setParent] = useState<string | null>(null);
  const [err, setErr] = useState("");

  const open = useCallback(async (p: string | null) => {
    try {
      const r = await api.get<{ path: string | null; parent: string | null; entries: FsEntry[] }>(`/api/fs${p ? `?path=${encodeURIComponent(p)}` : ""}`);
      setCwd(r.path);
      setParent(r.parent);
      setEntries(r.entries);
      setErr("");
      if (r.path) onChange(r.path);
    } catch (e: any) {
      setErr(e.message);
    }
  }, [onChange]);

  useEffect(() => { open(null); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div>
      <div className="row" style={{ marginBottom: 8 }}>
        <button className="btn sm icon" disabled={!cwd} onClick={() => open(parent)}><Icon name="back" size={14} /></button>
        <code className="grow ellipsis small">{cwd ?? "Roots"}</code>
      </div>
      <div className="fs-list">
        {entries.map((e) => (
          <div key={e.path} className={`fs-item ${value === e.path ? "sel" : ""}`} onClick={() => (e.dir ? open(e.path) : onChange(e.path))}>
            <Icon name={e.dir ? "folder" : e.name.endsWith(".blend") ? "blender" : e.name.endsWith(".uproject") ? "unreal" : "file"} size={15} />
            {e.name}
          </div>
        ))}
        {!entries.length && <div className="muted small" style={{ padding: 12 }}>Empty folder</div>}
      </div>
      {err && <div className="small" style={{ color: "var(--err)", marginTop: 6 }}>{err}</div>}
    </div>
  );
}

function AddSourceModal({ onClose, onAdded }: { roots: string[]; onClose: () => void; onAdded: () => void }) {
  const toast = useToast();
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [mode, setMode] = useState("linked");
  const [group, setGroup] = useState("auto");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    try {
      await api.post("/api/sources", { path, name: name || undefined, mode, options: { group_mode: group } });
      toast("Source added – scanning started", "ok");
      onAdded();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title="Add source" onClose={onClose} width={680} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!path || busy} onClick={submit}>Add & scan</button></>}>
      <p className="muted small" style={{ marginTop: 0 }}>
        Pick a folder (or a single .blend / .uproject) <b>as seen by the server</b> – in Docker, that's a path you mounted into the container (e.g. <code>/library</code>).
        Unreal projects and <code>Content</code> folders (e.g. Epic Launcher <code>VaultCache/*/data/Content</code>) are detected automatically.
      </p>
      <FsBrowser value={path} onChange={setPath} />
      <label className="field" style={{ marginTop: 14 }}><span>Path</span><input className="input mono" value={path} onChange={(e) => setPath(e.target.value)} placeholder="/library/Fab" /></label>
      <label className="field"><span>Name (optional)</span><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></label>
      <div className="row wrap" style={{ gap: 18 }}>
        <label className="field"><span>Storage</span>
          <select className="input" value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="linked">Link in place (files stay where they are)</option>
            <option value="import">Copy into Stash data folder</option>
          </select>
        </label>
        <label className="field"><span>.blend splitting</span>
          <select className="input" value={group} onChange={(e) => setGroup(e.target.value)}>
            <option value="auto">Auto (marked assets, else objects)</option>
            <option value="objects">One asset per root object</option>
            <option value="collections">One asset per collection</option>
            <option value="marked">Only Blender-marked assets</option>
          </select>
        </label>
      </div>
    </Modal>
  );
}

function SourceSettingsModal({ source, onClose, onSaved }: { source: Source; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(source.name);
  const [opts, setOpts] = useState<Record<string, any>>({ group_mode: "auto", render_thumbnails: true, extract_archives: true, ...source.options });
  const save = async () => {
    await api.patch(`/api/sources/${source.id}`, { name, options: opts });
    onSaved();
  };
  return (
    <Modal title="Source settings" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" onClick={save}>Save</button></>}>
      <label className="field"><span>Name</span><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></label>
      <label className="field"><span>.blend splitting</span>
        <select className="input" value={opts.group_mode} onChange={(e) => setOpts({ ...opts, group_mode: e.target.value })}>
          <option value="auto">Auto (marked assets, else objects)</option>
          <option value="objects">One asset per root object</option>
          <option value="collections">One asset per collection</option>
          <option value="marked">Only Blender-marked assets</option>
        </select>
      </label>
      <label className="check"><input type="checkbox" checked={opts.render_thumbnails} onChange={(e) => setOpts({ ...opts, render_thumbnails: e.target.checked })} />Render thumbnails with Blender (Cycles, CPU)</label>
      <label className="field"><span>Rescan automatically every … hours (0 = off)</span>
        <input className="input" type="number" min={0} step={1} value={opts.rescan_hours ?? 0} onChange={(e) => setOpts({ ...opts, rescan_hours: Number(e.target.value) })} />
      </label>
      <label className="check"><input type="checkbox" checked={opts.extract_archives} onChange={(e) => setOpts({ ...opts, extract_archives: e.target.checked })} />Unpack .zip asset packs</label>
      {opts.ignored?.length > 0 && (
        <div className="callout small">
          {opts.ignored.length} removed asset(s) are skipped on rescans.{" "}
          <button className="btn sm" onClick={() => setOpts({ ...opts, ignored: [] })}>Un-ignore all</button>
        </div>
      )}
    </Modal>
  );
}
