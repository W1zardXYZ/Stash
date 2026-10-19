import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { KIND_LABELS, PackDetail, PackInfo, api, packUrl } from "../api";
import { Icon } from "../components/Icon";
import { Modal, useToast } from "../components/ui";

function byLine(p: PackInfo) {
  if (!p.description) return null;
  if (p.meta.by === "lmstudio") return `written by ${p.meta.model || "LM Studio"}`;
  if (p.meta.by === "clip") return "from CLIP tags";
  return null;
}

export function PackList() {
  const navigate = useNavigate();
  const [packs, setPacks] = useState<PackInfo[] | null>(null);
  const [filter, setFilter] = useState("");
  const [only, setOnly] = useState<"all" | "described" | "undescribed">("all");
  const [editing, setEditing] = useState<PackInfo | null>(null);
  const load = useCallback(() => api.get<PackInfo[]>("/api/packs").then(setPacks), []);
  useEffect(() => { load(); }, [load]);
  const shown = (packs ?? []).filter((p) =>
    (!filter || `${p.name} ${p.description} ${p.keywords.join(" ")}`.toLowerCase().includes(filter.toLowerCase())) &&
    (only === "all" || (only === "described") === !!p.description));
  const described = (packs ?? []).filter((p) => p.description).length;
  return (
    <div className="page">
      <div className="page-head">
        <h1>Packs</h1>
        <span className="sub">
          The folder, zip or Unreal content folder each asset came from · {described} of {packs?.length ?? 0} described.
          Descriptions are searched with every asset of the pack.
        </span>
      </div>
      <div className="row wrap" style={{ marginBottom: 14, gap: 8 }}>
        <div className="search" style={{ maxWidth: 360 }}>
          <Icon name="search" size={16} />
          <input placeholder="Filter packs" value={filter} onChange={(e) => setFilter(e.target.value)} />
        </div>
        {(["all", "described", "undescribed"] as const).map((o) => (
          <button key={o} className={`btn sm ${only === o ? "primary" : "ghost"}`} onClick={() => setOnly(o)}>{o[0].toUpperCase() + o.slice(1)}</button>
        ))}
      </div>
      {packs === null ? <div className="muted">Loading…</div> : shown.length === 0 ? (
        <div className="empty"><Icon name="layers" size={44} stroke={1.1} /><h3>No packs</h3><p>Packs appear once a source with sub-folders, zips or Unreal content has been scanned.</p></div>
      ) : (
        <div className="cards">
          {shown.map((p) => (
            <div key={p.name} className="panel proj-card" onClick={() => navigate(`/?pack=${encodeURIComponent(p.name)}`)}>
              <div className="covers one">
                {p.cover ? <img src={p.cover} alt="" /> : <div style={{ display: "grid", placeItems: "center", color: "var(--text-3)" }}><Icon name="layers" size={36} stroke={1.1} /></div>}
              </div>
              <div className="panel-pad">
                <div className="row">
                  <strong className="grow" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={p.name}>{p.name}</strong>
                  <button className="btn ghost icon sm" title="Describe" onClick={(e) => { e.stopPropagation(); setEditing(p); }}><Icon name="edit" size={13} /></button>
                </div>
                <div className="muted small">{p.count} assets{byLine(p) ? ` · ${byLine(p)}` : ""}</div>
                {p.description
                  ? <p className="small pack-desc">{p.description}</p>
                  : <p className="small muted pack-desc">No description yet.</p>}
                {p.keywords.length > 0 && <div className="row wrap" style={{ gap: 4 }}>{p.keywords.slice(0, 8).map((k) => <span key={k} className="tag">{k}</span>)}</div>}
              </div>
            </div>
          ))}
        </div>
      )}
      {editing && <PackModal name={editing.name} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); }} />}
    </div>
  );
}

/** Shown above the library grid while it is filtered to one pack. */
export function PackBanner({ name }: { name: string }) {
  const [p, setP] = useState<PackInfo | null>(null);
  const [editing, setEditing] = useState(false);
  const load = useCallback(() => api.get<PackDetail>(`${packUrl(name)}?samples=0`).then(setP).catch(() => setP(null)), [name]);
  useEffect(() => { load(); }, [load]);
  if (!p) return null;
  return (
    <div className="toolbar pack-banner">
      <Icon name="layers" size={18} />
      <div className="grow">
        <strong>{p.name}</strong>
        <span className="muted small" style={{ marginLeft: 8 }}>{p.count} assets{byLine(p) ? ` · ${byLine(p)}` : ""}</span>
        {p.description && <div className="small" style={{ marginTop: 2 }}>{p.description}</div>}
      </div>
      <button className="btn sm" onClick={() => setEditing(true)}><Icon name="edit" size={13} />{p.description ? "Edit" : "Describe"}</button>
      {editing && <PackModal name={name} onClose={() => setEditing(false)} onSaved={() => { setEditing(false); load(); }} />}
    </div>
  );
}

function PackModal({ name, onClose, onSaved }: { name: string; onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const [p, setP] = useState<PackDetail | null>(null);
  const [description, setDescription] = useState("");
  const [keywords, setKeywords] = useState("");
  useEffect(() => {
    api.get<PackDetail>(`${packUrl(name)}?samples=8`).then((d) => {
      setP(d);
      setDescription(d.description);
      setKeywords(d.keywords.join(", "));
    });
  }, [name]);
  const save = async () => {
    try {
      await api.put(packUrl(name), {
        description,
        keywords: keywords.split(",").map((k) => k.trim()).filter(Boolean),
        meta: { by: "user" },
      });
      onSaved();
    } catch (e: any) {
      toast(e.message, "error");
    }
  };
  const clear = async () => { await api.del(packUrl(name)); onSaved(); };
  return (
    <Modal
      title={`Describe "${name}"`}
      onClose={onClose}
      footer={<>{p?.description && <button className="btn ghost danger" onClick={clear}>Clear</button>}<span className="grow" /><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" onClick={save} disabled={!p}>Save</button></>}
    >
      {!p ? <div className="muted">Loading…</div> : (
        <>
          {(p.gallery.length > 0 || p.samples.length > 0) && (
            <div className="row" style={{ gap: 6, overflowX: "auto", marginBottom: 10 }}>
              {[...p.gallery, ...p.samples.map((s) => s.thumbnail)].filter(Boolean).slice(0, 8).map((u) => (
                <img key={u!} src={u!} alt="" style={{ width: 72, height: 72, objectFit: "cover", borderRadius: 6, background: "var(--bg-2)" }} />
              ))}
            </div>
          )}
          <div className="muted small" style={{ marginBottom: 10 }}>
            {Object.entries(p.kinds).map(([k, n]) => `${n} ${KIND_LABELS[k] ?? k}`).join(" · ")}
            {p.auto_tags.length > 0 && <> · looks like: {p.auto_tags.slice(0, 8).map((t) => t.tag).join(", ")}</>}
          </div>
          <label className="field"><span>Description</span>
            <textarea className="input" rows={4} autoFocus value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What is this pack? Theme, style, what it contains…" />
          </label>
          <label className="field"><span>Keywords (comma separated, searched with every asset)</span>
            <input className="input" value={keywords} onChange={(e) => setKeywords(e.target.value)} placeholder="medieval, village, stylized" />
          </label>
        </>
      )}
    </Modal>
  );
}
