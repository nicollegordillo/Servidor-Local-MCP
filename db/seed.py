#!/usr/bin/env python3
"""
Generador de datos sinteticos para la base de datos de la distribuidora.

Proyecto 1 - CC3067 Redes - Universidad del Valle de Guatemala

Crea el archivo SQLite `distribuidora.db` a partir de `schema.sql` y lo
puebla con un conjunto de datos ficticios pero verosimiles:
    - ~100 productos en 8 categorias
    - 3 bodegas
    - 20 clientes minoristas
    - ~40 pedidos historicos con su detalle

La semilla del generador es fija (SEED = 3067), de modo que la base
resultante es identica en cualquier maquina y el escenario del proyecto
es completamente reproducible.

Uso:
    python db/seed.py
    python db/seed.py --db ruta/personalizada.db
"""

import argparse
import os
import random
import sqlite3
from datetime import datetime, timedelta

SEED = 3067
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "distribuidora.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

# ---------------------------------------------------------------------
# Insumos para generar el catalogo
# ---------------------------------------------------------------------

CATEGORIAS = {
    "Abarrotes": (
        ["Arroz", "Frijol negro", "Azucar blanca", "Sal refinada", "Harina de maiz",
         "Harina de trigo", "Aceite vegetal", "Pasta corta", "Pasta larga", "Lenteja",
         "Avena", "Incaparina"],
        ["libra", "bolsa", "quintal", "caja"],
        (8.0, 145.0),
    ),
    "Bebidas": (
        ["Gaseosa cola", "Gaseosa naranja", "Agua pura", "Jugo de naranja",
         "Jugo de manzana", "Bebida isotonica", "Te frio", "Nectar de durazno",
         "Agua mineral"],
        ["botella", "caja", "fardo"],
        (5.0, 120.0),
    ),
    "Lacteos": (
        ["Leche entera", "Leche deslactosada", "Queso fresco", "Crema", "Yogurt natural",
         "Yogurt de fresa", "Mantequilla", "Queso seco"],
        ["litro", "libra", "unidad", "caja"],
        (12.0, 95.0),
    ),
    "Limpieza": (
        ["Detergente en polvo", "Jabon de barra", "Cloro", "Desinfectante",
         "Limpiavidrios", "Suavizante", "Lavaplatos", "Esponja"],
        ["unidad", "galon", "bolsa", "caja"],
        (6.0, 110.0),
    ),
    "Higiene personal": (
        ["Papel higienico", "Shampoo", "Jabon de tocador", "Pasta dental",
         "Desodorante", "Cepillo dental", "Toallas humedas", "Rastrillo"],
        ["paquete", "unidad", "caja"],
        (7.0, 85.0),
    ),
    "Snacks": (
        ["Papas fritas", "Tortillitas de maiz", "Galleta de vainilla", "Galleta salada",
         "Mani salado", "Churros de queso", "Barra de cereal", "Chocolate en barra"],
        ["bolsa", "caja", "unidad"],
        (3.0, 75.0),
    ),
    "Enlatados": (
        ["Atun en agua", "Atun en aceite", "Sardina en tomate", "Frijol enlatado",
         "Maiz dulce", "Salsa de tomate", "Chile jalapeno", "Duraznos en almibar"],
        ["lata", "caja"],
        (6.0, 130.0),
    ),
    "Desechables": (
        ["Vaso desechable", "Plato desechable", "Bolsa plastica", "Servilleta",
         "Cuchara desechable", "Papel aluminio", "Film plastico"],
        ["paquete", "rollo", "caja"],
        (5.0, 68.0),
    ),
}

PRESENTACIONES = ["500 g", "1 kg", "2 kg", "250 ml", "600 ml", "1 L", "2 L",
                  "pack 6", "pack 12", "pack 24", "unidad"]

MARCAS = ["Del Valle", "La Ceiba", "Xelaju", "Tikal", "Atitlan", "San Marcos",
          "Quetzal", "Izabal", "Peten", "Antigua"]

BODEGAS = [
    (1, "Bodega Central",    "Zona 12, Ciudad de Guatemala"),
    (2, "Bodega Occidente",  "Quetzaltenango"),
    (3, "Bodega Oriente",    "Chiquimula"),
]

NOMBRES_NEGOCIO = [
    "Tienda La Bendicion", "Abarroteria El Ahorro", "Super Mini Sarita",
    "Tienda Don Chepe", "Despensa Familiar Nueva", "Mercadito La Esperanza",
    "Tienda El Progreso", "Abarroteria San Jose", "Super Tienda Maya",
    "Tienda La Economica", "Minimercado El Sol", "Tienda Santa Rosa",
    "Abarroteria El Buen Precio", "Tienda Los Cipreses", "Super El Trebol",
    "Tienda La Amistad", "Comercial Las Flores", "Tienda El Rosario",
    "Abarroteria La Union", "Tienda El Faro",
]

MUNICIPIOS = [
    "Guatemala", "Mixco", "Villa Nueva", "Quetzaltenango", "Escuintla",
    "Chimaltenango", "Antigua Guatemala", "Coban", "Chiquimula", "Jalapa",
]

ESTADOS = ["registrado", "en_preparacion", "despachado", "entregado"]


def generar_productos(rng):
    """Construye el catalogo de productos con SKUs deterministas."""
    productos = []
    contador = 1
    for categoria, (bases, unidades, (pmin, pmax)) in CATEGORIAS.items():
        palabras = categoria.split()
        if len(palabras) >= 2:                      # "Higiene personal" -> HP
            prefijo = (palabras[0][0] + palabras[1][0]).upper()
        else:                                       # "Abarrotes" -> AB
            prefijo = palabras[0][:2].upper()
        for base in bases:
            n_variantes = rng.randint(1, 2)
            for _ in range(n_variantes):
                marca = rng.choice(MARCAS)
                presentacion = rng.choice(PRESENTACIONES)
                nombre = f"{base} {marca} {presentacion}"
                precio = round(rng.uniform(pmin, pmax), 2)
                unidad = rng.choice(unidades)
                sku = f"{prefijo}-{contador:04d}"
                productos.append((sku, nombre, categoria, precio, unidad, 1))
                contador += 1
    return productos


def generar_existencias(rng, productos):
    """Asigna existencias por bodega. Algunos SKU quedan en cero a proposito
    para poder demostrar el manejo de faltantes en crear_pedido."""
    existencias = []
    for i, (sku, *_rest) in enumerate(productos):
        for bodega_id, _, _ in BODEGAS:
            if i % 17 == 0 and bodega_id != 1:
                cantidad = 0            # faltante intencional
            elif bodega_id == 1:
                cantidad = rng.randint(40, 500)
            else:
                cantidad = rng.randint(0, 180)
            existencias.append((sku, bodega_id, cantidad))
    return existencias


def generar_clientes(rng):
    clientes = []
    for i, nombre in enumerate(NOMBRES_NEGOCIO, start=1):
        nit = f"{rng.randint(1000000, 9999999)}-{rng.randint(0, 9)}"
        clientes.append((i, nombre, nit, rng.choice(MUNICIPIOS)))
    return clientes


def generar_pedidos(rng, productos, clientes, n_pedidos=40):
    """Genera pedidos historicos con fechas repartidas en los ultimos 120 dias."""
    pedidos, lineas = [], []
    hoy = datetime.now()
    for i in range(1, n_pedidos + 1):
        numero = f"PED-2026-{i:04d}"
        cliente_id = rng.choice(clientes)[0]
        bodega_id = rng.choice([b[0] for b in BODEGAS])
        fecha = (hoy - timedelta(days=rng.randint(1, 120),
                                 hours=rng.randint(0, 23))).strftime("%Y-%m-%d %H:%M:%S")
        estado = rng.choice(ESTADOS)

        total = 0.0
        seleccion = rng.sample(productos, rng.randint(1, 5))
        for prod in seleccion:
            sku, _nombre, _cat, precio, _um, _act = prod
            cantidad = rng.randint(1, 25)
            subtotal = round(precio * cantidad, 2)
            total += subtotal
            lineas.append((numero, sku, cantidad, precio, subtotal))

        pedidos.append((numero, cliente_id, bodega_id, fecha, estado, round(total, 2)))
    return pedidos, lineas


def main():
    parser = argparse.ArgumentParser(description="Genera la base de datos sintetica.")
    parser.add_argument("--db", default=DEFAULT_DB, help="Ruta del archivo SQLite a crear.")
    args = parser.parse_args()

    rng = random.Random(SEED)

    if os.path.exists(args.db):
        os.remove(args.db)

    conn = sqlite3.connect(args.db)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())

    productos = generar_productos(rng)
    existencias = generar_existencias(rng, productos)
    clientes = generar_clientes(rng)
    pedidos, lineas = generar_pedidos(rng, productos, clientes)

    conn.executemany("INSERT INTO productos VALUES (?, ?, ?, ?, ?, ?)", productos)
    conn.executemany("INSERT INTO bodegas VALUES (?, ?, ?)", BODEGAS)
    conn.executemany("INSERT INTO existencias VALUES (?, ?, ?)", existencias)
    conn.executemany("INSERT INTO clientes VALUES (?, ?, ?, ?)", clientes)
    conn.executemany("INSERT INTO pedidos VALUES (?, ?, ?, ?, ?, ?)", pedidos)
    conn.executemany(
        "INSERT INTO pedido_lineas (pedido_numero, sku, cantidad, precio_unitario, subtotal) "
        "VALUES (?, ?, ?, ?, ?)", lineas)

    conn.commit()
    conn.close()

    print(f"Base de datos creada en: {args.db}")
    print(f"  productos     : {len(productos)}")
    print(f"  bodegas       : {len(BODEGAS)}")
    print(f"  existencias   : {len(existencias)}")
    print(f"  clientes      : {len(clientes)}")
    print(f"  pedidos       : {len(pedidos)}")
    print(f"  lineas pedido : {len(lineas)}")


if __name__ == "__main__":
    main()
