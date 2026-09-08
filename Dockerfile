# ---------------------------------------------------------------------
# Imagen del servidor MCP remoto - Distribuidora El Quetzal
# Proyecto 1 - CC3067 Redes - Universidad del Valle de Guatemala
#
# Sin dependencias externas: solo se necesita Python. La base de datos se
# genera durante la construccion de la imagen, de modo que el contenedor
# arranca listo para atender peticiones.
# ---------------------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app

# Codigo del servidor. tools.py y server.py se reusan sin cambios; lo
# unico distinto respecto al servidor local es el transporte HTTP.
COPY server.py server_http.py tools.py ./
COPY db/ ./db/

# La base sintetica se construye en tiempo de imagen (semilla fija).
RUN python db/seed.py

# Cloud Run inyecta el puerto real en la variable PORT.
ENV PORT=8080
EXPOSE 8080

# Salida sin buffer para que la bitacora llegue completa a Cloud Logging.
ENV PYTHONUNBUFFERED=1

CMD ["python", "server_http.py"]
