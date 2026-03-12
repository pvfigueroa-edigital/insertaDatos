import functions_framework

@functions_framework.http
def main(request):
    print("Ejecutando extractor AWS de prueba...")
    return ("Inventario AWS extraído", 200)