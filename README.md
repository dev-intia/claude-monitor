# claude-monitor

Panel local para ver tus sesiones de Claude Code: cuánto gastan, cuáles te esperan y cuándo conviene limpiarlas o abrir una nueva.

- No cierra sesiones ni toca nada en `~/.claude` (salvo el instalador de hooks, si lo ejecutas). Lo único que hace, y solo si pulsas, es [traer al frente una sesión en VS Code](#saltar-a-una-sesión-en-vs-code).
- Solo en `127.0.0.1`. Rechaza peticiones cuyo `Host` no sea local.
- Sin dependencias: Python 3.13 o superior y su biblioteca estándar. Una sola página HTML, sin CDN ni fuentes remotas.
- No envía datos fuera y no llama a Claude: mirarlo no consume tokens.

Qué hacer ante cada aviso: [GUIA-DE-USO.md](GUIA-DE-USO.md).

## Arrancarlo

```
python monitor.py
```

Abre <http://127.0.0.1:8765/>. Otro puerto: `python monitor.py --port 9000` o `port` en [config.toml](config.toml). Si el puerto está ocupado, termina con un error y no prueba otro. Otros servidores locales en sus propios puertos no le afectan.

Comprueba tu versión con `python --version`. Con `uv`, por ejemplo: `uv run --python 3.13 monitor.py`.

La primera vez lee los ficheros modificados en los últimos 7 días. Después solo lee lo nuevo, también tras reiniciar. Un fichero más antiguo se lee cuando vuelve a cambiar.

Los tests: `python -m unittest discover -s tests -t .`

## Verlo desde otra máquina (túnel SSH)

En la máquina desde la que quieres mirar:

```
ssh -N -L 8765:127.0.0.1:8765 usuario@maquina-con-claude
```

Abre <http://localhost:8765/>. El panel sigue escuchando solo en local en la otra máquina. Puedes usar otro puerto local (`-L 9000:127.0.0.1:8765`). Las notificaciones funcionan por el túnel porque `localhost` cuenta como origen seguro; el permiso y los silencios se guardan en cada navegador.

## Panel local y remoto con una orden

Desde la carpeta del repositorio, en Windows:

```
.\monitores.cmd            # levanta lo que falte y abre las pestañas
.\monitores.cmd -SinAbrir  # igual, sin abrir el navegador
.\monitores.cmd -Parar     # para el panel local y, si lo hay, el túnel y el remoto
```

- Sin más configuración, solo levanta el panel local: <http://localhost:8765/>.
- Para añadir un panel remoto, copia `monitores.config.example.ps1` como `monitores.config.ps1`, que git ignora, y rellénalo. Se verá en <http://localhost:9000/>.
- Se puede repetir sin riesgo: lo que ya está en marcha no se toca.
- Si indicas una red de Tailscale, la orden comprueba que esté activa, pero no la cambia. Si estás en otra, levanta solo el panel local y lo indica.
- Cada pestaña guarda sus propios permisos de aviso y filtros, porque cada puerto cuenta como un sitio distinto. La del panel remoto solo avisa si está abierta.

La máquina remota debe tener el panel en `~/claude-monitor`, como servicio de usuario `claude-monitor.service`, con Python 3.13 o superior. En Linux no se comprueba el proceso de cada sesión, así que indica actividad reciente en lugar de «abierta».

Para actualizarla tras cambiar el código, sustituye `main` por la rama que quieras llevar:

```
git archive main | ssh usuario@maquina-remota "tar -x -C ~/claude-monitor"
ssh usuario@maquina-remota "systemctl --user restart claude-monitor.service"
```

## Saltar a una sesión en VS Code

Para que vaya fluido, instala la [extensión auxiliar](#extensión-auxiliar-de-vs-code). El detalle de cómo funciona está en [docs/SALTO-A-VSCODE.md](docs/SALTO-A-VSCODE.md).

Las sesiones iniciadas en la extensión de VS Code muestran su logo:

- **En color:** la sesión está abierta. Al pulsarlo, su ventana pasa al frente con su pestaña abierta.
- **En gris:** la sesión está cerrada. Al pulsarlo se reabre en su carpeta, en la ventana de esa carpeta o en una nueva, lo que puede tardar bastantes segundos. VS Code puede restaurar también otras pestañas de esa ventana.

Pulsar una notificación lleva a su sesión si está abierta en VS Code; si no, te lleva al panel y marca su fila. Las sesiones de terminal no tienen logo.

- Para encontrar la ventana lee `workspaceFolders` de `~/.claude/ide/*.lock`. Esos ficheros también guardan un token, que no se usa.
- La ventana cambia directamente, localizada por su título. Para que Windows lo permita, el panel genera un evento de ratón vacío, que no mueve nada. Si aun así no lo logra o hay dos ventanas con el mismo nombre de carpeta, recurre a `Code.exe`, más lento.
- La pestaña se abre al instante si la ventana tiene cargada la extensión auxiliar. Si no, pasa por un enlace `vscode://`, bastante más lento, que arranca un proceso nuevo.
- El enlace a la pestaña solo se envía cuando Windows confirma que la ventana correcta está delante: en otra abriría una segunda copia de la sesión. Si no llega a confirmarlo, te deja en la ventana y un mensaje junto a la hora te pide abrir la pestaña a mano. Un `window.title` personalizado en VS Code provoca siempre este caso.
- No funciona con ventanas de varias carpetas. La primera vez VS Code puede pedir permiso para que la extensión abra el enlace.
- Solo en Windows: en otros sistemas el logo no aparece. Si miras este panel por un túnel SSH, la ventana se mueve en la máquina del panel, no en la tuya.
- La petición es un `POST` a `/api/jump` que solo admite el ID de una sesión del registro, y el mismo origen que el panel. El resto de peticiones de escritura siguen rechazadas.

### Extensión auxiliar de VS Code

```
python install_extension.py      # empaqueta vscode-extension/ y la instala
python uninstall_extension.py    # la quita; el panel vuelve al enlace
```

Necesita la orden `code` en el `PATH`. Las ventanas abiertas suelen cargarla solas; si en `~/.claude-monitor/windows/` falta alguna, ejecuta **Developer: Reload Window** en ella cuando sus sesiones estén paradas. Sin extensión, el salto funciona igual, pero más lento. Escucha en un puerto propio de `127.0.0.1`, con un token nuevo cada vez, y publica ambos en `~/.claude-monitor/windows/<pid>.json`. Solo sabe abrir la pestaña de una sesión por su ID: no admite rutas ni mensajes, y rechaza cualquier petición de un navegador. Usa un comando interno de la extensión de Claude; si desaparece en una actualización, el panel vuelve al enlace.

`GET /api/jumps` enseña cómo fueron los últimos saltos (camino y milisegundos). Se guarda solo en memoria.

## Hooks (opcionales)

Sin hooks, el panel funciona con el historial y el registro de sesiones, y lo indica en pantalla. Con ellos, sabe con más precisión si una sesión pide permiso, si te toca y si los agentes en segundo plano han acabado de verdad.

Instalar. Enseña el cambio exacto, pide confirmación, hace copia de seguridad (`settings.json.bak-claude-monitor-<fecha>`) y conserva lo que ya tengas:

```
python install_hooks.py            # --dry-run solo enseña el cambio
```

Instálalo con el Python que quieras que ejecute el hook: queda su ruta absoluta en los ajustes.

Desinstalar. Quita solo los hooks de este panel:

```
python uninstall_hooks.py          # --dry-run solo enseña el cambio
```

Ambos aceptan `--settings RUTA` para actuar sobre otro fichero y `--yes` para no preguntar.

El hook es asíncrono, termina siempre con código 0 y no escribe en stdout. Si el panel está apagado, sigue funcionando sin molestar. Guarda en `~/.claude-monitor/hook-events.jsonl`, con tamaño limitado, solo `session_id`, evento, `notification_type`, número de tareas pendientes y hora. Nunca guarda `last_assistant_message`, `message`, `title` ni `tool_input`.

Eventos que usa:

| Evento | Efecto |
|---|---|
| `Notification` · `permission_prompt`, `elicitation_dialog`, `elicitation_url_dialog`, `agent_needs_input` | Espera permiso |
| `Notification` · `idle_prompt` | Te toca |
| `Stop` | Con tareas pendientes, esperando agentes y sin aviso. Sin pendientes, ha terminado |
| `SubagentStop` | Actualiza las tareas pendientes |
| `UserPromptSubmit` | Borra la espera |
| `SessionEnd` | Sesión cerrada |

Como tareas pendientes no cuenta las de tipo `shell` ni `monitor`: un servidor de desarrollo en segundo plano dejaría la sesión «esperando agentes» para siempre.

No hace falta reiniciar las sesiones abiertas: en las pruebas (versiones 2.1.316 a 2.1.318), las que ya estaban abiertas recogieron los hooks al poco de instalarlos.

En las pruebas, `idle_prompt` llegó de sesiones de terminal y de ninguna de la extensión de VS Code. Por eso «te toca» también se calcula con el historial, a los 60 s de terminar el turno (`idle_seconds`): funciona igual en ambos casos.

Con `claude -p` se disparan `UserPromptSubmit`, `Stop` y `SessionEnd`. Al no haber nadie a quien esperar, no hay `idle_prompt`: la sesión aparece un momento como trabajando y después como cerrada, sin avisos. Los hooks asíncronos pueden cancelarse al terminar el proceso, según la documentación, así que alguno podría perderse en sesiones `-p` muy cortas.

## Qué lee y qué guarda

| Fichero | Qué toma |
|---|---|
| `~/.claude/projects/<proyecto>/<sesión>.jsonl` | Tokens, modelo, horas, `cwd` y `entrypoint` de inicio, y el tipo de cada línea |
| `~/.claude/projects/<proyecto>/<sesión>/subagents/agent-*.jsonl` | Tokens, modelo y horas, aparte de la sesión principal |
| `agent-*.meta.json` | Solo `finished` |
| `~/.claude/sessions/<pid>.json` | `pid`, `sessionId`, `cwd`, `entrypoint`, `procStart`, `pidDomain`, `status`, `waitingFor`, `statusUpdatedAt` |
| `~/.claude/settings.json` | Solo la clave `hooks`, para saber si están instalados |
| `~/.claude/ide/*.lock` | Solo `workspaceFolders`, y solo al pulsar para saltar a una sesión |

Nunca abre los ficheros `*.key` de `~/.claude/sessions` ni credenciales. El texto de las conversaciones se analiza de paso para clasificar cada línea y se descarta: no se guarda ni se muestra, igual que los comandos y las rutas de ficheros tocados. La única excepción es el título de la sesión, el que le da Claude Code o el que le pongas tú: se guarda en la caché local y se muestra en la tabla, pero nunca en las notificaciones.

Guarda en `~/.claude-monitor/`, fuera de `~/.claude`: `cache.json` (desplazamientos, totales y, para poder reabrir cada sesión, la carpeta donde empezó, que no se envía a la página), `notifications.json` (lo ya avisado) y el fichero de eventos del hook. La extensión auxiliar, si la instalas, añade `windows/`. Puedes borrar la carpeta; solo perderás la memoria de avisos y se volverá a leer la última semana.

## Qué significan las cosas

### Unidades

El gasto se expresa en **unidades ponderadas** y en porcentaje, nunca en dinero. Es una **estimación**:

```
unidades = peso del modelo × (entrada×1 + escritura 5 min×1,25 + escritura 1 h×1,25 + lectura de caché×0,1 + salida×5)
```

Cada mensaje se pondera con su propio modelo. Todos los pesos están en [config.toml](config.toml). Un modelo que no esté en `[model_weights]` aparece como **desconocido**: se muestran sus tokens, pero no suma unidades, y un aviso en pantalla lo indica.

Un mismo `message.id` ocupa varias líneas. En versiones recientes repiten el mismo `usage`; en algunas antiguas la salida crece de una a otra (16, 16, 273). El panel se queda con el máximo de cada campo, lo que no cuenta de más ni se queda corto en ningún caso.

### Contexto

Es `input + cache_creation + cache_read` del último turno de la sesión principal, nunca de un subagente. Se muestran tokens absolutos, sin suponer una ventana: la barra crece con el dato y lleva marcas en los umbrales. Tras compactar pasa a `postTokens`; tras `/clear`, a 0.

### Estado

| Estado | Significado |
|---|---|
| Espera permiso | Bloqueada hasta que respondas |
| Te toca | Terminó su turno y no has escrito |
| Esperando agentes | Parada, con subagentes o tareas en marcha. No avisa |
| Trabajando | Turno en curso |
| Parada | Ni trabaja ni te espera |

Debajo, **abierta** o **cerrada** si se ha podido comprobar con el proceso real (`pid` + `procStart`). Si no, **actividad reciente**, que no garantiza que siga abierta. Esta comprobación solo se ha verificado en Windows; en otros sistemas el panel no afirma «abierta».

Las sesiones se ordenan por actividad: arriba, la de mensaje enviado o recibido más reciente. Cuentan también los resultados de herramientas de la sesión principal, pero no la actividad de sus subagentes.

Encima de la tabla hay un cuadrado por estado: pulsa uno o varios para ver solo esos; sin ninguno marcado se ven todas. **Solo abiertas** deja únicamente las sesiones con su proceso en marcha y se combina con los estados; donde no se puede comprobar el proceso pasa a llamarse **Solo con actividad reciente**. **Solo en VS Code** deja las sesiones con el logo en color, es decir, las abiertas ahora en la extensión; no aparece fuera de Windows. A la derecha, el buscador filtra por proyecto o nombre de sesión, sin distinguir mayúsculas ni acentos; si escribes varias palabras, deben aparecer todas. Pulsa la cabecera de una columna numérica para ordenarla de mayor a menor, con las filas sin valor al final; otra pulsación vuelve al orden por actividad. Filtro y orden se recuerdan en cada navegador; la búsqueda se borra al recargar. Nada de esto afecta a los avisos ni a los totales.

Un subagente cuenta como activo si su `.meta.json` no dice `finished`, escribió en los últimos 30 minutos y su sesión no está cerrada.

### Consejos

Son primeras estimaciones. Se aplica el primero que encaje:

| Consejo | Cuándo |
|---|---|
| Espera | Tiene subagentes activos. Nunca aconseja limpiar en este caso |
| Caché caducada, empieza de nuevo | Parada, más de 60 min desde su último turno y contexto de más de 60.000 |
| Conviene limpiar | 200.000 o más, parada y sin subagentes activos |
| Vigilar | Entre 150.000 y 200.000, o más si sigue en marcha o espera permiso |
| Seguir | Menos de 150.000 |

La caché restante se calcula desde el último turno, suponiendo 1 hora; es configurable porque también puede ser de 5 minutos.

## Notificaciones

Pulsa **Permitir notificaciones** en el panel. Hay un interruptor general y, en **Avisar de**, una casilla por tipo: marcada, avisa; desmarcada, queda en silencio. Solo afecta a los avisos emergentes; la tabla y el título de la pestaña no cambian. Avisan «espera permiso», «te toca», «conviene limpiar» y «caché caducada, empieza de nuevo». «Vigilar» solo se ve en el panel, y el título de la pestaña muestra cuántas sesiones requieren atención.

- Un aviso por cada espera nueva. Los de contexto, uno por sesión y nivel; vuelven a avisar si la sesión baja de nivel.
- Los de contexto esperan a que la sesión pare y no tenga subagentes, y no se adelantan a «te toca». Si coinciden, se unen: `Te toca · demo · 310K · conviene limpiar`.
- Las sesiones cerradas no avisan, aunque sigan en el panel.
- Al primer arranque no avisa de lo que ya estaba: solo de lo que cambie después.
- Lo ya avisado sobrevive a recargar la pestaña y a reiniciar el servidor.

Con **Probar aviso** compruebas al momento si los avisos llegan a tu pantalla: envía un ejemplo por cada tipo marcado, y ninguno si los avisos están desactivados. **Si no ves nada con el permiso concedido**, mira primero Windows: en *Configuración > Sistema > Notificaciones*, tu navegador debe estar activado. Si está desactivado, el navegador crea el aviso y el sistema no lo enseña; el panel no puede detectarlo. El título de la pestaña sigue funcionando en ese caso.

La pestaña tiene que estar abierta, aunque sea en segundo plano. Si el navegador la descarta para ahorrar memoria, no avisará hasta que vuelva a cargarse; en ese caso, exclúyela de esa función en los ajustes del navegador.

## Licencia

[MIT](LICENSE).
