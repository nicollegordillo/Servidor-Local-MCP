-- =====================================================================
-- Esquema de la base de datos - Distribuidora El Quetzal
-- Proyecto 1 - CC3067 Redes - Universidad del Valle de Guatemala
--
-- Modelo relacional que da soporte al servidor MCP de inventario y
-- pedidos. Todos los datos son sinteticos (ver seed.py).
-- =====================================================================

PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS pedido_lineas;
DROP TABLE IF EXISTS pedidos;
DROP TABLE IF EXISTS existencias;
DROP TABLE IF EXISTS productos;
DROP TABLE IF EXISTS bodegas;
DROP TABLE IF EXISTS clientes;

-- ---------------------------------------------------------------------
-- Catalogo de productos
-- ---------------------------------------------------------------------
CREATE TABLE productos (
    sku            TEXT PRIMARY KEY,
    nombre         TEXT    NOT NULL,
    categoria      TEXT    NOT NULL,
    precio         REAL    NOT NULL CHECK (precio > 0),
    unidad_medida  TEXT    NOT NULL DEFAULT 'unidad',
    activo         INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0, 1))
);

CREATE INDEX idx_productos_categoria ON productos (categoria);
CREATE INDEX idx_productos_nombre    ON productos (nombre);

-- ---------------------------------------------------------------------
-- Bodegas fisicas de la distribuidora
-- ---------------------------------------------------------------------
CREATE TABLE bodegas (
    id         INTEGER PRIMARY KEY,
    nombre     TEXT NOT NULL UNIQUE,
    ubicacion  TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- Existencias: cantidad de cada SKU en cada bodega
-- ---------------------------------------------------------------------
CREATE TABLE existencias (
    sku        TEXT    NOT NULL,
    bodega_id  INTEGER NOT NULL,
    cantidad   INTEGER NOT NULL CHECK (cantidad >= 0),
    PRIMARY KEY (sku, bodega_id),
    FOREIGN KEY (sku)       REFERENCES productos (sku),
    FOREIGN KEY (bodega_id) REFERENCES bodegas (id)
);

-- ---------------------------------------------------------------------
-- Clientes minoristas
-- ---------------------------------------------------------------------
CREATE TABLE clientes (
    id         INTEGER PRIMARY KEY,
    nombre     TEXT NOT NULL,
    nit        TEXT NOT NULL UNIQUE,
    municipio  TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- Pedidos (encabezado)
-- ---------------------------------------------------------------------
CREATE TABLE pedidos (
    numero      TEXT PRIMARY KEY,
    cliente_id  INTEGER NOT NULL,
    bodega_id   INTEGER NOT NULL,
    fecha       TEXT    NOT NULL,
    estado      TEXT    NOT NULL CHECK (estado IN ('registrado', 'en_preparacion', 'despachado', 'entregado', 'cancelado')),
    total       REAL    NOT NULL CHECK (total >= 0),
    FOREIGN KEY (cliente_id) REFERENCES clientes (id),
    FOREIGN KEY (bodega_id)  REFERENCES bodegas (id)
);

CREATE INDEX idx_pedidos_cliente ON pedidos (cliente_id);

-- ---------------------------------------------------------------------
-- Pedidos (detalle)
-- ---------------------------------------------------------------------
CREATE TABLE pedido_lineas (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pedido_numero   TEXT    NOT NULL,
    sku             TEXT    NOT NULL,
    cantidad        INTEGER NOT NULL CHECK (cantidad > 0),
    precio_unitario REAL    NOT NULL,
    subtotal        REAL    NOT NULL,
    FOREIGN KEY (pedido_numero) REFERENCES pedidos (numero),
    FOREIGN KEY (sku)           REFERENCES productos (sku)
);

CREATE INDEX idx_lineas_pedido ON pedido_lineas (pedido_numero);
