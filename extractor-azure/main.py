import json
import xlsxwriter
import requests
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any
from azure.identity import ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.web import WebSiteManagementClient
from azure.mgmt.storage import StorageManagementClient
from azure.mgmt.keyvault import KeyVaultManagementClient
from azure.mgmt.recoveryservices import RecoveryServicesClient
from azure.mgmt.recoveryservicesbackup.activestamp import RecoveryServicesBackupClient
from google.cloud import storage

# --- SERVICIO DE PRECIOS (INTEGRADO) ---
class AzurePricingService:
    def __init__(self):
        self.region_catalog_cache = {}
        self.api_url = "https://prices.azure.com/api/retail/prices"
        self.session = requests.Session()
        self.debug_counter = 0

    def _get_tier_info(self, size_gb, sku_name):
        sizes = [4, 6, 10, 15, 20, 30, 40, 50, 60, 70, 80]
        caps = [32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32767]
        tier_num = 80
        for i, cap in enumerate(caps):
            if size_gb <= cap:
                tier_num = sizes[i]
                break
        if "Premium" in sku_name:
            return f"P{tier_num}", "Premium"
        elif "StandardSSD" in sku_name:
            return f"E{tier_num}", "Standard SSD"
        else:
            return f"S{tier_num}", "Standard"

    def _load_region_catalog(self, region):
        if region in self.region_catalog_cache:
            return
        print(f"   â¬‡ï¸ Descargando catÃ¡logo {region} (Pricing)...")
        items_in_region = []
        query = f"serviceName eq 'Storage' and armRegionName eq '{region}' and priceType eq 'Consumption'"
        params = {"$filter": query}
        url = self.api_url
        try:
            while url:
                response = self.session.get(url, params=params)
                data = response.json()
                raw_items = data.get('Items', [])
                for item in raw_items:
                    p_name = item.get('productName', '')
                    if 'Managed Disk' in p_name or 'Premium SSD' in p_name:
                        items_in_region.append(item)
                url = data.get('NextPageLink')
                params = None
            self.region_catalog_cache[region] = items_in_region
        except Exception as e:
            print(f"   âŒ Error cargando catÃ¡logo {region}: {e}")
            self.region_catalog_cache[region] = []

    def get_disk_price(self, region, sku_name, size_gb):
        if not sku_name: return 0.0
        self._load_region_catalog(region)
        target_tier, target_type = self._get_tier_info(size_gb, sku_name)
        catalog = self.region_catalog_cache.get(region, [])
        for item in catalog:
            p_name = item.get('productName', '')
            sku_api = item.get('skuName', '')
            tier_match = (target_tier in sku_api) or (target_tier in p_name)
            if (target_type in p_name) and tier_match and ("LRS" in sku_api):
                return item.get('retailPrice', 0.0)
        return 0.0

# --- ZONA HORARIA CHILE (UTC-3 verano / UTC-4 invierno) ---
CL_TZ = timezone(timedelta(hours=-3))

# --- SCRIPT CONSOLIDADO PARA CLOUD FUNCTION: INNERGY (Azure Multi) ---

class AzureMultiReporter:
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.config = self._load_config()
        self.clients = self._normalize_clients(self.config)
        self.workbook = None
        self.pricing_service = AzurePricingService()
        self.db_vms = []
        self.db_disks = []
        self.credential = None
        self.suscripciones = []
        self.current_client = None

    def _load_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            raise FileNotFoundError(f"No se encontro el archivo: {self.config_path}")
        with open(self.config_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _normalize_clients(self, raw_config: Dict[str, Any]) -> list:
        if isinstance(raw_config, dict) and isinstance(raw_config.get('clients'), list):
            return raw_config['clients']

        clients = []
        if isinstance(raw_config, dict):
            for name, data in raw_config.items():
                if not isinstance(data, dict):
                    continue
                if not all(k in data for k in ['tenant_id', 'client_id', 'client_secret']):
                    continue
                clients.append({
                    "name": name,
                    "slug": str(name).lower().replace(" ", "-"),
                    "azure": {
                        "tenant_id": data['tenant_id'],
                        "client_id": data['client_id'],
                        "client_secret": data['client_secret']
                    },
                    "subscriptions": data.get('subscriptions', [])
                })
        return clients

    def filter_clients(self, requested):
        if not self.clients:
            return []
        if not requested:
            return self.clients

        if isinstance(requested, str):
            requested_list = [requested]
        elif isinstance(requested, list):
            requested_list = [r for r in requested if isinstance(r, str)]
        else:
            requested_list = []

        wanted = {r.strip().lower() for r in requested_list if r and isinstance(r, str)}
        if not wanted:
            return self.clients

        matched = []
        for client in self.clients:
            name = (client.get('name') or '').lower()
            slug = (client.get('slug') or '').lower()
            group = (client.get('group') or client.get('consolidated') or '').lower()
            if name in wanted or slug in wanted or (group and group in wanted):
                matched.append(client)
        return matched

    def set_active_client(self, client_conf: Dict[str, Any]):
        azure_conf = client_conf.get('azure', client_conf)
        if not isinstance(azure_conf, dict):
            raise KeyError(f"Config Azure invalida para cliente {client_conf.get('name', 'UNKNOWN')}")
        self.credential = ClientSecretCredential(
            tenant_id=azure_conf['tenant_id'],
            client_id=azure_conf['client_id'],
            client_secret=azure_conf['client_secret']
        )
        self.suscripciones = client_conf.get('subscriptions', [])
        self.current_client = client_conf

    # --- GOOGLE CLOUD STORAGE (Cloud Function: identidad del servicio, sin llaves en config) ---
    def subir_excel_a_gcp(self, archivo_excel: str) -> str:
        print("\nâ˜ï¸ Subiendo reporte a Google Cloud Storage...")
        try:
            # Magia Serverless: No necesita credenciales, usa la identidad de la Cloud Function
            storage_client = storage.Client()

            # Lee el nombre del bucket de las variables de entorno inyectadas por Terraform
            bucket_name = os.environ.get('BUCKET_REPORTES')
            if not bucket_name:
                print("   âŒ No se encontrÃ³ variable de entorno BUCKET_REPORTES.")
                return ""
            bucket = storage_client.bucket(bucket_name)

            filename = os.path.basename(archivo_excel)
            ruta_destino = f"{datetime.now(CL_TZ).strftime('%Y/%m')}/{filename}"
            blob = bucket.blob(ruta_destino)

            blob.upload_from_filename(archivo_excel, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            public_url = blob.public_url
            print(f"   âœ… Excel subido exitosamente: {public_url}")
            return public_url

        except Exception as e:
            print(f"   âŒ Error al subir a GCS: {e}")
            return ""

    # --- ENVÃO DE DATOS A API (Cloud Function: URL y API Key desde variables de entorno) ---
    def enviar_datos_a_api(self, stats_globales: Dict[str, Any], client_name: str, proyectos: list):
        # Lee la URL y la llave desde las variables de entorno secretas
        api_url = os.environ.get('API_INGESTA_URL')
        api_key = os.environ.get('API_KEY_SECRETA')

        if not api_url or not api_key:
            print("âŒ Faltan variables de entorno API_INGESTA_URL o API_KEY_SECRETA.")
            return

        print(f"\nðŸ“¡ Enviando datos a Cloud Function: {api_url}")
        try:
            stats = stats_globales
            archivo_url = stats_globales.get('archivoUrl', '')
            if stats_globales.get('archivosUrls'):
                archivo_url = json.dumps(stats_globales['archivosUrls'])

            payload = {
                "origen": client_name,
                "proveedor": "Azure",
                "fecha": datetime.now(CL_TZ).strftime('%Y-%m-%dT%H:%M:%S'),
                "estadisticas": {
                    "total_vms": stats.get('vms_total', 0),
                    "active": stats.get('active', 0),
                    "stopped": stats.get('stopped', 0),
                    "total_disks": stats.get('disks_total', 0),
                    "disks_unattached": stats.get('disks_unattached', 0),
                    "wasted_money": stats.get('wasted_money', 0.0),
                    "total_compromisos": 0,
                    "vms_protected": stats.get('vms_protected', 0),
                    "vms_unprotected": stats.get('vms_unprotected', 0),
                    "vms_ignored_backup": stats.get('vms_ignored_backup', 0),
                    "vms_linux": stats.get('linux', 0),
                    "vms_windows": stats.get('windows', 0)
                },
                "archivoUrl": archivo_url,
                "vms": self.db_vms,
                "discos": self.db_disks,
                "compromisos": [],
                "proyectos": proyectos
            }

            headers = {
                "Content-Type": "application/json",
                "x-api-key": api_key
            }

            response = requests.post(api_url, json=payload, headers=headers, timeout=60)

            if response.status_code == 200:
                print(f"   âœ… Datos procesados y guardados en BD. Respuesta: {response.text}")
            else:
                print(f"   âŒ Error de la API (CÃ³digo {response.status_code}): {response.text}")

        except Exception as e:
            print(f"   âŒ Error de red intentando enviar datos a la API: {e}")

    # --- OBTENER VMs CON BACKUP ---
    def obtener_vms_protegidas(self, sub_id):
        protected_vm_ids = set()
        try:
            r_client = RecoveryServicesClient(self.credential, sub_id)
            b_client = RecoveryServicesBackupClient(self.credential, sub_id)
            vaults = list(r_client.vaults.list_by_subscription_id())
            for vault in vaults:
                rg_name = vault.id.split('/')[4]
                try:
                    items = b_client.backup_protected_items.list(
                        vault.name, rg_name,
                        filter="backupManagementType eq 'AzureIaasVM'"
                    )
                    for item in items:
                        if item.properties and hasattr(item.properties, 'source_resource_id') and item.properties.source_resource_id:
                            protected_vm_ids.add(item.properties.source_resource_id.lower())
                except Exception:
                    pass
        except Exception as e:
            print(f"   âš ï¸ Error escaneando Backups en sub {sub_id}: {e}")
        return protected_vm_ids

    # --- LISTAR VNETS ---
    def listar_vnets(self, ws, row_index, sub_id, sub_name, fmt_cell) -> int:
        try:
            client = NetworkManagementClient(self.credential, sub_id)
            vnets = list(client.virtual_networks.list_all())

            for vnet in vnets:
                prefixes = ", ".join(vnet.address_space.address_prefixes) if vnet.address_space else ""

                ws.write(row_index, 0, vnet.id, fmt_cell)
                ws.write(row_index, 1, vnet.name, fmt_cell)
                ws.write(row_index, 2, prefixes, fmt_cell)
                ws.write(row_index, 3, vnet.location, fmt_cell)
                ws.write(row_index, 4, sub_name, fmt_cell)

                row_index += 1

        except Exception as e:
            print(f"Error procesando VNETs en {sub_name}: {e}")

        return row_index

    # --- LISTAR VMS ---
    def listar_vms(self, ws, row_index, sub_id, sub_name, fmt_cell, protected_ids):
        local_stats = {'active': 0, 'stopped': 0, 'windows': 0, 'linux': 0, 'total': 0, 'protected': 0, 'unprotected': 0, 'ignored_backup': 0}

        try:
            compute_client = ComputeManagementClient(self.credential, sub_id)
            network_client = NetworkManagementClient(self.credential, sub_id)
            vms = list(compute_client.virtual_machines.list_all())

            local_stats['total'] = len(vms)

            for vm in vms:
                rg = vm.id.split('/')[4]
                os_type = vm.storage_profile.os_disk.os_type if vm.storage_profile.os_disk.os_type else 'Unknown'

                if os_type == 'Windows': local_stats['windows'] += 1
                elif os_type == 'Linux': local_stats['linux'] += 1

                try:
                    instance_view = compute_client.virtual_machines.instance_view(rg, vm.name)
                    status = instance_view.statuses[-1].display_status
                except:
                    status = "Unknown"

                db_status = 'UNKNOWN'
                if status == 'VM running':
                    local_stats['active'] += 1
                    db_status = 'RUNNING'
                elif status in ['VM deallocated', 'VM stopped']:
                    local_stats['stopped'] += 1
                    db_status = 'STOPPED'

                is_protected = vm.id.lower() in protected_ids
                if is_protected:
                    local_stats['protected'] += 1
                elif db_status == 'RUNNING':
                    local_stats['unprotected'] += 1
                else:
                    local_stats['ignored_backup'] += 1

                try:
                    vm_sizes = compute_client.virtual_machine_sizes.list(vm.location)
                    size_info = next((s for s in vm_sizes if s.name == vm.hardware_profile.vm_size), None)
                    cpu = size_info.number_of_cores if size_info else 'N/A'
                    memory = size_info.memory_in_mb // 1024 if size_info else 'N/A'
                except:
                    cpu = 'N/A'
                    memory = 'N/A'

                image = vm.storage_profile.image_reference.offer if vm.storage_profile.image_reference else 'None'
                az = vm.zones[0] if vm.zones else 'None'
                host = vm.host if vm.host else 'None'
                tags = json.dumps(vm.tags) if vm.tags else 'None'

                ips = []
                vnets_list = []
                try:
                    for nic_ref in vm.network_profile.network_interfaces:
                        nic = network_client.network_interfaces.get(rg, nic_ref.id.split('/')[-1])
                        ipconf = nic.ip_configurations[0]
                        if ipconf.private_ip_address: ips.append(ipconf.private_ip_address)
                        if ipconf.subnet: vnets_list.append(ipconf.subnet.id.split('/')[-3])
                except: pass

                ip_principal = ips[0] if ips else '0.0.0.0'

                ws.write(row_index, 0, vm.vm_id, fmt_cell)
                ws.write(row_index, 1, vm.name, fmt_cell)
                ws.write(row_index, 2, status, fmt_cell)
                ws.write(row_index, 3, vm.time_created.strftime("%Y-%m-%d %H:%M:%S") if vm.time_created else 'N/A', fmt_cell)
                ws.write(row_index, 4, vm.hardware_profile.vm_size, fmt_cell)
                ws.write(row_index, 5, cpu, fmt_cell)
                ws.write(row_index, 6, memory, fmt_cell)
                ws.write(row_index, 7, image, fmt_cell)
                ws.write(row_index, 8, os_type, fmt_cell)
                ws.write(row_index, 9, ", ".join(ips) if ips else 'None', fmt_cell)
                ws.write(row_index, 10, az, fmt_cell)
                ws.write(row_index, 11, tags, fmt_cell)
                ws.write(row_index, 12, ", ".join(vnets_list) if vnets_list else 'None', fmt_cell)
                ws.write(row_index, 13, host, fmt_cell)
                ws.write(row_index, 14, rg, fmt_cell)
                ws.write(row_index, 15, vm.location, fmt_cell)
                ws.write(row_index, 16, sub_name, fmt_cell)

                row_index += 1

                self.db_vms.append({
                    "nombre": vm.name,
                    "estado": db_status,
                    "tipoInstancia": vm.hardware_profile.vm_size,
                    "ipPrivada": ip_principal,
                    "so": f"{os_type} ({image})",
                    "tieneRespaldo": is_protected,
                    "metodoRespaldo": "Azure Backup" if is_protected else "Ninguno",
                    "evidenciaRespaldo": "",
                    "resourceGroup": rg
                })

        except Exception as e:
            print(f"Error procesando VMs en {sub_name}: {e}")

        return row_index, local_stats

    # --- LISTAR DISCOS ---
    def listar_disks(self, ws, row_index, sub_id, sub_name, fmt_cell, fmt_money=None, fmt_money_red=None):
        local_stats = {'total': 0, 'unattached': 0, 'in_use': 0, 'wasted_money': 0.0}

        try:
            client = ComputeManagementClient(self.credential, sub_id)
            disks = list(client.disks.list())
            local_stats['total'] = len(disks)

            for disk in disks:
                state = disk.disk_state if hasattr(disk, 'disk_state') else "Unknown"

                monthly_price = 0.0
                if disk.sku and disk.location:
                    monthly_price = self.pricing_service.get_disk_price(disk.location, disk.sku.name, disk.disk_size_gb)

                es_replica_asr = "ASRReplica" in disk.name
                tiene_owner = bool(disk.managed_by)
                is_wasted = (state == "Unattached" and not es_replica_asr and not tiene_owner)

                wasted_amount = 0.0
                if is_wasted:
                    local_stats['unattached'] += 1
                    wasted_amount = monthly_price
                    local_stats['wasted_money'] += wasted_amount
                else:
                    local_stats['in_use'] += 1

                ws.write(row_index, 0, disk.name, fmt_cell)
                ws.write(row_index, 1, disk.disk_size_gb, fmt_cell)
                ws.write(row_index, 2, disk.provisioning_state, fmt_cell)
                ws.write(row_index, 3, disk.sku.name if disk.sku else "None", fmt_cell)
                ws.write(row_index, 4, disk.location, fmt_cell)
                ws.write(row_index, 5, disk.managed_by.split('/')[-1] if disk.managed_by else 'None', fmt_cell)
                ws.write(row_index, 6, disk.time_created.strftime("%Y-%m-%d %H:%M:%S") if disk.time_created else "None", fmt_cell)
                ws.write(row_index, 7, disk.tier if disk.tier else "None", fmt_cell)
                ws.write(row_index, 8, state, fmt_cell)
                ws.write(row_index, 9, ", ".join(disk.zones) if disk.zones else "None", fmt_cell)
                ws.write(row_index, 10, json.dumps(disk.tags) if disk.tags else "None", fmt_cell)
                ws.write(row_index, 11, disk.id.split('/')[4] if disk.id else 'None', fmt_cell)
                ws.write(row_index, 12, disk.encryption.type if disk.encryption and disk.encryption.type else 'None', fmt_cell)
                ws.write(row_index, 13, disk.max_shares if hasattr(disk, 'max_shares') and disk.max_shares not in [None, ''] else 'None', fmt_cell)
                ws.write(row_index, 14, sub_name, fmt_cell)

                if fmt_money:
                    ws.write(row_index, 15, monthly_price, fmt_money)
                    ws.write(row_index, 16, wasted_amount if is_wasted else 0, fmt_money_red if is_wasted else fmt_money)

                row_index += 1

                self.db_disks.append({
                    "nombre": disk.name,
                    "estado": state,
                    "tamanoGB": disk.disk_size_gb,
                    "resourceGroup": disk.id.split('/')[4] if disk.id else 'Unknown'
                })

        except Exception as e:
            print(f"Error procesando Discos en {sub_name}: {e}")

        return row_index, local_stats

    # --- LISTAR WEB APPS (App Services) ---
    def listar_web_apps(self, ws, row_index, sub_id, sub_name, fmt_cell):
        local_stats = {'total': 0, 'running': 0, 'stopped': 0}
        try:
            client = WebSiteManagementClient(self.credential, sub_id)
            web_apps = list(client.web_apps.list())
            local_stats['total'] = len(web_apps)
            for app in web_apps:
                state = app.state if app.state else "Unknown"
                if state.lower() == 'running': local_stats['running'] += 1
                else: local_stats['stopped'] += 1
                plan_name = app.server_farm_id.split('/')[-1] if app.server_farm_id else "N/A"

                ws.write(row_index, 0, app.name, fmt_cell)
                ws.write(row_index, 1, state, fmt_cell)
                ws.write(row_index, 2, app.default_host_name, fmt_cell)
                ws.write(row_index, 3, app.kind, fmt_cell)
                ws.write(row_index, 4, plan_name, fmt_cell)
                ws.write(row_index, 5, app.location, fmt_cell)
                ws.write(row_index, 6, app.resource_group, fmt_cell)
                ws.write(row_index, 7, str(app.https_only), fmt_cell)
                ws.write(row_index, 8, sub_name, fmt_cell)
                row_index += 1

        except Exception as e:
            print(f"Error procesando Web Apps en {sub_name}: {e}")
        return row_index, local_stats

    # --- LISTAR STORAGE ACCOUNTS ---
    def listar_storage(self, ws, row_index, sub_id, sub_name, fmt_cell):
        try:
            client = StorageManagementClient(self.credential, sub_id)
            accounts = list(client.storage_accounts.list())
            for acc in accounts:
                sku = acc.sku.name if acc.sku else "Unknown"
                kind = acc.kind if acc.kind else "Unknown"
                endpoint = acc.primary_endpoints.blob if acc.primary_endpoints else "N/A"
                ws.write(row_index, 0, acc.name, fmt_cell)
                ws.write(row_index, 1, sku, fmt_cell)
                ws.write(row_index, 2, kind, fmt_cell)
                ws.write(row_index, 3, acc.location, fmt_cell)
                ws.write(row_index, 4, acc.id.split('/')[4], fmt_cell)
                ws.write(row_index, 5, acc.access_tier if hasattr(acc, 'access_tier') else "N/A", fmt_cell)
                ws.write(row_index, 6, endpoint, fmt_cell)
                ws.write(row_index, 7, sub_name, fmt_cell)
                row_index += 1
        except Exception as e:
            print(f"Error procesando Storage en {sub_name}: {e}")
        return row_index

    # --- LISTAR KEY VAULTS ---
    def listar_key_vaults(self, ws, row_index, sub_id, sub_name, fmt_cell):
        try:
            client = KeyVaultManagementClient(self.credential, sub_id)
            vaults = list(client.vaults.list_by_subscription())
            for kv in vaults:
                sku = kv.properties.sku.name if kv.properties.sku else "Unknown"
                uri = kv.properties.vault_uri if kv.properties.vault_uri else "N/A"
                ws.write(row_index, 0, kv.name, fmt_cell)
                ws.write(row_index, 1, sku, fmt_cell)
                ws.write(row_index, 2, kv.location, fmt_cell)
                ws.write(row_index, 3, kv.id.split('/')[4], fmt_cell)
                ws.write(row_index, 4, uri, fmt_cell)
                ws.write(row_index, 5, str(kv.properties.enabled_for_deployment), fmt_cell)
                ws.write(row_index, 6, str(kv.properties.enabled_for_disk_encryption), fmt_cell)
                ws.write(row_index, 7, sub_name, fmt_cell)
                row_index += 1
        except Exception as e:
            print(f"Error procesando Key Vaults en {sub_name}: {e}")
        return row_index


def ejecutar_recoleccion(request):
    """Entry point para Google Cloud Functions."""
    try:
        # 1. Leer el mensaje HTTP (Payload) que envia el Scheduler
        request_json = request.get_json(silent=True) or {}

        reporter = AzureMultiReporter("config.json")

        requested = None
        if isinstance(request_json, dict):
            if 'clientes' in request_json:
                requested = request_json.get('clientes')
            elif 'cliente' in request_json:
                requested = request_json.get('cliente')

        if requested is None:
            requested = os.environ.get('CLIENTE_DEFAULT', 'INNERGY')

        selected_clients = reporter.filter_clients(requested)
        if not selected_clients:
            print(f"Advertencia: no se encontraron clientes para '{requested}'. Se procesaran todos.")
            selected_clients = reporter.clients

        if isinstance(request_json, dict) and request_json.get('origen'):
            CLIENTE_NOMBRE = request_json['origen']
        elif isinstance(requested, str):
            CLIENTE_NOMBRE = requested
        elif isinstance(requested, list) and len(requested) == 1 and isinstance(requested[0], str):
            CLIENTE_NOMBRE = requested[0]
        else:
            CLIENTE_NOMBRE = os.environ.get('CLIENTE_LABEL', 'CONSOLIDADO')

        print(f"Iniciando recoleccion para el cliente: {CLIENTE_NOMBRE}")

        fecha = datetime.now(CL_TZ).strftime("%Y-%m-%d")

        # --- ESTADISTICAS GLOBALES ---
        datos_globales = {
            'vms_total': 0, 'active': 0, 'stopped': 0, 'windows': 0, 'linux': 0,
            'vms_protected': 0, 'vms_unprotected': 0, 'vms_ignored_backup': 0,
            'disks_total': 0, 'disks_unattached': 0, 'disks_in_use': 0, 'wasted_money': 0.0,
            'total_webapps': 0, 'webapps_running': 0, 'webapps_stopped': 0,
            'subs_count': 0
        }

        total_subs = sum(len(c.get('subscriptions', [])) for c in selected_clients)
        datos_globales['subs_count'] = total_subs

        print(f"Procesando {len(selected_clients)} clientes / {total_subs} suscripciones...")

        proyectos = []
        all_urls = []

        for client_conf in selected_clients:
            try:
                reporter.set_active_client(client_conf)
            except KeyError as e:
                print(f"Error de configuracion: {e}")
                continue

            client_name = client_conf.get('name', 'UNKNOWN')
            client_slug = client_conf.get('slug') or client_name.lower().replace(" ", "-")
            lista_subs = reporter.suscripciones

            print(f"--> Procesando Cliente: {client_name} ({len(lista_subs)} suscripciones)...")

            # IMPORTANTE: En Cloud Functions SOLO puedes crear archivos en la carpeta /tmp/
            archivo_excel = f"/tmp/Inventario_Consolidado-{client_slug}_{fecha}.xlsx"
            print(f"Generando Excel para cliente: {client_name}...")

            wb = xlsxwriter.Workbook(archivo_excel)

            # FORMATOS
            fmt_head = wb.add_format({'bold': True, 'bg_color': '#B8CCE4', 'border': 1})
            fmt_cell = wb.add_format({'bg_color': '#DCE6F1', 'border': 1})
            fmt_money = wb.add_format({'num_format': '$#,##0.00', 'bg_color': '#DCE6F1', 'border': 1})
            fmt_money_red = wb.add_format({'num_format': '$#,##0.00', 'bg_color': '#FFC7CE', 'font_color': '#9C0006', 'border': 1, 'bold': True})

            # --- 1. PREPARAR HOJA VNET ---
            ws_vnet = wb.add_worksheet("VNET")
            headers_vnet = ['VNET ID', 'Name', 'Address Range', 'Location', 'Subscription']
            ws_vnet.write_row(0, 0, headers_vnet, fmt_head)
            row_vnet_idx = 1

            # --- 2. PREPARAR HOJA VM ---
            ws_vm = wb.add_worksheet("VM")
            headers_vm = ['VM ID', 'Nombre', 'Status', 'Created', 'Flavor', 'CPU', 'Memory (GB)', 'Image Name', 'Os Type', 'IP Address', 'AZ', 'TAGs', 'VNET', 'Host', 'RG', 'Location', 'Subscription']
            ws_vm.write_row(0, 0, headers_vm, fmt_head)
            row_vm_idx = 1

            # --- 3. PREPARAR HOJA DISK ---
            ws_disk = wb.add_worksheet("DISK")
            headers_disk = ['Name', 'Disk Size (GB)', 'Provisioning State', 'Disk Sku', 'Location', 'VM Managed', 'Time Created', 'Tier', 'Disk State', 'AZ', 'TAGs', 'RG', 'Encryption', 'Max Shares', 'Subscription', 'Monthly Price (Est)', 'Wasted Cost']
            ws_disk.write_row(0, 0, headers_disk, fmt_head)
            row_disk_idx = 1

            # --- 4. PREPARAR HOJA WEB APPS ---
            ws_web = wb.add_worksheet("WEB_APPS")
            headers_web = ['Name', 'State', 'HostName', 'Kind', 'AppServicePlan', 'Location', 'RG', 'HTTPS Only', 'Subscription']
            ws_web.write_row(0, 0, headers_web, fmt_head)
            row_web_idx = 1

            # --- 5. PREPARAR HOJA STORAGE ---
            ws_store = wb.add_worksheet("STORAGE")
            headers_store = ['Name', 'SKU', 'Kind', 'Location', 'RG', 'Access Tier', 'Blob Endpoint', 'Subscription']
            ws_store.write_row(0, 0, headers_store, fmt_head)
            row_store_idx = 1

            # --- 6. PREPARAR HOJA KEY VAULT ---
            ws_kv = wb.add_worksheet("KEY_VAULTS")
            headers_kv = ['Name', 'SKU', 'Location', 'RG', 'URI', 'Enabled Deploy', 'Enabled Disk Enc', 'Subscription']
            ws_kv.write_row(0, 0, headers_kv, fmt_head)
            row_kv_idx = 1

            for sub in lista_subs:
                sub_id = sub['id']
                sub_name = sub['name']
                print(f"--> Procesando Suscripcion: {sub_name}...")

                print(f"      Buscando backups en {sub_name}...")
                protected_set = reporter.obtener_vms_protegidas(sub_id)

                row_vnet_idx = reporter.listar_vnets(ws_vnet, row_vnet_idx, sub_id, sub_name, fmt_cell)
                row_vm_idx, stats_vm = reporter.listar_vms(ws_vm, row_vm_idx, sub_id, sub_name, fmt_cell, protected_set)

                datos_globales['vms_total'] += stats_vm['total']
                datos_globales['active'] += stats_vm['active']
                datos_globales['stopped'] += stats_vm['stopped']
                datos_globales['windows'] += stats_vm['windows']
                datos_globales['linux'] += stats_vm['linux']
                datos_globales['vms_protected'] += stats_vm['protected']
                datos_globales['vms_unprotected'] += stats_vm['unprotected']
                datos_globales['vms_ignored_backup'] += stats_vm['ignored_backup']

                row_disk_idx, stats_disk = reporter.listar_disks(ws_disk, row_disk_idx, sub_id, sub_name, fmt_cell, fmt_money, fmt_money_red)
                datos_globales['disks_total'] += stats_disk['total']
                datos_globales['disks_unattached'] += stats_disk['unattached']
                datos_globales['disks_in_use'] += stats_disk['in_use']
                datos_globales['wasted_money'] += stats_disk['wasted_money']

                row_web_idx, stats_web = reporter.listar_web_apps(ws_web, row_web_idx, sub_id, sub_name, fmt_cell)
                datos_globales['total_webapps'] += stats_web['total']
                datos_globales['webapps_running'] += stats_web['running']
                datos_globales['webapps_stopped'] += stats_web['stopped']

                row_store_idx = reporter.listar_storage(ws_store, row_store_idx, sub_id, sub_name, fmt_cell)
                row_kv_idx = reporter.listar_key_vaults(ws_kv, row_kv_idx, sub_id, sub_name, fmt_cell)

                proyectos.append({"nombre": sub['name'], "proveedor": "Azure"})

            wb.close()

            url_reporte = reporter.subir_excel_a_gcp(archivo_excel)
            if url_reporte:
                all_urls.append(url_reporte)

        if all_urls:
            datos_globales['archivosUrls'] = all_urls

        reporter.enviar_datos_a_api(datos_globales, CLIENTE_NOMBRE, proyectos)

        return (f"Ejecucion exitosa para {CLIENTE_NOMBRE}", 200)

    except Exception as e:
        error_msg = f"Fallo la ejecucion: {e}"
        print(error_msg)
        return (error_msg, 500)
