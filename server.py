#!/usr/bin/env python3
"""
Servidor MCP local - Distribuidora El Quetzal

Proyecto 1 - CC3067 Redes - Universidad del Valle de Guatemala

Implementacion manual del Model Context Protocol sobre JSON-RPC 2.0,
usando el transporte stdio. No se utiliza ningun SDK ni libreria de MCP
(FastMCP, mcp-python, etc.): el formato de los mensajes, el ruteo por
`method` y el emparejamiento peticion/respuesta por `id` se construyen
aqui con la biblioteca estandar de Python.

TRANSPORTE
----------
stdio con mensajes delimitados por salto de linea: cada mensaje JSON-RPC
ocupa exactamente una linea de stdin (peticiones del cliente) o de stdout
(respuestas del servidor). Por eso stdout queda reservado unicamente para
el protocolo; toda la bitacora se escribe a stderr y al archivo de log.

METODOS IMPLEMENTADOS
---------------------
    initialize                 handshake e intercambio de capacidades
    notifications/initialized  aviso del cliente (notificacion, sin respuesta)
    tools/list                 catalogo de herramientas disponibles
    tools/call                 invocacion de una herramienta
    ping                       verificacion de conexion viva
    shutdown                   cierre ordenado (extension de conveniencia)

ERRORES
-------
Se distinguen dos niveles, como exige la especificacion:
  * Errores de PROTOCOLO -> objeto `error` de JSON-RPC (-32601, -32602...).
  * Errores de NEGOCIO   -> respuesta valida con `isError: true` en el
    resultado, para que el LLM pueda leer el mensaje y reaccionar.

Uso:
    python server.py
    python server.py --log-file logs/mcp_server.log
"""

import argparse
import json
import logging
import os
import sys
import traceback

import tools

# ---------------------------------------------------------------------
# Identidad del servidor
# ---------------------------------------------------------------------
SERVER_NAME = "distribuidora-inventario"
SERVER_VERSION = "1.0.0"

# Versiones del protocolo que este servidor sabe hablar, de la mas
# reciente a la mas antigua. Durante el handshake se negocia una.
SUPPORTED_PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

# ---------------------------------------------------------------------
# Codigos de error estandar de JSON-RPC 2.0
# ---------------------------------------------------------------------
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

log = logging.getLogger("mcp.server")


# =====================================================================
# Construccion de mensajes JSON-RPC
# =====================================================================

def make_response(msg_id, result):
    """Respuesta exitosa: jsonrpc + id + result."""
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def make_error(msg_id, code, message, data=None):
    """Respuesta de error de protocolo: jsonrpc + id + error."""
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def make_tool_result(payload, is_error=False):
    """Resultado de `tools/call`.

    MCP devuelve el resultado de una herramienta como una lista de bloques
    de contenido. Aqui se serializa el diccionario de negocio a JSON
    indentado dentro de un bloque de texto, que es la forma mas comoda de
    consumir para un LLM.
    """
    if isinstance(payload, str):
        texto = payload
    else:
        texto = json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": texto}], "isError": is_error}


# =====================================================================
# Manejadores de los metodos del protocolo
# =====================================================================

def handle_initialize(params):
    """Handshake. El cliente propone una version del protocolo y anuncia
    sus capacidades; el servidor responde con la version negociada, las
    capacidades que ofrece y su identidad."""
    solicitada = (params or {}).get("protocolVersion", DEFAULT_PROTOCOL_VERSION)
    negociada = solicitada if solicitada in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION

    cliente = (params or {}).get("clientInfo", {})
    log.info("Handshake con cliente %s v%s | version solicitada=%s negociada=%s",
             cliente.get("name", "desconocido"), cliente.get("version", "?"),
             solicitada, negociada)

    return {
        "protocolVersion": negociada,
        "capabilities": {
            # listChanged en false: el catalogo de herramientas es estatico,
            # el servidor no emitira notificaciones de cambio.
            "tools": {"listChanged": False},
        },
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": (
            "Servidor de inventario y pedidos de una distribuidora mayorista. "
            "Flujo tipico: use 'buscar_productos' para obtener el SKU a partir "
            "del nombre, luego 'consultar_stock' para verificar existencias y "
            "finalmente 'crear_pedido'. Los precios estan en quetzales (GTQ)."
        ),
    }


def handle_tools_list(_params):
    """Devuelve el catalogo de herramientas con su JSON Schema de entrada."""
    log.info("Enviando catalogo de %d herramientas", len(tools.TOOLS))
    return {"tools": tools.TOOLS}


def handle_tools_call(params):
    """Invoca una herramienta y envuelve su salida en un resultado MCP."""
    params = params or {}
    nombre = params.get("name")
    argumentos = params.get("arguments") or {}

    if not nombre:
        raise ValueError("Falta el campo 'name' en los parametros de tools/call.")
    if nombre not in tools.HANDLERS:
        disponibles = ", ".join(tools.HANDLERS)
        raise ValueError(f"La herramienta '{nombre}' no existe. Disponibles: {disponibles}.")
    if not isinstance(argumentos, dict):
        raise ValueError("El campo 'arguments' debe ser un objeto.")

    log.info("Ejecutando herramienta '%s' con argumentos %s", nombre, argumentos)
    funcion = tools.HANDLERS[nombre]

    try:
        resultado = funcion(**argumentos)
        return make_tool_result(resultado, is_error=False)
    except tools.ToolError as exc:
        # Error de negocio: la llamada fue valida, la operacion no procede.
        log.warning("Herramienta '%s' rechazo la operacion: %s", nombre, exc)
        return make_tool_result(f"Error: {exc}", is_error=True)
    except TypeError as exc:
        # Argumentos que no encajan con la firma de la funcion.
        log.warning("Argumentos invalidos para '%s': %s", nombre, exc)
        return make_tool_result(
            f"Error: argumentos invalidos para '{nombre}'. Detalle: {exc}", is_error=True)


def handle_ping(_params):
    return {}


# Ruteo por nombre de metodo
METHODS = {
    "initialize": handle_initialize,
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
    "ping": handle_ping,
}

# Notificaciones reconocidas (no llevan `id` y no generan respuesta)
NOTIFICATIONS = {"notifications/initialized", "initialized", "notifications/cancelled"}


# =====================================================================
# Procesamiento de un mensaje entrante
# =====================================================================

def process_message(mensaje):
    """Recibe un mensaje JSON-RPC ya deserializado y devuelve la respuesta
    a enviar, o None si el mensaje era una notificacion."""

    if not isinstance(mensaje, dict):
        return make_error(None, INVALID_REQUEST, "El mensaje debe ser un objeto JSON.")

    if mensaje.get("jsonrpc") != "2.0":
        return make_error(mensaje.get("id"), INVALID_REQUEST,
                          "El campo 'jsonrpc' debe tener el valor '2.0'.")

    metodo = mensaje.get("method")
    msg_id = mensaje.get("id")
    params = mensaje.get("params")

    if not metodo:
        return make_error(msg_id, INVALID_REQUEST, "Falta el campo 'method'.")

    # Notificacion: sin 'id', no se responde nada.
    if msg_id is None:
        if metodo in NOTIFICATIONS:
            log.info("Notificacion recibida: %s", metodo)
        else:
            log.info("Notificacion desconocida ignorada: %s", metodo)
        return None

    if metodo == "shutdown":
        log.info("Cierre solicitado por el cliente.")
        return make_response(msg_id, {})

    if metodo not in METHODS:
        log.warning("Metodo no soportado: %s", metodo)
        return make_error(msg_id, METHOD_NOT_FOUND, f"Metodo no soportado: {metodo}")

    try:
        return make_response(msg_id, METHODS[metodo](params))
    except ValueError as exc:
        return make_error(msg_id, INVALID_PARAMS, str(exc))
    except Exception as exc:
        log.error("Error interno atendiendo '%s': %s", metodo, traceback.format_exc())
        return make_error(msg_id, INTERNAL_ERROR, f"Error interno del servidor: {exc}")


# =====================================================================
# Bucle principal (transporte stdio)
# =====================================================================

def serve_stdio():
    log.info("Servidor MCP '%s' v%s iniciado (transporte stdio)", SERVER_NAME, SERVER_VERSION)
    log.info("Base de datos: %s", tools.DB_PATH)

    for linea in sys.stdin:
        linea = linea.strip()
        if not linea:
            continue

        log.debug(">> %s", linea)

        detener = False
        try:
            mensaje = json.loads(linea)
        except json.JSONDecodeError as exc:
            respuesta = make_error(None, PARSE_ERROR, f"JSON invalido: {exc}")
        else:
            respuesta = process_message(mensaje)
            detener = isinstance(mensaje, dict) and mensaje.get("method") == "shutdown"

        if respuesta is not None:
            salida = json.dumps(respuesta, ensure_ascii=False)
            log.debug("<< %s", salida)
            sys.stdout.write(salida + "\n")
            sys.stdout.flush()

        if detener:
            break

    log.info("Servidor detenido.")


def configure_logging(log_file, verbose):
    """La bitacora va a stderr y opcionalmente a un archivo. Nunca a stdout,
    que esta reservado para los mensajes del protocolo."""
    nivel = logging.DEBUG if verbose else logging.INFO
    formato = logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s", "%H:%M:%S")

    log.setLevel(nivel)

    consola = logging.StreamHandler(sys.stderr)
    consola.setFormatter(formato)
    log.addHandler(consola)

    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        archivo = logging.FileHandler(log_file, encoding="utf-8")
        archivo.setFormatter(formato)
        log.addHandler(archivo)


def main():
    parser = argparse.ArgumentParser(
        description="Servidor MCP local de inventario y pedidos (JSON-RPC 2.0 sobre stdio).")
    parser.add_argument("--log-file", default=os.path.join("logs", "mcp_server.log"),
                        help="Archivo de bitacora. Use '' para desactivarlo.")
    parser.add_argument("--verbose", action="store_true",
                        help="Registra el contenido completo de cada mensaje JSON-RPC.")
    args = parser.parse_args()

    configure_logging(args.log_file or None, args.verbose)

    try:
        serve_stdio()
    except KeyboardInterrupt:
        log.info("Interrumpido por el usuario.")


if __name__ == "__main__":
    main()
