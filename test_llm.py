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
        return {"candidates": [{"content": {"role": "model", "parts": [
            {"text": "Déjame revisar el inventario."},
            {"functionCall": {"name": "distribuidora__consultar_stock",
                              "args": {"sku": "AB-0009"}}}]},
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


def probar(proveedor, titulo):
    print(f"\n{'=' * 68}\n{titulo}\n{'=' * 68}")
    historial = [{"rol": "usuario", "texto": "¿Tienen aceite vegetal?"}]

    # --- Vuelta 1: el modelo pide una herramienta
    r1 = proveedor.enviar(historial, HERRAMIENTAS, "Eres un asistente.")
    print(f"  URL           : {capturado['url'][:78]}")
    print(f"  texto         : {r1.texto!r}")
    print(f"  llamadas      : {[(l.nombre, l.argumentos) for l in r1.llamadas]}")
    print(f"  terminado     : {r1.terminado}")

    esquema_enviado = json.dumps(capturado["cuerpo"])
    prohibidas = [k for k in lp.CLAVES_NO_SOPORTADAS if f'"{k}"' in esquema_enviado]
    if proveedor.nombre == "gemini":
        print(f"  claves no soportadas en el cuerpo: {prohibidas or 'ninguna'}")
        assert not prohibidas, f"El esquema aun contiene {prohibidas}"

    # --- El anfitrion ejecuta y devuelve el resultado
    historial.append({"rol": "asistente", "texto": r1.texto, "llamadas": r1.llamadas})
    historial.append({"rol": "herramienta", "resultados": [
        lp.Resultado(r1.llamadas[0].id, r1.llamadas[0].nombre,
                     '{"total_disponible": 318}', False)]})

    # --- Vuelta 2: el modelo responde con el dato
    r2 = proveedor.enviar(historial, HERRAMIENTAS, "Eres un asistente.")
    print(f"  respuesta final: {r2.texto!r}  (terminado={r2.terminado})")

    print("\n  --- Cuerpo enviado en la vuelta 2 (extracto) ---")
    print("  " + json.dumps(capturado["cuerpo"], ensure_ascii=False,
                            indent=2)[:620].replace("\n", "\n  "))
    assert r2.terminado, "El segundo turno deberia cerrar"


probar(lp.ProveedorGemini("llave-falsa"), "GEMINI")
probar(lp.ProveedorAnthropic("llave-falsa"), "ANTHROPIC")

print(f"\n{'=' * 68}\nAmbos proveedores traducen correctamente el historial neutral.\n{'=' * 68}")
