#!/usr/bin/env python3
"""
Servidor MCP remoto - Distribuidora El Quetzal

Proyecto 1 - CC3067 Redes - Universidad del Valle de Guatemala

Misma funcionalidad que `server.py`, pero expuesta sobre HTTP para poder
desplegarla en un servicio de nube (Google Cloud Run, Cloudflare, etc.).

REUSO DE LA CAPA DE PROTOCOLO
-----------------------------
Este archivo NO reimplementa el protocolo: importa `process_message` de
`server.py` y `tools.py` sin modificarlos. Lo unico que cambia es el
transporte -- de tuberias stdin/stdout a peticiones HTTP POST. Esa es
precisamente la ventaja de haber separado protocolo y negocio desde el
inicio, y es lo que permite que el anfitrion use este servidor igual que
el local.

TRANSPORTE (Streamable HTTP)
----------------------------
    POST /mcp     cuerpo = un mensaje JSON-RPC; respuesta = un mensaje
                  JSON-RPC en application/json. Si el mensaje es una
                  notificacion (sin `id`), responde 202 sin cuerpo.
    GET  /health  verificacion de salud para el balanceador de la nube.
    GET  /        informacion basica del servidor.

Se usa `http.server` de la biblioteca estandar para no introducir
dependencias externas (Flask, FastAPI, etc.), manteniendo el requisito de
implementar el protocolo de forma manual.

Uso local:
    python server_http.py --port 8080
    curl -X POST http://localhost:8080/mcp \
         -H "Content-Type: application/json" \
         -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
"""

import argparse
import json
import logging
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Se reutiliza la capa de protocolo del servidor local, sin cambios.
from server import (
    SERVER_NAME,
    SERVER_VERSION,
    PARSE_ERROR,
    INVALID_REQUEST,
    make_error,
    process_message,
)
import tools

log = logging.getLogger("mcp.http")

# Sesiones activas. MCP identifica cada sesion con una cabecera
# Mcp-Session-Id que el servidor asigna durante el handshake.
SESIONES = set()

MAX_BODY_BYTES = 1_000_000   # limite defensivo para el cuerpo de la peticion


class MCPRequestHandler(BaseHTTPRequestHandler):
    """Traduce peticiones HTTP a mensajes JSON-RPC y viceversa."""

    protocol_version = "HTTP/1.1"
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

    # -- utilidades de respuesta --------------------------------------

    def _responder_json(self, codigo, cuerpo, cabeceras_extra=None):
        datos = json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(datos)))
        for clave, valor in (cabeceras_extra or {}).items():
            self.send_header(clave, valor)
        self.end_headers()
        self.wfile.write(datos)

    def _responder_vacio(self, codigo, cabeceras_extra=None):
        self.send_response(codigo)
        self.send_header("Content-Length", "0")
        for clave, valor in (cabeceras_extra or {}).items():
            self.send_header(clave, valor)
        self.end_headers()

    def log_message(self, formato, *args):
        """Redirige la bitacora de http.server al logger del proyecto."""
        log.info("%s - %s", self.address_string(), formato % args)

    # -- metodos HTTP --------------------------------------------------

    def do_GET(self):
        if self.path in ("/health", "/healthz"):
            self._responder_json(200, {"status": "ok", "server": SERVER_NAME})
        elif self.path == "/":
            self._responder_json(200, {
                "server": SERVER_NAME,
                "version": SERVER_VERSION,
                "transport": "streamable-http",
                "endpoint": "/mcp",
                "tools": [t["name"] for t in tools.TOOLS],
                "note": "Envie mensajes JSON-RPC 2.0 por POST a /mcp",
            })
        else:
            self._responder_json(404, {"error": "Ruta no encontrada"})

    def do_POST(self):
        if self.path.rstrip("/") not in ("/mcp", ""):
            self._responder_json(404, {"error": "Ruta no encontrada. Use POST /mcp"})
            return

        # --- Lectura del cuerpo
        try:
            longitud = int(self.headers.get("Content-Length", 0))
        except ValueError:
            longitud = 0

        if longitud <= 0:
            self._responder_json(400, make_error(None, INVALID_REQUEST, "Cuerpo vacio."))
            return
        if longitud > MAX_BODY_BYTES:
            self._responder_json(413, make_error(None, INVALID_REQUEST, "Cuerpo demasiado grande."))
            return

        crudo = self.rfile.read(longitud).decode("utf-8", errors="replace")
        log.debug(">> %s", crudo)

        # --- Parseo
        try:
            mensaje = json.loads(crudo)
        except json.JSONDecodeError as exc:
            self._responder_json(400, make_error(None, PARSE_ERROR, f"JSON invalido: {exc}"))
            return

        # --- Sesion: se asigna durante initialize y se reenvia despues
        cabeceras_extra = {}
        if isinstance(mensaje, dict) and mensaje.get("method") == "initialize":
            sesion = uuid.uuid4().hex
            SESIONES.add(sesion)
            cabeceras_extra["Mcp-Session-Id"] = sesion
            log.info("Nueva sesion iniciada: %s", sesion)

        # --- Procesamiento (capa de protocolo compartida con server.py)
        respuesta = process_message(mensaje)

        # Una notificacion no genera respuesta: se acusa recibo con 202.
        if respuesta is None:
            log.debug("<< (202 sin cuerpo, era una notificacion)")
            self._responder_vacio(202, cabeceras_extra)
            return

        log.debug("<< %s", json.dumps(respuesta, ensure_ascii=False))
        self._responder_json(200, respuesta, cabeceras_extra)

    def do_DELETE(self):
        """MCP permite cerrar una sesion con DELETE sobre el endpoint."""
        sesion = self.headers.get("Mcp-Session-Id")
        SESIONES.discard(sesion)
        log.info("Sesion cerrada: %s", sesion)
        self._responder_vacio(204)


def configure_logging(verbose):
    nivel = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=nivel,
        format="[%(asctime)s] %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,        # en la nube la bitacora va a stdout
    )
    log.setLevel(nivel)


def main():
    parser = argparse.ArgumentParser(
        description="Servidor MCP remoto (JSON-RPC 2.0 sobre HTTP).")
    # Cloud Run inyecta el puerto en la variable de entorno PORT.
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)),
                        help="Puerto de escucha. Por defecto $PORT o 8080.")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Interfaz de escucha. Debe ser 0.0.0.0 en la nube.")
    parser.add_argument("--verbose", action="store_true",
                        help="Registra el contenido completo de cada mensaje.")
    args = parser.parse_args()

    configure_logging(args.verbose)

    log.info("Servidor MCP '%s' v%s (transporte HTTP)", SERVER_NAME, SERVER_VERSION)
    log.info("Base de datos: %s", tools.DB_PATH)
    log.info("Escuchando en http://%s:%d/mcp", args.host, args.port)

    servidor = ThreadingHTTPServer((args.host, args.port), MCPRequestHandler)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        log.info("Interrumpido por el usuario.")
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
