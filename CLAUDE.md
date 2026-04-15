# CLAUDE.md — inji-issuer-deploy

Contexto completo para continuar el desarrollo de esta herramienta desde VS Code.
Última actualización: 24 de mayo de 2024 — Soporte para aprovisionamiento de Redis y paridad Web UI.

---

## Qué es este proyecto

CLI Python (+ web UI opcional) que automatiza el despliegue de un nuevo emisor de
Credenciales Verificables sobre el stack Inji de MOSIP, replicando la arquitectura
de producción de RENIEC Perú. Agnóstico de nube: soporta AWS, Azure, GCP y on-premise.

El path recomendado para la primera instalación real es **on-premise con k3s/Kubernetes**.
El path AWS/EKS existe pero está en beta y no es el camino principal.

El ecosistema completo al que pertenece esta herramienta:

```
IDPeru (auth nacional)  ←── autentica ciudadanos, emite tokens con DNI
       ↓
Certify {emisor}        ←── valida token IDPeru, llama plugin de datos, firma VC
       ↓
mimoto (directorio)     ←── lista todos los emisores para la wallet
       ↓
inji-wallet             ←── wallet ciudadana, descarga y presenta VCs
       ↓
inji-verify + SDK       ←── verifica VCs en portales de terceros
```

Cada emisor (RENIEC, MTC, CDPI, etc.) corre su propio Certify en infraestructura separada.
Solo mimoto y la wallet son compartidos.

---

## Setup local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Dependencias de producción (pyproject.toml):
- `click>=8.1` — CLI
- `jinja2>=3.1` — generación de archivos de configuración
- `boto3>=1.34` — AWS SDK
- `kubernetes>=29.0` — kubectl Python client
- `httpx>=0.27` — smoke tests HTTP
- `rich>=13.7` — output de terminal
- `pyyaml>=6.0` — parseo de YAML
- `fastapi` + `uvicorn` — web UI

```bash
# Correr tests
python -m pytest tests/ -v
```

---

## Estructura del proyecto

```
inji_issuer_deploy/
├── cli.py              # Entrypoint Click: run, status, reset, phase, preflight, web, bootstrap
├── state.py            # DeployState + IssuerConfig — persistencia en JSON
├── cloud.py            # Interfaz CloudProvider abstracta + verificación de credenciales
├── orchestrator.py     # Lógica compartida entre CLI y web UI (run_phase, state_snapshot, etc.)
├── webapp.py           # FastAPI web UI — capa fina sobre el mismo motor de fases del CLI
├── bootstrap.py        # Subcomando bootstrap ubuntu-onprem
├── phases/
│   ├── collect.py      # Fase 0: CLI interactiva, recoge config + selecciona provider
│   ├── aws_infra.py    # Fase 1 LEGACY: solo AWS, boto3 directo (ver PENDIENTE #1)
│   ├── infra.py        # Fase 1 NUEVA: cloud-agnóstica, delega a CloudProvider
│   ├── config_gen.py   # Fase 2: renderiza Jinja2 → 6 archivos de configuración
│   ├── k8s_deploy.py   # Fase 3: Helm install + kubectl + patch mimoto
│   └── register.py     # Fase 4: POST /credential-configurations + smoke tests
└── providers/
    ├── aws.py          # CloudProvider para AWS (ECR, Secrets Manager, IAM, Route53, ACM, S3)
    ├── azure.py        # CloudProvider para Azure (ACR, Key Vault, Managed Identity, Blob)
    ├── gcp.py          # CloudProvider para GCP (Artifact Registry, Secret Manager, GCS)
    └── onprem.py       # CloudProvider para on-premise (Harbor, Vault/K8s, MinIO/ConfigMap)

bootstrap-ubuntu-onprem.sh   # Script bash para preparar un VPS Ubuntu como operador
docs/
├── onprem-ubuntu-vps.md
├── onprem-first-real-runbook.md
└── examples/onprem-simulation.md
terraform/               # Módulos Terraform para el path AWS (beta)
tests/
```

**Archivos de estado de prueba en la raíz:**
- `inji-deploy-state.json` — estado del despliegue activo
- `inji-deploy-state-test.json` — estado de referencia para tests (AWS)
- `inji-deploy-state-test-on-premise.json` — estado de referencia para tests (on-prem, emisor `cdpi`)

---

## Cómo funciona — las 5 fases

### Fase 0 — collect (`phases/collect.py`)
CLI interactiva. Pregunta los datos mínimos y al final selecciona y verifica el
cloud provider. Gestiona la lógica de autogeneración de hosts para DB y Redis.

Datos que recoge: `issuer_id` (slug), `issuer_name`, `base_domain`, `eks_cluster_name`,
`rds_host`, `idperu_jwks_uri`, `idperu_issuer_uri`, `document_number_claim`,
`data_api_base_url`, `data_api_auth_type`, `scope_mappings[]`, `provision_db`,
`provision_redis`, `shared_configmaps`, `shared_config_source_namespace`, provider y su config.

La web UI omite esta fase interactiva y la reemplaza con un POST a `/api/issuer-config`.

### Fase 1 — infra (`phases/infra.py`)
Provisión de infraestructura cloud. Usa `CloudProvider` abstracto.
Crea: namespace K8s, registros de contenedores (3), secretos (3), workload identity,
DNS (si hay zona gestionada), certificado TLS, nota sobre el schema de BD.

**Outputs clave** guardados en `state.phases["infra"].outputs`:
- `namespace` — nombre del namespace K8s (ej. `inji-mtc`)
- `db_name` — nombre del schema de BD (ej. `inji_mtc`)
- `pod_identity_role_arn` — referencia al workload identity
- `registry_uris` — dict `{svc: uri}` con URIs de los registros

### Fase 2 — config_gen (`phases/config_gen.py`)
Renderiza Jinja2 con los datos de Fase 0 y los outputs de Fase 1.
Escribe en `.inji-deploy/{issuer_id}/`:
- `certify-{id}.properties` — Spring Boot config para Certify
- `helm-values-certify.yaml` — overrides de Helm para Certify
- `helm-values-softhsm.yaml` — valores de SoftHSM
- `k8s-configmap.yaml` — ConfigMap con variables del emisor
- `mimoto-issuer-patch.json` — entrada JSON para el directorio de emisores
- `db-init-values.yaml` — valores para el Job de init de BD

**Importante sobre el dominio de mimoto:** se puede configurar explícitamente con
`mimoto_base_url` en `IssuerConfig`. Si no se configura, se deriva automáticamente
como `mimoto.{tld1}.{tld2}` del `base_domain`, lo que puede ser incorrecto para
dominios como `duckdns.org` o `example.com`.

### Fase 3 — k8s_deploy (`phases/k8s_deploy.py`)
Helm install en orden: ConfigMaps compartidos → ConfigMap del emisor → DB init
(solo si `provision_db=True`) → SoftHSM → inji-certify.

Luego: descarga `mimoto-issuers-config.json` del storage, agrega la nueva entrada,
sube el JSON actualizado, y hace `kubectl rollout restart` del deployment de mimoto.

**Comportamiento de `provision_db`:**
- `False` (default): asume PostgreSQL externo ya existente, omite DB init
- `True`: crea el Secret `inji-{id}-db-secret` con credenciales generadas y ejecuta
  el Helm chart `postgres-init` para crear el schema

**Shared ConfigMaps:** si un ConfigMap no existe en el namespace fuente, muestra
advertencia y continúa (no falla). Los ConfigMaps más comunes son `artifactory-share`
y `config-server-share`.

### Fase 4 — register (`phases/register.py`)
Espera que Certify esté `UP` en `/actuator/health` (timeout: 5 min).
POST a `/credential-configurations` por cada scope definido.
Smoke tests: GET `/.well-known/openid-credential-issuer`, GET `/issuers` de mimoto.
Imprime el reporte final con endpoints, estados, y los pasos manuales post-deploy.

---

## Web UI (`webapp.py` + `orchestrator.py`)

```bash
inji-issuer-deploy web --host 0.0.0.0 --port 8000
```

- **FastAPI** sobre el mismo motor de fases del CLI
- `orchestrator.py` es la capa compartida: `run_phase()`, `state_snapshot()`,
  `update_state_from_payload()`, `phase_gate()`
- La Fase 0 se reemplaza con `POST /api/issuer-config` (no hay CLI interactiva en web)
- Las fases 1–4 se ejecutan via `POST /api/run/phase/{phase_name}`
- Los artefactos generados se exponen en `GET /api/artifacts/{nombre}`
- El output de Rich Console se captura en un buffer y se devuelve en `logs` del JSON
- Archivos estáticos servidos desde `inji_issuer_deploy/webui/`

**`IssuerConfigPayload` (webapp.py):** debe tener exactamente los mismos campos que
`IssuerConfig` (state.py). Si se agrega un campo nuevo a `IssuerConfig`, también
hay que agregarlo a `IssuerConfigPayload` para que la web UI lo persista.

---

## La abstracción de cloud provider

### Interfaz (`cloud.py`)

```python
class CloudProvider(abc.ABC):
    def name(self) -> str
    def verify_credentials(self) -> tuple[bool, str]
    def ensure_registry_repo(self, repo_name: str) -> str          # retorna URI
    def ensure_secret(self, name, description, placeholder) -> str  # retorna referencia
    def read_secret(self, reference: str) -> dict
    def ensure_workload_identity(self, issuer_id, namespace, cfg) -> str
    def find_dns_zone(self, domain: str) -> str | None
    def ensure_tls_certificate(self, domain: str) -> str | None
    def read_config_file(self, bucket, key) -> dict
    def write_config_file(self, bucket, key, data) -> None
    def dry_run_plan(self, issuer_id, cfg) -> list[tuple[str, str]]
```

### Agregar un nuevo provider
1. Crear `inji_issuer_deploy/providers/micloud.py` con clase que extiende `CloudProvider`
2. Implementar los 11 métodos abstractos
3. Agregar el caso en `cloud.py → get_provider()`
4. Agregar preguntas específicas en `collect.py` sección de deployment target
5. Agregar tests en `tests/test_cloud_providers.py`

---

## Estado del estado (`state.py`)

```python
@dataclass
class IssuerConfig:
    # Identidad
    issuer_id: str              # slug único, e.g. "mtc"
    issuer_name: str
    issuer_description: str
    issuer_logo_url: str
    base_domain: str            # e.g. "certify.mtc.gob.pe"

    # Infraestructura cloud
    aws_region: str             # default "sa-east-1"
    aws_account_id: str
    eks_cluster_name: str
    rds_host: str
    rds_port: int               # default 5432
    rds_admin_secret_arn: str

    # IDPeru
    idperu_jwks_uri: str
    idperu_issuer_uri: str
    document_number_claim: str  # default "individualId"
    filiation_claim: str        # vacío = sin filiación

    # API de datos del emisor
    data_api_base_url: str
    data_api_auth_type: str     # "mtls" | "oauth2" | "apikey"
    data_api_secret_arn: str
    data_api_token_url: str     # solo para oauth2

    # Credential types
    scope_mappings: list[dict]  # [{scope, profile, service, display_name, requires_filiation}]

    # Mimoto
    mimoto_issuers_s3_bucket: str
    mimoto_issuers_s3_key: str  # default "mimoto-issuers-config.json"
    mimoto_service_namespace: str
    mimoto_service_name: str
    mimoto_base_url: str        # URL base de mimoto — si vacío, se deriva del base_domain

    # Kubernetes / recursos compartidos
    shared_config_source_namespace: str   # namespace fuente para copiar ConfigMaps
    shared_configmaps: list[str]          # ConfigMaps a copiar al namespace del emisor

    # Helm charts
    helm_repo_name: str
    helm_repo_url: str
    certify_chart_ref: str
    postgres_init_chart_ref: str
    postgres_init_chart_version: str
    softhsm_chart_ref: str
    softhsm_namespace: str
    certify_image: str
    chart_version: str
    softhsm_chart_version: str
    node_selector: dict

    # Comportamiento de deploy
    provision_db: bool          # True → crear Secret DB y ejecutar postgres-init
    provision_redis: bool       # True → instalar Redis via Helm en el cluster
    redis_chart_ref: str        # default "bitnami/redis"
    redis_chart_version: str    # default "18.1.6"

@dataclass
class DeployState:
    issuer: IssuerConfig
    provider_cfg: dict          # CloudProviderConfig serializado
    phases: dict[str, PhaseStatus]
    # phases keys: "collect", "infra", "config_gen", "k8s_deploy", "register"
```

El estado se persiste en `inji-deploy-state.json` (configurable con `INJI_STATE_FILE`).
Todas las fases son idempotentes — si se interrumpe, re-ejecutar retoma desde donde quedó.

**ALIAS de fases:** `aws-infra` → `infra`, `config` → `config_gen`, `deploy` → `k8s_deploy`.

---

## Comportamiento crítico de Spring Boot — scan-base-package

**Problema conocido y resuelto:** Spring Boot ejecuta `@ComponentScan` con el
placeholder `${mosip.certify.integration.scan-base-package}` **antes** de cargar
los archivos apuntados por `SPRING_CONFIG_LOCATION`. Si la propiedad solo está en
el `.properties` file, el arranque falla con:

```
IllegalArgumentException: Could not resolve placeholder
'mosip.certify.integration.scan-base-package'
```

**Solución aplicada:** la Fase 2 genera en `helm-values-certify.yaml`:
```yaml
extraEnv:
  - name: SPRING_CONFIG_LOCATION
    value: "file:///config/certify-{issuer_id}.properties"
  - name: MOSIP_CERTIFY_INTEGRATION_SCAN_BASE_PACKAGE
    value: "io.mosip.certify.restapidataprovider.integration"
```

Las variables de entorno se cargan en el `Environment` de Spring antes del
component scan, resolviendo el placeholder. El prefijo `file:///` es necesario
para que Spring Boot reconozca la ruta como un archivo del sistema de archivos.

**Si el pod ya estaba instalado con la versión vieja de los valores:**
```bash
# Opción rápida (sin re-deploy)
kubectl set env deploy/inji-certify-{id} -n inji-{id} \
  MOSIP_CERTIFY_INTEGRATION_SCAN_BASE_PACKAGE=io.mosip.certify.restapidataprovider.integration

# Opción limpia (regenerar y upgrade)
inji-issuer-deploy phase config
helm upgrade inji-certify-{id} mosip/inji-certify \
  -n inji-{id} \
  -f .inji-deploy/{id}/helm-values-certify.yaml \
  --version {chart_version}
```

---

## Trabajo pendiente — lista priorizada

### PENDIENTE #1 (crítico) — Unificar las dos versiones de Fase 1
El CLI todavía despacha a `aws_infra.run()` (la versión legacy solo AWS, con boto3
directo) en lugar de `infra.run()` (la nueva versión cloud-agnóstica).

```python
# En cli.py, cambiar:
from inji_issuer_deploy.phases import aws_infra  # LEGACY
# Por:
from inji_issuer_deploy.phases import infra       # NUEVA

# Y en _run_phase():
elif name == "infra":
    infra.run(state, dry_run=dry_run)   # era aws_infra.run(...)
```

### PENDIENTE #2 — Helm values para Azure/GCP/onprem
`config_gen.py` genera `serviceAccount.annotations` con la anotación de AWS EKS
Pod Identity (`eks.amazonaws.com/role-arn`). Para otros providers:

- Azure: `azure.workload.identity/client-id: "{azure_client_id}"`
- GCP: `iam.gke.io/gcp-service-account: "{sa_email}"`
- On-premise: sin anotación

La sección del template Jinja2 en `HELM_VALUES_CERTIFY` necesita parametrizarse
por provider.

### PENDIENTE #3 — mTLS hacia la API de datos no está implementado
`collect.py` acepta `data_api_auth_type=mtls` pero `config_gen.py` no genera la
configuración de Spring para keystore/truststore ni el `RestTemplate` con SSL.

Cuando `data_api_auth_type == "mtls"`, hay que agregar al template de properties:
```properties
mosip.certify.data-provider-plugin.restapi.ssl.key-store=...
mosip.certify.data-provider-plugin.restapi.ssl.key-store-password=...
```

### PENDIENTE #4 — Campos VC en la colección (scope_mappings)
`register.py` registra credential configs con templates VC mínimos (solo `id`,
`issuer`, `issuanceDate`). Los campos reales los define cada emisor manualmente
después con `PUT /credential-configurations/{scope}`.

Sería útil que `collect.py` recoja los campos de `credentialSubject` por tipo de
credencial y los incorpore al template Velocity y al `credentialSubjectDefinition`.

### PENDIENTE #5 — Trust Registry no implementado
El ecosistema carece de Trust Registry. El RNE (Registro Nacional de Emisores)
propuesto es para descubrimiento (wallet). El Trust Registry es para verificación
(verifiers externos). Ver sección "mimoto" más abajo para el detalle.

### PENDIENTE #6 — mimoto_base_url no se pregunta en la CLI interactiva
Se agregó el campo `mimoto_base_url` a `IssuerConfig` y a `IssuerConfigPayload`
(web UI) pero `collect.py` no lo pregunta todavía. Si el dominio de mimoto no
coincide con la derivación automática, hay que setearlo manualmente en el JSON de
estado o via la web UI.

### PENDIENTE #7 — Re-run de fase config no resetea el estado de la fase
Si se necesita regenerar los archivos (ej. después de un fix), hay que ejecutar
`inji-issuer-deploy phase config` directamente. El estado de la fase `config_gen`
no se resetea automáticamente, pero la fase reescribe los archivos de todas formas.

---

## Contexto del ecosistema RENIEC — datos reales

### Repositorios GitHub (privados, org IUGO-RENIEC)
- `inji-certify` — fork MOSIP v0.12.2, sin cambios
- `rest-api-dataprovider-plugin` — plugin custom IUGO, conecta Certify a RENIEC
- `mimoto` — fork MOSIP v0.20.0, sin cambios
- `inji-wallet` — fork MOSIP v0.20.0, sin cambios
- `inji-verify` — fork MOSIP v0.15.1, sin cambios
- `inji-verify-sdk` — SDK TypeScript + wrapper AngularJS custom IUGO

### Plugin de datos RENIEC (`rest-api-dataprovider-plugin`)
El plugin lee del token IDPeru el claim `individualId` (el DNI del ciudadano)
y el `scope` (qué credencial pide). Luego llama a:
- `ws-actas-reniec` — para credenciales de tipo acta (nacimiento, matrimonio)
- `ws-dnid-reniec` — para credenciales de tipo DNI (mayor de edad, filiación)

El package del plugin que Certify escanea:
```
io.mosip.certify.restapidataprovider.integration
```

Propiedades relevantes:
```properties
mosip.certify.integration.scan-base-package=io.mosip.certify.restapidataprovider.integration
certify.data-provider-plugin.restapi.document-number-key=individualId
mosip.certify.data-provider-plugin.restapi.scope-endpoint-mapping={"acta-nacimiento": "ACTA_NAC"}
mosip.certify.data-provider-plugin.restapi.context-path.acta_nacimiento=ws-actas-reniec
mosip.certify.data-provider-plugin.restapi.base-url=https://dnidigidesa.reniec.gob.pe
```

### IDPeru — capa de identidad compartida
IDPeru es el proveedor OIDC nacional de Perú. Equivale a eSignet en el stack MOSIP.
- JWKS: `https://idperu.gob.pe/v1/idperu/oauth/.well-known/jwks.json`
- Emite access tokens con claim `individualId` = DNI del ciudadano autenticado
- Todos los emisores validan sus tokens contra el mismo JWKS
- Un nuevo emisor necesita registrar su Certify como cliente OIDC en IDPeru

### Infraestructura AWS producción (arquitectura de referencia)
- EKS 1.33 en `sa-east-1`, c6a.large nodes, Istio activado
- RDS PostgreSQL Multi-AZ, 100GB, credenciales en Secrets Manager
- Redis 7.1 (ElastiCache) para caché de sesiones OID4VCI
- CloudHSM para claves de firma de VCs
- 3 cuentas AWS separadas: dev / staging / production
- EKS Pod Identity para permisos granulares por pod

### Hallazgos críticos pendientes en el stack RENIEC
(Referencia: reporte técnico `inji-reniec-final-review-en.docx`)

| ID | Archivo | Línea | Descripción |
|----|---------|-------|-------------|
| F-01 | `DataProviderRepositoryImpl.java` | 48 | Sin auth hacia APIs RENIEC — `new RestTemplate()` sin SSL/auth |
| F-02 | `OpenID4VPSession.ts` | 181 | Polling recursivo sin delay |
| F-03 | `VerifiablePresentationSubmissionServiceImpl.java` | 179 | Nonce VP no validado |
| F-04 | `VerifiablePresentationRequestServiceImpl.java` | 66 | `HashMap` no thread-safe |
| F-05 | `DataProviderRepositoryImpl.java` | 48 | `RestTemplate` sin timeouts |
| F-06 | `api.ts` | 9-10 | Nonce con `btoa(Date.now())` — no criptográfica |
| F-15 | `mimoto-issuers-config.json` | 4-5 | Emisores RENIEC no configurados en mimoto |

---

## Flujo OID4VCI completo (referencia)

```
Ciudadano abre wallet → selecciona "DNI Digital" (RENIEC)
   ↓
Wallet redirige a IDPeru (PKCE auth code flow)
   ↓
IDPeru autentica al ciudadano (DNI + biometría/PIN)
   ↓
IDPeru emite access token: { scope: "acta-nacimiento", individualId: "12345678", iss: "idperu..." }
   ↓
Wallet/mimoto intercambia auth code → access token
   ↓
POST https://certify.reniec.gob.pe/v1/certify/issuance/credential
  Authorization: Bearer {access_token}
  { format: "ldp_vc", ... }
   ↓
Certify:
  1. Valida token contra IDPeru JWKS
  2. Extrae scope → busca credential config en BD
  3. Extrae individualId → pasa al plugin
   ↓
Plugin llama ws-actas-reniec con el DNI
   ↓
RENIEC devuelve datos → Certify renderiza template Velocity → firma VC con CloudHSM
   ↓
VC retornada al wallet → ciudadano la tiene en su dispositivo
```

---

## mimoto — el directorio de emisores

### Cómo funciona hoy
mimoto lee `mimoto-issuers-config.json` de una URL HTTP base.
En producción RENIEC esto apunta a un S3 bucket.
TTL de caché: 60 minutos (Caffeine).

Cada entrada en el JSON:
```json
{
  "issuer_id": "RENIEC",
  "credential_issuer_host": "https://certify.reniec.gob.pe/v1/certify",
  "wellknown_endpoint": "https://certify.reniec.gob.pe/.well-known/openid-credential-issuer",
  "client_id": "inji-wallet-reniec",
  "redirect_uri": "io.mosip.residentapp.inji://oauthredirect",
  "authorization_audience": "https://idperu.gob.pe/v1/idperu/oauth/v2/token",
  "proxy_token_endpoint": "https://idperu.gob.pe/v1/idperu/oauth/v2/token",
  "enabled": "true"
}
```

### Error SSL en smoke test de mimoto
Si el smoke test de Fase 4 falla con `TLSV1_ALERT_INTERNAL_ERROR`, las causas
típicas son:
1. mimoto exige mTLS (certificado de cliente) — el smoke test no lo provee
2. Incompatibilidad de versión TLS entre el cliente Python y el servidor mimoto
3. Certificado mal configurado en mimoto

Diagnóstico:
```bash
curl -v --insecure https://mimoto.tu-dominio.org/v1/mimoto/issuers
```

### Gobernanza propuesta (RNE)
El RNE (Registro Nacional de Emisores) reemplaza el archivo estático en S3 por
un servicio HTTP operado por la SGD/PCM. Solo requiere cambiar
`config.server.file.storage.uri` en mimoto para apuntar al RNE.

### Trust Registry (roadmap)
Diferente al RNE. El RNE es para descubrimiento (wallet). El Trust Registry es
para verificación (verifiers externos que no usan mimoto).

```json
{
  "issuerDID": "did:web:certify.mtc.gob.pe",
  "legalName": "Ministerio de Transportes y Comunicaciones",
  "authorizedCredentialTypes": [{
    "type": "LicenciaConducirCredential",
    "validFrom": "2026-01-01",
    "validUntil": "2028-12-31",
    "authorizedBy": "did:web:sgd.pcm.gob.pe"
  }],
  "status": "active"
}
```

---

## Comandos útiles

```bash
# Ciclo completo on-prem
inji-issuer-deploy bootstrap ubuntu-onprem --dry-run
inji-issuer-deploy phase collect
inji-issuer-deploy phase infra --dry-run
inji-issuer-deploy phase config --dry-run
inji-issuer-deploy run

# Web UI
inji-issuer-deploy web --host 0.0.0.0 --port 8000

# Diagnóstico en el cluster
kubectl get pods -n inji-{id}
kubectl logs -n inji-{id} deploy/inji-certify-{id} --tail=50
kubectl describe pod -n inji-{id} -l app=inji-certify
kubectl get ingress -n inji-{id}
kubectl get certificate -n inji-{id}

# Upgrade de Helm release después de regenerar config
inji-issuer-deploy phase config
helm upgrade inji-certify-{id} mosip/inji-certify \
  -n inji-{id} \
  -f .inji-deploy/{id}/helm-values-certify.yaml \
  --version {chart_version}

# Semilla de estado para testing (on-prem)
python - << 'EOF'
import os
os.environ["INJI_STATE_FILE"] = "/tmp/test-onprem.json"
from inji_issuer_deploy.state import DeployState, IssuerConfig, save_state
from inji_issuer_deploy.cloud import CloudProviderConfig
from dataclasses import asdict

cfg = IssuerConfig(
    issuer_id="cdpi", issuer_name="CDPI New Issuer",
    issuer_description="Test issuer",
    issuer_logo_url="https://example.com/logo.png",
    base_domain="cdpi-cli-test.duckdns.org",
    rds_host="postgres-cdpi",
    idperu_jwks_uri="https://idperu.gob.pe/jwks.json",
    idperu_issuer_uri="https://idperu.gob.pe/v1/idperu",
    data_api_base_url="https://api.cdpi.gob.pe",
    data_api_auth_type="apikey",
    data_api_secret_arn="cdpi-api-key",
    mimoto_issuers_s3_bucket="mimoto-config",
    mimoto_base_url="https://mimoto.duckdns.org/v1/mimoto",
    provision_db=True,
    scope_mappings=[],
)
state = DeployState(issuer=cfg)
state.provider_cfg = asdict(CloudProviderConfig(provider="onprem"))
state.mark_done("collect")
save_state(state)
print("Estado creado")
EOF
```

---

## Convenciones de código

- Python 3.11+, `from __future__ import annotations` en todos los módulos
- Dataclasses para todo el estado — sin Pydantic en el core (Pydantic solo en webapp.py)
- Fases idempotentes: siempre verificar si el recurso existe antes de crearlo
- Outputs de fase persistidos en `state.phases[name].outputs` como dict plano
- Mensajes: `_step()` inicio, `_ok()` éxito, `_skip()` skip, `_warn()` advertencia, `_fail()` error
- Tests en `tests/` con pytest; mockear a nivel de SDK, no de red
- El `--dry-run` nunca toca infraestructura y debe funcionar sin credenciales
- Si se agrega un campo a `IssuerConfig`, también actualizar `IssuerConfigPayload` en `webapp.py`

---

## Dependencias de SDK por provider

```bash
# AWS (incluido en pyproject.toml)
pip install boto3

# Azure
pip install azure-identity azure-mgmt-containerregistry azure-keyvault-secrets \
            azure-storage-blob azure-mgmt-dns azure-mgmt-network azure-mgmt-msi

# GCP
pip install google-auth google-cloud-storage google-cloud-secret-manager \
            google-cloud-container google-cloud-dns google-api-python-client \
            google-cloud-artifact-registry

# On-premise (MinIO opcional)
pip install minio
```
