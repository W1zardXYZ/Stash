import { useCallback, useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import { api, Status } from "./api";
import { Icon } from "./components/Icon";
import { ToastProvider, useToast } from "./components/ui";
import AssetPage from "./pages/AssetPage";
import Library from "./pages/Library";
import { IntegrationsPage, JobsPage, Login, TagsPage, UploadOverlay } from "./pages/Misc";
import { PackList } from "./pages/Packs";
import { ProjectList, ProjectPage } from "./pages/Projects";
import Sources from "./pages/Sources";
import "./styles.css";

/** Walk dropped folders (webkitGetAsEntry) so folder structure survives the upload. */
async function collectDropped(items: DataTransferItemList): Promise<{ file: File; path: string }[]> {
  const out: { file: File; path: string }[] = [];
  const walk = async (entry: any, prefix: string): Promise<void> => {
    if (entry.isFile) {
      const file: File = await new Promise((res, rej) => entry.file(res, rej));
      out.push({ file, path: prefix + file.name });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      let batch: any[];
      do {
        batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        for (const e of batch) await walk(e, `${prefix}${entry.name}/`);
      } while (batch.length);
    }
  };
  const entries = Array.from(items).map((i) => (i as any).webkitGetAsEntry?.()).filter(Boolean);
  for (const e of entries) await walk(e, "");
  return out;
}

function Shell() {
  const toast = useToast();
  const [running, setRunning] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [theme, setTheme] = useState(() => localStorage.getItem("stash.theme") || "dark");

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("stash.theme", theme);
  }, [theme]);

  useEffect(() => {
    const poll = () => api.get<Status>("/api/status").then((s) => setRunning((s.jobs.running || 0) + (s.jobs.queued || 0))).catch(() => undefined);
    poll();
    const t = setInterval(poll, 3000);
    return () => clearInterval(t);
  }, []);

  const onDrop = useCallback(async (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (!e.dataTransfer?.items?.length) return;
    const files = await collectDropped(e.dataTransfer.items);
    if (!files.length) return;
    const fd = new FormData();
    files.forEach(({ file, path }) => { fd.append("files", file); fd.append("paths", path); });
    toast(`Uploading ${files.length} file(s)…`);
    try {
      await api.post("/api/upload", fd);
      toast("Upload complete – indexing", "ok");
    } catch (err: any) {
      toast(err.message, "error");
    }
  }, [toast]);

  useEffect(() => {
    let depth = 0;
    const enter = (e: DragEvent) => { if (e.dataTransfer?.types.includes("Files")) { depth++; setDragging(true); } };
    const leave = () => { depth = Math.max(0, depth - 1); if (!depth) setDragging(false); };
    const over = (e: DragEvent) => e.preventDefault();
    const drop = (e: DragEvent) => { depth = 0; onDrop(e); };
    window.addEventListener("dragenter", enter);
    window.addEventListener("dragleave", leave);
    window.addEventListener("dragover", over);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragenter", enter);
      window.removeEventListener("dragleave", leave);
      window.removeEventListener("dragover", over);
      window.removeEventListener("drop", drop);
    };
  }, [onDrop]);

  return (
    <div className="app">
      <nav className="rail">
        <img className="logo" src="/favicon.svg" alt="Stash" />
        <NavLink to="/" end data-tip="Library"><Icon name="library" size={20} /></NavLink>
        <NavLink to="/projects" data-tip="Projects"><Icon name="project" size={20} /></NavLink>
        <NavLink to="/packs" data-tip="Packs"><Icon name="layers" size={20} /></NavLink>
        <NavLink to="/tags" data-tip="Tags"><Icon name="tag" size={20} /></NavLink>
        <NavLink to="/sources" data-tip="Sources"><Icon name="folder" size={20} /></NavLink>
        <NavLink to="/jobs" data-tip="Jobs">
          <Icon name="jobs" size={20} />
          {running > 0 && <span className="badge">{running > 99 ? "99+" : running}</span>}
        </NavLink>
        <span className="spacer" />
        <NavLink to="/integrations" data-tip="Blender / Unreal / add-ons"><Icon name="plug" size={20} /></NavLink>
        <a onClick={() => setTheme(theme === "dark" ? "light" : "dark")} data-tip="Toggle theme" style={{ cursor: "pointer" }}>
          <Icon name={theme === "dark" ? "sun" : "moon"} size={19} />
        </a>
      </nav>
      <main className="main">
        <Routes>
          <Route path="/" element={<Library />} />
          <Route path="/asset/:id" element={<AssetPage />} />
          <Route path="/projects" element={<ProjectList />} />
          <Route path="/projects/:id" element={<ProjectPage />} />
          <Route path="/packs" element={<PackList />} />
          <Route path="/tags" element={<TagsPage />} />
          <Route path="/sources" element={<Sources />} />
          <Route path="/jobs" element={<JobsPage />} />
          <Route path="/integrations" element={<IntegrationsPage />} />
        </Routes>
      </main>
      {dragging && <UploadOverlay />}
    </div>
  );
}

function App() {
  const [auth, setAuth] = useState<"checking" | "ok" | "login">("checking");
  const check = useCallback(() => {
    api.get<{ auth_required: boolean; authorized: boolean }>("/api/auth/me")
      .then((r) => setAuth(!r.auth_required || r.authorized ? "ok" : "login"))
      .catch(() => setAuth("ok"));
  }, []);
  useEffect(() => {
    check();
    const h = () => setAuth("login");
    window.addEventListener("stash:unauthorized", h);
    return () => window.removeEventListener("stash:unauthorized", h);
  }, [check]);
  if (auth === "checking") return null;
  if (auth === "login") return <Login onDone={() => setAuth("ok")} />;
  return (
    <BrowserRouter>
      <Shell />
    </BrowserRouter>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <ToastProvider>
    <App />
  </ToastProvider>,
);
