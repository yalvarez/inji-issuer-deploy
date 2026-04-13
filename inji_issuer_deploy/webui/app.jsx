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
  issuer_description: "",
  base_domain: "demo.example.org",
  provider: "onprem",
  provisioner: "python",
  data_api_base_url: "",
  data_api_auth_type: "mtls",
  idperu_jwks_uri: "",
  idperu_issuer_uri: "",
  document_number_claim: "individualId",
  filiation_claim: "",
  shared_config_source_namespace: "config-server",
  shared_configmaps: [],
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

function phaseApiName(key) {
  if (key === "config") {
    return "config_gen";
  }
  if (key === "deploy") {
    return "k8s_deploy";
  }
  return key;
}

function App() {
  const [stateFile] = useState("inji-deploy-state.json");
  const [form, setForm] = useState(DEFAULT_FORM);
  const [snapshot, setSnapshot] = useState(null);
  const [preflight, setPreflight] = useState(null);
  const [logs, setLogs] = useState("Ready. Use the wizard to continue.");
  const [busy, setBusy] = useState(false);
  const [currentStep, setCurrentStep] = useState(0);
  const [showAdvanced, setShowAdvanced] = useState(false);

  const phaseMetaMap = useMemo(() => {
    const entries = (snapshot?.phases || []).map((phase) => [phaseKeyFromStatusName(phase.name), phase]);
    return Object.fromEntries(entries);
  }, [snapshot]);

  const nextPhaseKey = useMemo(() => {
    if (!snapshot) {
      return "collect";
    }
    const next = snapshot?.next_phase;
    return next ? phaseKeyFromStatusName(next) : null;
  }, [snapshot]);

  const currentPhaseKey = PHASE_SEQUENCE[currentStep];
  const currentPhaseMeta = phaseMetaMap[currentPhaseKey] || {};
  const currentPhaseInfo = PHASE_INFO[currentPhaseKey];
  const stepLocked = currentPhaseKey !== "collect" && (currentPhaseMeta.locked ?? true);
  const canGoBack = currentStep > 0;
  const canGoNext = currentStep < PHASE_SEQUENCE.length - 1;

  const collectReady = useMemo(() => {
    return Boolean(
      (form.issuer_id || "").trim() &&
      (form.issuer_name || "").trim() &&
      (form.base_domain || "").trim()
    );
  }, [form]);

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

  useEffect(() => {
    if (!snapshot) {
      return;
    }
    if (nextPhaseKey) {
      const idx = PHASE_SEQUENCE.indexOf(nextPhaseKey);
      if (idx >= 0) {
        setCurrentStep(idx);
      }
    }
  }, [snapshot, nextPhaseKey]);

  const updateField = (name, value) => {
    setForm((current) => ({ ...current, [name]: value }));
  };

  const saveConfig = async () => {
    setBusy(true);
    try {
      const payload = await apiPost("/api/issuer-config", form, stateFile);
      setSnapshot(payload.state);
      setLogs(payload.message || "Phase 0 saved.");
      setCurrentStep((prev) => Math.min(prev + 1, PHASE_SEQUENCE.length - 1));
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
      const payload = await apiPost(`/api/run/phase/${phaseApiName(phase)}`, { dry_run: dryRun }, stateFile);
      setLogs(payload.logs || payload.error || "No output returned.");
      if (payload.state) {
        setSnapshot(payload.state);
      } else {
        await refreshState();
      }
      if (!dryRun) {
        setCurrentStep((prev) => Math.min(prev + 1, PHASE_SEQUENCE.length - 1));
      }
    } catch (error) {
      setLogs(`Phase ${phase} failed: ${error.message}`);
    } finally {
      setBusy(false);
    }
  };

  const goToStep = (idx) => {
    if (idx < 0 || idx >= PHASE_SEQUENCE.length) {
      return;
    }
    setCurrentStep(idx);
  };

  const runCurrentStep = async (dryRun) => {
    if (currentPhaseKey === "collect") {
      await saveConfig();
      return;
    }
    await runPhase(currentPhaseKey, dryRun);
  };

  return (
    <div className="page">
      <header className="hero">
        <div>
          <p className="eyebrow">FastAPI + React MVP</p>
          <h1>Inji Issuer Deploy Wizard</h1>
          <p>
            Step-by-step flow with only essential actions. The CLI/state model remains the source of truth.
          </p>
        </div>
      </header>

      <section className="card wide wizard-card">
        <h2>Wizard steps</h2>
        <div className="wizard-strip">
          {PHASE_SEQUENCE.map((key, index) => {
            const info = PHASE_INFO[key];
            const phaseMeta = phaseMetaMap[key] || {};
            const status = phaseMeta.status || "pending";
            const locked = phaseMeta.locked || false;
            const isCurrent = currentPhaseKey === key;
            return (
              <div key={key} className={`wizard-step wizard-step-${status} ${locked ? "wizard-step-locked" : ""} ${isCurrent ? "wizard-step-current" : ""}`}>
                <div className="wizard-step-icon">{info.icon}</div>
                <div>
                  <div className="wizard-step-title">{index + 1}. {info.short}</div>
                  <div className="muted wizard-step-note">
                    {locked ? "Locked" : isCurrent ? "Current" : "Available"}
                  </div>
                </div>
                <PhaseBadge status={status} />
              </div>
            );
          })}
        </div>
      </section>

      <main className="wizard-main">
        <section className="card">
          <h2>{currentPhaseInfo.title}</h2>
          <p className="helper phase-description">{currentPhaseInfo.description}</p>

          {currentPhaseKey === "collect" && (
            <>
              <div className="form-grid compact">
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
                <label>Shared config namespace<input value={form.shared_config_source_namespace || ""} onChange={(e) => updateField("shared_config_source_namespace", e.target.value)} /></label>
              </div>

              <button type="button" className="link-button" onClick={() => setShowAdvanced((v) => !v)}>
                {showAdvanced ? "Hide advanced fields" : "Show advanced fields"}
              </button>

              {showAdvanced && (
                <div className="form-grid compact advanced-grid">
                  <label>Data API base URL<input value={form.data_api_base_url || ""} onChange={(e) => updateField("data_api_base_url", e.target.value)} /></label>
                  <label>IDPeru JWKS URI<input value={form.idperu_jwks_uri || ""} onChange={(e) => updateField("idperu_jwks_uri", e.target.value)} /></label>
                  <label>IDPeru issuer URI<input value={form.idperu_issuer_uri || ""} onChange={(e) => updateField("idperu_issuer_uri", e.target.value)} /></label>
                  <label>Registry backend
                    <select value={form.onprem_registry_backend || "plain"} onChange={(e) => updateField("onprem_registry_backend", e.target.value)}>
                      <option value="plain">plain</option>
                      <option value="harbor">harbor</option>
                    </select>
                  </label>
                  <label>Secrets backend
                    <select value={form.onprem_secrets_backend || "k8s"} onChange={(e) => updateField("onprem_secrets_backend", e.target.value)}>
                      <option value="k8s">k8s</option>
                      <option value="vault">vault</option>
                    </select>
                  </label>
                  <label>Cert issuer kind<input value={form.onprem_cert_issuer_kind || ""} onChange={(e) => updateField("onprem_cert_issuer_kind", e.target.value)} /></label>
                  <label>Cert issuer name<input value={form.onprem_cert_issuer_name || ""} onChange={(e) => updateField("onprem_cert_issuer_name", e.target.value)} /></label>
                  <label>Shared ConfigMaps (comma separated)
                    <input
                      value={Array.isArray(form.shared_configmaps) ? form.shared_configmaps.join(", ") : (form.shared_configmaps || "")}
                      onChange={(e) => updateField("shared_configmaps", e.target.value.split(",").map((item) => item.trim()).filter(Boolean))}
                    />
                  </label>
                </div>
              )}
            </>
          )}

          {currentPhaseKey !== "collect" && (
            <div className="step-summary">
              <div className="phase-item">
                <div>
                  <strong>Step status</strong>
                  <div className="muted">{stepLocked ? (currentPhaseMeta.locked_reason || "Complete previous steps first.") : "This step is unlocked and ready."}</div>
                </div>
                <PhaseBadge status={currentPhaseMeta.status || "pending"} />
              </div>
            </div>
          )}

          {currentPhaseKey === "infra" && (
            <div className="preflight-box">
              <div className="button-row">
                <button onClick={runPreflight} disabled={busy || stepLocked}>Run preflight</button>
              </div>
              {preflight?.checks?.length ? (
                <div className="phase-list compact-list">
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
                <p className="muted">Run preflight for the readiness checklist.</p>
              )}
            </div>
          )}

          <div className="wizard-actions">
            <button type="button" onClick={() => goToStep(currentStep - 1)} disabled={!canGoBack || busy}>Back</button>
            {currentPhaseKey === "collect" ? (
              <button type="button" onClick={saveConfig} disabled={busy || !collectReady}>Save step</button>
            ) : (
              <button type="button" onClick={() => runCurrentStep(false)} disabled={busy || stepLocked}>Run step</button>
            )}
            <button type="button" onClick={() => goToStep(currentStep + 1)} disabled={!canGoNext || busy}>Next</button>
          </div>
        </section>

        <section className="card">
          <details>
            <summary>Technical output</summary>
            <pre className="log-box">{logs}</pre>
          </details>
        </section>
      </main>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
