# Guía de uso según los avisos

Qué hacer con una sesión según lo que indica el panel. Son criterios de partida: los umbrales son estimaciones y se ajustan en [config.toml](config.toml).

## Regla general

**El contexto se paga en cada turno, y es mucho más caro si la caché ha caducado.** Por eso conviene cerrar los temas pronto y dejar lo importante en ficheros, no en la conversación.

Coste aproximado de un turno, solo por leer el contexto (unidades del panel, no dinero):

| Situación | Cálculo | Unidades |
|---|---|---|
| Sesión nueva, contexto de 45K | 45K × 1,25 | ~56 |
| 200K con caché viva | 200K × 0,1 | ~20 |
| 200K con caché caducada | 200K × 1,25 | ~250 |
| 300K con caché viva | 300K × 0,1 | ~30 |
| 300K con caché caducada | 300K × 1,25 | ~375 |

Con la caché viva, seguir unos turnos sale barato. Lo caro es volver a una sesión grande después de una pausa.

## Qué hacer ante cada aviso

| El panel dice | Qué significa | Qué hacer |
|---|---|---|
| **Seguir** | Menos de 150K | Trabaja con normalidad |
| **Vigilar** | De 150K a 200K, o más si sigue en marcha | No la interrumpas. Remata lo que tengas entre manos y evita empezar otro tema en esta sesión |
| **Conviene limpiar** | 200K o más, parada y sin subagentes | Ver la sección siguiente |
| **Caché caducada, empieza de nuevo** | Parada más de 60 min, con más de 60K | No sigas en ella por inercia. Ver más abajo |
| **Espera** | Tiene subagentes activos | No toques nada hasta que acaben |
| **Espera permiso** | Está bloqueada hasta que respondas | Responde. Si tardas más de 60 min, la caché caduca |
| **Te toca** | Terminó y espera tu mensaje | Si el tema sigue, responde antes de 60 min. Si ha terminado, ciérralo |

La caché de 60 min es una suposición del panel; también puede durar 5 min. Si es tu caso, cambia `ttl_minutes` en `config.toml`.

## Conviene limpiar: más de 200K

200K es el umbral del panel, no un límite de Claude. Pasarlo no rompe nada, pero cada turno arrastra todo ese contexto. Elige según cómo esté el trabajo, siempre con la sesión parada:

| Situación | Qué hacer |
|---|---|
| **El tema ha terminado** o el siguiente es otro | Guarda notas si hay algo que conservar y usa `/clear` o abre otra sesión. Es la mejor opción |
| **Estás a mitad y necesitas lo hablado** | Usa `/compact` indicando qué conservar, con la caché todavía viva |
| **Estás a punto de terminar**, unos pocos turnos | Termina primero: con la caché viva es barato |
| **Vas a parar un rato largo** | Guarda notas ahora, antes de que caduque la caché, y sigue después en una sesión nueva |

Ejemplo para compactar:

```
/compact Conserva las decisiones y su motivo, los ficheros modificados, lo que está a medias y el siguiente paso. Descarta los resultados de exploración.
```

Ejemplo para guardar notas antes de limpiar:

```
Actualiza las notas del proyecto con: qué ha quedado hecho, decisiones y por qué, lo que está a medias, siguiente paso y qué ficheros mirar primero. No incluyas lo que ya se deduzca del código o de git.
```

## Caché caducada

Cualquier mensaje a esa sesión, también pedirle notas o compactarla, vuelve a procesar todo su contexto. Por eso:

1. **Si lo importante ya está en ficheros o en git:** abre una sesión nueva y no vuelvas a escribir en la antigua.
2. **Si hay algo valioso solo en esa conversación:** pídele una vez las notas, asumiendo el coste, y abre una sesión nueva.
3. **No la compactes para seguir en ella:** pagas el mismo coste y además te quedas con un resumen.

## Al acabar el día

1. Guarda las notas con la caché todavía viva.
2. Haz commit de lo terminado, o anota qué queda sin confirmar.
3. Mañana abre una sesión nueva y pídele que lea las notas y te diga el siguiente paso antes de empezar.

No compactes al final del día: mañana la caché habrá caducado igualmente.

## Para no llegar a 200K

- Una sesión por tema.
- Encarga las exploraciones amplias a subagentes, que solo devuelven la conclusión.
- A partir de **Vigilar**, ve pensando en un punto de corte natural.
- Lleva a `CLAUDE.md` las reglas permanentes, para no tener que repetirlas.
