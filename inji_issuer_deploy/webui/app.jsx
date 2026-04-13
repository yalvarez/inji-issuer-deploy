const { useEffect, useMemo, useRef, useState } = React;

// ─── i18n ────────────────────────────────────────────────────────────────────
const I18N = {
  en: {
    appTitle: "Inji Issuer Deploy Wizard",
    appSubtitle: "Step-by-step flow with only essential actions. The CLI/state model remains the source of truth.",
    wizardSteps: "Wizard steps",
    stepLocked: "Locked",
    stepCurrent: "Current",
    stepAvailable: "Available",
    back: "Back",
    next: "Next",
    saveStep: "Save step",
    runStep: "Run step",
    stepStatus: "Step status",
    stepReady: "This step is unlocked and ready.",
    runPreflight: "Run preflight",
    preflightHint: "Run preflight for the readiness checklist.",
    showAdvanced: "Show advanced fields",
    hideAdvanced: "Hide advanced fields",
    technicalOutput: "Technical output",
    // field labels
    fields: {
      issuer_id:                      { label: "Issuer ID",                      tip: "Short identifier used in all Kubernetes resource names.",          example: "e.g. mtc" },
      issuer_name:                    { label: "Issuer name",                     tip: "Human-readable display name shown in the Inji wallet.",            example: "e.g. Ministerio de Transportes" },
      base_domain:                    { label: "Base domain",                     tip: "The public domain where the Certify service will be exposed.",     example: "e.g. certify.mtc.gob.pe" },
      provider:                       { label: "Provider",                        tip: "Target infrastructure provider for this deployment.",              example: "" },
      provisioner:                    { label: "Provisioner",                     tip: "How infrastructure resources are created: directly via Python or by generating a Terraform plan.", example: "" },
      shared_config_source_namespace: { label: "Shared config namespace",         tip: "Kubernetes namespace that holds the shared ConfigMaps for your environment.", example: "e.g. config-server" },
      data_api_base_url:              { label: "Data API base URL",               tip: "Base URL of the issuer's data source API.",                        example: "e.g. https://api.licencias.mtc.gob.pe" },
      idperu_jwks_uri:                { label: "IDPeru JWKS URI",                 tip: "URL exposing the public keys used to verify IDPeru tokens.",       example: "e.g. https://idperu.gob.pe/oauth/.well-known/jwks.json" },
      idperu_issuer_uri:              { label: "IDPeru issuer URI",               tip: "OIDC issuer URI configured in IDPeru.",                            example: "e.g. https://idperu.gob.pe/v1/idperu" },
      onprem_registry_backend:        { label: "Registry backend",                tip: "Container registry type: plain (no auth) or private Harbor.",      example: "" },
      onprem_secrets_backend:         { label: "Secrets backend",                 tip: "Where Kubernetes secrets are stored: native k8s Secrets or HashiCorp Vault.", example: "" },
      onprem_cert_issuer_kind:        { label: "Cert issuer kind",                tip: "Cert-manager resource kind used to issue TLS certificates.",       example: "e.g. ClusterIssuer" },
      onprem_cert_issuer_name:        { label: "Cert issuer name",                tip: "Name of the ClusterIssuer or Issuer already installed in the cluster.", example: "e.g. letsencrypt-prod" },
      shared_configmaps:              { label: "Shared ConfigMaps",               tip: "Comma-separated names of ConfigMaps that exist in the shared config namespace.", example: "e.g. global-config, issuer-common" },
    },
  },
  es: {
    appTitle: "Asistente de despliegue Inji Issuer",
    appSubtitle: "Flujo paso a paso con solo las acciones esenciales. El CLI/estado es la fuente de verdad.",
    wizardSteps: "Pasos del asistente",
    stepLocked: "Bloqueado",
    stepCurrent: "Actual",
    stepAvailable: "Disponible",
    back: "Atrás",
    next: "Siguiente",
    saveStep: "Guardar paso",
    runStep: "Ejecutar paso",
    stepStatus: "Estado del paso",
    stepReady: "Este paso está desbloqueado y listo.",
    runPreflight: "Ejecutar preflight",
    preflightHint: "Ejecuta el preflight para ver la lista de verificación.",
    showAdvanced: "Mostrar campos avanzados",
    hideAdvanced: "Ocultar campos avanzados",
    technicalOutput: "Salida técnica",
    fields: {
      issuer_id:                      { label: "ID del emisor",                   tip: "Identificador corto usado en todos los nombres de recursos de Kubernetes.",      example: "ej. mtc" },
      issuer_name:                    { label: "Nombre del emisor",               tip: "Nombre legible que aparece en la billetera Inji.",                               example: "ej. Ministerio de Transportes" },
      base_domain:                    { label: "Dominio base",                    tip: "Dominio público donde se expondrá el servicio Certify.",                         example: "ej. certify.mtc.gob.pe" },
      provider:                       { label: "Proveedor",                       tip: "Proveedor de infraestructura destino para este despliegue.",                     example: "" },
      provisioner:                    { label: "Aprovisionador",                  tip: "Cómo se crean los recursos: directamente vía Python o generando un plan Terraform.", example: "" },
      shared_config_source_namespace: { label: "Namespace de configuración compartida", tip: "Namespace de Kubernetes que contiene los ConfigMaps compartidos del entorno.", example: "ej. config-server" },
      data_api_base_url:              { label: "URL base de la API de datos",     tip: "URL base de la API que provee los datos del emisor.",                           example: "ej. https://api.licencias.mtc.gob.pe" },
      idperu_jwks_uri:                { label: "URI JWKS de IDPeru",              tip: "URL que expone las claves públicas para verificar tokens de IDPeru.",            example: "ej. https://idperu.gob.pe/oauth/.well-known/jwks.json" },
      idperu_issuer_uri:              { label: "URI emisor IDPeru",               tip: "URI del emisor OIDC configurado en IDPeru.",                                     example: "ej. https://idperu.gob.pe/v1/idperu" },
      onprem_registry_backend:        { label: "Backend de registro",             tip: "Tipo de registro de contenedores: plain (sin autenticación) o Harbor privado.",  example: "" },
      onprem_secrets_backend:         { label: "Backend de secretos",             tip: "Dónde se almacenan los secretos: Secrets nativos de k8s o HashiCorp Vault.",     example: "" },
      onprem_cert_issuer_kind:        { label: "Tipo de emisor TLS",              tip: "Tipo de recurso cert-manager usado para emitir certificados TLS.",               example: "ej. ClusterIssuer" },
      onprem_cert_issuer_name:        { label: "Nombre del emisor TLS",           tip: "Nombre del ClusterIssuer o Issuer ya instalado en el clúster.",                  example: "ej. letsencrypt-prod" },
      shared_configmaps:              { label: "ConfigMaps compartidos",          tip: "Nombres separados por coma de ConfigMaps que existen en el namespace compartido.", example: "ej. global-config, issuer-common" },
    },
  },
  fr: {
    appTitle: "Assistant de déploiement Inji Issuer",
    appSubtitle: "Flux étape par étape avec uniquement les actions essentielles. Le CLI/état reste la source de vérité.",
    wizardSteps: "Étapes de l'assistant",
    stepLocked: "Verrouillé",
    stepCurrent: "Actuel",
    stepAvailable: "Disponible",
    back: "Retour",
    next: "Suivant",
    saveStep: "Enregistrer l'étape",
    runStep: "Exécuter l'étape",
    stepStatus: "Statut de l'étape",
    stepReady: "Cette étape est déverrouillée et prête.",
    runPreflight: "Exécuter le preflight",
    preflightHint: "Exécutez le preflight pour voir la liste de vérification.",
    showAdvanced: "Afficher les champs avancés",
    hideAdvanced: "Masquer les champs avancés",
    technicalOutput: "Sortie technique",
    fields: {
      issuer_id:                      { label: "ID de l'émetteur",                tip: "Identifiant court utilisé dans tous les noms de ressources Kubernetes.",         example: "ex. mtc" },
      issuer_name:                    { label: "Nom de l'émetteur",               tip: "Nom lisible affiché dans le portefeuille Inji.",                                 example: "ex. Ministerio de Transportes" },
      base_domain:                    { label: "Domaine de base",                 tip: "Domaine public où le service Certify sera exposé.",                              example: "ex. certify.mtc.gob.pe" },
      provider:                       { label: "Fournisseur",                     tip: "Fournisseur d'infrastructure cible pour ce déploiement.",                        example: "" },
      provisioner:                    { label: "Provisionneur",                   tip: "Comment les ressources sont créées : directement via Python ou en générant un plan Terraform.", example: "" },
      shared_config_source_namespace: { label: "Namespace de configuration partagée", tip: "Namespace Kubernetes contenant les ConfigMaps partagées de l'environnement.", example: "ex. config-server" },
      data_api_base_url:              { label: "URL de base de l'API de données", tip: "URL de base de l'API fournissant les données de l'émetteur.",                   example: "ex. https://api.licencias.mtc.gob.pe" },
      idperu_jwks_uri:                { label: "URI JWKS IDPeru",                 tip: "URL exposant les clés publiques pour vérifier les jetons IDPeru.",               example: "ex. https://idperu.gob.pe/oauth/.well-known/jwks.json" },
      idperu_issuer_uri:              { label: "URI émetteur IDPeru",             tip: "URI de l'émetteur OIDC configuré dans IDPeru.",                                  example: "ex. https://idperu.gob.pe/v1/idperu" },
      onprem_registry_backend:        { label: "Backend de registre",             tip: "Type de registre de conteneurs : plain (sans auth) ou Harbor privé.",           example: "" },
      onprem_secrets_backend:         { label: "Backend de secrets",              tip: "Où les secrets sont stockés : Secrets k8s natifs ou HashiCorp Vault.",           example: "" },
      onprem_cert_issuer_kind:        { label: "Type d'émetteur TLS",             tip: "Type de ressource cert-manager utilisé pour émettre des certificats TLS.",      example: "ex. ClusterIssuer" },
      onprem_cert_issuer_name:        { label: "Nom de l'émetteur TLS",           tip: "Nom du ClusterIssuer ou de l'Issuer déjà installé dans le cluster.",            example: "ex. letsencrypt-prod" },
      shared_configmaps:              { label: "ConfigMaps partagées",            tip: "Noms séparés par des virgules des ConfigMaps dans le namespace partagé.",        example: "ex. global-config, issuer-common" },
    },
  },
};

// ─── Tooltip component ───────────────────────────────────────────────────────
function Tooltip({ tip, example }) {
  const [visible, setVisible] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    function handleClick(e) {
      if (ref.current && !ref.current.contains(e.target)) {
        setVisible(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  return (
    <span className="tooltip-wrap" ref={ref}>
      <button
        type="button"
        className="tooltip-trigger"
        onClick={() => setVisible((v) => !v)}
        aria-label="Help"
      >?</button>
      {visible && (
        <span className="tooltip-box" role="tooltip">
          {tip}
          {example && <span className="tooltip-example">{example}</span>}
        </span>
      )}
    </span>
  );
}

// ─── Field wrapper: label + tooltip + input ──────────────────────────────────
function Field({ fieldKey, t, children }) {
  const f = t.fields[fieldKey] || { label: fieldKey, tip: "", example: "" };
  return (
    <label>
      <span className="field-label-row">
        {f.label}
        {f.tip && <Tooltip tip={f.tip} example={f.example} />}
      </span>
      {children}
    </label>
  );
}

// ─── Phase info (static, keys only — labels come from i18n) ──────────────────

const PHASE_INFO = {
  collect:  { icon: "🧾" },
  infra:    { icon: "🧱" },
  config:   { icon: "⚙️"  },
  deploy:   { icon: "🚀" },
  register: { icon: "✅" },
};

// ─── Phase i18n ───────────────────────────────────────────────────────────────
const PHASE_I18N = {
  en: {
    collect:  { short: "Collect issuer data",            title: "Phase 0 — Issuer configuration",       description: "Capture the issuer identity, provider choice, domain, shared config sources, and integration endpoints." },
    infra:    { short: "Prepare infrastructure",         title: "Phase 1 — Infrastructure provisioning", description: "Validate access, prepare namespace/secrets/registry/TLS hooks, and confirm the target environment is ready." },
    config:   { short: "Generate config files",          title: "Phase 2 — Configuration generation",    description: "Render the properties, Helm values, ConfigMaps, and issuer patch artifacts required for deployment." },
    deploy:   { short: "Deploy services to Kubernetes",  title: "Phase 3 — Kubernetes deployment",       description: "Apply the generated configuration and run the Helm-based deployment into the target namespace." },
    register: { short: "Register and verify issuer",     title: "Phase 4 — Credential registration",     description: "Register credential metadata and perform the final smoke tests for the issuer endpoints." },
  },
  es: {
    collect:  { short: "Recopilar datos del emisor",     title: "Fase 0 — Configuración del emisor",     description: "Captura la identidad del emisor, la elección de proveedor, dominio, fuentes de configuración compartida y endpoints de integración." },
    infra:    { short: "Preparar infraestructura",       title: "Fase 1 — Aprovisionamiento de infraestructura", description: "Valida el acceso, prepara el namespace/secretos/registro/TLS y confirma que el entorno está listo." },
    config:   { short: "Generar archivos de config",     title: "Fase 2 — Generación de configuración",  description: "Renderiza las properties, valores Helm, ConfigMaps y artefactos de parche del emisor necesarios para el despliegue." },
    deploy:   { short: "Desplegar servicios en K8s",     title: "Fase 3 — Despliegue en Kubernetes",     description: "Aplica la configuración generada y ejecuta el despliegue basado en Helm en el namespace destino." },
    register: { short: "Registrar y verificar emisor",   title: "Fase 4 — Registro de credenciales",     description: "Registra los metadatos de credenciales y realiza las pruebas finales de los endpoints del emisor." },
  },
  fr: {
    collect:  { short: "Collecter les données",          title: "Phase 0 — Configuration de l'émetteur", description: "Capture l'identité de l'émetteur, le choix du fournisseur, le domaine, les sources de config partagées et les endpoints d'intégration." },
    infra:    { short: "Préparer l'infrastructure",      title: "Phase 1 — Provisionnement de l'infrastructure", description: "Valide l'accès, prépare le namespace/secrets/registre/TLS et confirme que l'environnement cible est prêt." },
    config:   { short: "Générer les fichiers de config", title: "Phase 2 — Génération de la configuration", description: "Génère les properties, les valeurs Helm, les ConfigMaps et les artefacts de patch de l'émetteur." },
    deploy:   { short: "Déployer sur Kubernetes",        title: "Phase 3 — Déploiement Kubernetes",      description: "Applique la configuration générée et exécute le déploiement Helm dans le namespace cible." },
    register: { short: "Enregistrer et vérifier",        title: "Phase 4 — Enregistrement des credentials", description: "Enregistre les métadonnées de credentials et exécute les tests de fumée finaux sur les endpoints de l'émetteur." },
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
  const [lang, setLang] = useState("en");

  const t = I18N[lang] || I18N.en;
  const phaseI18n = PHASE_I18N[lang] || PHASE_I18N.en;

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
  const currentPhaseLabels = phaseI18n[currentPhaseKey] || {};
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
      const logText = (payload.logs || "").trim();
      const errorText = (!payload.ok && payload.error) ? `\n\n⚠ FAILED: ${payload.error}` : "";
      setLogs((logText + errorText).trim() || "No output returned.");
      if (payload.state) {
        setSnapshot(payload.state);
      } else {
        await refreshState();
      }
      if (payload.ok && !dryRun) {
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
        <div className="hero-title">
          <p className="eyebrow">FastAPI + React MVP</p>
          <h1>{t.appTitle}</h1>
          <p>{t.appSubtitle}</p>
        </div>
        <div className="lang-switcher">
          {["en", "es", "fr"].map((l) => (
            <button
              key={l}
              type="button"
              onClick={() => setLang(l)}
              className={`lang-btn ${lang === l ? "lang-btn-active" : ""}`}
            >{l.toUpperCase()}</button>
          ))}
        </div>
      </header>

      <section className="card wide wizard-card">
        <h2>{t.wizardSteps}</h2>
        <div className="wizard-strip">
          {PHASE_SEQUENCE.map((key, index) => {
            const info = PHASE_INFO[key];
            const labels = phaseI18n[key] || {};
            const phaseMeta = phaseMetaMap[key] || {};
            const status = phaseMeta.status || "pending";
            const locked = phaseMeta.locked || false;
            const isCurrent = currentPhaseKey === key;
            const stateLabel = locked ? t.stepLocked : isCurrent ? t.stepCurrent : t.stepAvailable;
            return (
              <div key={key} className={`wizard-step wizard-step-${status} ${locked ? "wizard-step-locked" : ""} ${isCurrent ? "wizard-step-current" : ""}`}>
                <div className="wizard-step-icon">{info.icon}</div>
                <div>
                  <div className="wizard-step-title">{index + 1}. {labels.short}</div>
                  <div className="muted wizard-step-note">{stateLabel}</div>
                </div>
                <PhaseBadge status={status} />
              </div>
            );
          })}
        </div>
      </section>

      <main className="wizard-main">
        <section className="card">
          <h2>{currentPhaseLabels.title}</h2>
          <p className="helper phase-description">{currentPhaseLabels.description}</p>

          {currentPhaseKey === "collect" && (
            <>
              <div className="form-grid compact">
                <Field fieldKey="issuer_id" t={t}><input value={form.issuer_id || ""} onChange={(e) => updateField("issuer_id", e.target.value)} /></Field>
                <Field fieldKey="issuer_name" t={t}><input value={form.issuer_name || ""} onChange={(e) => updateField("issuer_name", e.target.value)} /></Field>
                <Field fieldKey="base_domain" t={t}><input value={form.base_domain || ""} onChange={(e) => updateField("base_domain", e.target.value)} /></Field>
                <Field fieldKey="provider" t={t}>
                  <select value={form.provider || "onprem"} onChange={(e) => updateField("provider", e.target.value)}>
                    <option value="onprem">onprem</option>
                    <option value="aws">aws</option>
                    <option value="azure">azure</option>
                    <option value="gcp">gcp</option>
                  </select>
                </Field>
                <Field fieldKey="provisioner" t={t}>
                  <select value={form.provisioner || "python"} onChange={(e) => updateField("provisioner", e.target.value)}>
                    <option value="python">python</option>
                    <option value="terraform">terraform</option>
                  </select>
                </Field>
                <Field fieldKey="shared_config_source_namespace" t={t}><input value={form.shared_config_source_namespace || ""} onChange={(e) => updateField("shared_config_source_namespace", e.target.value)} /></Field>
              </div>

              <button type="button" className="link-button" onClick={() => setShowAdvanced((v) => !v)}>
                {showAdvanced ? t.hideAdvanced : t.showAdvanced}
              </button>

              {showAdvanced && (
                <div className="form-grid compact advanced-grid">
                  <Field fieldKey="data_api_base_url" t={t}><input value={form.data_api_base_url || ""} onChange={(e) => updateField("data_api_base_url", e.target.value)} /></Field>
                  <Field fieldKey="idperu_jwks_uri" t={t}><input value={form.idperu_jwks_uri || ""} onChange={(e) => updateField("idperu_jwks_uri", e.target.value)} /></Field>
                  <Field fieldKey="idperu_issuer_uri" t={t}><input value={form.idperu_issuer_uri || ""} onChange={(e) => updateField("idperu_issuer_uri", e.target.value)} /></Field>
                  <Field fieldKey="onprem_registry_backend" t={t}>
                    <select value={form.onprem_registry_backend || "plain"} onChange={(e) => updateField("onprem_registry_backend", e.target.value)}>
                      <option value="plain">plain</option>
                      <option value="harbor">harbor</option>
                    </select>
                  </Field>
                  <Field fieldKey="onprem_secrets_backend" t={t}>
                    <select value={form.onprem_secrets_backend || "k8s"} onChange={(e) => updateField("onprem_secrets_backend", e.target.value)}>
                      <option value="k8s">k8s</option>
                      <option value="vault">vault</option>
                    </select>
                  </Field>
                  <Field fieldKey="onprem_cert_issuer_kind" t={t}><input value={form.onprem_cert_issuer_kind || ""} onChange={(e) => updateField("onprem_cert_issuer_kind", e.target.value)} /></Field>
                  <Field fieldKey="onprem_cert_issuer_name" t={t}><input value={form.onprem_cert_issuer_name || ""} onChange={(e) => updateField("onprem_cert_issuer_name", e.target.value)} /></Field>
                  <Field fieldKey="shared_configmaps" t={t}>
                    <input
                      value={Array.isArray(form.shared_configmaps) ? form.shared_configmaps.join(", ") : (form.shared_configmaps || "")}
                      onChange={(e) => updateField("shared_configmaps", e.target.value.split(",").map((item) => item.trim()).filter(Boolean))}
                    />
                  </Field>
                </div>
              )}
            </>
          )}

          {currentPhaseKey !== "collect" && (
            <div className="step-summary">
              <div className="phase-item">
                <div>
                  <strong>{t.stepStatus}</strong>
                  <div className="muted">{stepLocked ? (currentPhaseMeta.locked_reason || "") : t.stepReady}</div>
                </div>
                <PhaseBadge status={currentPhaseMeta.status || "pending"} />
              </div>
            </div>
          )}

          {currentPhaseKey === "infra" && (
            <div className="preflight-box">
              <div className="button-row">
                <button onClick={runPreflight} disabled={busy || stepLocked}>{t.runPreflight}</button>
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
                <p className="muted">{t.preflightHint}</p>
              )}
            </div>
          )}

          <div className="wizard-actions">
            <button type="button" onClick={() => goToStep(currentStep - 1)} disabled={!canGoBack || busy}>{t.back}</button>
            {currentPhaseKey === "collect" ? (
              <button type="button" onClick={saveConfig} disabled={busy || !collectReady}>{t.saveStep}</button>
            ) : (
              <button type="button" onClick={() => runCurrentStep(false)} disabled={busy || stepLocked}>{t.runStep}</button>
            )}
            <button type="button" onClick={() => goToStep(currentStep + 1)} disabled={!canGoNext || busy}>{t.next}</button>
          </div>
        </section>

        <section className="card">
          <details>
            <summary>{t.technicalOutput}</summary>
            <pre className="log-box">{logs}</pre>
          </details>
        </section>
      </main>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
