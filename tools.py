#!/usr/bin/env python3
"""
Herramientas expuestas por el servidor MCP de la distribuidora.

Proyecto 1 - CC3067 Redes - Universidad del Valle de Guatemala

Este modulo contiene UNICAMENTE la logica de negocio y las declaraciones
de las herramientas (nombre, descripcion y JSON Schema de entrada). El
manejo del protocolo JSON-RPC vive en `server.py`, de modo que la misma
implementacion sirve tanto para el servidor local (stdio) como para el
servidor remoto (HTTP).

Cada funcion `tool_*` devuelve un diccionario serializable a JSON. Si la
operacion falla por una razon de negocio (SKU inexistente, stock
insuficiente, etc.) se lanza `ToolError`, que `server.py` traduce a una
respuesta de herramienta con `isError: true` -- no a un error JSON-RPC,
pues el protocolo distingue entre "la llamada fallo" y "la herramienta
ejecuto y reporto un problema".
"""

import os
import re
import sqlite3
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("MCP_DB_PATH", os.path.join(BASE_DIR, "db", "distribuidora.db"))

# URL del servicio web de tipo de cambio del Banco de Guatemala.
BANGUAT_URL = "https://www.banguat.gob.gt/variables/ws/TipoCambio.asmx"
BANGUAT_TIMEOUT = 8       # segundos
TIPO_CAMBIO_FALLBACK = 7.70   # usado solo si el servicio no responde


class ToolError(Exception):
    """Error de negocio dentro de una herramienta (no un error de protocolo)."""


# =====================================================================
# Acceso a datos
# =====================================================================

def get_connection():
    if not os.path.exists(DB_PATH):
        raise ToolError(
            f"No se encontro la base de datos en '{DB_PATH}'. "
            "Ejecute 'python db/seed.py' antes de iniciar el servidor."
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _rows_to_dicts(rows):
    return [dict(row) for row in rows]


# =====================================================================
# Herramienta 1: buscar_productos
# =====================================================================

def tool_buscar_productos(consulta=None, categoria=None, limite=10):
    """Busca en el catalogo por coincidencia parcial de nombre y/o categoria."""
    if limite is None:
        limite = 10
    if not isinstance(limite, int) or not (1 <= limite <= 50):
        raise ToolError("El parametro 'limite' debe ser un entero entre 1 y 50.")

    sql = ("SELECT sku, nombre, categoria, precio, unidad_medida "
           "FROM productos WHERE activo = 1")
    params = []

    if consulta:
        sql += " AND LOWER(nombre) LIKE ?"
        params.append(f"%{consulta.lower()}%")
    if categoria:
        sql += " AND LOWER(categoria) = ?"
        params.append(categoria.lower())

    sql += " ORDER BY nombre LIMIT ?"
    params.append(limite)

    with get_connection() as conn:
        filas = _rows_to_dicts(conn.execute(sql, params).fetchall())

    return {
        "encontrados": len(filas),
        "criterio": {"consulta": consulta, "categoria": categoria},
        "productos": filas,
        "moneda": "GTQ",
    }


# =====================================================================
# Herramienta 2: consultar_stock
# =====================================================================

def tool_consultar_stock(sku, bodega_id=None):
    """Devuelve las existencias de un SKU, por bodega o consolidadas."""
    if not sku:
        raise ToolError("El parametro 'sku' es obligatorio.")

    with get_connection() as conn:
        producto = conn.execute(
            "SELECT sku, nombre, categoria, precio, unidad_medida "
            "FROM productos WHERE sku = ?", (sku.upper(),)
        ).fetchone()

        if producto is None:
            raise ToolError(
                f"El SKU '{sku}' no existe en el catalogo. "
                "Use 'buscar_productos' para obtener codigos validos."
            )

        sql = ("SELECT b.id AS bodega_id, b.nombre AS bodega, b.ubicacion, e.cantidad "
               "FROM existencias e JOIN bodegas b ON b.id = e.bodega_id "
               "WHERE e.sku = ?")
        params = [sku.upper()]
        if bodega_id is not None:
            sql += " AND b.id = ?"
            params.append(bodega_id)
        sql += " ORDER BY b.id"

        existencias = _rows_to_dicts(conn.execute(sql, params).fetchall())

    if bodega_id is not None and not existencias:
        raise ToolError(f"No existe la bodega con id {bodega_id}. Bodegas validas: 1, 2, 3.")

    total = sum(e["cantidad"] for e in existencias)
    return {
        "producto": dict(producto),
        "existencias": existencias,
        "total_disponible": total,
        "hay_stock": total > 0,
    }


# =====================================================================
# Herramienta 3: crear_pedido
# =====================================================================

def _siguiente_numero_pedido(conn):
    fila = conn.execute("SELECT COUNT(*) AS n FROM pedidos").fetchone()
    return f"PED-{datetime.now().year}-{fila['n'] + 1:04d}"


def tool_crear_pedido(cliente_id, lineas, bodega_id=1):
    """Valida existencias, descuenta inventario y registra un pedido nuevo.

    Es la unica herramienta que escribe en la base de datos. La operacion
    es atomica: si cualquier linea falla la validacion, no se escribe nada.
    """
    if not isinstance(cliente_id, int):
        raise ToolError("El parametro 'cliente_id' debe ser un entero.")
    if not lineas or not isinstance(lineas, list):
        raise ToolError("El parametro 'lineas' debe ser una lista con al menos un elemento.")
    if bodega_id is None:
        bodega_id = 1

    conn = get_connection()
    try:
        cliente = conn.execute(
            "SELECT id, nombre, nit, municipio FROM clientes WHERE id = ?", (cliente_id,)
        ).fetchone()
        if cliente is None:
            raise ToolError(f"No existe el cliente con id {cliente_id}.")

        bodega = conn.execute(
            "SELECT id, nombre FROM bodegas WHERE id = ?", (bodega_id,)
        ).fetchone()
        if bodega is None:
            raise ToolError(f"No existe la bodega con id {bodega_id}. Bodegas validas: 1, 2, 3.")

        # --- Fase de validacion: nada se escribe hasta aprobar todas las lineas
        detalle, total = [], 0.0
        for i, linea in enumerate(lineas, start=1):
            if not isinstance(linea, dict) or "sku" not in linea or "cantidad" not in linea:
                raise ToolError(f"La linea {i} debe tener los campos 'sku' y 'cantidad'.")

            sku = str(linea["sku"]).upper()
            cantidad = linea["cantidad"]

            if not isinstance(cantidad, int) or cantidad <= 0:
                raise ToolError(f"La cantidad de la linea {i} ({sku}) debe ser un entero positivo.")

            producto = conn.execute(
                "SELECT sku, nombre, precio FROM productos WHERE sku = ? AND activo = 1",
                (sku,)
            ).fetchone()
            if producto is None:
                raise ToolError(f"El SKU '{sku}' (linea {i}) no existe o esta inactivo.")

            existencia = conn.execute(
                "SELECT cantidad FROM existencias WHERE sku = ? AND bodega_id = ?",
                (sku, bodega_id)
            ).fetchone()
            disponible = existencia["cantidad"] if existencia else 0

            if disponible < cantidad:
                raise ToolError(
                    f"Stock insuficiente de '{producto['nombre']}' ({sku}) en "
                    f"{bodega['nombre']}: se solicitaron {cantidad} y hay {disponible}. "
                    "Consulte 'consultar_stock' para revisar otras bodegas."
                )

            subtotal = round(producto["precio"] * cantidad, 2)
            total += subtotal
            detalle.append({
                "sku": sku,
                "nombre": producto["nombre"],
                "cantidad": cantidad,
                "precio_unitario": producto["precio"],
                "subtotal": subtotal,
            })

        # --- Fase de escritura
        numero = _siguiente_numero_pedido(conn)
        fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total = round(total, 2)

        conn.execute(
            "INSERT INTO pedidos (numero, cliente_id, bodega_id, fecha, estado, total) "
            "VALUES (?, ?, ?, ?, 'registrado', ?)",
            (numero, cliente_id, bodega_id, fecha, total)
        )
        for d in detalle:
            conn.execute(
                "INSERT INTO pedido_lineas (pedido_numero, sku, cantidad, precio_unitario, subtotal) "
                "VALUES (?, ?, ?, ?, ?)",
                (numero, d["sku"], d["cantidad"], d["precio_unitario"], d["subtotal"])
            )
            conn.execute(
                "UPDATE existencias SET cantidad = cantidad - ? WHERE sku = ? AND bodega_id = ?",
                (d["cantidad"], d["sku"], bodega_id)
            )
        conn.commit()

        return {
            "numero": numero,
            "estado": "registrado",
            "fecha": fecha,
            "cliente": dict(cliente),
            "bodega": dict(bodega),
            "lineas": detalle,
            "total": total,
            "moneda": "GTQ",
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =====================================================================
# Herramienta 4: consultar_pedido
# =====================================================================

def tool_consultar_pedido(numero):
    """Devuelve el encabezado y el detalle de un pedido."""
    if not numero:
        raise ToolError("El parametro 'numero' es obligatorio.")

    with get_connection() as conn:
        pedido = conn.execute(
            "SELECT p.numero, p.fecha, p.estado, p.total, "
            "       c.id AS cliente_id, c.nombre AS cliente, c.municipio, "
            "       b.nombre AS bodega "
            "FROM pedidos p "
            "JOIN clientes c ON c.id = p.cliente_id "
            "JOIN bodegas  b ON b.id = p.bodega_id "
            "WHERE UPPER(p.numero) = ?", (numero.upper(),)
        ).fetchone()

        if pedido is None:
            raise ToolError(f"No existe el pedido '{numero}'.")

        lineas = _rows_to_dicts(conn.execute(
            "SELECT l.sku, pr.nombre, l.cantidad, l.precio_unitario, l.subtotal "
            "FROM pedido_lineas l JOIN productos pr ON pr.sku = l.sku "
            "WHERE l.pedido_numero = ? ORDER BY l.id", (pedido["numero"],)
        ).fetchall())

    resultado = dict(pedido)
    resultado["lineas"] = lineas
    resultado["moneda"] = "GTQ"
    return resultado


# =====================================================================
# Herramienta 5: historial_cliente
# =====================================================================

def tool_historial_cliente(cliente_id, limite=5):
    """Lista los pedidos mas recientes de un cliente con un resumen agregado."""
    if not isinstance(cliente_id, int):
        raise ToolError("El parametro 'cliente_id' debe ser un entero.")
    if limite is None:
        limite = 5
    if not isinstance(limite, int) or not (1 <= limite <= 50):
        raise ToolError("El parametro 'limite' debe ser un entero entre 1 y 50.")

    with get_connection() as conn:
        cliente = conn.execute(
            "SELECT id, nombre, nit, municipio FROM clientes WHERE id = ?", (cliente_id,)
        ).fetchone()
        if cliente is None:
            raise ToolError(f"No existe el cliente con id {cliente_id}.")

        pedidos = _rows_to_dicts(conn.execute(
            "SELECT numero, fecha, estado, total FROM pedidos "
            "WHERE cliente_id = ? ORDER BY fecha DESC LIMIT ?", (cliente_id, limite)
        ).fetchall())

        resumen = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS acumulado "
            "FROM pedidos WHERE cliente_id = ?", (cliente_id,)
        ).fetchone()

    return {
        "cliente": dict(cliente),
        "pedidos_mostrados": len(pedidos),
        "pedidos_totales": resumen["n"],
        "monto_acumulado": round(resumen["acumulado"], 2),
        "pedidos": pedidos,
        "moneda": "GTQ",
    }


# =====================================================================
# Herramienta 6: convertir_total_a_dolares (fuente de datos externa)
# =====================================================================

def _consultar_tipo_cambio():
    """Consulta el tipo de cambio de referencia del dia al web service SOAP
    del Banco de Guatemala. Devuelve (tasa, fuente)."""
    sobre = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Body><TipoCambioDia xmlns="http://www.banguat.gob.gt/variables/ws/" />'
        '</soap:Body></soap:Envelope>'
    ).encode("utf-8")

    peticion = urllib.request.Request(
        BANGUAT_URL,
        data=sobre,
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": "http://www.banguat.gob.gt/variables/ws/TipoCambioDia",
        },
        method="POST",
    )

    with urllib.request.urlopen(peticion, timeout=BANGUAT_TIMEOUT) as respuesta:
        cuerpo = respuesta.read().decode("utf-8", errors="replace")

    coincidencia = re.search(r"<referencia>([\d.]+)</referencia>", cuerpo, re.IGNORECASE)
    if not coincidencia:
        raise ValueError("La respuesta del servicio no contiene el campo de referencia.")

    return float(coincidencia.group(1)), "Banco de Guatemala (TipoCambioDia)"


def tool_convertir_total_a_dolares(numero):
    """Convierte el total de un pedido a dolares usando la tasa del dia.

    Es la unica herramienta que consume una fuente externa al sistema. Si
    el servicio no responde se usa una tasa de respaldo y se indica en el
    resultado, de modo que el chatbot pueda advertirlo al usuario.
    """
    pedido = tool_consultar_pedido(numero)

    try:
        tasa, fuente = _consultar_tipo_cambio()
        en_linea = True
    except Exception as exc:                       # red caida, timeout, formato inesperado
        tasa, fuente = TIPO_CAMBIO_FALLBACK, f"valor de respaldo local ({exc.__class__.__name__})"
        en_linea = False

    return {
        "numero": pedido["numero"],
        "cliente": pedido["cliente"],
        "total_gtq": pedido["total"],
        "total_usd": round(pedido["total"] / tasa, 2),
        "tipo_cambio": tasa,
        "fuente_tipo_cambio": fuente,
        "consulta_en_linea": en_linea,
        "consultado": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# =====================================================================
# Catalogo de herramientas expuesto por tools/list
# =====================================================================

CATEGORIAS_VALIDAS = [
    "Abarrotes", "Bebidas", "Lacteos", "Limpieza",
    "Higiene personal", "Snacks", "Enlatados", "Desechables",
]

TOOLS = [
    {
        "name": "buscar_productos",
        "description": (
            "Busca productos en el catalogo de la distribuidora por coincidencia "
            "parcial de nombre y/o por categoria. Devuelve SKU, nombre, categoria, "
            "precio unitario en quetzales y unidad de medida. Utilice esta "
            "herramienta cuando el usuario mencione un producto por nombre y se "
            "necesite obtener su SKU."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "consulta": {
                    "type": "string",
                    "description": "Texto a buscar dentro del nombre del producto, por ejemplo 'arroz'.",
                },
                "categoria": {
                    "type": "string",
                    "description": "Categoria exacta a filtrar.",
                    "enum": CATEGORIAS_VALIDAS,
                },
                "limite": {
                    "type": "integer",
                    "description": "Numero maximo de resultados (1-50). Por defecto 10.",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "consultar_stock",
        "description": (
            "Consulta las existencias disponibles de un producto identificado por "
            "su SKU. Si se indica una bodega devuelve solo esa; de lo contrario "
            "devuelve el desglose de las tres bodegas y el total consolidado."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sku": {
                    "type": "string",
                    "description": "Codigo del producto, por ejemplo 'AB-0001'.",
                },
                "bodega_id": {
                    "type": "integer",
                    "description": "1 = Bodega Central, 2 = Bodega Occidente, 3 = Bodega Oriente.",
                    "enum": [1, 2, 3],
                },
            },
            "required": ["sku"],
            "additionalProperties": False,
        },
    },
    {
        "name": "crear_pedido",
        "description": (
            "Registra un pedido nuevo para un cliente. Valida que el cliente y "
            "los SKU existan y que haya existencias suficientes en la bodega "
            "indicada; si todo es valido descuenta el inventario y devuelve el "
            "numero de pedido con su total. La operacion es atomica: si una sola "
            "linea falla no se registra nada."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cliente_id": {
                    "type": "integer",
                    "description": "Identificador del cliente (1-20 en los datos de prueba).",
                },
                "lineas": {
                    "type": "array",
                    "description": "Lineas del pedido.",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "sku": {"type": "string", "description": "Codigo del producto."},
                            "cantidad": {"type": "integer", "minimum": 1,
                                         "description": "Unidades solicitadas."},
                        },
                        "required": ["sku", "cantidad"],
                        "additionalProperties": False,
                    },
                },
                "bodega_id": {
                    "type": "integer",
                    "description": "Bodega desde la que se despacha. Por defecto 1 (Central).",
                    "enum": [1, 2, 3],
                    "default": 1,
                },
            },
            "required": ["cliente_id", "lineas"],
            "additionalProperties": False,
        },
    },
    {
        "name": "consultar_pedido",
        "description": (
            "Devuelve el estado, la fecha, el cliente, el detalle de lineas y el "
            "total de un pedido a partir de su numero, por ejemplo 'PED-2026-0007'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "numero": {
                    "type": "string",
                    "description": "Numero del pedido, con formato PED-AAAA-NNNN.",
                },
            },
            "required": ["numero"],
            "additionalProperties": False,
        },
    },
    {
        "name": "historial_cliente",
        "description": (
            "Lista los pedidos mas recientes de un cliente e incluye el numero "
            "total de pedidos y el monto acumulado historico."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cliente_id": {
                    "type": "integer",
                    "description": "Identificador del cliente.",
                },
                "limite": {
                    "type": "integer",
                    "description": "Cantidad de pedidos a mostrar (1-50). Por defecto 5.",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 5,
                },
            },
            "required": ["cliente_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "convertir_total_a_dolares",
        "description": (
            "Convierte el total de un pedido de quetzales a dolares usando el tipo "
            "de cambio de referencia del dia publicado por el Banco de Guatemala. "
            "Si el servicio externo no responde utiliza una tasa de respaldo e "
            "informa que el dato no proviene de la consulta en linea."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "numero": {
                    "type": "string",
                    "description": "Numero del pedido a convertir.",
                },
            },
            "required": ["numero"],
            "additionalProperties": False,
        },
    },
]

# Mapa nombre -> funcion, usado por server.py al despachar tools/call
HANDLERS = {
    "buscar_productos": tool_buscar_productos,
    "consultar_stock": tool_consultar_stock,
    "crear_pedido": tool_crear_pedido,
    "consultar_pedido": tool_consultar_pedido,
    "historial_cliente": tool_historial_cliente,
    "convertir_total_a_dolares": tool_convertir_total_a_dolares,
}
