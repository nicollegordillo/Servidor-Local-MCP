#!/usr/bin/env python3
"""
Proveedores de LLM - conexion a nivel de API

Proyecto 1 - CC3067 Redes - Universidad del Valle de Guatemala

El anfitrion no depende de un proveedor concreto. Mantiene el historial
de la conversacion en un formato neutral y cada proveedor lo traduce al
formato de su propia API. Cambiar de LLM es cambiar una clase; el cliente
MCP, los servidores y la logica del anfitrion no se tocan.

Todas las peticiones se construyen con `urllib` de la biblioteca
estandar, sin SDK, para dejar visible el intercambio HTTP tal como pide
el objetivo "comprender como interactuar con un LLM a nivel de la API".

FORMATO NEUTRAL DEL HISTORIAL
-----------------------------
    {"rol": "usuario",    "texto": str}
    {"rol": "asistente",  "texto": str, "llamadas": [Llamada, ...]}
    {"rol": "herramienta","resultados": [Resultado, ...]}

Cada proveedor implementa `enviar(historial, herramientas)` y devuelve un
objeto `RespuestaLLM` con el texto producido, las llamadas a herramientas
solicitadas y si el turno termino.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

TIMEOUT = 120
MAX_TOKENS = 2000

# Codigos HTTP que indican una falla temporal del proveedor y no un error
# en la peticion: saturacion del servicio (503), limite de peticiones por
# minuto (429) y errores internos (500, 502, 504). Ante estos conviene
# reintentar en lugar de abortar el turno.
CODIGOS_TRANSITORIOS = {429, 500, 502, 503, 504}
REINTENTOS = 3
ESPERA_BASE = 2.0        # segundos; se duplica en cada intento
ESPERA_MAXIMA = 70.0     # tope para la espera sugerida por el proveedor


def _espera_sugerida(detalle):
    """Extrae del cuerpo del error cuantos segundos pide esperar la API.

    Al superar la cuota, los proveedores indican el tiempo de reintento.
    Gemini lo expone de dos formas: como campo `retryDelay` ("25s") en los
    detalles estructurados del error, y como texto dentro del mensaje
    ("Please retry in 25.26s"). La unidad varia entre segundos y
    milisegundos segun cuanto falte para que se renueve la cuota, asi que
    ambas se contemplan. Respetar ese valor evita reintentar antes de
    tiempo y volver a recibir el mismo rechazo.
    """
    for patron in (r'"retryDelay"\s*:\s*"([\d.]+)(ms|s)"',
                   r"retry in ([\d.]+)\s*(ms|s)"):
        encontrado = re.search(patron, detalle, re.IGNORECASE)
        if not encontrado:
            continue
        try:
            valor = float(encontrado.group(1))
        except ValueError:
            continue

        if encontrado.group(2).lower() == "ms":
            valor /= 1000.0

        # Un segundo extra de margen sobre lo que pide la API.
        return min(valor + 1.0, ESPERA_MAXIMA)

    return None


# =====================================================================
# Estructuras neutrales
# =====================================================================

class Llamada:
    """Peticion del modelo para invocar una herramienta."""

    def __init__(self, identificador, nombre, argumentos):
        self.id = identificador
        self.nombre = nombre
        self.argumentos = argumentos or {}


class Resultado:
    """Salida de una herramienta, que se devuelve al modelo."""

    def __init__(self, identificador, nombre, texto, error=False):
        self.id = identificador
        self.nombre = nombre
        self.texto = texto
        self.error = error


class RespuestaLLM:
    def __init__(self, texto, llamadas, terminado, crudo=None):
        self.texto = texto              # texto visible para el usuario
        self.llamadas = llamadas        # lista de Llamada
        self.terminado = terminado      # True si no pide herramientas
        # Representacion original de la respuesta, tal como la devolvio el
        # proveedor. Algunos modelos (Gemini 3.x) exigen que ciertas
        # partes se reenvien intactas en el historial, asi que se guardan
        # sin interpretarlas. Formato: {"proveedor": str, "partes": [...]}
        self.crudo = crudo


# =====================================================================
# Utilidades comunes
# =====================================================================

def _post_json(url, cuerpo, cabeceras, etiqueta):
    """POST con cuerpo JSON. Devuelve la respuesta ya deserializada.

    Ante fallas temporales del proveedor (saturacion, limite de tasa) se
    reintenta con espera creciente. Los errores de la peticion en si
    (400, 401, 404) no se reintentan porque volverian a fallar igual.
    """
    peticion = urllib.request.Request(
        url,
        data=json.dumps(cuerpo, ensure_ascii=False).encode("utf-8"),
        headers=cabeceras,
        method="POST",
    )

    espera = ESPERA_BASE
    for intento in range(1, REINTENTOS + 1):
        try:
            with urllib.request.urlopen(peticion, timeout=TIMEOUT) as respuesta:
                return json.loads(respuesta.read().decode("utf-8"))

        except urllib.error.HTTPError as exc:
            detalle = exc.read().decode("utf-8", errors="replace")

            if exc.code in CODIGOS_TRANSITORIOS and intento < REINTENTOS:
                sugerida = _espera_sugerida(detalle)
                demora = sugerida if sugerida is not None else espera

                if exc.code == 429:
                    motivo = "cuota o limite de peticiones agotado"
                else:
                    motivo = "servicio saturado"

                print(f"  ({etiqueta}: {motivo}, HTTP {exc.code}. "
                      f"Reintento {intento} de {REINTENTOS - 1} en {demora:.0f} s...)")
                time.sleep(demora)
                espera *= 2
                continue

            if exc.code == 429:
                raise RuntimeError(
                    f"Cuota agotada en {etiqueta} (HTTP 429).\n"
                    f"  La capa gratuita limita el numero de peticiones. Opciones:\n"
                    f"    - esperar a que se renueve la cuota\n"
                    f"    - probar otro modelo con --model\n"
                    f"    - cambiar de proveedor con --provider\n"
                    f"  Detalle: {detalle[:300]}")

            raise RuntimeError(f"Error HTTP {exc.code} de {etiqueta}: {detalle[:500]}")

        except urllib.error.URLError as exc:
            if intento < REINTENTOS:
                print(f"  ({etiqueta}: sin conexion. "
                      f"Reintento {intento} de {REINTENTOS - 1} en {espera:.0f} s...)")
                time.sleep(espera)
                espera *= 2
                continue
            raise RuntimeError(f"No se pudo contactar {etiqueta}: {exc.reason}")

    raise RuntimeError(f"{etiqueta} no respondio tras {REINTENTOS} intentos.")


class ProveedorLLM:
    """Interfaz que deben cumplir todos los proveedores."""

    nombre = "generico"

    def enviar(self, historial, herramientas, system):
        raise NotImplementedError


# =====================================================================
# Anthropic (Claude)
# =====================================================================

class ProveedorAnthropic(ProveedorLLM):
    """API de mensajes de Anthropic.

    Las herramientas viajan en `tools` con su JSON Schema tal cual, y el
    modelo responde con bloques `tool_use` cuando quiere invocarlas.
    """

    nombre = "anthropic"
    URL = "https://api.anthropic.com/v1/messages"
    VERSION = "2023-06-01"
    MODELO_POR_DEFECTO = "claude-sonnet-4-5"

    def __init__(self, api_key, modelo=None):
        if not api_key:
            raise RuntimeError(
                "Falta la variable de entorno ANTHROPIC_API_KEY.\n"
                "  Windows PowerShell: $env:ANTHROPIC_API_KEY=\"sk-ant-...\"\n"
                "  Linux/macOS       : export ANTHROPIC_API_KEY=sk-ant-..."
            )
        self.api_key = api_key
        self.modelo = modelo or self.MODELO_POR_DEFECTO

    # -- traduccion del historial neutral al formato de Anthropic ------

    def _mensajes(self, historial):
        mensajes = []
        for entrada in historial:
            rol = entrada["rol"]

            if rol == "usuario":
                mensajes.append({"role": "user", "content": entrada["texto"]})

            elif rol == "asistente":
                bloques = []
                if entrada.get("texto"):
                    bloques.append({"type": "text", "text": entrada["texto"]})
                for llamada in entrada.get("llamadas", []):
                    bloques.append({
                        "type": "tool_use",
                        "id": llamada.id,
                        "name": llamada.nombre,
                        "input": llamada.argumentos,
                    })
                mensajes.append({"role": "assistant", "content": bloques})

            elif rol == "herramienta":
                bloques = [{
                    "type": "tool_result",
                    "tool_use_id": r.id,
                    "content": r.texto,
                    "is_error": r.error,
                } for r in entrada["resultados"]]
                mensajes.append({"role": "user", "content": bloques})

        return mensajes

    def enviar(self, historial, herramientas, system):
        cuerpo = {
            "model": self.modelo,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": self._mensajes(historial),
        }
        if herramientas:
            cuerpo["tools"] = [{
                "name": h["nombre"],
                "description": h["descripcion"],
                "input_schema": h["esquema"],
            } for h in herramientas]

        datos = _post_json(self.URL, cuerpo, {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.VERSION,
        }, "la API de Anthropic")

        textos, llamadas = [], []
        for bloque in datos.get("content", []):
            if bloque.get("type") == "text":
                textos.append(bloque.get("text", ""))
            elif bloque.get("type") == "tool_use":
                llamadas.append(Llamada(bloque["id"], bloque["name"], bloque.get("input")))

        return RespuestaLLM(
            texto="\n".join(t for t in textos if t.strip()),
            llamadas=llamadas,
            terminado=datos.get("stop_reason") != "tool_use",
        )


# =====================================================================
# Google Gemini
# =====================================================================

# Gemini acepta un subconjunto de OpenAPI para los esquemas de funcion y
# rechaza la peticion completa si encuentra claves que no reconoce. Estas
# son las que hay que retirar de los esquemas que publica el servidor MCP.
CLAVES_NO_SOPORTADAS = {
    "additionalProperties", "$schema", "default",
    "minItems", "maxItems", "minimum", "maximum",
}


def limpiar_esquema(esquema):
    """Adapta un JSON Schema de MCP al subconjunto que acepta Gemini.

    Dos ajustes son necesarios:

    1. Retirar las palabras clave que Gemini no reconoce; su presencia
       hace que rechace la peticion completa con HTTP 400.

    2. Gemini solo admite `enum` en campos de tipo string. Un enum
       numerico (por ejemplo bodega_id con [1, 2, 3]) provoca el error
       "Invalid value at ... enum[0] (TYPE_STRING)". En esos casos se
       retira el enum y los valores permitidos se trasladan a la
       descripcion, de modo que el modelo conserve la informacion aunque
       ya no este validada por el esquema.
    """
    if isinstance(esquema, dict):
        limpio = {}
        for clave, valor in esquema.items():
            if clave in CLAVES_NO_SOPORTADAS:
                continue
            limpio[clave] = limpiar_esquema(valor)

        # Enum solo se admite sobre cadenas.
        valores = limpio.get("enum")
        if valores is not None and limpio.get("type") != "string":
            if all(isinstance(v, str) for v in valores):
                limpio["type"] = "string"
            else:
                limpio.pop("enum")
                listado = ", ".join(str(v) for v in valores)
                descripcion = limpio.get("description", "").rstrip()
                separador = " " if descripcion and not descripcion.endswith(".") else ""
                limpio["description"] = (
                    f"{descripcion}{separador} Valores permitidos: {listado}.".strip())

        # Gemini exige que un objeto declare 'properties'.
        if limpio.get("type") == "object" and "properties" not in limpio:
            limpio["properties"] = {}
        return limpio

    if isinstance(esquema, list):
        return [limpiar_esquema(elemento) for elemento in esquema]

    return esquema


class ProveedorGemini(ProveedorLLM):
    """API de Google Gemini (Generative Language API).

    Diferencias relevantes frente a Anthropic:
      - El rol del modelo se llama 'model', no 'assistant'.
      - Las herramientas se declaran en `tools[0].functionDeclarations`.
      - El modelo pide herramientas con partes `functionCall`, que NO
        llevan identificador: la correlacion se hace por nombre.
      - Los resultados se devuelven como partes `functionResponse` dentro
        de un mensaje con rol 'user'.
      - El esquema de parametros debe limpiarse (ver limpiar_esquema).
    """

    nombre = "gemini"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
    # Los identificadores de modelo cambian con el tiempo; si la API
    # responde 404 indicando que el modelo ya no existe, el mensaje de
    # error nombra el sustituto. Tambien puede pasarse con --model.
    MODELO_POR_DEFECTO = "gemini-3.6-flash"

    def __init__(self, api_key, modelo=None):
        if not api_key:
            raise RuntimeError(
                "Falta la variable de entorno GEMINI_API_KEY.\n"
                "  Obtenga una llave gratuita en https://aistudio.google.com/apikey\n"
                "  Windows PowerShell: $env:GEMINI_API_KEY=\"...\"\n"
                "  Linux/macOS       : export GEMINI_API_KEY=..."
            )
        self.api_key = api_key
        self.modelo = modelo or self.MODELO_POR_DEFECTO

    # -- traduccion del historial neutral al formato de Gemini ---------

    def _contents(self, historial):
        contenidos = []
        for entrada in historial:
            rol = entrada["rol"]

            if rol == "usuario":
                contenidos.append({"role": "user",
                                   "parts": [{"text": entrada["texto"]}]})

            elif rol == "asistente":
                # Los modelos Gemini 3.x acompanan cada functionCall con
                # una `thoughtSignature` que debe devolverse intacta en el
                # historial; si falta, la API responde 400. Por eso se
                # reenvian las partes originales tal como llegaron, en
                # lugar de reconstruirlas a partir del formato neutral.
                crudo = entrada.get("crudo")
                if crudo and crudo.get("proveedor") == self.nombre and crudo.get("partes"):
                    contenidos.append({"role": "model", "parts": crudo["partes"]})
                    continue

                partes = []
                if entrada.get("texto"):
                    partes.append({"text": entrada["texto"]})
                for llamada in entrada.get("llamadas", []):
                    partes.append({"functionCall": {
                        "name": llamada.nombre,
                        "args": llamada.argumentos,
                    }})
                if partes:
                    contenidos.append({"role": "model", "parts": partes})

            elif rol == "herramienta":
                partes = []
                for r in entrada["resultados"]:
                    # functionResponse.response debe ser un objeto JSON.
                    carga = {"error": r.texto} if r.error else {"resultado": r.texto}
                    partes.append({"functionResponse": {
                        "name": r.nombre,
                        "response": carga,
                    }})
                contenidos.append({"role": "user", "parts": partes})

        return contenidos

    def enviar(self, historial, herramientas, system):
        url = f"{self.BASE_URL}/{self.modelo}:generateContent?key={self.api_key}"

        cuerpo = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": self._contents(historial),
            "generationConfig": {"maxOutputTokens": MAX_TOKENS},
        }

        if herramientas:
            cuerpo["tools"] = [{
                "functionDeclarations": [{
                    "name": h["nombre"],
                    "description": h["descripcion"][:1000],
                    "parameters": limpiar_esquema(h["esquema"]),
                } for h in herramientas]
            }]

        datos = _post_json(url, cuerpo, {"Content-Type": "application/json"},
                           "la API de Gemini")

        candidatos = datos.get("candidates") or []
        if not candidatos:
            motivo = datos.get("promptFeedback", {}).get("blockReason", "sin candidatos")
            return RespuestaLLM(f"(el modelo no devolvio respuesta: {motivo})", [], True)

        candidato = candidatos[0]
        partes = candidato.get("content", {}).get("parts") or []

        textos, llamadas = [], []
        for indice, parte in enumerate(partes):
            if "text" in parte:
                textos.append(parte["text"])
            elif "functionCall" in parte:
                fc = parte["functionCall"]
                # Gemini no asigna identificadores; se genera uno local
                # para poder emparejar la llamada con su resultado.
                llamadas.append(Llamada(
                    identificador=f"call_{len(historial)}_{indice}",
                    nombre=fc.get("name", ""),
                    argumentos=fc.get("args") or {},
                ))

        return RespuestaLLM(
            texto="\n".join(t for t in textos if t.strip()),
            llamadas=llamadas,
            terminado=not llamadas,
            crudo={"proveedor": self.nombre, "partes": partes},
        )


# =====================================================================
# Seleccion del proveedor
# =====================================================================

PROVEEDORES = {
    "anthropic": (ProveedorAnthropic, "ANTHROPIC_API_KEY"),
    "gemini": (ProveedorGemini, "GEMINI_API_KEY"),
}


def crear_proveedor(nombre=None, modelo=None):
    """Instancia el proveedor indicado.

    Si no se indica ninguno se toma de LLM_PROVIDER, y si tampoco esta
    definida se elige el primero cuya llave de API este presente en el
    entorno.
    """
    nombre = (nombre or os.environ.get("LLM_PROVIDER") or "").lower().strip()

    if not nombre:
        for candidato, (_clase, variable) in PROVEEDORES.items():
            if os.environ.get(variable):
                nombre = candidato
                break

    if not nombre:
        raise RuntimeError(
            "No se encontro ninguna llave de API en el entorno.\n"
            "  Defina GEMINI_API_KEY o ANTHROPIC_API_KEY, o indique el\n"
            "  proveedor con --provider."
        )

    if nombre not in PROVEEDORES:
        disponibles = ", ".join(PROVEEDORES)
        raise RuntimeError(f"Proveedor desconocido: '{nombre}'. Disponibles: {disponibles}.")

    clase, variable = PROVEEDORES[nombre]
    modelo = modelo or os.environ.get("LLM_MODEL")
    return clase(os.environ.get(variable), modelo)
