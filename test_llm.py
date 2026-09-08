#!/usr/bin/env python3
"""Verifica la traduccion del historial neutral al formato de cada
proveedor, sin llamar a las APIs reales ni consumir creditos.

Se sustituye la funcion de red por una que captura el cuerpo enviado y
devuelve una respuesta con la forma documentada de cada API. Asi se
comprueba que el anfitrion arma bien las peticiones y que interpreta
correctamente las respuestas, incluidas las llamadas a herramientas.
"""
import json
import sys

sys.path.insert(0, ".")
import llm_providers as lp

capturado = {}


def falso_post(url, cuerpo, cabeceras, etiqueta):
    capturado["url"] = url
    capturado["cuerpo"] = cuerpo
    capturado["cabeceras"] = cabeceras

    if "gemini" in etiqueta.lower():
        # ¿Es la primera vuelta o ya vienen resultados de herramienta?
        tiene_resultado = any(
            "functionResponse" in parte
            for c in cuerpo["contents"] for parte in c.get("parts", []))
        if tiene_resultado:
            return {"candidates": [{"content": {"role": "model", "parts": [
                {"text": "Si, hay 318 cajas en Bodega Central."}]},
                "finishReason": "STOP"}]}
        # Los modelos Gemini 3.x adjuntan thoughtSignature a la parte
        # functionCall y exigen recibirla de vuelta en el historial.
        return {"candidates": [{"content": {"role": "model", "parts": [
            {"text": "Déjame revisar el inventario."},
            {"functionCall": {"name": "distribuidora__consultar_stock",
                              "args": {"sku": "AB-0009"}},
             "thoughtSignature": "FIRMA_DE_PRUEBA_12345"}]},
            "finishReason": "STOP"}]}

    tiene_resultado = any(
        isinstance(m.get("content"), list) and
        any(b.get("type") == "tool_result" for b in m["content"])
        for m in cuerpo["messages"])
    if tiene_resultado:
        return {"content": [{"type": "text", "text": "Si, hay 318 cajas."}],
                "stop_reason": "end_turn"}
    return {"content": [
        {"type": "text", "text": "Déjame revisar el inventario."},
        {"type": "tool_use", "id": "toolu_01",
         "name": "distribuidora__consultar_stock", "input": {"sku": "AB-0009"}}],
        "stop_reason": "tool_use"}


lp._post_json = falso_post

# Esquema real tomado del servidor MCP, con claves que Gemini no acepta.
HERRAMIENTAS = [{
    "nombre": "distribuidora__consultar_stock",
    "descripcion": "[distribuidora] Consulta las existencias de un producto.",
    "esquema": {
        "type": "object",
        "properties": {
            "sku": {"type": "string", "description": "Codigo del producto."},
            "bodega_id": {"type": "integer", "description": "1, 2 o 3.", "enum": [1, 2, 3]},
            "limite": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        },
        "required": ["sku"],
        "additionalProperties": False,
    },
}]


def validar_esquema_gemini(esquema, ruta="parameters"):
    """Reproduce las validaciones que aplica Gemini y que provocan HTTP 400.

    Sin esto, la prueba con un servidor simulado aceptaria cualquier
    esquema y no detectaria los rechazos reales de la API.
    """
    fallos = []
    if isinstance(esquema, dict):
        for clave in esquema:
            if clave in lp.CLAVES_NO_SOPORTADAS:
                fallos.append(f"{ruta}: clave no soportada '{clave}'")

        valores = esquema.get("enum")
        if valores is not None:
            if esquema.get("type") != "string":
                fallos.append(f"{ruta}.enum: enum sobre type='{esquema.get('type')}' "
                              f"(Gemini solo admite enum en cadenas)")
            for i, valor in enumerate(valores):
                if not isinstance(valor, str):
                    fallos.append(f"{ruta}.enum[{i}]: valor no textual {valor!r}")

        for clave, valor in esquema.items():
            fallos.extend(validar_esquema_gemini(valor, f"{ruta}.{clave}"))

    elif isinstance(esquema, list):
        for i, elemento in enumerate(esquema):
            fallos.extend(validar_esquema_gemini(elemento, f"{ruta}[{i}]"))

    return fallos


def probar(proveedor, titulo):
    print(f"\n{'=' * 68}\n{titulo}\n{'=' * 68}")
    historial = [{"rol": "usuario", "texto": "¿Tienen aceite vegetal?"}]

    # --- Vuelta 1: el modelo pide una herramienta
    r1 = proveedor.enviar(historial, HERRAMIENTAS, "Eres un asistente.")
    print(f"  URL           : {capturado['url'][:78]}")
    print(f"  texto         : {r1.texto!r}")
    print(f"  llamadas      : {[(l.nombre, l.argumentos) for l in r1.llamadas]}")
    print(f"  terminado     : {r1.terminado}")

    if proveedor.nombre == "gemini":
        declaracion = capturado["cuerpo"]["tools"][0]["functionDeclarations"][0]
        fallos = validar_esquema_gemini(declaracion["parameters"])
        if fallos:
            print("  esquema RECHAZADO por Gemini:")
            for f in fallos:
                print(f"    - {f}")
            raise AssertionError("El esquema enviado no es valido para Gemini")
        print("  esquema        : valido para Gemini")
        print(f"  bodega_id      : "
              f"{json.dumps(declaracion['parameters']['properties']['bodega_id'], ensure_ascii=False)}")

    # --- El anfitrion ejecuta y devuelve el resultado
    historial.append({"rol": "asistente", "texto": r1.texto,
                      "llamadas": r1.llamadas, "crudo": r1.crudo})
    historial.append({"rol": "herramienta", "resultados": [
        lp.Resultado(r1.llamadas[0].id, r1.llamadas[0].nombre,
                     '{"total_disponible": 318}', False)]})

    # --- Vuelta 2: el modelo responde con el dato
    r2 = proveedor.enviar(historial, HERRAMIENTAS, "Eres un asistente.")
    print(f"  respuesta final: {r2.texto!r}  (terminado={r2.terminado})")

    if proveedor.nombre == "gemini":
        enviado = json.dumps(capturado["cuerpo"])
        assert "thoughtSignature" in enviado, \
            "La thoughtSignature no se reenvio; Gemini 3.x devolveria HTTP 400"
        assert "FIRMA_DE_PRUEBA_12345" in enviado, "La firma llego alterada"
        print("  thoughtSignature: reenviada intacta en el historial")

    print("\n  --- Cuerpo enviado en la vuelta 2 (extracto) ---")
    print("  " + json.dumps(capturado["cuerpo"], ensure_ascii=False,
                            indent=2)[:620].replace("\n", "\n  "))
    assert r2.terminado, "El segundo turno deberia cerrar"


probar(lp.ProveedorGemini("llave-falsa"), "GEMINI")
probar(lp.ProveedorAnthropic("llave-falsa"), "ANTHROPIC")

print(f"\n{'=' * 68}\nAmbos proveedores traducen correctamente el historial neutral.\n{'=' * 68}")
