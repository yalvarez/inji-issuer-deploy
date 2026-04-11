const { useEffect, useMemo, useState } = React;

const PHASE_INFO = {
  collect: {
    title: "Phase 0 — Issuer configuration",
    short: "Collect issuer data",
    icon: "🧾",
    description: "Capture the issuer identity, provider choice, domain, shared config sources, and integration endpoints.",
  },
  infra: {
    title: "Phase 1 — Infrastructure provisioning",
    short: "Prepare infrastructure",
    icon: "🧱",
    description: "Validate access, prepare namespace/secrets/registry/TLS hooks, and confirm the target environment is ready.",
  },
  config: {
    title: "Phase 2 — Configuration generation",
    short: "Generate config files",
    icon: "⚙️",
    description: "Render the properties, Helm values, ConfigMaps, and issuer patch artifacts required for deployment.",
  },
  deploy: {
    title: "Phase 3 — Kubernetes deployment",
    short: "Deploy services to Kubernetes",
    icon: "🚀",
    description: "Apply the generated configuration and run the Helm-based deployment into the target namespace.",
  },
  register: {
    title: "Phase 4 — Credential registration",
    short: "Register and verify issuer",
    icon: "✅",
    description: "Register credential metadata and perform the final smoke tests for the issuer endpoints.",
  },
};

const PHASE_SEQUENCE = ["collect", "infra", "config", "deploy", "register"];

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

function phaseKeyFromStatusName(name) {
  return name.replace("config_gen", "config").replace("k8s_deploy", "deploy");
}

function App() {
  const [stateFile, setStateFile] = useState("inji-deploy-state.json");
  const [form, setForm] = useState(DEFAULT_FORM);
  const [snapshot, setSnapshot] = useState(null);
  const [preflight, setPreflight] = useState(null);
  const [logs, setLogs] = useState("Ready. Save Phase 0 data or refresh the current state.");
  const [busy, setBusy] = useState(false);

  const artifactCount = useMemo(() => snapshot?.artifacts?.length || 0, [snapshot]);

  const phaseMetaMap = useMemo(() => {
    const entries = (snapshot?.phases || []).map((phase) => [phaseKeyFromStatusName(phase.name), phase]);
    return Object.fromEntries(entries);
  }, [snapshot]);

  const statusMap = useMemo(() => {
    return Object.fromEntries(Object.entries(phaseMetaMap).map(([key, phase]) => [key, phase.status]));
  }, [phaseMetaMap]);

  const nextPhaseKey = useMemo(() => {
    if (!snapshot) {
      return "collect";
    }
    const next = snapshot?.next_phase;
    return next ? phaseKeyFromStatusName(next) : null;
  }, [snapshot]);

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
      setPreflight(payload);
      const lines = (payload.checks || []).map((item) => `- [${item.status}] ${item.label}: ${item.detail}`);
      setLogs(`${payload.ok ? "Preflight OK" : "Preflight check failed"}\n\n${payload.summary}\n${lines.join("\n")}`);
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

      <section className="card wide wizard-card">
        <h2>Guided deployment flow</h2>
        <p className="helper phase-description">Follow the phases from left to right. The UI now unlocks each step only after the required earlier phase has been completed.</p>
        <p className="wizard-summary">
          <strong>Next recommended action:</strong>{" "}
          {nextPhaseKey ? PHASE_INFO[nextPhaseKey]?.title : "All phases are complete. You can review or rerun completed steps if needed."}
        </p>
        <div className="wizard-strip">
          {PHASE_SEQUENCE.map((key, index) => {
            const info = PHASE_INFO[key];
            const phaseMeta = phaseMetaMap[key] || {};
            const status = phaseMeta.status || "pending";
            const locked = phaseMeta.locked || false;
            const isCurrent = nextPhaseKey === key;
            return (
              <div key={key} className={`wizard-step wizard-step-${status} ${locked ? "wizard-step-locked" : ""} ${isCurrent ? "wizard-step-current" : ""}`}>
                <div className="wizard-step-icon">{info.icon}</div>
                <div>
                  <div className="wizard-step-title">{index}. {info.short}</div>
                  <div className="muted wizard-step-text">{info.description}</div>
                  <div className="muted wizard-step-note">
                    {locked ? (phaseMeta.locked_reason || "Complete the previous phase first.") : isCurrent ? "This is the current step to execute next." : "Available once earlier steps are satisfied."}
                  </div>
                </div>
                <PhaseBadge status={status} />
              </div>
            );
          })}
        </div>
      </section>

      <main className="grid">
        <section className="card">
          <h2>{PHASE_INFO.collect.title}</h2>
          <p className="helper phase-description">{PHASE_INFO.collect.description}</p>
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
          <div className="phase-execution-list">
            {["infra", "config", "deploy", "register"].map((key) => {
              const info = PHASE_INFO[key];
              const phaseMeta = phaseMetaMap[key] || {};
              const locked = phaseMeta.locked ?? true;
              const isCurrent = nextPhaseKey === key;
              return (
                <div key={key} className={`phase-action-card ${locked ? "phase-action-card-locked" : ""} ${isCurrent ? "phase-action-card-current" : ""}`}>
                  <div>
                    <strong>{info.icon} {info.short}</strong>
                    <div className="muted">{info.description}</div>
                    <div className="muted">{locked ? (phaseMeta.locked_reason || "Complete the previous phase first.") : isCurrent ? "This step is unlocked and ready to run now." : "This step is available because the earlier phases are already satisfied."}</div>
                  </div>
                  <div className="button-pair">
                    <button onClick={() => runPhase(key, true)} disabled={busy || locked}>{info.short.split(" ")[0]} dry-run</button>
                    <button onClick={() => runPhase(key, false)} disabled={busy || locked}>Run {key}</button>
                  </div>
                </div>
              );
            })}
          </div>
          <p className="helper">For the web flow, Phase 0 is the form above. All other phases reuse the same Python engine as the CLI and are only unlocked in order.</p>
        </section>

        <section className="card wide">
          <h2>Deployment status</h2>
          {snapshot?.phases?.length ? (
            <div className="phase-list">
              {snapshot.phases.map((phase) => (
                <div key={phase.name} className="phase-item">
                  <div>
                    <strong>{phase.label}</strong>
                    <div className="muted">{PHASE_INFO[phaseKeyFromStatusName(phase.name)]?.description || (phase.error || phase.completed_at || "Pending")}</div>
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

        <section className="card wide">
          <h2>Preflight readiness</h2>
          {preflight?.checks?.length ? (
            <div className="phase-list">
              {preflight.checks.map((item) => (
                <div key={item.name} className="phase-item">
                  <div>
                    <strong>{item.label}</strong>
                    <div className="muted">{item.detail}</div>
                  </div>
                  <span className={`badge badge-${item.status}`}>{item.status}</span>
                </div>
              ))}
            </div>
          ) : (
            <p className="muted">Run preflight to see the go/no-go checklist for the current environment.</p>
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
