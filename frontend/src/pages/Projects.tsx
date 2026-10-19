import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Project, TARGET_LABELS, api, waitForJob } from "../api";
import { Icon } from "../components/Icon";
import { Modal, Progress, useToast } from "../components/ui";
import Library from "./Library";

const COLORS = ["#7c5cff", "#3ecf8e", "#f5a524", "#f0506e", "#4fa3ff", "#2ec4c4", "#ff7a45", "#a3aab6"];

export function ProjectList() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[]>([]);
  const [creating, setCreating] = useState(false);
  const load = useCallback(() => api.get<Project[]>("/api/projects").then(setProjects), []);
  useEffect(() => { load(); }, [load]);
  return (
    <div className="page">
      <div className="page-head">
        <h1>Projects</h1>
        <span className="sub">Collect assets for a game, film or scene – then export them all at once</span>
        <span className="grow" />
        <button className="btn primary" onClick={() => setCreating(true)}><Icon name="plus" size={15} />New project</button>
      </div>
      {projects.length === 0 ? (
        <div className="empty"><Icon name="project" size={44} stroke={1.1} /><h3>No projects yet</h3><p>Create one, then add assets from the library (select → Add to project).</p></div>
      ) : (
        <div className="cards">
          {projects.map((p) => {
            const covers = p.covers.filter(Boolean) as string[];
            return (
              <div key={p.id} className="panel proj-card" onClick={() => navigate(`/projects/${p.id}`)}>
                <div className={`covers ${covers.length < 2 ? "one" : ""}`} style={{ borderBottom: `3px solid ${p.color || "var(--accent)"}` }}>
                  {covers.length ? covers.slice(0, 2).map((c) => <img key={c} src={c} alt="" />) : <div style={{ display: "grid", placeItems: "center", color: "var(--text-3)" }}><Icon name="project" size={36} stroke={1.1} /></div>}
                </div>
                <div className="panel-pad">
                  <strong>{p.name}</strong>
                  <div className="muted small">{p.count} assets{p.description ? ` · ${p.description}` : ""}</div>
                </div>
              </div>
            );
          })}
        </div>
      )}
      {creating && <ProjectModal onClose={() => setCreating(false)} onSaved={() => { setCreating(false); load(); }} />}
    </div>
  );
}

function ProjectModal({ project, onClose, onSaved }: { project?: Project; onClose: () => void; onSaved: (p: Project) => void }) {
  const toast = useToast();
  const [name, setName] = useState(project?.name ?? "");
  const [description, setDescription] = useState(project?.description ?? "");
  const [color, setColor] = useState(project?.color || COLORS[0]);
  const save = async () => {
    try {
      const body = { name, description, color };
      const p = project ? await api.patch<Project>(`/api/projects/${project.id}`, body) : await api.post<Project>("/api/projects", body);
      onSaved(p);
    } catch (e: any) {
      toast(e.message, "error");
    }
  };
  return (
    <Modal title={project ? "Edit project" : "New project"} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!name} onClick={save}>Save</button></>}>
      <label className="field"><span>Name</span><input className="input" autoFocus value={name} onChange={(e) => setName(e.target.value)} /></label>
      <label className="field"><span>Description</span><textarea className="input" value={description} onChange={(e) => setDescription(e.target.value)} /></label>
      <div className="row">{COLORS.map((c) => <button key={c} onClick={() => setColor(c)} style={{ width: 26, height: 26, borderRadius: 7, background: c, border: color === c ? "2px solid var(--text)" : "2px solid transparent", cursor: "pointer" }} />)}</div>
    </Modal>
  );
}

export function ProjectPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const [p, setP] = useState<Project | null>(null);
  const [editing, setEditing] = useState(false);
  const [exporting, setExporting] = useState(false);
  const load = useCallback(() => api.get<Project>(`/api/projects/${id}`).then(setP).catch((e) => toast(e.message, "error")), [id, toast]);
  useEffect(() => { load(); }, [load]);
  if (!p) return <div className="page muted">Loading…</div>;
  const header = (
    <div className="toolbar" style={{ borderBottom: `2px solid ${p.color || "var(--accent)"}` }}>
      <button className="btn ghost icon" onClick={() => navigate("/projects")}><Icon name="back" size={16} /></button>
      <div className="grow">
        <strong style={{ fontSize: 16 }}>{p.name}</strong>
        <span className="muted small" style={{ marginLeft: 10 }}>{p.count} assets {p.description && `· ${p.description}`}</span>
      </div>
      <button className="btn" onClick={() => setEditing(true)}><Icon name="edit" size={14} />Edit</button>
      <button className="btn primary" onClick={() => setExporting(true)}><Icon name="download" size={14} />Export project</button>
      <button className="btn ghost icon danger" onClick={async () => { if (confirm(`Delete project "${p.name}"? Assets stay in the library.`)) { await api.del(`/api/projects/${p.id}`); navigate("/projects"); } }}><Icon name="trash" size={14} /></button>
    </div>
  );
  return (
    <>
      <Library projectId={p.id} header={header} />
      {editing && <ProjectModal project={p} onClose={() => setEditing(false)} onSaved={(np) => { setEditing(false); setP({ ...p, ...np }); }} />}
      {exporting && <PackageModal project={p} onClose={() => setExporting(false)} />}
    </>
  );
}

function PackageModal({ project, onClose }: { project: Project; onClose: () => void }) {
  const [target, setTarget] = useState("blend");
  const [job, setJob] = useState<any>(null);
  const start = async () => {
    const r = await api.post<{ job_id: number }>(`/api/projects/${project.id}/package`, { target });
    const done = await waitForJob(r.job_id, setJob);
    if (done.status === "done") window.location.href = `/api/jobs/${done.id}/download`;
  };
  const busy = job && (job.status === "queued" || job.status === "running");
  return (
    <Modal title={`Export "${project.name}"`} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Close</button><button className="btn primary" disabled={busy} onClick={start}>Build package</button></>}>
      <p className="muted small" style={{ marginTop: 0 }}>Every asset is converted to the chosen format (or its original files where that format doesn't apply) and zipped.</p>
      <label className="field"><span>Format</span>
        <select className="input" value={target} onChange={(e) => setTarget(e.target.value)}>
          {["blend", "unreal", "glb", "fbx", "fbx_unity", "obj", "usdz", "stl", "original"].map((t) => <option key={t} value={t}>{TARGET_LABELS[t]?.label ?? t}</option>)}
        </select>
      </label>
      {job && (
        <div className="col">
          <div className="small">{job.status} {job.message}</div>
          <Progress value={job.progress} />
          {job.status === "done" && <a className="btn" href={`/api/jobs/${job.id}/download`}><Icon name="download" size={14} />Download {job.result?.name}</a>}
          {job.result?.errors?.length > 0 && <pre className="log">{job.result.errors.join("\n")}</pre>}
          {job.status === "error" && <div style={{ color: "var(--err)" }}>{job.message}</div>}
        </div>
      )}
    </Modal>
  );
}
