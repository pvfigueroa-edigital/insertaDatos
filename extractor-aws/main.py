import functions_framework
import requests

@functions_framework.http
def ejecutar_recoleccion(request):
    print("Ejecutando extractor AWS de prueba...")
    return ("Inventario AWS extraído", 200)