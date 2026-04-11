const { useEffect, useMemo, useState } = React;

const DEFAULT_FORM = {
  issuer_id: "demo-onprem",
  issuer_name: "Demo OnPrem",
  issuer_description: "Thin UI over the CLI engine",
  base_domain: "demo.example.org",
  provider: "onprem",
  provisioner: "python",
  data_api_base_url: "https://api.example.org",
  data_api_auth_type: "mtls",
  idperu_jwks_uri: "https://id.example.org/jwks",
  idperu_issuer_uri: "https://id.example.org/issuer",
  document_number_claim: "individualId",
  filiation_claim: "",
  shared_config_source_namespace: "platform-shared",
  shared_configmaps: ["global-config", "issuer-common"],
  onprem_registry_backend: "plain",
  onprem_secrets_backend: "k8s",
  onprem_cert_issuer_name: "letsencrypt-prod",
  onprem_cert_issuer_kind: "ClusterIssuer",
};

async function apiGet(path, stateFile) {
  const url = new URL(path, window.location.origin);
  if (stateFile) {
    url.searchParams.set("state_file", stateFile);
  }
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  const contentType = response.headers.get("content-type") || "";
  return contentType.includes("application/json") ? response.json() : response.text();
}

async function apiPost(path, body, stateFile) {
  const url = new URL(path, window.location.origin);
  if (stateFile) {
    url.searchParams.set("state_file", stateFile);
  }
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : null,
  });
  const payload = await response.json();
  return payload;
}

function PhaseBadge({ status }) {
  return <span className={`badge badge-${status}`}>{status}</span>;
}

function App() {
  const [stateFile, setStateFile] = useState("inji-deploy-state.json");
  const [form, setForm] = useState(DEFAULT_FORM);
  const [snapshot, setSnapshot] = useState(null);
  const [logs, setLogs] = useState("Ready. Save Phase 0 data or refresh the current state.");
  const [busy, setBusy] = useState(false);

  const artifactCount = useMemo(() => snapshot?.artifacts?.length || 0, [snapshot]);

  const refreshState = async () => {
    setBusy(true);
    try {
      const payload = await apiGet("/api/state", stateFile);
      setSnapshot(payload);
      if (payload?.issuer?.issuer_id) {
        setForm((current) => ({ ...current, ...payload.issuer, ...payload.provider_cfg }));
      }
    } catch (error) {
      setLogs(`Unable to load state: ${error.message}`);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    refreshState();
  }, []);

  const updateField = (name, value) => {
    setForm((current) => ({ ...current, [name]: value }));
  };

  const saveConfig = async () => {
    setBusy(true);
    try {
      const payload = await apiPost("/api/issuer-config", form, stateFile);
      setSnapshot(payload.state);
      setLogs(payload.message || "Phase 0 saved.");
    } catch (error) {
      setLogs(`Save failed: ${error.message}`);
    } finally {
      setBusy(false);
    }
  };

  const runPreflight = async () => {
    setBusy(true);
    try {
      const payload = await apiPost("/api/preflight", {}, stateFile);
      setLogs(`${payload.ok ? "Preflight OK" : "Preflight check failed"}\n\n${payload.message}`);
    } catch (error) {
      setLogs(`Preflight failed: ${error.message}`);
    } finally {
      setBusy(false);
    }
  };

  const runPhase = async (phase, dryRun) => {
    setBusy(true);
    try {
      const payload = await apiPost(`/api/run/phase/${phase}`, { dry_run: dryRun }, stateFile);
      setLogs(payload.logs || payload.error || "No output returned.");
      if (payload.state) {
        setSnapshot(payload.state);
      } else {
        await refreshState();
      }
    } catch (error) {
      setLogs(`Phase ${phase} failed: ${error.message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <header className="hero">
        <div>
          <p className="eyebrow">FastAPI + React MVP</p>
          <h1>Inji Issuer Deploy UI</h1>
          <p>
            Thin operational layer over the <code>inji-issuer-deploy</code> CLI. The CLI/state model
            remains the source of truth for on-prem and future cloud providers.
          </p>
        </div>
        <div className="hero-actions">
          <label>
            State file
            <input value={stateFile} onChange={(e) => setStateFile(e.target.value)} />
          </label>
          <button onClick={refreshState} disabled={busy}>Refresh state</button>
          <button onClick={runPreflight} disabled={busy}>Run preflight</button>
        </div>
      </header>

      <main className="grid">
        <section className="card">
          <h2>Phase 0 — Issuer configuration</h2>
          <div className="form-grid">
            <label>Issuer ID<input value={form.issuer_id || ""} onChange={(e) => updateField("issuer_id", e.target.value)} /></label>
            <label>Issuer name<input value={form.issuer_name || ""} onChange={(e) => updateField("issuer_name", e.target.value)} /></label>
            <label>Base domain<input value={form.base_domain || ""} onChange={(e) => updateField("base_domain", e.target.value)} /></label>
            <label>Provider
              <select value={form.provider || "onprem"} onChange={(e) => updateField("provider", e.target.value)}>
                <option value="onprem">onprem</option>
                <option value="aws">aws</option>
                <option value="azure">azure</option>
                <option value="gcp">gcp</option>
              </select>
            </label>
            <label>Provisioner
              <select value={form.provisioner || "python"} onChange={(e) => updateField("provisioner", e.target.value)}>
                <option value="python">python</option>
                <option value="terraform">terraform</option>
              </select>
            </label>
            <label>Data API base URL<input value={form.data_api_base_url || ""} onChange={(e) => updateField("data_api_base_url", e.target.value)} /></label>
            <label>IDPeru JWKS URI<input value={form.idperu_jwks_uri || ""} onChange={(e) => updateField("idperu_jwks_uri", e.target.value)} /></label>
            <label>IDPeru issuer URI<input value={form.idperu_issuer_uri || ""} onChange={(e) => updateField("idperu_issuer_uri", e.target.value)} /></label>
            <label>Shared ConfigMaps (comma separated)
              <input
                value={Array.isArray(form.shared_configmaps) ? form.shared_configmaps.join(", ") : (form.shared_configmaps || "")}
                onChange={(e) => updateField("shared_configmaps", e.target.value.split(",").map((item) => item.trim()).filter(Boolean))}
              />
            </label>
            <label>Source namespace<input value={form.shared_config_source_namespace || ""} onChange={(e) => updateField("shared_config_source_namespace", e.target.value)} /></label>
          </div>
          <div className="button-row">
            <button onClick={saveConfig} disabled={busy}>Save Phase 0</button>
          </div>
        </section>

        <section className="card">
          <h2>Phase execution</h2>
          <div className="button-grid">
            <button onClick={() => runPhase("infra", true)} disabled={busy}>Phase 1 dry-run</button>
            <button onClick={() => runPhase("infra", false)} disabled={busy}>Run Phase 1</button>
            <button onClick={() => runPhase("config", true)} disabled={busy}>Phase 2 dry-run</button>
            <button onClick={() => runPhase("config", false)} disabled={busy}>Run Phase 2</button>
            <button onClick={() => runPhase("deploy", true)} disabled={busy}>Phase 3 dry-run</button>
            <button onClick={() => runPhase("deploy", false)} disabled={busy}>Run Phase 3</button>
            <button onClick={() => runPhase("register", true)} disabled={busy}>Phase 4 dry-run</button>
            <button onClick={() => runPhase("register", false)} disabled={busy}>Run Phase 4</button>
          </div>
          <p className="helper">For the web flow, Phase 0 is the form above. All other phases reuse the same Python engine as the CLI.</p>
        </section>

        <section className="card wide">
          <h2>Deployment status</h2>
          {snapshot?.phases?.length ? (
            <div className="phase-list">
              {snapshot.phases.map((phase) => (
                <div key={phase.name} className="phase-item">
                  <div>
                    <strong>{phase.label}</strong>
                    <div className="muted">{phase.error || phase.completed_at || "Pending"}</div>
                  </div>
                  <PhaseBadge status={phase.status} />
                </div>
              ))}
            </div>
          ) : (
            <p className="muted">No deployment state found yet.</p>
          )}
        </section>

        <section className="card">
          <h2>Artifacts</h2>
          <p className="muted">Generated files detected: {artifactCount}</p>
          <ul className="artifact-list">
            {(snapshot?.artifacts || []).map((artifact) => (
              <li key={artifact.path}><code>{artifact.name}</code></li>
            ))}
          </ul>
        </section>

        <section className="card wide">
          <h2>Logs / output</h2>
          <pre className="log-box">{logs}</pre>
        </section>
      </main>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
