# 🚀 Arquitectura ITAM Multi-Cloud Serverless - Entel

Este proyecto utiliza un modelo GitOps (Infraestructura como Código + CI/CD) para gestionar despliegues en múltiples nubes (GCP, AWS, Azure, Huawei Cloud).

> ⚠️ **REGLA DE ORO:** Absolutamente TODA la infraestructura (Cloud Functions, Cloud Schedulers, Cloud Run) se maneja a través de este repositorio de GitHub. **PROHIBIDO** realizar cambios manuales directamente en las consolas de los proveedores Cloud, ya que Terraform sobrescribirá o eliminará cualquier cambio no documentado aquí.

---

## 📘 Escenario 1: Agregar un Nuevo Cliente (Nube Existente)

Imagina que Entel firma con "Cliente Alpha" para GCP. Como la infraestructura base de esta nube ya existe y la Cloud Function está corriendo, el proceso es ligero: no se toca código Python ni se reinventa infraestructura, solo se conecta un nuevo temporizador (cron).

### Paso 1: Actualizar el Secreto Real (La Bóveda)

Debe hacerse primero para que el despliegue no falle por falta de credenciales.

1. En tu entorno local seguro, abre tu archivo de credenciales de GCP (o de la nube a la cual insertar el nuevo cliente) `config.json` donde están todas las credenciales verdaderas de los clientes.
2. Agrega el bloque del nuevo cliente manteniendo la estructura y copia todo el texto.
```
    "CLIENTE ALPHA": {
        "auth_type": "impersonate",
        "target_sa": "-Service Account a Impersonar-",
        "projects": [
            {"project_id": "project-id", "name": "name(project_id)"},
            {"project_id": "project_id", "name": "name(project_id)"},
            {"project_id": "project_id", "name": "name(project_id)"}
        ]
    }
```
3. En GitHub, navega a **Settings > Secrets and variables > Actions**.
4. Edita el secreto (`GCP_CONFIG` / `AZURE_CONFIG` / `HUAWEI_CONFIG`) y pega el JSON completo.
5. Actualiza el secreto **Update Secret**.

> ⚠️ Se debe pegar el JSON completo, con toda la configuración de los clientes anteriores, más las nuevas a agregar, ya que se reescribe todo el secreto.


### Paso 2: Actualizar Terraform (El Reloj)

Abre `prueba-terraform-gcp/main.tf`. Busca el mapa `trabajos_extraccion` y agrega el nuevo cliente y su cronograma:

```terraform
"alpha-gcp" = {
  nube    = "gcp"
  cliente = "CLIENTE ALPHA"
  cron    = "30 8 * * *" # Se ejecuta a las 8:30 AM
}
```

### Paso 3: Documentar el Formato (Opcional - Recomendado)

Abre `extractor-gcp/config.example.json` y agrega al cliente con datos falsos. Esto sirve como documentación viva para que el equipo conozca la estructura esperada sin exponer secretos reales.

### Paso 4: Desplegar

Haz commit y push de los cambios.

> ✨ **Resultado:** Terraform no apagará ni recreará la función de GCP existente. Únicamente aprovisionará y conectará un nuevo Cloud Scheduler de forma no destructiva.

### Paso 5: Ejecutar Cron en Proyecto GCP

Navega a la consola de Google Cloud, ir a Cloud Scheduler, aparecerá el cron con el nombre del nuevo cliente `cron-gcp-alpha`, para ejecutarlo, `Actions -> Force Run`

---

## 🔄 Escenario 2: Cambiar la Autenticación de un Cliente Existente

Supongamos que "Cliente Alpha" de GCP usaba llaves JSON (`"auth_type": "json"`) y las normativas de ciberseguridad ahora exigen el uso de Impersonation (`"auth_type": "impersonate"`). No necesitas modificar código en Terraform ni en Python.

### Paso 1: Modificar el Secreto en GitHub

1. Modifica el archivo de credenciales de GCP (config.json)
**Configuración Anterior (JSON Keys):**

```json
"CLIENTE ALPHA": {
  "auth_type": "json",
  "json_keys": {
    "type": "service_account",
    "project_id": "proyecto-alpha",
    "private_key": "-----BEGIN PRIVATE KEY-----\nMIIE..."
  }
}
```
2. Agrega el bloque del nuevo cliente manteniendo la estructura y copia todo el texto.
**Nueva Configuración (Impersonation):**

```json
"CLIENTE ALPHA": {
  "auth_type": "impersonate",
  "target_sa": "sa-lectura@proyecto-alpha.iam.gserviceaccount.com",  <-- Service Account a impersonar
  "projects": [
    {"project_id": "project_id", "name": "project_id"},
    {"project_id": "project_id", "name": "project_id"},
    {"project_id": "project_id", "name": "project_id"},
    {"project_id": "project_id", "name": "project_id"}
  ]
}
```

3. En GitHub, navega a **Settings > Secrets and variables > Actions**.
4. Edita el secreto `GCP_CONFIG` y pega el JSON completo.
5. Actualiza el secreto **Update Secret**.




### Paso 2: Forzar la Actualización del Pipeline

GitHub Actions no se dispara automáticamente al cambiar un secreto. Para inyectar la nueva configuración tienes dos opciones:

- **Opción A:** Modifica el archivo config.example.json, cambiando la estrucutra del cliente y haz push.
- **Opción B:** Haz un commit con un cambio cosmético en el código (ej. un salto de línea en este README.md) y haz push.
- **Opción C:** En la pestaña **Actions**, selecciona el último despliegue exitoso y ejecuta **"Re-run all jobs"**.


> ✨ **Resultado:** El pipeline inyectará el nuevo JSON efímero, Terraform actualizará la función y el cliente comenzará a usar Impersonation en su próximo ciclo.

---

## 🏗️ Escenario 3: Integrar una Nueva Nube (ej. AWS)

Este es el proceso completo para añadir un proveedor Cloud desde cero, incorporando las lecciones aprendidas y resoluciones de errores críticos en producción.

### Paso 1: Crear el Código de Extracción

Crea la carpeta `extractor-aws/` con los siguientes 3 archivos:

#### 1. `main.py`

> 🐛 **Error Común:** Usar `def main(request):` hace que el despliegue falle si Terraform espera otro nombre.  
> 💡 **Solución:** El nombre de la función principal debe coincidir exactamente con el `entry_point` definido en Terraform.

```python
import requests # Importa todas las librerías necesarias

# IMPORTANTE: Coincidir con el entry_point de Terraform
def ejecutar_recoleccion(request):
    print("Iniciando extracción AWS...")
    return ("OK", 200)
```

#### 2. `requirements.txt`

> 🐛 **Error Común:** Dejar el archivo vacío cuando el código usa librerías externas (ej. `requests`). El contenedor fallará en el puerto 8080 con `ModuleNotFoundError`.  
> 💡 **Solución:** Lista siempre las dependencias reales. Google Gen2 inyecta automáticamente `functions-framework` si el `entry_point` es correcto, por lo que no es necesario listarlo aquí.

```
requests==2.31.0
```

#### 3. `config.json`

Crea localmente el archivo de configuracion de los clientes asociados a la nueva nube.

#### 4. `config.example.json`

Crea una estructura de prueba con datos simulados que documente el formato de credenciales esperado para esta nube.

### Paso 2: Crear el Secreto de la Nueva Nube

En GitHub Secrets, crea un nuevo secreto llamado `AWS_CONFIG` y pega el JSON con las credenciales reales de los clientes de AWS (contenido del config.json local).

### Paso 3: Actualizar el Pipeline CI/CD

Abre `.github/workflows/terraform-deploy.yml` e instruye al robot para inyectar el nuevo secreto.

> 🐛 **Error Común:** Si eliminas una nube en el futuro y olvidas quitar su línea aquí, el pipeline explotará con el error `No such file or directory`.

```yaml
    - name: Inyectar Secretos
      env:
        SECRET_AWS: ${{ secrets.AWS_CONFIG }}  # <- NUEVO
      run: |
        echo "$SECRET_AWS" > extractor-aws/config.json  # <- NUEVO
```

### Paso 4: Declarar la Nueva Nube en Terraform

Abre `prueba-terraform-gcp/main.tf` y realiza dos adiciones:

**1. Actualiza la variable de nubes soportadas:**

```terraform
nubes = toset(["azure", "gcp", "huawei", "aws"])  # <- Agregar "aws"
```

**2. Registra el primer cliente en el mapa de trabajos:**

```terraform
"cliente-aws" = { nube = "aws", cliente = "MI CLIENTE AWS", cron = "0 10 * * *" }
```

### Paso 5: Verificar Permisos IAM (Prevención del Error 403)

> 🐛 **Error Común:** Terraform crea la función exitosamente pero falla en el último paso con `403 Permission denied on resource... run.services.setIamPolicy`.  
> 💡 **Solución:** La Cuenta de Servicio (Service Account) que utiliza GitHub Actions para desplegar necesita tener asignado el rol de **Administrador de Cloud Run** (`roles/run.admin`) en GCP. Esto le permite otorgar permisos al Cloud Scheduler para invocar la nueva función.

### Paso 6: Desplegar

Haz commit de la nueva carpeta `extractor-aws/`, el archivo `.github/` y `prueba-terraform-gcp/`. Al hacer push, Terraform empaquetará el código, aprovisionará la función aislada y activará su respectivo Cloud Scheduler.

---

## 🔒 Arquitectura de Seguridad (DevSecOps)

| Componente | Mecanismo de Protección |
|---|---|
| **Estado Remoto** | El cerebro de Terraform (`terraform.tfstate`) se almacena de forma segura y centralizada en un Bucket de GCS dentro del proyecto de despliegue. |
| **Inyección Efímera** | Ninguna credencial o llave privada existe en este repositorio. Los archivos `config.json` son reconstruidos dinámicamente en memoria por GitHub Actions usando variables de entorno (`$SECRET_NAME`) y se destruyen al finalizar el pipeline. |
| **Prevención de Fugas** | El archivo `.gitignore` está configurado para bloquear preventivamente cualquier intento de subir archivos `.json` locales que contengan credenciales reales. |
