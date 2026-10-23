# Checklist del día de la hackathon (22–23 oct 2026)

Antes del evento no sabemos cómo invoca el servidor al equipo. Este checklist sirve para averiguarlo en minutos con nuestras propias herramientas y decidir rápido. Todo lo marcado **(gratis)** solo lee; lo marcado **(de pago)** invoca modelos o runtimes.

Abre cada terminal con:
```powershell
$env:PATH = "C:\dev\afc\tools\node_modules\.bin;C:\dev\afc\.venv\Scripts;" + $env:PATH
$env:AWS_PROFILE = "afc-dev"; $env:AWS_REGION = "us-east-1"; $env:AWS_DEFAULT_REGION = "us-east-1"
cd C:\dev\afc\agentic-football-sample-agents
```

## 1. La víspera
- [ ] **Tests offline (gratis):** `python -m unittest discover -s afc_tests -p "test_*.py"` debe acabar en `OK`; `python afc_tools\sim_match.py` debe dar `invalid 0` e `incompatible 0`.
- [ ] **Cambios locales pendientes:** decidir qué se despliega antes del partido (ver la sección *Pending local changes* del runbook). Desplegar con el procedimiento del apartado 6.
- [ ] **Alerta de presupuesto** en Billing → Budgets (la creas tú).

## 2. Por la mañana, antes del primer partido
- [ ] **Credenciales (gratis):** `aws sts get-caller-identity` → `user/afc-dev`.
- [ ] **Estado (gratis):** los cinco runtimes en `READY`: `python afc_tools\e2e_invoke.py` muestra el plan con los 5 ARN.
- [ ] **Prueba real (de pago, 5 llamadas):** `python afc_tools\e2e_invoke.py --invoke` → `5/5 runtimes returned a valid command`.

## 3. Preguntas para los organizadores (la primera vez que haya contacto)
| Pregunta | Por qué importa | Cómo lo comprobamos nosotros si no hay respuesta |
|---|---|---|
| ¿Cómo registramos el equipo? ¿ARN de cada runtime, uno por posición? | Sin esto no jugamos | Los ARN salen en `e2e_invoke.py` (plan) |
| ¿Desde qué cuenta AWS invoca el servidor? ¿Hace falta darle permiso de invocación (política de recurso) o credenciales? | Por defecto solo pueden invocar identidades de nuestra cuenta con permiso IAM; para otra cuenta haría falta concederlo (no lo hemos verificado) | Si no llega ninguna llamada: `match_watch.py` muestra `calls 0` |
| Formato del payload: ¿`{"prompt": "{gameState, teamId, myPlayers}"}` como en el sample? | Si es otro, jugaríamos a ciegas | `match_watch.py` → `PROTOCOL` (nombres de claves, sin valores) |
| ¿Usa el mismo `runtimeSessionId` durante todo el partido o uno nuevo por tick? | En caliente ~1 s por tick; en frío 7–9 s | `match_watch.py` → `sessions: reused / new session per call` |
| Ritmo de ticks y tiempo máximo de respuesta | Si llegamos tarde, ¿se descarta el comando? | `match_watch.py` → `interval` frente a `handler` |
| ¿`myPlayers` y `teamId` significan lo mismo que en el sample? ¿Coordenadas reflejadas para el equipo visitante? | Cada runtime controla un jugador fijo | `match_watch.py` → `asked (team, player)` + capturas del GK |
| Semántica de `duration`, `PRESS_BALL` e `INTERCEPT`, límites de cuota | Ajuste táctico | Capturas + logs |

## 4. Primeros minutos del primer partido
- [ ] `python afc_tools\match_watch.py --minutes 5 --team <nuestro id>` **(gratis)**. Leer en este orden:

| Señal | Normal | Si no | Acción |
|---|---|---|---|
| `calls` | > 0 en los cinco | 0 | El servidor no nos llama: ¿permiso entre cuentas? ¿ARN mal registrado? |
| `PROTOCOL` (solo si están desplegados los cambios pendientes) | ausente | aparece | Mirar las claves del aviso y una captura del GK; adaptar el handler (apartado 6) |
| `asked (team, player)` | GK→0 … FWD2→4, nuestro equipo | `UNEXPECTED` | Revisar `myPlayers`/`teamId` en una captura antes de tocar nada |
| `sessions` | `reused` | `new session per call` | Cada tick será lento (7–9 s). Valorar probar `AFC_PLATFORM_VERSION=V2` (apartado 6) |
| `interval` frente a `handler` | `handler` muy por debajo de `interval` | `handler` cerca de `interval` | Más reglas deterministas o modelos más rápidos |
| `sources` | sobre todo `model`, algo de `validator_fallback` | `error_fallback` / `last_resort` | Leer el log group de ese runtime |

## 5. Entre partidos
- [ ] **Capturas del GK (gratis):** exportar el log del GK y ejecutar `lab\experiment_7\run.py extract <log> <dir>` y `inspect` (desde el repo de OneDrive). Quitar duplicados por hash (cada captura sale dos veces).
- [ ] **Calibrar con estados reales:** posesión (`CONTROL_RADIUS` 3, `MIN_SEPARATION` 5, campos de equipo), rango de stamina, tamaño del área (16,5 × ±20,16), coordenadas del visitante.
- [ ] **Rechazos del validador por agente:** si un agente acumula `validator_fallback`, revisar el motivo en sus logs (`rejected by validator (...)`).

## 6. Redespliegue rápido (pide aprobación antes)
```powershell
cd ai-team-strands-balanced
$env:AGENTCORE_SKIP_INSTALL = "1"
$env:UV_CONSTRAINT = "C:\dev\afc\agentic-football-sample-agents\afc_tools\constraints-deployed-20261018.txt"
# opcional, solo si las sesiones son nuevas por llamada y se decide probarlo: $env:AFC_PLATFORM_VERSION = "V2"
python ..\afc_tools\with_lib.py agentcore deploy --diff --target default    # revisar: solo lo esperado
python ..\afc_tools\with_lib.py agentcore deploy --target default
cd ..; python afc_tools\e2e_invoke.py --invoke                             # 5/5 antes de seguir
```
- **V2 está sin probar** con nuestros zips. La documentación lo menciona para despliegues de código, pero no trae un ejemplo con zip. **Marcha atrás:** redesplegar sin la variable. Cambiar la versión de plataforma no recrea los runtimes, así que los ARN se mantienen.
- **Límite de V2:** 1,5 KB de variables de entorno en despliegues de código. Las nuestras ocupan menos de 30 bytes.

## 7. Después del evento
- [ ] Quitar `envVars` (captura) del GK en `agentcore/agentcore.json` y redesplegar, o eliminar el stack (sección *Cleanup* del runbook).
- [ ] Desactivar las claves de acceso de `afc-dev`, quitar `AfcCdkBootstrapResources` y dejar de usar el perfil root `afc`.
- [ ] Opcional: poner retención a los log groups `/aws/bedrock-agentcore/runtimes/*` (hoy "never expire").
