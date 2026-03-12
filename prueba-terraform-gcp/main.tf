#1. Configuración del Proyecto
terraform {
  backend "gcs" {
    bucket  = "entel-itam-tfstate-758290996725" # El nombre del bucket que acabamos de crear
    prefix  = "terraform/state"
  }
}

provider "google" {
  project = "entel-dashboards-reporterias"
  region  = "us-central1"
}

variable "google_client_id" {
  description = "ID de cliente de Google OAuth"
  type        = string
}

variable "google_client_secret" {
  description = "Secreto de cliente de Google OAuth"
  type        = string
  sensitive   = true # Esto evita que Terraform lo imprima en la terminal
}

variable "jwt_secret" {
  description = "Secreto para firmar los JWT"
  type        = string
  sensitive   = true
}


# Variable mágica para obtener el número de tu proyecto automáticamente
data "google_project" "project" {}

# ==========================================
# NUEVO: BÓVEDA DE SECRETOS (SECRET MANAGER)
# ==========================================
# A. Creamos el "casillero" en la bóveda
resource "google_secret_manager_secret" "db_url_secret" {
  secret_id = "database-url"
  replication {
    auto {}
  }
}

# B. Guardamos la contraseña real adentro (El Secreto)
resource "google_secret_manager_secret_version" "db_url_secret_version" {
  secret      = google_secret_manager_secret.db_url_secret.id
  secret_data = "postgresql://admin_itam:PasswordSeguro123!@10.55.0.3:5432/inventario_itam"
}

# C. Le damos la llave de la bóveda a Cloud Run
resource "google_secret_manager_secret_iam_member" "secret_access" {
  secret_id = google_secret_manager_secret.db_url_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${data.google_project.project.number}-compute@developer.gserviceaccount.com"
}

# ==========================================
# 2. Desplegar Cloud Run (Tu servicio principal)
# ==========================================
resource "google_cloud_run_v2_service" "frontend_app" {
  name     = "dashboard-itam-frontend"
  location = "us-central1"

  template {
    containers {
      image = "us-central1-docker.pkg.dev/entel-dashboards-reporterias/repo-dashboards/frontend:v1"

      # ¡AQUÍ ESTÁ LA MAGIA! Ya no hay texto plano, lee de la bóveda de Google
      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db_url_secret.secret_id
            version = "latest"
          }
        }
      }

      # Google OAuth
      env {
        name  = "GOOGLE_CLIENT_ID"
        value = var.google_client_id
      }
      env {
        name  = "GOOGLE_CLIENT_SECRET"
        value = var.google_client_secret
      }
      env {
        name  = "JWT_SECRET"
        value = var.jwt_secret
      }
      env {
        name  = "AUTH_ALLOWED_EMAIL_DOMAINS"
        value = "enteldigital.cl,entel.cl"
      }
      env {
        name  = "NEXTAUTH_URL"
        value = "https://dashboard-itam-frontend-758290996725.us-central1.run.app/" 
      }
    }

    vpc_access {
      network_interfaces {
        network    = "vpc-itam-dashboards"
        subnetwork = "subred-itam-us-central1"
      }
      egress = "PRIVATE_RANGES_ONLY"
    }
  }

  # Evita que Terraform sobreescriba las actualizaciones automáticas de tu GitHug
  lifecycle {
    ignore_changes = [
      client,
      client_version,
      template[0].labels,
      template[0].containers[0].env,
      template[0].containers[0].image
    ]
  }
}

# 3. Hacer que la URL sea pública
resource "google_cloud_run_service_iam_member" "public_access" {
  location = google_cloud_run_v2_service.frontend_app.location
  project  = google_cloud_run_v2_service.frontend_app.project
  service  = google_cloud_run_v2_service.frontend_app.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# 4. Mostrar la URL final en la terminal
output "url_del_servicio" {
  value = google_cloud_run_v2_service.frontend_app.uri
}

# 5. Repositorio en Artifact Registry
resource "google_artifact_registry_repository" "repo_frontend" {
  location      = "us-central1"
  repository_id = "repo-dashboards"
  description   = "Repositorio Docker para el frontend de Next.js"
  format        = "DOCKER"
}

# ==========================================
# 6. INFRAESTRUCTURA DE LA API (CLOUD FUNCTION)
# ==========================================

# A. El Conector VPC que creamos a mano
resource "google_vpc_access_connector" "conector_itam" {
  name          = "conector-itam"
  region        = "us-central1"
  network       = "vpc-itam-dashboards"
  ip_cidr_range = "10.8.0.0/28"
}

# B. Un "Bucket" (Disco Duro) para guardar el código fuente de tu función
resource "google_storage_bucket" "function_bucket" {
  name     = "bucket-codigo-api-itam-${data.google_project.project.number}"
  location = "US"
}

# C. Terraform comprime tu carpeta 'api-ingesta-itam' en un archivo .zip
data "archive_file" "api_zip" {
  type        = "zip"
  # Asegúrate de que esta ruta apunte a la carpeta donde creaste el index.js
  source_dir  = "${path.module}/../api-ingesta-itam" 
  output_path = "${path.module}/api-ingesta-itam.zip"
}

# D. Terraform sube el .zip al Bucket
resource "google_storage_bucket_object" "api_zip_object" {
  name   = "api-ingesta-itam-${data.archive_file.api_zip.output_md5}.zip"
  bucket = google_storage_bucket.function_bucket.name
  source = data.archive_file.api_zip.output_path
}

# E. Desplegar la Cloud Function (Gen 2) usando el .zip
resource "google_cloudfunctions2_function" "api_ingesta" {
  name        = "api-ingesta-itam"
  location    = "us-central1"
  description = "API para recibir inventario desde GitHub Actions"

  build_config {
    runtime     = "nodejs22"
    entry_point = "recibirInventario"
    source {
      storage_source {
        bucket = google_storage_bucket.function_bucket.name
        object = google_storage_bucket_object.api_zip_object.name
      }
    }
  }

  service_config {
    max_instance_count = 2
    available_memory   = "256M"
    timeout_seconds    = 60
    
    # Conexión a la red privada
    vpc_connector                 = google_vpc_access_connector.conector_itam.id
    vpc_connector_egress_settings = "PRIVATE_RANGES_ONLY"

    # Inyección de Secretos desde la Bóveda
    secret_environment_variables {
      key        = "DATABASE_URL"
      project_id = data.google_project.project.project_id
      secret     = google_secret_manager_secret.db_url_secret.secret_id
      version    = "latest"
    }
    
    # Aquí asumimos que el secreto de la API Key ya existe en GCP (lo creaste manual)
    secret_environment_variables {
      key        = "API_KEY_SECRETA"
      project_id = data.google_project.project.project_id
      secret     = "api-key-ingesta" 
      version    = "latest"
    }
  }
}

# ==========================================
# 7. ALMACENAMIENTO DE REPORTES (CLOUD STORAGE)
# ==========================================

# A. Crear el Bucket para los archivos Excel
resource "google_storage_bucket" "reportes_itam" {
  name          = "reportes-itam-${data.google_project.project.number}"
  location      = "US"
  force_destroy = true # Permite borrar el bucket aunque tenga archivos dentro

  # Configuración para que funcione como un servidor de archivos web
  uniform_bucket_level_access = true
  
  cors {
    origin          = ["*"]
    method          = ["GET", "HEAD", "PUT", "POST", "DELETE"]
    response_header = ["*"]
    max_age_seconds = 3600
  }
}

# B. Hacer que cualquier persona con el link pueda descargar el Excel
resource "google_storage_bucket_iam_member" "reportes_publicos" {
  bucket = google_storage_bucket.reportes_itam.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}

# C. Mostrar el nombre del bucket al terminar
output "nombre_bucket_reportes" {
  value = google_storage_bucket.reportes_itam.name
}

# ==========================================
# 8. BALANCEADOR DE CARGAS (LOAD BALANCER HTTPS)
# ==========================================

# A. Reservar una IP Pública Global estática
resource "google_compute_global_address" "lb_ip" {
  name = "ip-global-itam-frontend"
}

# B. Crear un "Grupo de Extremos de Red" (NEG) que apunte a tu Cloud Run
resource "google_compute_region_network_endpoint_group" "cloudrun_neg" {
  name                  = "neg-itam-frontend"
  network_endpoint_type = "SERVERLESS"
  region                = "us-central1"
  cloud_run {
    service = google_cloud_run_v2_service.frontend_app.name
  }
}

# C. Servicio Backend (Gestiona el tráfico hacia el NEG)
resource "google_compute_backend_service" "lb_backend" {
  name        = "backend-itam-frontend"
  protocol    = "HTTPS"
  port_name   = "http"
  timeout_sec = 30

  backend {
    group = google_compute_region_network_endpoint_group.cloudrun_neg.id
  }
}

# D. Mapa de URL (El "router" interno que dirige el tráfico al backend)
resource "google_compute_url_map" "lb_url_map" {
  name            = "urlmap-itam-frontend"
  default_service = google_compute_backend_service.lb_backend.id
}

# E. Certificado SSL Gestionado por Google
resource "google_compute_managed_ssl_certificate" "lb_cert" {
  name = "cert-itam-frontend"
  managed {
    # ⚠️ ATENCIÓN: Reemplaza esto con el dominio real que pedirás en Entel
    domains = ["inventario-cloud.entel.cl"] 
  }
}

# F. Proxy HTTPS (Une el Mapa de URL con el Certificado SSL)
resource "google_compute_target_https_proxy" "lb_https_proxy" {
  name             = "proxy-https-itam-frontend"
  url_map          = google_compute_url_map.lb_url_map.id
  ssl_certificates = [google_compute_managed_ssl_certificate.lb_cert.id]
}

# G. Regla de Reenvío (Abre la puerta 443 de internet y la conecta a tu IP)
resource "google_compute_global_forwarding_rule" "lb_forwarding_rule" {
  name                  = "forwarding-rule-itam-frontend"
  target                = google_compute_target_https_proxy.lb_https_proxy.id
  port_range            = "443"
  ip_address            = google_compute_global_address.lb_ip.id
}

# H. Output: Mostrar la IP Pública al terminar
output "ip_del_balanceador" {
  value       = google_compute_global_address.lb_ip.address
  description = "Entrega esta IP al equipo DNS de Entel para que apunten el dominio."
}

# ==========================================
# 9. RECOLECTORES MULTI-CLOUD (Bucle for_each)
# ==========================================

# 1. Definimos las Nubes y los Clientes

locals {
  # A. Lista de nubes (Para crear 1 Cloud Function por cada una)
  nubes = toset(["azure", "gcp", "huawei"])

  # B. Lista de Trabajos (Para crear 1 Cloud Scheduler por cliente/nube)
  trabajos_extraccion = {
    "innergy-azure" = {
      nube    = "azure"
      cliente = "INNERGY"
      cron    = "0 8 * * *"
    },
    "gassur-azure" = {
      nube    = "azure"
      cliente = "GASSUR"
      cron    = "02 8 * * *"
    },
    "metrogas-azure" = {
      nube    = "azure"
      cliente = "METROGAS"
      cron    = "04 8 * * *"
    },
    "copec-gcp" = {
      nube    = "gcp"
      cliente = "COPEC"
      cron    = "06 8 * * *"
    },
    "capel-gcp" = {
      nube    = "gcp"
      cliente = "ENTEL CAPEL"
      cron    = "08 8 * * *"
    },
    "orizon-gcp" = {
      nube    = "huawei"
      cliente = "ORIZON"
      cron    = "10 8 * * *"
    },
    "capredena-gcp" = {
      nube    = "gcp"
      cliente = "CAPREDENA"
      cron    = "12 8 * * *"
    },

    "ine-gcp" = {
      nube    = "gcp"
      cliente = "INE"
      cron    = "15 8 * * *"
    }
  }
}

# 2. Cuentas de Servicio Compartidas (Una para todos los extractores, otra para todos los schedulers)
resource "google_service_account" "sa_extractor" {
  account_id   = "sa-extractor-itam"
  display_name = "SA para las Cloud Functions de Extraccion"
}

resource "google_service_account" "sa_scheduler" {
  account_id   = "sa-scheduler-itam"
  display_name = "SA para los Cloud Schedulers"
}

resource "google_storage_bucket_iam_member" "extractor_bucket_access" {
  bucket = google_storage_bucket.reportes_itam.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.sa_extractor.email}"
}

# 2.5 Buscar el secreto de la API Key que creaste manualmente
data "google_secret_manager_secret" "api_key_secreta" {
  secret_id = "api-key-ingesta"
}

# 2.6 Darle permiso al Robot Extractor para leer esta API Key
resource "google_secret_manager_secret_iam_member" "extractor_secret_access" {
  secret_id = data.google_secret_manager_secret.api_key_secreta.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.sa_extractor.email}"
}

# 3. Comprimir cada carpeta dinámicamente
data "archive_file" "extractor_zips" {
  for_each    = local.nubes
  type        = "zip"
  # Busca carpetas llamadas: extractor-azure, extractor-aws, etc.
  source_dir  = "${path.module}/../extractor-${each.key}" 
  output_path = "${path.module}/extractor-${each.key}.zip"
}

# 4. Subir cada ZIP al Bucket
resource "google_storage_bucket_object" "extractor_zip_objects" {
  for_each = local.nubes
  name     = "extractor-${each.key}-${data.archive_file.extractor_zips[each.key].output_md5}.zip"
  bucket   = google_storage_bucket.function_bucket.name
  source   = data.archive_file.extractor_zips[each.key].output_path
}

# 5. Crear 1 Cloud Function por cada Nube
resource "google_cloudfunctions2_function" "extractores" {
  for_each    = local.nubes
  name        = "extractor-${each.key}"
  location    = "us-central1"
  description = "Extrae datos de ${each.key} y los manda a la API"

  build_config {
    runtime     = "python310"
    # Asegúrate de que todos tus scripts main.py tengan una función llamada "ejecutar_recoleccion"
    entry_point = "ejecutar_recoleccion" 
    source {
      storage_source {
        bucket = google_storage_bucket.function_bucket.name
        object = google_storage_bucket_object.extractor_zip_objects[each.key].name
      }
    }
  }

  service_config {
    max_instance_count    = 1
    available_memory      = "512M"
    timeout_seconds       = 540 # 9 minutos
    service_account_email = google_service_account.sa_extractor.email
    
    environment_variables = {
      API_INGESTA_URL = google_cloudfunctions2_function.api_ingesta.service_config[0].uri
      BUCKET_REPORTES = google_storage_bucket.reportes_itam.name
      NUBE_OBJETIVO   = each.key # Para que el script sepa en qué nube está corriendo
    }

    secret_environment_variables {
      key        = "API_KEY_SECRETA"
      project_id = data.google_project.project.project_id
      secret     = "api-key-ingesta" 
      version    = "latest"
    }
  }
}

# 6. Permisos para que el Scheduler invoque a las funciones
resource "google_cloud_run_service_iam_member" "scheduler_invokers" {
  for_each = local.nubes
  location = google_cloudfunctions2_function.extractores[each.key].location
  service  = google_cloudfunctions2_function.extractores[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.sa_scheduler.email}"
}

# 7. Crear los Cron Jobs basados en la lista de trabajos
resource "google_cloud_scheduler_job" "cron_extractores" {
  for_each         = local.trabajos_extraccion
  
  # ¡LA MAGIA AQUÍ!: Reemplazamos los espacios en blanco " " por guiones "-" solo para el nombre del recurso
  name             = "cron-${each.value.nube}-${replace(lower(each.value.cliente), " ", "-")}"
  
  description      = "Extraccion de ${each.value.nube} para el cliente ${each.value.cliente}"
  schedule         = each.value.cron
  time_zone        = "America/Santiago"
  region           = "us-central1"

  http_target {
    http_method = "POST"
    uri         = google_cloudfunctions2_function.extractores[each.value.nube].service_config[0].uri
    
    # El payload sigue mandando el nombre original con espacio, lo cual es correcto
    body        = base64encode(jsonencode({
      "cliente": each.value.cliente
    }))

    headers = {
      "Content-Type" = "application/json"
    }

    oidc_token {
      service_account_email = google_service_account.sa_scheduler.email
    }
  }
}
