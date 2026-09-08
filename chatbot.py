#!/usr/bin/env python3
"""
Chatbot anfitrion MCP - Distribuidora El Quetzal

Proyecto 1 - CC3067 Redes - Universidad del Valle de Guatemala

Implementa el actor "Anfitrion" del protocolo MCP: coordina varios
clientes (uno por servidor), agrega las herramientas de todos ellos y se
las ofrece al LLM. Cuando el modelo decide usar una, el anfitrion la
ejecuta contra el servidor correspondiente y le devuelve el resultado.

FUNCIONALIDADES (numeradas segun el enunciado)
----------------------------------------------
 1) Conexion con el LLM a nivel de su API HTTP, construida con urllib --
    sin SDK. El proveedor es intercambiable (Gemini o Anthropic); ver
    llm_providers.py.
 2) Contexto de sesion: el historial completo viaja en cada peticion, de
    modo que las preguntas de seguimiento se resuelven correctamente.
 3) Bitacora de todas las interacciones con los servidores MCP, visible
    en vivo y consultable con el comando /log.
 4) Uso de servidores MCP oficiales (Filesystem y Git) junto al servidor
    propio, todos declarados en servers_config.json.

El LLM nunca ejecuta nada: unicamente decide que herramienta invocar y
con que argumentos. La ejecucion la realiza este anfitrion a traves del
cliente MCP.

Uso:
    $env:GEMINI_API_KEY="..."       (Windows PowerShell)
    export GEMINI_API_KEY=...       (Linux / macOS)
    python chatbot.py

    python chatbot.py --provider anthropic     (para usar Claude)
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

from mcp_client import MCPClient, MCPError, build_client
from llm_providers import Resultado, crear_proveedor

# ---------------------------------------------------------------------
# Configuracion del LLM
# ---------------------------------------------------------------------
# El proveedor concreto (Gemini, Anthropic, ...) se resuelve en
# llm_providers.py. Aqui solo se fija el tope de rondas de herramientas.
MAX_ITERACIONES_HERRAMIENTAS = 8

SYSTEM_PROMPT = """Eres el asistente de Distribuidora El Quetzal, una distribuidora \
mayorista de productos de consumo en Guatemala. Atiendes a clientes minoristas.

Tienes herramientas conectadas por MCP para consultar el catalogo, el inventario y los \
pedidos, y para registrar pedidos nuevos. Usalas siempre que la pregunta requiera datos \
reales; nunca inventes precios, existencias ni numeros de pedido.

Pautas:
- Si el usuario menciona un producto por nombre, primero busca su SKU con buscar_productos.
- Verifica existencias antes de comprometerte con un pedido.
- Si una herramienta devuelve un error, explicaselo al usuario en lenguaje claro y \
propon una alternativa (por ejemplo, despachar desde otra bodega).
- Los precios estan en quetzales (GTQ). Se breve y concreto.

Tambien tienes herramientas de sistema de archivos y de Git si estan disponibles."""

# ---------------------------------------------------------------------
# Colores para la interfaz de terminal
# ---------------------------------------------------------------------


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ROJO = "\033[38;5;203m"
    VERDE = "\033[38;5;114m"
    AMARILLO = "\033[38;5;179m"
    AZUL = "\033[38;5;110m"
    MAGENTA = "\033[38;5;176m"
    CIAN = "\033[38;5;116m"
    GRIS = "\033[38;5;245m"

    @classmethod
    def desactivar(cls):
        for atributo in list(vars(cls)):
            if atributo.isupper():
                setattr(cls, atributo, "")


def habilitar_ansi():
    """En Windows hay que activar el procesamiento de secuencias ANSI."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # -11 = STD_OUTPUT_HANDLE, 7 = ENABLE_VIRTUAL_TERMINAL_PROCESSING | actual
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        return True
    except Exception:
        return False


def titulo(texto, color=C.CIAN):
    ancho = 68
    print(f"\n{color}{'─' * ancho}\n{C.BOLD}{texto}{C.RESET}{color}\n{'─' * ancho}{C.RESET}")


# =====================================================================
# Bitacora de interacciones MCP (requisito 3)
# =====================================================================

class BitacoraMCP:
    """Registra cada mensaje intercambiado con los servidores MCP.

    Se mantiene en memoria para el comando /log y se escribe a archivo
    para poder revisarla despues o contrastarla con la captura de
    Wireshark.
    """

    def __init__(self, ruta_archivo="logs/chatbot_mcp.log", mostrar_en_vivo=True):
        self.entradas = []
        self.mostrar_en_vivo = mostrar_en_vivo
        self.ruta_archivo = ruta_archivo
        if ruta_archivo:
            os.makedirs(os.path.dirname(os.path.abspath(ruta_archivo)), exist_ok=True)

    def registrar(self, servidor, direccion, mensaje):
        marca = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        entrada = {
            "hora": marca,
            "servidor": servidor,
            "direccion": direccion,          # "enviado" | "recibido"
            "mensaje": mensaje,
        }
        self.entradas.append(entrada)

        if self.ruta_archivo:
            flecha = ">>" if direccion == "enviado" else "<<"
            linea = (f"[{marca}] {servidor:<14} {flecha} "
                     f"{json.dumps(mensaje, ensure_ascii=False)}\n")
            with open(self.ruta_archivo, "a", encoding="utf-8") as f:
                f.write(linea)

        if self.mostrar_en_vivo:
            self._imprimir(entrada, compacto=True)

    def _imprimir(self, entrada, compacto=False):
        mensaje = entrada["mensaje"] or {}
        enviado = entrada["direccion"] == "enviado"
        flecha = f"{C.AZUL}>>{C.RESET}" if enviado else f"{C.MAGENTA}<<{C.RESET}"

        if compacto:
            if enviado:
                metodo = mensaje.get("method", "?")
                extra = ""
                if metodo == "tools/call":
                    extra = f" {C.BOLD}{mensaje.get('params', {}).get('name', '')}{C.RESET}"
                etiqueta = f"{metodo}{extra}"
            else:
                if "error" in mensaje:
                    etiqueta = f"{C.ROJO}error {mensaje['error'].get('code')}{C.RESET}"
                else:
                    resultado = mensaje.get("result", {})
                    if resultado.get("isError"):
                        etiqueta = f"{C.AMARILLO}resultado (isError){C.RESET}"
                    else:
                        etiqueta = f"{C.VERDE}resultado{C.RESET}"
            print(f"  {C.GRIS}{entrada['hora']}{C.RESET} {flecha} "
                  f"{C.GRIS}{entrada['servidor']}{C.RESET}  {etiqueta}")
        else:
            print(f"  {C.GRIS}{entrada['hora']} {entrada['servidor']}{C.RESET} {flecha}")
            print(f"    {json.dumps(mensaje, ensure_ascii=False)[:500]}")

    def mostrar(self, ultimas=None, detallado=False):
        entradas = self.entradas[-ultimas:] if ultimas else self.entradas
        if not entradas:
            print(f"{C.GRIS}  (sin interacciones registradas todavia){C.RESET}")
            return
        for entrada in entradas:
            self._imprimir(entrada, compacto=not detallado)
        print(f"{C.GRIS}  {len(self.entradas)} mensajes en total. "
              f"Archivo: {self.ruta_archivo}{C.RESET}")


# =====================================================================
# Anfitrion
# =====================================================================

class Anfitrion:
    """Coordina los clientes MCP y el LLM."""

    def __init__(self, config_path="servers_config.json", mostrar_log=True,
                 proveedor=None, modelo=None):
        self.config_path = config_path
        self.bitacora = BitacoraMCP(mostrar_en_vivo=mostrar_log)
        self.clientes = {}                  # nombre -> MCPClient
        self.mapa_herramientas = {}         # nombre expuesto al LLM -> (cliente, tool)
        self.historial = []                 # contexto de la conversacion (requisito 2)
        self.llm = crear_proveedor(proveedor, modelo)

    # -- conexion a los servidores -------------------------------------

    def conectar_servidores(self):
        if not os.path.exists(self.config_path):
            raise RuntimeError(f"No se encontro '{self.config_path}'.")

        with open(self.config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        servidores = config.get("mcpServers", {})
        titulo("Conectando a los servidores MCP")

        for nombre, ajustes in servidores.items():
            if ajustes.get("disabled"):
                print(f"  {C.GRIS}○ {nombre:<16} desactivado en la configuracion{C.RESET}")
                continue

            try:
                cliente = build_client(nombre, ajustes, logger=self.bitacora.registrar)
                cliente.connect()
                self.clientes[nombre] = cliente

                for herramienta in cliente.tools:
                    # Se prefija con el servidor para evitar colisiones de
                    # nombres entre servidores distintos.
                    expuesto = f"{nombre}__{herramienta['name']}"[:64]
                    self.mapa_herramientas[expuesto] = (cliente, herramienta)

                info = cliente.server_info
                print(f"  {C.VERDE}✓{C.RESET} {C.BOLD}{nombre:<22}{C.RESET}"
                      f"{info.get('name', '?')} v{info.get('version', '?')}  "
                      f"{C.GRIS}{len(cliente.tools)} herramientas | "
                      f"{cliente.transport.descripcion()}{C.RESET}")

            except Exception as exc:
                print(f"  {C.ROJO}✗{C.RESET} {C.BOLD}{nombre:<22}{C.RESET}"
                      f"{C.ROJO}{exc}{C.RESET}")

        if not self.clientes:
            raise RuntimeError("No se pudo conectar a ningun servidor MCP.")

        print(f"\n  {len(self.mapa_herramientas)} herramientas disponibles "
              f"en {len(self.clientes)} servidores.")

    def herramientas_para_llm(self):
        """Catalogo MCP en formato neutral; cada proveedor lo traduce."""
        catalogo = []
        for expuesto, (cliente, herramienta) in self.mapa_herramientas.items():
            catalogo.append({
                "nombre": expuesto,
                "descripcion": f"[{cliente.nombre}] {herramienta.get('description', '')}",
                "esquema": herramienta.get("inputSchema", {"type": "object", "properties": {}}),
            })
        return catalogo

    def ejecutar_herramienta(self, nombre, argumentos):
        if nombre not in self.mapa_herramientas:
            return f"Error: la herramienta '{nombre}' no esta disponible.", True

        cliente, herramienta = self.mapa_herramientas[nombre]
        try:
            return cliente.call_tool(herramienta["name"], argumentos)
        except MCPError as exc:
            return f"Error de protocolo MCP: {exc}", True
        except Exception as exc:
            return f"Error inesperado al ejecutar la herramienta: {exc}", True

    # -- turno de conversacion -----------------------------------------

    def procesar(self, entrada_usuario):
        """Un turno completo: puede requerir varias rondas de herramientas."""
        self.historial.append({"rol": "usuario", "texto": entrada_usuario})
        herramientas = self.herramientas_para_llm()

        for _ronda in range(MAX_ITERACIONES_HERRAMIENTAS):
            respuesta = self.llm.enviar(self.historial, herramientas, SYSTEM_PROMPT)

            # El historial conserva el texto y las llamadas solicitadas,
            # que es lo que mantiene el contexto entre turnos (requisito 2).
            self.historial.append({
                "rol": "asistente",
                "texto": respuesta.texto,
                "llamadas": respuesta.llamadas,
            })

            if respuesta.texto.strip():
                print(f"\n{C.BOLD}{C.VERDE}Asistente:{C.RESET} {respuesta.texto.strip()}")

            if respuesta.terminado:
                return

            # Ejecutar cada herramienta solicitada por el modelo
            resultados = []
            for llamada in respuesta.llamadas:
                texto, hubo_error = self.ejecutar_herramienta(
                    llamada.nombre, llamada.argumentos)
                resultados.append(Resultado(llamada.id, llamada.nombre,
                                            texto, hubo_error))

            self.historial.append({"rol": "herramienta", "resultados": resultados})

        print(f"{C.AMARILLO}  (se alcanzo el limite de rondas de herramientas){C.RESET}")

    def cerrar(self):
        for cliente in self.clientes.values():
            try:
                cliente.close()
            except Exception:
                pass


# =====================================================================
# Interfaz de terminal
# =====================================================================

BANNER = f"""{C.CIAN}
  ╔══════════════════════════════════════════════════════════════╗
  ║   {C.BOLD}DISTRIBUIDORA EL QUETZAL{C.RESET}{C.CIAN}  ·  Asistente de inventario      ║
  ║   {C.GRIS}Chatbot anfitrion MCP  ·  CC3067 Redes  ·  UVG{C.RESET}{C.CIAN}             ║
  ╚══════════════════════════════════════════════════════════════╝{C.RESET}"""

AYUDA = f"""
  {C.BOLD}Comandos{C.RESET}
    {C.CIAN}/help{C.RESET}       muestra esta ayuda
    {C.CIAN}/tools{C.RESET}      lista las herramientas disponibles por servidor
    {C.CIAN}/servers{C.RESET}    estado de los servidores conectados (con ping)
    {C.CIAN}/log{C.RESET}        ultimas interacciones MCP  ({C.GRIS}/log todo{C.RESET} para el detalle)
    {C.CIAN}/quiet{C.RESET}      alterna el registro en vivo de mensajes MCP
    {C.CIAN}/reset{C.RESET}      borra el contexto de la conversacion
    {C.CIAN}/salir{C.RESET}      termina la sesion

  {C.BOLD}Ejemplos{C.RESET}
    {C.GRIS}¿Tienen aceite vegetal disponible?
    Soy el cliente 3, quiero 10 cajas de aceite vegetal
    ¿Cuanto seria ese pedido en dolares?
    ¿Quien fue Alan Turing?  {C.DIM}(pregunta general, sin herramientas){C.RESET}"""


def comando_tools(anfitrion):
    titulo("Herramientas disponibles")
    for nombre_servidor, cliente in anfitrion.clientes.items():
        print(f"\n  {C.BOLD}{C.CIAN}{nombre_servidor}{C.RESET} "
              f"{C.GRIS}({cliente.transport.descripcion()}){C.RESET}")
        for herramienta in cliente.tools:
            descripcion = (herramienta.get("description") or "").split(".")[0][:70]
            print(f"    {C.VERDE}·{C.RESET} {herramienta['name']:<30} {C.GRIS}{descripcion}{C.RESET}")


def comando_servers(anfitrion):
    titulo("Servidores conectados")
    for nombre_servidor, cliente in anfitrion.clientes.items():
        try:
            latencia = cliente.ping()
            estado = f"{C.VERDE}activo{C.RESET}  {C.GRIS}{latencia:.1f} ms{C.RESET}"
        except Exception as exc:
            estado = f"{C.ROJO}sin respuesta ({exc}){C.RESET}"
        info = cliente.server_info
        print(f"  {C.BOLD}{nombre_servidor:<16}{C.RESET}{estado}")
        print(f"    {C.GRIS}{info.get('name', '?')} v{info.get('version', '?')} · "
              f"protocolo {cliente.protocol_version} · "
              f"{cliente.transport.descripcion()}{C.RESET}")


def main():
    parser = argparse.ArgumentParser(description="Chatbot anfitrion MCP.")
    parser.add_argument("--config", default="servers_config.json",
                        help="Archivo de configuracion de servidores.")
    parser.add_argument("--provider", default=None,
                        help="Proveedor del LLM: gemini o anthropic. "
                             "Por defecto se detecta segun la llave presente en el entorno.")
    parser.add_argument("--model", default=None,
                        help="Modelo del LLM. Por defecto el del proveedor elegido.")
    parser.add_argument("--quiet", action="store_true",
                        help="No muestra los mensajes MCP en vivo.")
    parser.add_argument("--no-color", action="store_true",
                        help="Desactiva los colores de la terminal.")
    args = parser.parse_args()

    if args.no_color or not habilitar_ansi():
        C.desactivar()

    print(BANNER)

    try:
        anfitrion = Anfitrion(args.config, mostrar_log=not args.quiet,
                              proveedor=args.provider, modelo=args.model)
        anfitrion.conectar_servidores()
    except Exception as exc:
        print(f"\n{C.ROJO}No se pudo iniciar: {exc}{C.RESET}")
        sys.exit(1)

    print(AYUDA)
    print(f"\n{C.GRIS}LLM: {anfitrion.llm.nombre} · modelo {anfitrion.llm.modelo}{C.RESET}")

    try:
        while True:
            try:
                entrada = input(f"\n{C.BOLD}{C.AZUL}Tu:{C.RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not entrada:
                continue

            # -- comandos internos
            if entrada.startswith("/"):
                partes = entrada.lower().split()
                comando = partes[0]

                if comando in ("/salir", "/quit", "/exit"):
                    break
                elif comando == "/help":
                    print(AYUDA)
                elif comando == "/tools":
                    comando_tools(anfitrion)
                elif comando == "/servers":
                    comando_servers(anfitrion)
                elif comando == "/log":
                    titulo("Bitacora de interacciones MCP")
                    detallado = len(partes) > 1 and partes[1] in ("todo", "full", "-v")
                    anfitrion.bitacora.mostrar(ultimas=None if detallado else 30,
                                               detallado=detallado)
                elif comando == "/quiet":
                    anfitrion.bitacora.mostrar_en_vivo = not anfitrion.bitacora.mostrar_en_vivo
                    estado = "activado" if anfitrion.bitacora.mostrar_en_vivo else "desactivado"
                    print(f"{C.GRIS}  Registro en vivo {estado}.{C.RESET}")
                elif comando == "/reset":
                    anfitrion.historial.clear()
                    print(f"{C.GRIS}  Contexto de la conversacion borrado.{C.RESET}")
                else:
                    print(f"{C.AMARILLO}  Comando desconocido. Use /help.{C.RESET}")
                continue

            # -- turno normal de conversacion
            try:
                inicio = time.time()
                anfitrion.procesar(entrada)
                print(f"{C.GRIS}  ({time.time() - inicio:.1f} s){C.RESET}")
            except RuntimeError as exc:
                print(f"{C.ROJO}  {exc}{C.RESET}")
            except Exception as exc:
                print(f"{C.ROJO}  Error inesperado: {exc}{C.RESET}")

    finally:
        print(f"\n{C.GRIS}Cerrando servidores MCP...{C.RESET}")
        anfitrion.cerrar()
        print(f"{C.CIAN}Hasta luego.{C.RESET}\n")


if __name__ == "__main__":
    main()
