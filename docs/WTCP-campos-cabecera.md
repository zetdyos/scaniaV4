# WTCP: envío, impuestos, subtotal y descuentos llegan en la respuesta y no se muestran

**De:** SCANIA · **Para:** WTCP · **Fecha:** 2026-10-18 · **Referencia:** `docs/CONTRATO-WTCP.md` §5.1

## Qué pasa

En la prueba del contenedor del 18/10, WTCP muestra vacíos los campos de envío, descuentos, cupones y otros datos de
cabecera. SCANIA sí los devuelve en `result.json`. Medido sobre las 92 facturas del contenedor:

| Campo (ruta en cada factura) | Facturas con valor |
|---|---|
| `ocr.envio` | 79 de 92 (86 %) |
| `ocr.impuestos` | 68 de 92 (74 %) |
| `ocr.subtotal_articulos` | 67 de 92 (73 %) |
| `ocr.proveedor` | 64 de 92 (70 %) |
| `ocr.fecha_pedido` | 64 de 92 (70 %) |
| `ocr.descuentos` (lista no vacía) | 8 de 92 (solo 11 facturas imprimen descuentos o cupones) |

Si en WTCP salen vacíos en la mayoría de los invoices, el problema está en cómo WTCP lee o guarda esos campos.

## Comprobarlo con un caso

Con el API-Token de prueba (`X-API-Key`):

```bash
curl -s -H "X-API-Key: $TOKEN" http://ai.techstudi.com:8091/api/jobs/20261018-123831-510b55/result.json
```

Esa factura (Amazon, 29/09/2026) trae, entre otros campos:

```json
{
  "id": 0,
  "total": "2349.94",
  "ocr": {
    "proveedor": "amazon",
    "fecha_pedido": "September 29, 2026",
    "subtotal_articulos": "2399.94",
    "envio": "0.00",
    "impuestos": "0.00",
    "descuentos": [{"concepto": "Your Coupon Savings", "importe": "50.00"}]
  }
}
```

Otros casos para contrastar con la pantalla de WTCP:

| Trabajo | Qué debería mostrar WTCP |
|---|---|
| `20261018-093840-39f3ec` (Temu) | Subtotal 213.18; descuentos: tres conceptos (170.94 + 4.16 + 19.98 = 195.08) |
| `20261018-124049-9ccc77` (Amazon) | Subtotal 19.99, envío 14.99, impuestos 0.00 |
| `20261018-123126-6873d4` (Amazon) | Subtotal 89.99, envío 6.99, impuestos 0.00 |
| `20261018-124814-b18b07` | Impuestos 4.90; descuento "Shipping Discount" 10.00 |

## Dónde mirar en WTCP

1. **Ruta.** Estos campos están **dentro del objeto `ocr` de cada factura** (`factura.ocr.envio`), no en la raíz de la
   factura (`factura.envio` no existe) ni en las líneas.
2. **Tipo.** Los importes son **texto** (`"14.99"`), no números JSON. Un lector que espere número los descarta.
   `"0.00"` es un valor real (envío gratis o impuesto cero) y debe mostrarse como 0.00, no como vacío.
3. **Vacío.** Un campo que el papel no imprime llega como `""`, no como `null` ni ausente.
4. **Descuentos y cupones.** `ocr.descuentos` es una **lista** de `{"concepto", "importe"}`. Los cupones también
   llegan aquí (el `concepto` dice "Coupon", "Promotion"…). El contrato pide que WTCP **sume** los `importe` (todos
   en positivo). No hay un campo aparte para cupones.
5. **Lectura en un solo paso.** WTCP lee `result.json` una vez, cuando `state = done` (§1). Si guarda el resultado
   antes de esa lectura o desde `GET /api/jobs/{id}` (que no trae las facturas), los campos quedan vacíos.

## Campo nuevo: `ocr.other_charges`

Cada factura trae, además de `envio`, `impuestos`, `subtotal_articulos` y `descuentos`, una categoría abierta para
cargos que no encajan en ninguna de ellas (por ejemplo, un recargo o una tasa que aún no hemos visto):

```json
"ocr": {
  "descuentos":    [{"concepto": "Your Coupon Savings", "importe": "50.00"}],
  "other_charges": [{"concepto": "...", "importe": "12.50"}]
}
```

- Misma forma que `descuentos`: lista de `{"concepto": texto, "importe": importe}`. El importe va en positivo y como
  texto con punto decimal.
- **Hoy siempre llega vacío (`[]`).** SCANIA lo rellenará cuando defina su extracción; WTCP no tiene que cambiar nada
  cuando eso ocurra.
- Qué debe hacer WTCP: leerlo de `factura.ocr.other_charges`, mostrar cada concepto con su importe y no contarlo como
  mercadería (las líneas `goods` siguen siendo lo único que se declara). Una lista vacía no es un error.

## `line_kind`: no todas las líneas son mercadería

Desde el 2026-10-18 las líneas que SCANIA lee con su modelo de lenguaje llegan clasificadas. Además de `goods`,
`line_kind` puede valer `shipping`, `tax`, `discount`, `subtotal_or_total` u `other_charges`; por ejemplo, el
"Subtotal", el "Sales Tax" o el "Total" impresos en la lista de una factura.

- **Solo las líneas `goods` se declaran en el SED y cuentan para el precio**, como ya dice el contrato (§5.2).
- Las demás se pueden mostrar en la revisión como información; no deben sumarse como mercadería.
- Si hoy WTCP trata todas las líneas como mercadería, tiene que filtrar por `line_kind == "goods"`.

## Cuándo llegan vacíos desde SCANIA

- Cuando el papel no imprime el dato (por ejemplo, la mayoría de las facturas no tienen descuentos).
- En una parte de las facturas (entre el 10 y el 15 % en este contenedor), el papel sí trae el dato, pero SCANIA aún
  no lo extrae. Está identificado del lado de SCANIA y no depende de WTCP.
