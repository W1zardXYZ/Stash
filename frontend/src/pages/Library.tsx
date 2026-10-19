import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { AssetBrief, Facets, KIND_LABELS, Project, Source, api, formatCount, getStatus, qs } from "../api";
import { Icon, KIND_ICON } from "../components/Icon";
import { AssetCard, Modal, useToast } from "../components/ui";

const PER_PAGE = 80;
const KIND_ORDER = ["model", "skeletal_model", "animation", "material", "texture", "hdri", "node_group", "scene", "blueprint", "unreal_asset", "addon", "audio", "other"];
const FLAG_LABELS: Record<string, [string, string]> = {
  favorite: ["Favorites", "star"],
  rigged: ["Rigged", "bone"],
  animated: ["Animated", "film"],
  procedural: ["Procedural", "sparkle"],
  textured: ["Textured", "image"],
  untagged: ["Untagged", "tag"],
  duplicate: ["Duplicates", "copy"],
};

function toggleIn(list: string[], v: string) {
  return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
}

export default function Library({ projectId, header }: { projectId?: number; header?: React.ReactNode }) {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const toast = useToast();
  const q = params.get("q") ?? "";
  const kinds = (params.get("kind") ?? "").split(",").filter(Boolean);
  const tags = (params.get("tags") ?? "").split(",").filter(Boolean);
  const flags = (params.get("flags") ?? "").split(",").filter(Boolean);
  const source = params.get("source") ?? "";
  const ueClass = params.get("ue_class") ?? "";
  const pack = params.get("pack") ?? "";
  const sort = params.get("sort") ?? (q ? "relevance" : "updated");
  const order = params.get("order") ?? "desc";
  const rootOnly = params.get("root") === "1";
  const smart = params.get("smart") === "1";

  const [items, setItems] = useState<AssetBrief[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [facets, setFacets] = useState<Facets | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [selecting, setSelecting] = useState(false);
  const [cardSize, setCardSize] = useState(() => Number(localStorage.getItem("stash.card") || 180));
  const [searchText, setSearchText] = useState(q);
  const [bulkTag, setBulkTag] = useState(false);
  const [showSide, setShowSide] = useState(false);
  const [aiEnabled, setAiEnabled] = useState(false);
  const [localAi, setLocalAi] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const reqId = useRef(0);

  const update = useCallback(
    (patch: Record<string, string | string[] | null>) => {
      const next = new URLSearchParams(params);
      for (const [k, v] of Object.entries(patch)) {
        const s = Array.isArray(v) ? v.join(",") : v;
        if (s) next.set(k, s);
        else next.delete(k);
      }
      setParams(next, { replace: true });
    },
    [params, setParams],
  );

  const query = useMemo(
    () => ({ q, kind: kinds, tags, flags, source, ue_class: ueClass, pack, semantic: smart && q ? 1 : undefined, sort, order, project: projectId, parent: rootOnly ? "root" : undefined }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [params.toString(), projectId],
  );

  const load = useCallback(
    async (p: number) => {
      const id = ++reqId.current;
      setLoading(true);
      try {
        const r = await api.get<{ items: AssetBrief[]; total: number }>(`/api/assets${qs({ ...query, page: p, per_page: PER_PAGE })}`);
        if (id !== reqId.current) return;
        setTotal(r.total);
        setItems((prev) => (p === 1 ? r.items : [...prev, ...r.items]));
        setPage(p);
      } catch (e: any) {
        toast(e.message, "error");
      } finally {
        if (id === reqId.current) setLoading(false);
      }
    },
    [query, toast],
  );

  useEffect(() => {
    load(1);
    api.get<Facets>(`/api/facets${qs({ q: smart ? undefined : q, source, project: projectId })}`).then(setFacets).catch(() => undefined);
  }, [load, q, source, projectId, smart]);

  useEffect(() => {
    api.get<Source[]>("/api/sources").then(setSources).catch(() => undefined);
    api.get<Project[]>("/api/projects").then(setProjects).catch(() => undefined);
    getStatus().then((s) => { setAiEnabled(s.ai); setLocalAi(!!s.local_ai?.enabled); }).catch(() => undefined);
  }, []);

  useEffect(() => setSearchText(q), [q]);
  useEffect(() => {
    const t = setTimeout(() => searchText !== q && update({ q: searchText, sort: null }), 250);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchText]);

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if ((e.key === "/" || (e.key === "k" && (e.metaKey || e.ctrlKey))) && document.activeElement?.tagName !== "INPUT") {
        e.preventDefault();
        searchRef.current?.focus();
      }
      if (e.key === "Escape") { setSelected(new Set()); setSelecting(false); }
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, []);

  const onScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    if (!loading && items.length < total && el.scrollTop + el.clientHeight > el.scrollHeight - 600) load(page + 1);
  };

  const lastClicked = useRef<number | null>(null);
  const clickCard = (a: AssetBrief, idx: number, e: React.MouseEvent) => {
    if (selecting || e.shiftKey || e.metaKey || e.ctrlKey) {
      const next = new Set(selected);
      if (e.shiftKey && lastClicked.current !== null) {
        const [from, to] = [lastClicked.current, idx].sort((x, y) => x - y);
        for (let i = from; i <= to; i++) next.add(items[i].id);
      } else if (next.has(a.id)) next.delete(a.id);
      else next.add(a.id);
      lastClicked.current = idx;
      setSelected(next);
      setSelecting(true);
      return;
    }
    navigate(`/asset/${a.id}`);
  };

  const bulk = async (body: Record<string, unknown>, msg: string) => {
    try {
      await api.post("/api/assets/bulk", { ids: [...selected], ...body });
      toast(msg, "ok");
      load(1);
      api.get<Facets>(`/api/facets${qs({ q: smart ? undefined : q, source, project: projectId })}`).then(setFacets);
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const setSize = (v: number) => {
    setCardSize(v);
    localStorage.setItem("stash.card", String(v));
  };

  const activeFilters = kinds.length + tags.length + flags.length + (source ? 1 : 0) + (ueClass ? 1 : 0) + (pack ? 1 : 0);

  return (
    <div className={`library ${showSide ? "show-side" : ""}`}>
      <aside className="sidebar">
        <h4>Type</h4>
        {KIND_ORDER.filter((k) => facets?.kinds[k]).map((k) => (
          <div key={k} className={`facet ${kinds.includes(k) ? "on" : ""}`} onClick={() => update({ kind: toggleIn(kinds, k) })}>
            <Icon name={KIND_ICON[k]} size={15} />
            {KIND_LABELS[k]}
            <span className="count">{formatCount(facets!.kinds[k])}</span>
          </div>
        ))}
        <h4>Features</h4>
        {Object.entries(FLAG_LABELS).map(([f, [label, icon]]) =>
          facets?.flags[f] ? (
            <div key={f} className={`facet ${flags.includes(f) ? "on" : ""}`} onClick={() => update({ flags: toggleIn(flags, f) })}>
              <Icon name={icon} size={15} />
              {label}
              <span className="count">{formatCount(facets.flags[f])}</span>
            </div>
          ) : null,
        )}
        <div className={`facet ${rootOnly ? "on" : ""}`} onClick={() => update({ root: rootOnly ? null : "1" })} title="Hide objects extracted from files">
          <Icon name="layers" size={15} />Top-level only
        </div>
        {!projectId && sources.length > 0 && (
          <>
            <h4>Sources</h4>
            {sources.map((s) => (
              <div key={s.id} className={`facet ${source === String(s.id) ? "on" : ""}`} onClick={() => update({ source: source === String(s.id) ? null : String(s.id) })}>
                <Icon name={s.kind === "unreal_project" ? "unreal" : s.kind === "blender_file" ? "blender" : "folder"} size={15} />
                <span className="ellipsis">{s.name}</span>
                <span className="count">{formatCount(s.asset_count)}</span>
              </div>
            ))}
          </>
        )}
        {facets && Object.keys(facets.packs ?? {}).length > 1 && (
          <>
            <h4>Packs</h4>
            {Object.entries(facets.packs).slice(0, 40).map(([p, n]) => (
              <div key={p} className={`facet ${pack === p ? "on" : ""}`} onClick={() => update({ pack: pack === p ? null : p })} title={p}>
                <Icon name="layers" size={15} />
                <span className="ellipsis">{p}</span>
                <span className="count">{formatCount(n)}</span>
              </div>
            ))}
          </>
        )}
        {facets && Object.keys(facets.ue_classes).length > 0 && (
          <>
            <h4>Unreal class</h4>
            {Object.entries(facets.ue_classes).sort((a, b) => b[1] - a[1]).slice(0, 14).map(([c, n]) => (
              <div key={c} className={`facet ${ueClass === c ? "on" : ""}`} onClick={() => update({ ue_class: ueClass === c ? null : c })}>
                <Icon name="unreal" size={15} />
                <span className="ellipsis">{c}</span>
                <span className="count">{formatCount(n)}</span>
              </div>
            ))}
          </>
        )}
        {facets && facets.tags.length > 0 && (
          <>
            <h4>Tags</h4>
            {facets.tags.slice(0, 60).map((t) => (
              <div key={t.name} className={`facet ${tags.includes(t.name) ? "on" : ""}`} onClick={() => update({ tags: toggleIn(tags, t.name) })}>
                <span className="dot" style={{ background: t.color || "var(--text-3)" }} />
                <span className="ellipsis">{t.name}</span>
                <span className="count">{formatCount(t.count)}</span>
              </div>
            ))}
          </>
        )}
      </aside>

      <section className="lib-main" style={{ position: "relative" }}>
        {header}
        <div className="toolbar">
          <button className="btn icon ghost" onClick={() => setShowSide(!showSide)} title="Filters"><Icon name="filter" size={16} /></button>
          <div className="search">
            <Icon name="search" size={16} />
            <input ref={searchRef} placeholder="Search names, tags, materials, bones… ( -exclude  &quot;phrase&quot; )" value={searchText} onChange={(e) => setSearchText(e.target.value)} />
            {searchText ? <button className="btn ghost sm icon" onClick={() => setSearchText("")}><Icon name="x" size={14} /></button> : <kbd>/</kbd>}
          </div>
          {localAi && (
            <button className={`btn ${smart ? "on" : ""}`} onClick={() => update({ smart: smart ? null : "1" })} title="Smart search: match what thumbnails look like, not just names and tags (on-device AI)">
              <Icon name="sparkle" size={15} />Smart
            </button>
          )}
          <select className="input" style={{ width: 175 }} value={sort} onChange={(e) => update({ sort: e.target.value })}>
            {q && <option value="relevance">Relevance</option>}
            <option value="updated">Recently updated</option>
            <option value="created">Recently added</option>
            <option value="name">Name</option>
            <option value="size">File size</option>
            <option value="polys">Polygon count</option>
            <option value="rating">Rating</option>
          </select>
          <button className="btn icon" title="Sort order" onClick={() => update({ order: order === "asc" ? "desc" : "asc" })}>{order === "asc" ? "↑" : "↓"}</button>
          <input type="range" min={120} max={320} value={cardSize} onChange={(e) => setSize(Number(e.target.value))} title="Thumbnail size" style={{ width: 90 }} />
          <button className={`btn ${selecting ? "on" : ""}`} onClick={() => { setSelecting(!selecting); setSelected(new Set()); }}>
            <Icon name="select" size={15} />Select
          </button>
        </div>
        <div className="grid-wrap" onScroll={onScroll}>
          <div className="result-info">
            <span>{loading && page === 1 ? "Loading…" : `${total.toLocaleString()} assets`}</span>
            {activeFilters > 0 && (
              <button className="btn ghost sm" onClick={() => update({ kind: null, tags: null, flags: null, source: null, ue_class: null, pack: null })}>
                <Icon name="x" size={12} />Clear {activeFilters} filter{activeFilters > 1 ? "s" : ""}
              </button>
            )}
            {selecting && (
              <button className="btn ghost sm" onClick={() => setSelected(new Set(items.map((i) => i.id)))}>Select all loaded</button>
            )}
          </div>
          {!loading && items.length === 0 ? (
            <div className="empty">
              <Icon name="cube" size={44} stroke={1.1} />
              <h3>{total === 0 && !q && !activeFilters ? "Your library is empty" : "Nothing matches"}</h3>
              {total === 0 && !q && !activeFilters ? (
                <p>Add a folder, Unreal project or .blend file under <a href="/sources" style={{ color: "var(--accent-2)" }}>Sources</a>, or drop files anywhere on this page.</p>
              ) : (
                <p>Try a different search or remove some filters.</p>
              )}
            </div>
          ) : (
            <div className={`grid ${selecting ? "selecting" : ""}`} style={{ ["--card" as any]: `${cardSize}px` }}>
              {items.map((a, i) => (
                <AssetCard
                  key={a.id}
                  asset={a}
                  selected={selected.has(a.id)}
                  onClick={(e) => clickCard(a, i, e)}
                  onSelect={() => clickCard(a, i, { shiftKey: false, metaKey: true, ctrlKey: false } as React.MouseEvent)}
                />
              ))}
            </div>
          )}
          {loading && page > 1 && <div className="muted" style={{ padding: 20, textAlign: "center" }}>Loading more…</div>}
        </div>

        {selected.size > 0 && (
          <div className="bulkbar">
            <strong style={{ padding: "0 6px" }}>{selected.size} selected</strong>
            <button className="btn sm" onClick={() => setBulkTag(true)}><Icon name="tag" size={14} />Tag</button>
            <select
              className="input" style={{ height: 28, width: 170 }} value=""
              onChange={(e) => e.target.value && bulk({ add_to_project: Number(e.target.value) }, "Added to project")}
            >
              <option value="">Add to project…</option>
              {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
            {projectId && <button className="btn sm" onClick={() => bulk({ remove_from_project: projectId }, "Removed from project")}>Remove from project</button>}
            <button className="btn sm" title="Send to Blender" onClick={() => api.post("/api/deliveries", { app: "blender", asset_ids: [...selected] }).then(() => toast("Sent to Blender", "ok"))}><Icon name="blender" size={14} /></button>
            <button className="btn sm" title="Send to Unreal" onClick={() => api.post("/api/deliveries", { app: "unreal", asset_ids: [...selected] }).then(() => toast("Sent to Unreal", "ok"))}><Icon name="unreal" size={14} /></button>
            <button className="btn sm" onClick={() => bulk({ favorite: true }, "Marked as favorite")}><Icon name="star" size={14} /></button>
            {localAi && (
              <button className="btn sm" title="Add the on-device AI tag suggestions as real tags" onClick={() => api.post<{ added: number }>("/api/ai/accept-suggestions", { ids: [...selected] }).then((r) => { toast(`${r.added} suggested tags added`, "ok"); load(1); }).catch((e) => toast(e.message, "error"))}>
                <Icon name="check" size={14} />Accept suggestions
              </button>
            )}
            {aiEnabled && (
              <button className="btn sm" title="Categorize & tag with Claude (cloud)" onClick={() => api.post<{ jobs: number[] }>("/api/ai/tag", { ids: [...selected] }).then((r) => toast(`AI tagging queued for ${r.jobs.length} asset(s)`, "ok")).catch((e) => toast(e.message, "error"))}>
                <Icon name="sparkle" size={14} />Claude tag
              </button>
            )}
            <button className="btn sm danger" onClick={() => confirm(`Remove ${selected.size} assets from the library? Files on disk are not touched.`) && bulk({ delete: true }, "Removed")}>
              <Icon name="trash" size={14} />
            </button>
            <button className="btn sm ghost" onClick={() => { setSelected(new Set()); setSelecting(false); }}><Icon name="x" size={14} /></button>
          </div>
        )}
        {bulkTag && <BulkTagModal onClose={() => setBulkTag(false)} onApply={(add, remove) => { setBulkTag(false); bulk({ add_tags: add, remove_tags: remove }, "Tags updated"); }} suggestions={facets?.tags.map((t) => t.name) ?? []} />}
      </section>
    </div>
  );
}

function BulkTagModal({ onClose, onApply, suggestions }: { onClose: () => void; onApply: (add: string[], remove: string[]) => void; suggestions: string[] }) {
  const [add, setAdd] = useState("");
  const [remove, setRemove] = useState("");
  const split = (s: string) => s.split(",").map((x) => x.trim().toLowerCase()).filter(Boolean);
  return (
    <Modal title="Tag selected assets" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" onClick={() => onApply(split(add), split(remove))}>Apply</button></>}>
      <label className="field"><span>Add tags (comma separated)</span><input className="input" autoFocus list="bulk-tags" value={add} onChange={(e) => setAdd(e.target.value)} /></label>
      <label className="field"><span>Remove tags</span><input className="input" list="bulk-tags" value={remove} onChange={(e) => setRemove(e.target.value)} /></label>
      <datalist id="bulk-tags">{suggestions.map((s) => <option key={s} value={s} />)}</datalist>
    </Modal>
  );
}
