#!/usr/bin/env python3
"""
Cliente de prueba para el servidor MCP local.

Proyecto 1 - CC3067 Redes - Universidad del Valle de Guatemala

Levanta `server.py` como subproceso, se comunica con el por stdin/stdout
y ejecuta un recorrido completo del protocolo: handshake, listado de
herramientas, llamadas exitosas, casos de error de negocio y errores de
protocolo. Sirve para verificar el servidor sin necesidad del chatbot.

No forma parte del entregable funcional; es una herramienta de desarrollo
y de demostracion.

Uso:
    python test_client.py
"""

import json
import subprocess
import sys


class ClientePrueba:
    def __init__(self):
        self.proceso = subprocess.Popen(
            [sys.executable, "-u", "server.py", "--log-file", ""],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,   # la bitacora del servidor se descarta aqui
            text=True,
            bufsize=1,
        )
        self.siguiente_id = 1

    def enviar(self, metodo, params=None, es_notificacion=False):
        mensaje = {"jsonrpc": "2.0", "method": metodo}
        if params is not None:
            mensaje["params"] = params
        if not es_notificacion:
            mensaje["id"] = self.siguiente_id
            self.siguiente_id += 1

        self.proceso.stdin.write(json.dumps(mensaje) + "\n")
        self.proceso.stdin.flush()

        if es_notificacion:
            return None

        linea = self.proceso.stdout.readline()
        if not linea:
            raise RuntimeError("El servidor cerro la conexion inesperadamente.")
        return json.loads(linea)

    def cerrar(self):
        try:
            self.enviar("shutdown")
        except Exception:
            pass
        self.proceso.stdin.close()
        self.proceso.wait(timeout=5)


def titulo(texto):
    print(f"\n{'=' * 66}\n{texto}\n{'=' * 66}")


def mostrar(respuesta, solo_primeras=None):
    """Imprime la respuesta. Para tools/call extrae el bloque de texto."""
    if "error" in respuesta:
        print(f"  ERROR JSON-RPC {respuesta['error']['code']}: {respuesta['error']['message']}")
        return

    resultado = respuesta.get("result", {})
    if "content" in resultado:
        texto = resultado["content"][0]["text"]
        marca = "[isError]" if resultado.get("isError") else "[ok]"
        lineas = texto.splitlines()
        if solo_primeras and len(lineas) > solo_primeras:
            texto = "\n".join(lineas[:solo_primeras]) + f"\n  ... ({len(lineas)} lineas en total)"
        print(f"  {marca}\n{texto}")
    else:
        print(json.dumps(resultado, ensure_ascii=False, indent=2)[:900])


def main():
    cliente = ClientePrueba()

    try:
        titulo("1. initialize (handshake)")
        r = cliente.enviar("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "cliente-de-prueba", "version": "1.0.0"},
        })
        info = r["result"]
        print(f"  Servidor  : {info['serverInfo']['name']} v{info['serverInfo']['version']}")
        print(f"  Protocolo : {info['protocolVersion']}")
        print(f"  Capacidades: {info['capabilities']}")

        titulo("2. notifications/initialized (notificacion, sin respuesta)")
        cliente.enviar("notifications/initialized", es_notificacion=True)
        print("  Enviada. El servidor no responde a las notificaciones.")

        titulo("3. tools/list")
        r = cliente.enviar("tools/list")
        for herramienta in r["result"]["tools"]:
            requeridos = herramienta["inputSchema"].get("required", [])
            print(f"  - {herramienta['name']:<28} requeridos: {requeridos}")

        titulo("4. tools/call -> buscar_productos (categoria='Abarrotes')")
        r = cliente.enviar("tools/call", {
            "name": "buscar_productos",
            "arguments": {"categoria": "Abarrotes", "limite": 3},
        })
        mostrar(r)
        # Los SKU se toman del resultado anterior, no se escriben a mano,
        # para que las pruebas sigan sirviendo si cambia el catalogo.
        productos = json.loads(r["result"]["content"][0]["text"])["productos"]
        sku_a, sku_b = productos[0]["sku"], productos[1]["sku"]

        titulo(f"5. tools/call -> consultar_stock ({sku_a})")
        r = cliente.enviar("tools/call", {
            "name": "consultar_stock",
            "arguments": {"sku": sku_a},
        })
        mostrar(r)

        titulo("6. tools/call -> crear_pedido (caso exitoso)")
        r = cliente.enviar("tools/call", {
            "name": "crear_pedido",
            "arguments": {
                "cliente_id": 3,
                "bodega_id": 1,
                "lineas": [
                    {"sku": sku_a, "cantidad": 10},
                    {"sku": sku_b, "cantidad": 5},
                ],
            },
        })
        mostrar(r)
        numero = json.loads(r["result"]["content"][0]["text"])["numero"]

        titulo(f"7. tools/call -> consultar_pedido ({numero})")
        r = cliente.enviar("tools/call", {
            "name": "consultar_pedido", "arguments": {"numero": numero}})
        mostrar(r, solo_primeras=18)

        titulo("8. tools/call -> historial_cliente (cliente 3)")
        r = cliente.enviar("tools/call", {
            "name": "historial_cliente", "arguments": {"cliente_id": 3, "limite": 3}})
        mostrar(r, solo_primeras=20)

        titulo("9. tools/call -> crear_pedido con stock insuficiente (error de negocio)")
        r = cliente.enviar("tools/call", {
            "name": "crear_pedido",
            "arguments": {"cliente_id": 3, "lineas": [{"sku": sku_a, "cantidad": 999999}]},
        })
        mostrar(r)

        titulo("10. tools/call -> SKU inexistente (error de negocio)")
        r = cliente.enviar("tools/call", {
            "name": "consultar_stock", "arguments": {"sku": "XX-9999"}})
        mostrar(r)

        titulo("11. tools/call -> herramienta inexistente (error de protocolo -32602)")
        r = cliente.enviar("tools/call", {"name": "herramienta_falsa", "arguments": {}})
        mostrar(r)

        titulo("12. Metodo no soportado (error de protocolo -32601)")
        r = cliente.enviar("resources/list")
        mostrar(r)

        titulo("13. ping")
        r = cliente.enviar("ping")
        print(f"  Respuesta: {r['result']}")

        titulo("14. tools/call -> convertir_total_a_dolares (fuente externa)")
        r = cliente.enviar("tools/call", {
            "name": "convertir_total_a_dolares", "arguments": {"numero": numero}})
        mostrar(r)

        print(f"\n{'=' * 66}\nRecorrido completo finalizado sin excepciones.\n{'=' * 66}")

    finally:
        cliente.cerrar()


if __name__ == "__main__":
    main()
