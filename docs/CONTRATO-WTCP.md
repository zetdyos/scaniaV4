# SCANIA v4: lo que WTCP necesita de la respuesta

**De:** WTCP · **Para:** SCANIA · **Fecha:** 2026-10-17

> Copia del contrato recibido de WTCP (cliente de la API). Estado de cumplimiento en SCANIA v4 al final, en
> "Estado en SCANIA v4".

Este documento define solo el intercambio de datos entre WTCP y SCANIA v4: qué manda WTCP, qué campos lee de la respuesta y qué hace con cada uno. Cómo SCANIA lee, separa o clasifica las facturas lo decide SCANIA.

Se apoya en la API que SCANIA ya publica en `http://192.168.1.77:8091/docs`: las mismas rutas y los mismos campos. La sección 6 lista lo que todavía falta.

---

## 1. Rutas que usa WTCP

| Ruta | Para qué |
|---|---|
| `GET /api/health` | Antes de cada pasada. Si no responde, o si algún `models[].ok` es `false`, WTCP no sube nada y reintenta más tarde. |
| `POST /api/jobs` | Subir un archivo. |
| `GET /api/jobs/{id}` | Consultar el estado cada 5 segundos, hasta `done` o `error`. |
| `GET /api/jobs/{id}/result.json` | Leer las facturas, una sola vez, cuando el trabajo está `done`. |

WTCP no usa `?full=1`, `/pdf`, `GET /api/jobs` ni el websocket.

---

## 2. Lo que manda WTCP

- `POST /api/jobs` en `multipart/form-data`, campo `file`.
- Un archivo por trabajo: PDF, PNG, JPG/JPEG o BMP, con nombre en ASCII.
- WTCP sube un archivo, espera a que termine y recién entonces sube el siguiente.
- Cada subida es un trabajo nuevo, aunque el archivo ya se haya subido antes (por ejemplo, cuando una persona pide volver a analizar un invoice).
- En todas las llamadas manda el API-Token en la cabecera `X-API-Key` (sección 7).

---

## 3. Estado del trabajo (`GET /api/jobs/{id}`)

WTCP lee estos campos:

| Campo | Qué hace WTCP |
|---|---|
| `id` | Lo guarda con el análisis del invoice. |
| `state` | `queued` / `running`: sigue esperando. `done`: lee `result.json`. `error`: mira `error_code`. |
| `queue_position` | Solo para su log. |
| `n_pages` | Lo guarda. `0` en un trabajo `done` = el archivo no tiene páginas legibles: lo marca para que lo mire una persona. |
| `error` | Lo muestra a la persona que revisa. |
| `error_code` **(nuevo)** | Solo con `state = error`. `UNREADABLE`: el problema es el archivo, WTCP lo marca para que lo mire una persona. `INTERNAL`: el problema es de SCANIA, WTCP lo reintenta más tarde sin marcar nada. |

WTCP espera hasta 45 minutos por archivo. Si para entonces no terminó, corta la pasada y lo vuelve a subir en la siguiente.

---

## 4. Respuestas HTTP

| Respuesta | Qué entiende WTCP |
|---|---|
| `201` en `POST /api/jobs`, con `{"id": "..."}` | Trabajo creado. |
| `200` | OK. |
| `401` / `403` | Problema con el API-Token: corta la pasada y reintenta más tarde. |
| `404` con `{"detail": "trabajo no encontrado"}` en `GET /api/jobs/{id}` | El trabajo se perdió: lo vuelve a subir en la próxima pasada. |
| Otro `4xx` en `POST /api/jobs` | El archivo no se aceptó: lo marca para que lo mire una persona, con el `detail`. |
| `5xx`, `408`, `429` o sin respuesta | SCANIA no está disponible: corta la pasada y reintenta más tarde. |

---

## 5. El resultado (`GET /api/jobs/{id}/result.json`)

Un array de facturas, como hoy.

- **Array vacío:** el archivo no tiene ninguna factura (un albarán, un recibo de pago, una confirmación de envío). WTCP saltea ese archivo. Si ninguno de los archivos del invoice es una factura, se lo avisa a una persona.
- **Sin líneas `goods`:** si ninguna factura trae líneas `goods`, WTCP marca el invoice para que lo mire una persona.

### 5.1 Lo que WTCP lee de cada factura

| Campo | Formato | Qué hace WTCP |
|---|---|---|
| `id` | entero | Junto con `seq`, identifica cada línea. |
| `total` | importe | El precio del package es la **suma de `total` de todas las facturas del archivo**. En el SED, la suma de las líneas de una factura no puede superar su `total`. |
| `total_is_printed` | booleano | `true` = el total está impreso en el papel. WTCP lo guarda como "total impreso". |
| `motivo_revision` | `""`, `LECTURA`, `CLASIFICACION` o `ESTRUCTURA` | `""` = SCANIA da la factura por buena: WTCP la usa para el precio y el SED **sin que nadie la revise**. Cualquier otro valor manda el invoice a "Needs validation", y una persona lo revisa antes de declararlo. La pantalla lo explica así: `LECTURA` = revisar las cifras contra el papel; `CLASIFICACION` = revisar los códigos; `ESTRUCTURA` = el documento no parece una factura de mercadería. |
| `mismatch` | texto | Con `LECTURA`, WTCP lo muestra tal cual, como explicación, a la persona que revisa. WTCP no arma mensajes propios. Tiene que venir en inglés. |
| `version_catalogo` | texto | Se guarda como la versión del catálogo con la que se clasificó. |
| `ocr.proveedor` | texto | Vendedor o tienda. Se muestra. |
| `ocr.fecha_pedido` | texto | Fecha de la factura, tal cual está impresa. WTCP la interpreta. |
| `ocr.subtotal_articulos` | importe o `""` | Subtotal. Se muestra. |
| `ocr.envio` | importe o `""` | Envío. Se muestra. |
| `ocr.impuestos` | importe o `""` | Impuestos. Se muestran. |
| `ocr.descuentos` | lista de `{"concepto": texto, "importe": importe}` **(forma nueva)** | WTCP suma los `importe` y lo muestra como descuento. Cada `importe` va en positivo. Solo descuentos **aplicados** (los que cuadran subtotal + cargos − descuentos = total); los ahorros informativos ya incluidos en el precio («You saved», «Price includes $X off», monedas) no se incluyen **(desde 2026-10-19)**. |
| `ocr.other_charges` | lista de `{"concepto": texto, "importe": importe}` **(añadido por SCANIA, 2026-10-18)** | Cargos que no son artículos, envío, impuestos ni descuentos. Se muestran; no son mercadería. Se rellena con las líneas `other_charges` cuyo importe está literal en el OCR. |
| `lineas` | lista | Ver 5.2. |

### 5.2 Lo que WTCP lee de cada línea

| Campo | Formato | Qué hace WTCP |
|---|---|---|
| `line_kind` | texto: `goods`, `shipping`, `tax`, `discount`, `subtotal_or_total`, `other_charges` o `payment` (dinero pagado o pendiente: cobro a tarjeta, saldo) **(valores desde 2026-10-18; `payment` desde 2026-10-18 noche)** | Solo las líneas `goods` son mercadería, y son las que WTCP declara. Las demás se ignoran para el SED (se pueden mostrar en la revisión). |
| `seq` | entero | Junto con el `id` de la factura, identifica la línea. |
| `descripcion` | texto | Va al SED y a la pantalla de revisión. |
| `cantidad` | número en texto | Se guarda y se muestra. |
| `importe_linea` | importe | El valor de la línea en el SED. |
| `divisa` | código ISO 4217 | La moneda. |
| `ocr.precio_unitario` | importe o `""` | Se guarda y se muestra. |
| `ocr.pagina` | entero | Página del archivo, para ubicar la línea en el PDF. |
| `codigo` | 10 dígitos o `""` **(hoy vacío)** | El Schedule B de exportación de EE. UU. que se declara en el SED. WTCP calcula con él el arancel de importación de Curaçao. Si el código no está en el catálogo Schedule B de WTCP, lo elige una persona. |
| `estado` | `sugerencia_segura`, `dudosa` o `no_clasificado` **(hoy vacío)** | `dudosa` y `no_clasificado` se resaltan para que una persona confirme el código. `no_clasificado` va con `codigo` vacío. |
| `confianza_modelo` | `alta`, `media`, `baja` o `""` | Se muestra tal cual. |
| `confianza` **(nuevo)** | número de 0 a 1, o `null` | Se guarda y se muestra; hoy no decide nada en WTCP. `null` si no hay código. |
| `canal` **(nuevo)** | texto, o `null` | El canal del que sale `confianza`. WTCP solo compara valores de `confianza` del mismo canal. |
| `calibrado` **(nuevo)** | booleano, o `null` | `true` = `confianza` es una probabilidad: de las líneas con 0.80, alrededor del 80 % tienen el código correcto. |

`confianza`, `canal` y `calibrado` significan lo mismo que en la ruta `/confianza` de v3; ahora vienen en la línea.

### 5.3 Formato

- Importes: texto con punto decimal y sin separador de miles, como hoy (`"90.94"`).
- Texto sin valor: `""`, como hoy.
- `confianza` es un número JSON; `calibrado` es un booleano JSON.
- Campos de más: WTCP ignora todo lo que no está en esta sección. `hs6`, `candidatos`, `referencia`, `estado` de la factura, `result_version` y el resto de `ocr` pueden seguir viniendo.
- Una respuesta que no cumple este formato se registra como error y WTCP no intenta interpretarla.

### 5.4 Ejemplo

Factura real de `31143.pdf`. Los valores de `codigo`, `estado` y confianza son ilustrativos; se omiten los campos que WTCP no lee.

```json
[
  {
    "id": 1,
    "motivo_revision": "",
    "mismatch": "",
    "total": "199.99",
    "total_is_printed": true,
    "version_catalogo": "SCHEDULE_B 2026-07",
    "lineas": [
      {
        "line_kind": "goods",
        "seq": 0,
        "descripcion": "LEQC Professional Japanese Hangiri Sushi Rice Mixing Tub Set - Wooden Oke Bowl with Paddle & Makisu",
        "cantidad": "1",
        "importe_linea": "199.99",
        "divisa": "USD",
        "codigo": "4419900000",
        "estado": "sugerencia_segura",
        "confianza_modelo": "alta",
        "confianza": 0.91,
        "canal": "catalogo",
        "calibrado": false,
        "ocr": {"precio_unitario": "199.99", "pagina": 1}
      }
    ],
    "ocr": {
      "proveedor": "amazon",
      "fecha_pedido": "September 26, 2026",
      "subtotal_articulos": "199.99",
      "envio": "0.00",
      "impuestos": "0.00",
      "descuentos": []
    }
  }
]
```

---

## 6. Lo que falta hoy

1. `codigo` y `estado` en cada línea `goods` (hoy vienen vacíos).
2. `confianza`, `canal` y `calibrado` en cada línea.
3. `error_code` en el estado del trabajo, cuando `state = error`.
4. La forma de cada elemento de `ocr.descuentos`: `{"concepto": texto, "importe": importe}`.
5. Aceptar PNG, JPG/JPEG y BMP en `POST /api/jobs` (hoy la documentación dice "PDF a procesar").
6. `mismatch` en inglés.
7. El acceso de la sección 7: API-Token obligatorio en todas las rutas `/api/...`, la página demo solo en `127.0.0.1`, y un API-Token de prueba y otro de producción para WTCP.

---

## 7. Acceso

| Qué | Dónde | Cómo |
|---|---|---|
| Las APIs: todas las rutas `/api/...`, incluidos `/api/health`, `/api/jobs/{id}/pdf` y el websocket | `http://192.168.1.77:8091` | Con API-Token en la cabecera `X-API-Key`. Sin token, o con uno inválido, responden `401` con `{"detail": "..."}`. |
| La página demo ("Demo OCR de facturas", la raíz `/`) | Solo `http://127.0.0.1:8091`, desde la misma máquina | Desde la red no responde. |
| La documentación (`/docs`, `/redoc`, `/openapi.json`) | `http://192.168.1.77:8091` | Sin token: no muestra datos. |

- SCANIA le da a WTCP dos API-Tokens: uno de prueba y uno de producción.
- WTCP guarda el API-Token en su configuración, nunca en el código, y lo manda en todas las llamadas.
- El motivo: hoy cualquiera en la red puede abrir la página demo, listar todos los trabajos y bajar los PDFs de los clientes sin clave.

---

## Estado en SCANIA v4 (2026-10-17)

| Punto de §6 | Estado |
|---|---|
| 1-2. Clasificación (`codigo`, `estado`, `confianza`, `canal`, `calibrado`) | **Pendiente.** Los campos ya vienen en cada línea con su forma (`""` / `null`); los rellenará la fase de clasificación, que aún no existe. `CLASIFICACION` en `motivo_revision` también queda para esa fase. |
| 3. `error_code` | Hecho. `INTERNAL` también para trabajos cortados por un reinicio del servidor. |
| 4. `ocr.descuentos` | Hecho (`importe` en positivo). |
| 5. PNG, JPG/JPEG, BMP | Hecho. La imagen se convierte a un PDF de una página antes de leerla; `/pdf` sirve ese PDF. |
| 6. `mismatch` en inglés | Hecho (también `ocr.incidencias`). |
| 7. Acceso | Hecho. Tokens en `api_keys.json` (fuera de git). Desde `127.0.0.1` no se pide token: es la propia página demo. |

Notas de SCANIA:
- Un archivo dañado, vacío, de otro tipo, con nombre no ASCII o de más de 50 MB se rechaza en `POST /api/jobs` con `400`/`413` y `detail` en inglés, sin crear trabajo.
- Un documento sin número de pedido reconocible y sin líneas de artículos sale como factura con `motivo_revision: "ESTRUCTURA"`; SCANIA no devuelve `[]` por inferencia (un `[]` equivocado haría saltar una factura real sin que nadie la vea). `[]` solo si el archivo no tiene páginas.
- `ocr.proveedor` hoy es el formato reconocido (`amazon`, `temu`) o `""`, no el nombre de la tienda.
