import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { AssetBrief, KIND_LABELS, formatCount } from "../api";
import { Icon, KIND_ICON } from "./Icon";

// ---------------------------------------------------------------- toasts
interface Toast { id: number; text: string; kind: "info" | "ok" | "error" }
const ToastCtx = createContext<(text: string, kind?: Toast["kind"]) => void>(() => undefined);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((text: string, kind: Toast["kind"] = "info") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "error" ? 7000 : 3500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toast-wrap">
        {toasts.map((t) => <div key={t.id} className={`toast ${t.kind}`}>{t.text}</div>)}
      </div>
    </ToastCtx.Provider>
  );
}

export const useToast = () => useContext(ToastCtx);

// ---------------------------------------------------------------- modal
export function Modal({ title, onClose, children, footer, width }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; width?: number }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <div className="modal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" style={width ? { width: `min(${width}px, 100%)` } : undefined}>
        <div className="m-head">
          <h3>{title}</h3>
          <button className="btn ghost icon sm" onClick={onClose} title="Close"><Icon name="x" size={16} /></button>
        </div>
        <div className="m-body">{children}</div>
        {footer && <div className="m-foot">{footer}</div>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- keyboard access
/** Click + Enter/Space for an element that can't be a <button> because it holds other controls or block content. */
export function pressable<E extends Element>(onActivate: (e: React.MouseEvent<E> | React.KeyboardEvent<E>) => void) {
  return {
    role: "button",
    tabIndex: 0,
    onClick: onActivate,
    onKeyDown: (e: React.KeyboardEvent<E>) => {
      if ((e.key === "Enter" || e.key === " ") && e.target === e.currentTarget) {
        e.preventDefault();
        onActivate(e);
      }
    },
  };
}

// ---------------------------------------------------------------- cards
export function KindBadge({ kind }: { kind: string }) {
  return <span className={`kind ${kind}`}>{KIND_LABELS[kind] ?? kind}</span>;
}

export function Thumb({ asset, size = 56 }: { asset: Pick<AssetBrief, "thumbnail" | "kind">; size?: number }) {
  return asset.thumbnail ? (
    <img src={asset.thumbnail} alt="" loading="lazy" />
  ) : (
    <Icon name={KIND_ICON[asset.kind] ?? "file"} size={size} stroke={1.2} className="placeholder" />
  );
}

export function AssetCard({ asset, selected, onClick, onSelect }: { asset: AssetBrief; selected?: boolean; onClick: (e: React.MouseEvent | React.KeyboardEvent) => void; onSelect?: () => void }) {
  const cover = asset.kind === "texture" || asset.kind === "hdri" || (asset.kind === "material" && asset.format === "pbr");
  return (
    <div className={`card ${selected ? "selected" : ""}`} {...pressable(onClick)} aria-label={asset.name} title={asset.source_path}>
      <div className={`thumb ${cover ? "cover" : ""}`}>
        <Thumb asset={asset} />
        <div className="flags">
          {asset.is_rigged && <span className="flag"><Icon name="bone" size={11} />rig</span>}
          {asset.is_animated && <span className="flag"><Icon name="film" size={11} />anim</span>}
          {asset.is_procedural && <span className="flag"><Icon name="sparkle" size={11} />proc</span>}
          {!!asset.child_count && <span className="flag"><Icon name="layers" size={11} />{asset.child_count}</span>}
        </div>
        {asset.favorite && !selected && <span className="fav"><Icon name="star" size={15} /></span>}
        {onSelect && (
          <span className="check" {...pressable((e) => { e.stopPropagation(); onSelect(); })} role="checkbox" aria-checked={!!selected} aria-label={`Select ${asset.name}`}>
            {selected && <Icon name="check" size={13} stroke={3} />}
          </span>
        )}
        {asset.status === "missing" && <div className="missing">missing</div>}
      </div>
      <div className="meta">
        <div className="name">{asset.name}</div>
        <div className="sub">
          <KindBadge kind={asset.kind} />
          <span className="ellipsis">{asset.ue_class && asset.ue_class !== KIND_LABELS[asset.kind] ? asset.ue_class : asset.format}</span>
          {asset.poly_count > 0 && <span style={{ marginLeft: "auto" }}>{formatCount(asset.poly_count)}△</span>}
        </div>
      </div>
    </div>
  );
}

export function MiniCard({ asset, note, onClick }: { asset: AssetBrief; note?: string; onClick: () => void }) {
  return (
    <div className="mini" {...pressable(onClick)} title={asset.name}>
      <div className="t"><Thumb asset={asset} size={28} /></div>
      <div className="n">{asset.name}</div>
      {note && <div className="r">{note}</div>}
    </div>
  );
}

// ---------------------------------------------------------------- tag input
export function TagInput({ tags, onAdd, onRemove, suggestions = [] }: { tags: string[]; onAdd: (t: string[]) => void; onRemove: (t: string) => void; suggestions?: string[] }) {
  const [v, setV] = useState("");
  const submit = () => {
    const parts = v.split(",").map((x) => x.trim().toLowerCase()).filter(Boolean);
    if (parts.length) onAdd(parts);
    setV("");
  };
  const listId = "tag-suggest";
  return (
    <div className="col" style={{ gap: 8 }}>
      <div className="chips">
        {tags.map((t) => (
          <span key={t} className="tag">{t}<button onClick={() => onRemove(t)} title="Remove"><Icon name="x" size={12} /></button></span>
        ))}
        {!tags.length && <span className="muted small">No tags yet</span>}
      </div>
      <div className="row">
        <input className="input" placeholder="Add tags (comma separated)…" value={v} list={listId} onChange={(e) => setV(e.target.value)} onKeyDown={(e) => e.key === "Enter" && submit()} />
        <button className="btn" onClick={submit} title="Add tags"><Icon name="plus" size={14} /></button>
        <datalist id={listId}>{suggestions.map((s) => <option key={s} value={s} />)}</datalist>
      </div>
    </div>
  );
}

export function Stars({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  return (
    <span className="stars">
      {[1, 2, 3, 4, 5].map((i) => (
        <button key={i} className={i <= value ? "on" : ""} onClick={() => onChange(i === value ? 0 : i)} title={`${i} star${i > 1 ? "s" : ""}`} aria-pressed={i <= value}><Icon name="star" size={16} /></button>
      ))}
    </span>
  );
}

export function Progress({ value }: { value: number }) {
  return <div className="progress"><div style={{ width: `${Math.round(value * 100)}%` }} /></div>;
}

export function CopyField({ value }: { value: string }) {
  const toast = useToast();
  return (
    <div className="copy">
      <code>{value}</code>
      <button className="btn sm icon" onClick={() => { navigator.clipboard?.writeText(value); toast("Copied", "ok"); }} title="Copy"><Icon name="copy" size={14} /></button>
    </div>
  );
}
