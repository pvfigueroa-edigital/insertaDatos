import json
import os
import time
import xlsxwriter
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List

from google.oauth2 import service_account
from google.auth import impersonated_credentials, default
from google.cloud import compute_v1
from google.cloud import billing_v1
from google.cloud import storage as gcs_storage

# --- ZONA HORARIA CHILE ---
CL_TZ = timezone(timedelta(hours=-3))

# --- FABRICA DE CREDENCIALES GCP ---
def obtener_credenciales_gcp(config_cliente: Dict[str, Any]):
    """
    Lee la configuracion del cliente y devuelve las credenciales adecuadas,
    ya sea usando un JSON tradicional o Impersonation (Keyless).
    """
    tipo_auth = config_cliente.get("auth_type")
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]

    if tipo_auth == "impersonate":
        target_sa = config_cliente.get("target_sa")
        if not target_sa:
            raise ValueError("Falta 'target_sa' para auth_type=impersonate")

        print(f"   Usando Impersonation para la SA: {target_sa}")
        credenciales_base, _ = default(scopes=scopes)
        credenciales_cliente = impersonated_credentials.Credentials(
            source_credentials=credenciales_base,
            target_principal=target_sa,
            target_scopes=scopes
        )
        return credenciales_cliente

    if tipo_auth == "json":
        print("   Usando llaves JSON estaticas...")
        llaves = config_cliente.get("json_keys")
        if not llaves:
            raise ValueError("Falta 'json_keys' para auth_type=json")
        credenciales_cliente = service_account.Credentials.from_service_account_info(llaves, scopes=scopes)
        return credenciales_cliente

    raise ValueError(f"Tipo de autenticacion desconocido: {tipo_auth}")


# --- SERVICIO DE PRECIOS GCP ---
class GCPPricingService:
    def __init__(self, credentials=None):
        self.credentials = credentials
        self.sku_cache = {}
        self.disk_map = {
            "pd-standard": "Standard",
            "pd-ssd": "SSD",
            "pd-balanced": "Balanced",
            "pd-extreme": "Extreme"
        }

    def _load_prices_for_region(self, region):
        if region in self.sku_cache:
            return
        try:
            if self.credentials:
                client = billing_v1.CloudCatalogClient(credentials=self.credentials)
            else:
                client = billing_v1.CloudCatalogClient()
            service_name = "services/6F81-5844-456A"
            request = billing_v1.ListSkusRequest(parent=service_name)
            page_result = client.list_skus(request=request)

            region_prices = {}
            for sku in page_result:
                if region not in sku.service_regions:
                    continue
                desc = sku.description.lower()
                if "pd capacity" in desc or "balanced pd capacity" in desc or "ssd pd capacity" in desc:
                    if sku.pricing_info and sku.pricing_info[0].pricing_expression.tiered_rates:
                        rate = sku.pricing_info[0].pricing_expression.tiered_rates[0]
                        price_usd = (rate.unit_price.units) + (rate.unit_price.nanos / 1e9)
                        if "ssd pd capacity" in desc:
                            region_prices["pd-ssd"] = price_usd
                        elif "balanced pd capacity" in desc:
                            region_prices["pd-balanced"] = price_usd
                        elif "extreme pd capacity" in desc:
                            region_prices["pd-extreme"] = price_usd
                        elif "standard pd capacity" in desc:
                            region_prices["pd-standard"] = price_usd

            self.sku_cache[region] = region_prices
        except Exception as e:
            print(f"   Error cargando precios GCP: {e}")
            self.sku_cache[region] = {}

    def get_disk_price(self, region, disk_type_url, size_gb):
        try:
            short_type = disk_type_url.split('/')[-1]
            region_base = "-".join(region.split('-')[:-1])
            self._load_prices_for_region(region_base)
            prices = self.sku_cache.get(region_base, {})
            unit_price = prices.get(short_type, 0.0)
            if unit_price == 0:
                if "ssd" in short_type:
                    unit_price = 0.17
                elif "balanced" in short_type:
                    unit_price = 0.10
                else:
                    unit_price = 0.04
            return unit_price * size_gb
        except Exception:
            return 0.0


class GCPCloudReporter:
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.config = self._load_config()
        self.pricing_service = None

        self.db_vms = []
        self.db_disks = []
        self.db_compromisos = []

    def _load_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            raise FileNotFoundError(f"No se encontro el archivo: {self.config_path}")
        with open(self.config_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def activar_credencial_json(self, sa_key_dict: Dict[str, Any]) -> str:
        temp_file = "/tmp/temp_current_key.json"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(sa_key_dict, f)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = temp_file
        return temp_file

    def _normalize_projects(self, config_cliente: Dict[str, Any]) -> List[Dict[str, Any]]:
        projects = []

        if isinstance(config_cliente.get("projects"), list):
            projects = config_cliente["projects"]
        elif isinstance(config_cliente.get("proyectos"), list):
            # Compatibilidad con configs tipo copec/capel
            for p in config_cliente["proyectos"]:
                projects.append({
                    "name": p.get("nombre_reporte", p.get("project_id")),
                    "project_id": p.get("project_id"),
                    "auth_type": "json",
                    "json_keys": p.get("sa_key")
                })
        else:
            if config_cliente.get("project_id"):
                projects = [config_cliente]

        normalized = []
        for p in projects:
            if not isinstance(p, dict):
                continue
            project_id = p.get("project_id") or config_cliente.get("project_id")
            if not project_id:
                continue
            # Si hay SA JSON, el project_id de la llave es la fuente de verdad
            sa_keys = p.get("json_keys") or p.get("sa_key") or config_cliente.get("json_keys")
            if isinstance(sa_keys, dict) and sa_keys.get("project_id"):
                project_id = sa_keys["project_id"]
            normalized.append({
                "name": p.get("name") or p.get("nombre_reporte") or project_id,
                "project_id": project_id,
                "auth_type": p.get("auth_type") or config_cliente.get("auth_type"),
                "target_sa": p.get("target_sa") or config_cliente.get("target_sa"),
                "json_keys": sa_keys
            })
        return normalized

    def detect_os(self, instance):
        try:
            for disk in instance.disks:
                if disk.boot:
                    license_str = "".join(disk.licenses).lower() if disk.licenses else ""
                    if "windows" in license_str:
                        return "Windows"
            return "Linux"
        except Exception:
            return "Unknown"

    def _is_transient_error(self, error: Exception) -> bool:
        msg = str(error).lower()
        return any(
            marker in msg
            for marker in [
                "internal error",
                "please try again",
                "unavailable",
                "deadline",
                "timeout",
                "rate limit",
                "503",
                "500",
                "504",
            ]
        )

    def get_machine_details(self, machine_type_url: str, credentials):
        if credentials:
            client = compute_v1.MachineTypesClient(credentials=credentials)
        else:
            client = compute_v1.MachineTypesClient()
        try:
            parts = machine_type_url.split('/')
            info = client.get(project=parts[-5], zone=parts[-3], machine_type=parts[-1])
            return info.guest_cpus, info.memory_mb / 1024
        except Exception:
            return "N/A", "N/A"

    def obtener_mapa_politicas_discos(self, project_id: str, credentials):
        if credentials:
            disk_client = compute_v1.DisksClient(credentials=credentials)
        else:
            disk_client = compute_v1.DisksClient()
        disk_map = {}
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                agg_list = disk_client.aggregated_list(project=project_id)
                for _, response in agg_list:
                    if response.disks:
                        for disk in response.disks:
                            has_policy = True if disk.resource_policies else False
                            disk_map[disk.self_link] = has_policy
                total = len(disk_map)
                with_policy = sum(1 for v in disk_map.values() if v)
                print(f"   Backup Map: {project_id} discos={total}, con_policy={with_policy}")
                return disk_map
            except Exception as e:
                if attempt < max_attempts and self._is_transient_error(e):
                    wait_s = 2 ** (attempt - 1)
                    print(
                        f"   Error leyendo backups en {project_id} (intento {attempt}/{max_attempts}): {e}. "
                        f"Reintentando en {wait_s}s..."
                    )
                    time.sleep(wait_s)
                    continue
                print(f"   Error leyendo backups de discos en {project_id}: {e}")
                return disk_map
        return disk_map
    def listar_recursos(self, project_name_report, project_id, credentials, ws_vm, ws_disk, ws_comm, row_vm, row_disk, row_comm, fmt_cell, fmt_money, fmt_red_money):
        try:
            if credentials:
                inst_client = compute_v1.InstancesClient(credentials=credentials)
                disk_client = compute_v1.DisksClient(credentials=credentials)
            else:
                inst_client = compute_v1.InstancesClient()
                disk_client = compute_v1.DisksClient()

            stats = {
                'total_vms': 0, 'active': 0, 'stopped': 0,
                'total_disks': 0, 'disks_unattached': 0, 'disks_in_use': 0,
                'wasted_money': 0.0, 'linux': 0, 'windows': 0,
                'total_commitments': 0,
                'vms_protected': 0, 'vms_unprotected': 0, 'vms_ignored_backup': 0
            }

            disk_policy_map = self.obtener_mapa_politicas_discos(project_id, credentials)

            agg_list = inst_client.aggregated_list(project=project_id)
            for zone_path, response in agg_list:
                if response.instances:
                    for inst in response.instances:
                        stats['total_vms'] += 1

                        status = inst.status
                        db_status = "STOPPED"
                        if status == "RUNNING":
                            stats['active'] += 1
                            db_status = "RUNNING"
                        else:
                            stats['stopped'] += 1

                        os_t = self.detect_os(inst)
                        if os_t == "Windows":
                            stats['windows'] += 1
                        else:
                            stats['linux'] += 1

                        cpus, mem = self.get_machine_details(inst.machine_type, credentials)

                        ip_int = inst.network_interfaces[0].network_i_p if inst.network_interfaces else "None"
                        ip_ext = "None"
                        net_name = "None"
                        if inst.network_interfaces:
                            ni = inst.network_interfaces[0]
                            net_name = ni.network.split('/')[-1]
                            if ni.access_configs:
                                ip_ext = ni.access_configs[0].nat_i_p if ni.access_configs[0].nat_i_p else "None"

                        labels = ", ".join([f"{k}:{v}" for k, v in inst.labels.items()]) if inst.labels else "None"

                        is_protected = False
                        protection_method = "Ninguno"
                        evidence = "Sin politicas"

                        if inst.labels:
                            negative_patterns = ("no-", "sin-", "skip-", "exclude", "disabled")
                            false_values = ("false", "no", "0", "disabled", "none", "off")
                            for key, val in inst.labels.items():
                                k = key.lower()
                                v = str(val).lower() if val else ""
                                if any(k.startswith(neg) for neg in negative_patterns):
                                    continue
                                if v in false_values:
                                    continue
                                if "actifio" in k:
                                    is_protected = True
                                    protection_method = "Backup & DR (Actifio)"
                                    evidence = f"Label: {key}={val}"
                                    stats['_protected_by_label'] = stats.get('_protected_by_label', 0) + 1
                                    break
                                if k in ("backup", "has-backup", "backup-enabled", "backup_enabled", "respaldo"):
                                    is_protected = True
                                    protection_method = "Backup por Label"
                                    evidence = f"Label: {key}={val}"
                                    stats['_protected_by_label'] = stats.get('_protected_by_label', 0) + 1
                                    break

                        if not is_protected:
                            for disk_att in inst.disks:
                                disk_source = disk_att.source
                                if disk_source in disk_policy_map and disk_policy_map[disk_source] == True:
                                    is_protected = True
                                    protection_method = "Snapshot Nativo"
                                    evidence = "Politica en Disco"
                                    stats['_protected_by_policy'] = stats.get('_protected_by_policy', 0) + 1
                                    break

                        if is_protected:
                            stats['vms_protected'] += 1
                        elif db_status == "RUNNING":
                            stats['vms_unprotected'] += 1
                        else:
                            stats['vms_ignored_backup'] += 1

                        ws_vm.write(row_vm, 0, project_name_report, fmt_cell)
                        ws_vm.write(row_vm, 1, inst.id, fmt_cell)
                        ws_vm.write(row_vm, 2, inst.name, fmt_cell)
                        ws_vm.write(row_vm, 3, status, fmt_cell)
                        ws_vm.write(row_vm, 4, inst.creation_timestamp, fmt_cell)
                        ws_vm.write(row_vm, 5, inst.machine_type.split('/')[-1], fmt_cell)
                        ws_vm.write(row_vm, 6, cpus, fmt_cell)
                        ws_vm.write(row_vm, 7, mem, fmt_cell)
                        ws_vm.write(row_vm, 8, ip_int, fmt_cell)
                        ws_vm.write(row_vm, 9, ip_ext, fmt_cell)
                        ws_vm.write(row_vm, 10, net_name, fmt_cell)
                        ws_vm.write(row_vm, 11, inst.zone.split('/')[-1], fmt_cell)
                        ws_vm.write(row_vm, 12, labels, fmt_cell)
                        ws_vm.write(row_vm, 13, "SI" if is_protected else "NO", fmt_cell)
                        ws_vm.write(row_vm, 14, protection_method, fmt_cell)
                        row_vm += 1

                        self.db_vms.append({
                            "nombre": inst.name,
                            "estado": db_status,
                            "tipoInstancia": inst.machine_type.split('/')[-1],
                            "ipPrivada": ip_int,
                            "so": os_t,
                            "tieneRespaldo": is_protected,
                            "metodoRespaldo": protection_method,
                            "evidenciaRespaldo": evidence,
                            "resourceGroup": project_name_report
                        })

            agg_list_disks = disk_client.aggregated_list(project=project_id)
            for zone_path, response in agg_list_disks:
                if response.disks:
                    zone_name = zone_path.split('/')[-1]
                    for d in response.disks:
                        stats['total_disks'] += 1

                        in_use_by = "None"
                        db_state = "UNATTACHED"
                        if d.users:
                            in_use_by = d.users[0].split('/')[-1]
                            stats['disks_in_use'] += 1
                            db_state = "IN_USE"
                        else:
                            stats['disks_unattached'] += 1

                        cost_monthly = self.pricing_service.get_disk_price(zone_name, d.type_, d.size_gb)
                        wasted = cost_monthly if db_state == "UNATTACHED" else 0
                        stats['wasted_money'] += wasted

                        labels_d = ", ".join([f"{k}:{v}" for k, v in d.labels.items()]) if d.labels else "None"

                        ws_disk.write(row_disk, 0, project_name_report, fmt_cell)
                        ws_disk.write(row_disk, 1, d.status, fmt_cell)
                        ws_disk.write(row_disk, 2, d.name, fmt_cell)
                        ws_disk.write(row_disk, 3, d.type_.split('/')[-1], fmt_cell)
                        ws_disk.write(row_disk, 4, d.size_gb, fmt_cell)
                        ws_disk.write(row_disk, 5, cost_monthly, fmt_money)
                        ws_disk.write(row_disk, 6, wasted if wasted > 0 else 0, fmt_red_money if wasted > 0 else fmt_money)
                        ws_disk.write(row_disk, 7, zone_name, fmt_cell)
                        ws_disk.write(row_disk, 8, d.creation_timestamp, fmt_cell)
                        ws_disk.write(row_disk, 9, in_use_by, fmt_cell)
                        ws_disk.write(row_disk, 10, labels_d, fmt_cell)
                        row_disk += 1

                        self.db_disks.append({
                            "nombre": d.name,
                            "estado": db_state,
                            "tamanoGB": d.size_gb,
                            "resourceGroup": project_name_report
                        })

            print(
                f"   Backup Stats: {project_id} vms_protected={stats.get('vms_protected', 0)} "
                f"label={stats.get('_protected_by_label', 0)} policy={stats.get('_protected_by_policy', 0)}"
            )

            try:
                if credentials:
                    comm_client = compute_v1.RegionCommitmentsClient(credentials=credentials)
                else:
                    comm_client = compute_v1.RegionCommitmentsClient()
                req_comm = compute_v1.AggregatedListRegionCommitmentsRequest(project=project_id)
                agg_list_comm = comm_client.aggregated_list(request=req_comm)

                for region_path, response in agg_list_comm:
                    if response.commitments:
                        region_clean = region_path.split('/')[-1]
                        for comm in response.commitments:
                            stats['total_commitments'] += 1
                            try:
                                ts = comm.end_timestamp
                                if isinstance(ts, int):
                                    end_dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                                else:
                                    end_dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                                now = datetime.now(timezone.utc)
                                days_remaining = (end_dt - now).days
                                fecha_fin_iso = end_dt.isoformat()
                                fecha_fin_excel = end_dt.strftime('%Y-%m-%d')
                            except Exception:
                                days_remaining = 0
                                fecha_fin_iso = None
                                fecha_fin_excel = "Error"

                            ws_comm.write(row_comm, 0, project_name_report, fmt_cell)
                            ws_comm.write(row_comm, 1, comm.name, fmt_cell)
                            ws_comm.write(row_comm, 2, region_clean, fmt_cell)
                            ws_comm.write(row_comm, 3, comm.status, fmt_cell)
                            ws_comm.write(row_comm, 4, fecha_fin_excel, fmt_cell)
                            ws_comm.write(row_comm, 5, days_remaining, fmt_cell)
                            row_comm += 1

                            try:
                                created_dt = datetime.fromisoformat(
                                    comm.creation_timestamp.replace('Z', '+00:00')
                                ) if comm.creation_timestamp else None
                                created_str = created_dt.isoformat() if created_dt else ""
                            except Exception:
                                created_str = ""

                            self.db_compromisos.append({
                                "nombre": comm.name,
                                "region": region_clean,
                                "estado": comm.status,
                                "fechaFin": fecha_fin_iso or "",
                                "diasRestantes": days_remaining,
                                "creadoEn": created_str
                            })

            except Exception as e:
                if "disabled" not in str(e):
                    print(f"   Error commitments: {e}")

            return row_vm, row_disk, row_comm, stats

        except Exception as e:
            print(f"Error critico en proyecto {project_name_report}: {e}")
            return row_vm, row_disk, row_comm, {
                'active': 0, 'stopped': 0, 'total_vms': 0,
                'windows': 0, 'linux': 0,
                'disks_in_use': 0, 'disks_unattached': 0, 'total_disks': 0,
                'wasted_money': 0.0,
                'total_commitments': 0,
                'vms_protected': 0, 'vms_unprotected': 0, 'vms_ignored_backup': 0
            }

    def subir_excel_a_gcp(self, archivo_excel: str) -> str:
        print("Subiendo reporte a Google Cloud Storage...")
        try:
            bucket_name = os.environ.get('BUCKET_REPORTES')
            if not bucket_name:
                print("No se encontro variable de entorno BUCKET_REPORTES.")
                return ""

            storage_client = gcs_storage.Client()
            bucket = storage_client.bucket(bucket_name)

            filename = os.path.basename(archivo_excel)
            ruta_destino = f"{datetime.now(CL_TZ).strftime('%Y/%m')}/{filename}"
            blob = bucket.blob(ruta_destino)

            blob.upload_from_filename(
                archivo_excel,
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

            public_url = blob.public_url
            print(f"Excel subido exitosamente: {public_url}")
            return public_url

        except Exception as e:
            print(f"Error al subir a GCS: {e}")
            return ""

    def enviar_datos_a_api(self, stats_globales: Dict[str, Any], client_name: str, compromisos: list, proyectos: list):
        api_url = os.environ.get('API_INGESTA_URL')
        api_key = os.environ.get('API_KEY_SECRETA')

        if not api_url or not api_key:
            print("Faltan variables de entorno API_INGESTA_URL o API_KEY_SECRETA.")
            return

        print(f"Enviando datos a Cloud Function: {api_url}")
        try:
            stats = stats_globales
            payload = {
                "origen": client_name,
                "proveedor": "Google Cloud",
                "fecha": datetime.now(CL_TZ).strftime('%Y-%m-%dT%H:%M:%S'),
                "estadisticas": {
                    "total_vms": stats.get('total_vms', 0),
                    "active": stats.get('active', 0),
                    "stopped": stats.get('stopped', 0),
                    "total_disks": stats.get('total_disks', 0),
                    "disks_unattached": stats.get('disks_unattached', 0),
                    "wasted_money": stats.get('wasted_money', 0.0),
                    "total_compromisos": stats.get('total_commitments', 0),
                    "vms_protected": stats.get('vms_protected', 0),
                    "vms_unprotected": stats.get('vms_unprotected', 0),
                    "vms_ignored_backup": stats.get('vms_ignored_backup', 0),
                    "vms_linux": stats.get('linux', 0),
                    "vms_windows": stats.get('windows', 0)
                },
                "archivoUrl": stats_globales.get('archivoUrl', ''),
                "vms": self.db_vms,
                "discos": self.db_disks,
                "compromisos": compromisos,
                "proyectos": proyectos
            }

            headers = {
                "Content-Type": "application/json",
                "x-api-key": api_key
            }

            response = requests.post(api_url, json=payload, headers=headers, timeout=60)

            if response.status_code == 200:
                print(f"Datos procesados y guardados en BD. Respuesta: {response.text}")
            else:
                print(f"Error de la API (Codigo {response.status_code}): {response.text}")

        except Exception as e:
            print(f"Error de red intentando enviar datos a la API: {e}")


def ejecutar_recoleccion(request):
    """Entry point para Google Cloud Functions."""
    try:
        request_json = request.get_json(silent=True) or {}

        if isinstance(request_json, dict) and 'cliente' in request_json:
            CLIENTE_NOMBRE = request_json['cliente']
        else:
            CLIENTE_NOMBRE = os.environ.get('CLIENTE_DEFAULT', '')

        if not CLIENTE_NOMBRE:
            return ("Falta cliente en request y CLIENTE_DEFAULT no esta definido", 400)

        reporter = GCPCloudReporter("config.json")
        config_cliente = reporter.config.get(CLIENTE_NOMBRE)
        if not config_cliente:
            return (f"Cliente no encontrado en config.json: {CLIENTE_NOMBRE}", 404)

        proyectos_conf = reporter._normalize_projects(config_cliente)
        if not proyectos_conf:
            return (f"Cliente sin proyectos configurados: {CLIENTE_NOMBRE}", 400)

        excel_file = f"/tmp/Inventario_FinOps_GCP_{CLIENTE_NOMBRE.replace(' ', '_')}_{datetime.now(CL_TZ).strftime('%Y-%m-%d')}.xlsx"
        wb = xlsxwriter.Workbook(excel_file)

        fmt_h = wb.add_format({'bold': True, 'bg_color': '#B8CCE4', 'border': 1})
        fmt_c = wb.add_format({'border': 1})
        fmt_money = wb.add_format({'num_format': '$#,##0.00', 'border': 1})
        fmt_red_money = wb.add_format({'num_format': '$#,##0.00', 'bg_color': '#FFC7CE', 'font_color': '#9C0006', 'border': 1, 'bold': True})

        ws1 = wb.add_worksheet("VM Instances")
        h1 = ["Project Name", "Instance Id", "Name", "Status", "Creation time", "Machine type", "CPU", "Memory (GB)", "Internal IP", "External IP", "Network", "Zone", "Labels", "Tiene Respaldo", "Metodo Respaldo"]
        for i, val in enumerate(h1):
            ws1.write(0, i, val, fmt_h)

        ws2 = wb.add_worksheet("Disk Information")
        h2 = ["Project Name", "Status", "Name", "Type", "Size (GB)", "Costo Mensual Est.", "DESPERDICIO ($)", "Zone", "Creation Time", "In Use By", "Labels"]
        for i, val in enumerate(h2):
            ws2.write(0, i, val, fmt_h)

        ws3 = wb.add_worksheet("COMMITMENTS")
        h3 = ["Project Name", "Contract Name", "Region", "Status", "Expiry Date", "Days Remaining"]
        for i, val in enumerate(h3):
            ws3.write(0, i, val, fmt_h)

        r_vm, r_disk, r_comm = 1, 1, 1

        g_stats = {
            'active': 0, 'stopped': 0, 'total_vms': 0,
            'windows': 0, 'linux': 0,
            'disks_in_use': 0, 'disks_unattached': 0, 'total_disks': 0,
            'wasted_money': 0.0,
            'total_commitments': 0,
            'vms_protected': 0, 'vms_unprotected': 0, 'vms_ignored_backup': 0
        }

        for p in proyectos_conf:
            print(f"--> Cambiando identidad para proyecto: {p['name']}...")
            creds = obtener_credenciales_gcp(p)
            reporter.pricing_service = GCPPricingService(creds)

            r_vm, r_disk, r_comm, s = reporter.listar_recursos(
                p['name'],
                p['project_id'],
                creds,
                ws1,
                ws2,
                ws3,
                r_vm,
                r_disk,
                r_comm,
                fmt_c,
                fmt_money,
                fmt_red_money
            )

            g_stats['active'] += s.get('active', 0)
            g_stats['stopped'] += s.get('stopped', 0)
            g_stats['total_vms'] += s.get('total_vms', 0)
            g_stats['windows'] += s.get('windows', 0)
            g_stats['linux'] += s.get('linux', 0)
            g_stats['disks_in_use'] += s.get('disks_in_use', 0)
            g_stats['disks_unattached'] += s.get('disks_unattached', 0)
            g_stats['total_disks'] += s.get('total_disks', 0)
            g_stats['wasted_money'] += s.get('wasted_money', 0.0)
            g_stats['total_commitments'] += s.get('total_commitments', 0)
            g_stats['vms_protected'] += s.get('vms_protected', 0)
            g_stats['vms_unprotected'] += s.get('vms_unprotected', 0)
            g_stats['vms_ignored_backup'] += s.get('vms_ignored_backup', 0)

        wb.close()

        url_reporte = reporter.subir_excel_a_gcp(excel_file)
        g_stats['archivoUrl'] = url_reporte

        proyectos = []
        for p in proyectos_conf:
            proyectos.append({"nombre": p['name'], "proveedor": "Google Cloud"})

        reporter.enviar_datos_a_api(g_stats, CLIENTE_NOMBRE, reporter.db_compromisos, proyectos)

        return (f"Ejecucion exitosa para {CLIENTE_NOMBRE}", 200)

    except Exception as e:
        error_msg = f"Fallo la ejecucion: {e}"
        print(error_msg)
        return (error_msg, 500)
