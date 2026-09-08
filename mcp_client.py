#!/usr/bin/env python3
"""
Cliente MCP - implementacion manual de JSON-RPC 2.0

Proyecto 1 - CC3067 Redes - Universidad del Valle de Guatemala

Este modulo implementa el actor "Cliente" del protocolo MCP: mantiene la
conexion con un servidor y conoce como utilizarlo. El anfitrion
(`chatbot.py`) coordina varios de estos clientes, uno por servidor.

Todo el protocolo esta construido a mano con la biblioteca estandar: la
serializacion de mensajes, el emparejamiento peticion/respuesta por `id`
y el manejo de errores. No se usa ningun SDK de MCP.

TRANSPORTES SOPORTADOS
----------------------
- stdio : lanza el servidor como subproceso y conversa por sus tuberias.
          Es el transporte de los servidores locales.
- http  : envia peticiones POST a un servidor remoto (Streamable HTTP).
          Es el transporte del servidor desplegado en la nube.

Ambos hablan exactamente los mismos mensajes JSON-RPC; lo unico que
cambia es como viajan. Esa simetria es la razon por la que el anfitrion
usa el servidor remoto igual que el local, sin ningun caso especial.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "chatbot-distribuidora"
CLIENT_VERSION = "1.0.0"


class MCPError(Exception):
    """Error devuelto por el servidor como objeto `error` de JSON-RPC."""

    def __init__(self, code, message, data=None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


# =====================================================================
# Transportes
# =====================================================================

class StdioTransport:
    """Lanza el servidor como subproceso y conversa por stdin/stdout.

    Los mensajes van delimitados por salto de linea: se escribe una linea
    con el JSON de la peticion y se lee una linea con el JSON de la
    respuesta. El stderr del servidor se desvia a un archivo para que su
    bitacora no interfiera con el protocolo.
    """

    kind = "stdio"

    def __init__(self, command, args=None, env=None, stderr_path=None):
        self.command = command
        self.args = args or []
        self.env = env
        self.stderr_path = stderr_path
        self.process = None
        self._stderr_file = None
        self._lock = threading.Lock()

    def descripcion(self):
        return f"stdio: {self.command} {' '.join(self.args)}"

    def connect(self):
        ejecutable = shutil.which(self.command) or self.command

        entorno = os.environ.copy()
        if self.env:
            entorno.update(self.env)

        if self.stderr_path:
            os.makedirs(os.path.dirname(os.path.abspath(self.stderr_path)), exist_ok=True)
            self._stderr_file = open(self.stderr_path, "a", encoding="utf-8")
            salida_error = self._stderr_file
        else:
            salida_error = subprocess.DEVNULL

        self.process = subprocess.Popen(
            [ejecutable] + self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=salida_error,
            text=True,
            bufsize=1,
            encoding="utf-8",
            env=entorno,
        )

    def send(self, mensaje, espera_respuesta=True):
        if self.process is None or self.process.poll() is not None:
            raise MCPError(-32000, "El proceso del servidor no esta activo.")

        linea = json.dumps(mensaje, ensure_ascii=False)

        with self._lock:
            self.process.stdin.write(linea + "\n")
            self.process.stdin.flush()

            if not espera_respuesta:
                return None

            respuesta = self.process.stdout.readline()

        if not respuesta:
            raise MCPError(-32000, "El servidor cerro la conexion sin responder.")
        return json.loads(respuesta)

    def close(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
        if self._stderr_file:
            self._stderr_file.close()


class HttpTransport:
    """Envia los mensajes JSON-RPC por HTTP POST al servidor remoto.

    Sigue el transporte Streamable HTTP de MCP: cada peticion es un POST
    con el mensaje JSON-RPC en el cuerpo. El servidor puede responder con
    `application/json` o con `text/event-stream`; ambos casos se manejan
    aqui. El identificador de sesion que devuelve el servidor durante el
    handshake se reenvia en las peticiones siguientes.
    """

    kind = "http"

    def __init__(self, url, timeout=30, headers=None):
        self.url = url
        self.timeout = timeout
        self.headers_extra = headers or {}
        self.session_id = None

    def descripcion(self):
        return f"http: {self.url}"

    def connect(self):
        # HTTP no mantiene un proceso vivo; la conexion se abre por peticion.
        pass

    def send(self, mensaje, espera_respuesta=True):
        cuerpo = json.dumps(mensaje, ensure_ascii=False).encode("utf-8")

        cabeceras = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        cabeceras.update(self.headers_extra)
        if self.session_id:
            cabeceras["Mcp-Session-Id"] = self.session_id

        peticion = urllib.request.Request(self.url, data=cuerpo,
                                          headers=cabeceras, method="POST")

        try:
            with urllib.request.urlopen(peticion, timeout=self.timeout) as respuesta:
                # El servidor asigna la sesion durante `initialize`.
                sesion = respuesta.headers.get("Mcp-Session-Id")
                if sesion:
                    self.session_id = sesion

                if not espera_respuesta:
                    respuesta.read()
                    return None

                tipo = (respuesta.headers.get("Content-Type") or "").lower()
                crudo = respuesta.read().decode("utf-8")

                if "text/event-stream" in tipo:
                    return self._parse_sse(crudo)
                if not crudo.strip():
                    return None
                return json.loads(crudo)

        except urllib.error.HTTPError as exc:
            detalle = exc.read().decode("utf-8", errors="replace")[:300]
            raise MCPError(-32000, f"HTTP {exc.code} desde el servidor remoto: {detalle}")
        except urllib.error.URLError as exc:
            raise MCPError(-32000, f"No se pudo contactar al servidor remoto: {exc.reason}")

    @staticmethod
    def _parse_sse(texto):
        """Extrae el ultimo mensaje JSON de un flujo Server-Sent Events."""
        ultimo = None
        for linea in texto.splitlines():
            if linea.startswith("data:"):
                contenido = linea[5:].strip()
                if contenido:
                    ultimo = json.loads(contenido)
        return ultimo

    def close(self):
        pass


# =====================================================================
# Cliente MCP
# =====================================================================

class MCPClient:
    """Cliente de un servidor MCP.

    Encapsula el ciclo de vida completo: handshake, descubrimiento de
    herramientas e invocacion. Cada mensaje enviado y recibido se entrega
    al `logger` que provee el anfitrion, que es como se cumple el
    requisito de mantener y mostrar la bitacora de interacciones.
    """

    def __init__(self, nombre, transport, logger=None):
        self.nombre = nombre
        self.transport = transport
        self.logger = logger
        self.tools = []
        self.server_info = {}
        self.protocol_version = None
        self._next_id = 1
        self.conectado = False

    # -- utilidades internas ------------------------------------------

    def _nuevo_id(self):
        actual = self._next_id
        self._next_id += 1
        return actual

    def _log(self, direccion, mensaje):
        if self.logger:
            self.logger(self.nombre, direccion, mensaje)

    def _request(self, metodo, params=None):
        """Envia una peticion (con `id`) y devuelve el campo `result`."""
        mensaje = {"jsonrpc": "2.0", "id": self._nuevo_id(), "method": metodo}
        if params is not None:
            mensaje["params"] = params

        self._log("enviado", mensaje)
        respuesta = self.transport.send(mensaje, espera_respuesta=True)
        self._log("recibido", respuesta)

        if respuesta is None:
            raise MCPError(-32000, f"Sin respuesta para '{metodo}'.")

        if "error" in respuesta:
            err = respuesta["error"]
            raise MCPError(err.get("code"), err.get("message"), err.get("data"))

        # El `id` de la respuesta debe coincidir con el de la peticion.
        if respuesta.get("id") != mensaje["id"]:
            raise MCPError(-32000,
                           f"Respuesta descoordinada: se esperaba id={mensaje['id']} "
                           f"y llego id={respuesta.get('id')}.")

        return respuesta.get("result", {})

    def _notify(self, metodo, params=None):
        """Envia una notificacion (sin `id`); el servidor no responde."""
        mensaje = {"jsonrpc": "2.0", "method": metodo}
        if params is not None:
            mensaje["params"] = params

        self._log("enviado", mensaje)
        self.transport.send(mensaje, espera_respuesta=False)

    # -- ciclo de vida -------------------------------------------------

    def connect(self):
        """Handshake completo y descubrimiento de herramientas."""
        self.transport.connect()

        resultado = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        })

        self.server_info = resultado.get("serverInfo", {})
        self.protocol_version = resultado.get("protocolVersion")

        # El cliente confirma que esta listo. Es una notificacion.
        self._notify("notifications/initialized")

        self.tools = self._request("tools/list").get("tools", [])
        self.conectado = True
        return self

    def call_tool(self, nombre, argumentos):
        """Invoca una herramienta y devuelve (texto, hubo_error)."""
        resultado = self._request("tools/call", {
            "name": nombre,
            "arguments": argumentos or {},
        })

        bloques = resultado.get("content", [])
        texto = "\n".join(b.get("text", "") for b in bloques if b.get("type") == "text")
        return texto, bool(resultado.get("isError"))

    def ping(self):
        inicio = time.time()
        self._request("ping")
        return (time.time() - inicio) * 1000  # milisegundos

    def close(self):
        if self.conectado:
            try:
                self._request("shutdown")
            except Exception:
                pass       # los servidores oficiales no implementan `shutdown`
        self.transport.close()
        self.conectado = False


# =====================================================================
# Construccion desde el archivo de configuracion
# =====================================================================

def build_client(nombre, config, logger=None, stderr_dir="logs"):
    """Crea un MCPClient a partir de una entrada de servers_config.json.

    Entrada stdio:
        { "transport": "stdio", "command": "python3", "args": ["server.py"] }
    Entrada http:
        { "transport": "http", "url": "https://.../mcp" }
    """
    tipo = config.get("transport", "stdio").lower()

    if tipo == "stdio":
        comando = config.get("command")
        if not comando:
            raise ValueError(f"El servidor '{nombre}' no define 'command'.")

        # Las variables de entorno pueden traer ${VAR} para expandirse.
        entorno = {k: os.path.expandvars(v) for k, v in (config.get("env") or {}).items()}
        argumentos = [os.path.expandvars(a) for a in config.get("args", [])]

        transporte = StdioTransport(
            command=comando,
            args=argumentos,
            env=entorno,
            stderr_path=os.path.join(stderr_dir, f"{nombre}.stderr.log"),
        )

    elif tipo == "http":
        url = os.path.expandvars(config.get("url", ""))
        if not url:
            raise ValueError(f"El servidor '{nombre}' no define 'url'.")
        transporte = HttpTransport(url=url, timeout=config.get("timeout", 30),
                                   headers=config.get("headers"))

    else:
        raise ValueError(f"Transporte no soportado para '{nombre}': {tipo}")

    return MCPClient(nombre, transporte, logger=logger)


if __name__ == "__main__":
    # Prueba rapida: conectarse al servidor local y listar herramientas.
    cliente = build_client("distribuidora", {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["server.py"],
    })
    cliente.connect()
    print(f"Conectado a {cliente.server_info.get('name')} "
          f"v{cliente.server_info.get('version')} "
          f"(protocolo {cliente.protocol_version})")
    for herramienta in cliente.tools:
        print(f"  - {herramienta['name']}")
    cliente.close()
