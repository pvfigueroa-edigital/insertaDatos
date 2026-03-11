"""
Extractor Huawei Cloud para Google Cloud Functions.

Extrae la misma información que orizon_cloudfunction/s1.py:
- VPCs (hoja VPC)
- Instancias ECS (hoja ECS): ID, nombre, estado, flavor, CPU, RAM, imagen, SO, IP, SG, AZ, tags, VPC, DEH
- Discos EVS (hoja EVS): ID, nombre, tamaño, estado, tipo, costo mensual, desperdicio, AZ, servidor adjunto
- Backups CBR para detectar VMs protegidas

El Scheduler invoca la función con payload: {"cliente": "NOMBRE_CLIENTE"}.
Credenciales por cliente en config.json (access_key, secret_key, project_id, region).

Variables de entorno en la Cloud Function:
- BUCKET_REPORTES: bucket GCS donde subir el Excel
- API_INGESTA_URL: URL de la Cloud Function de ingesta
- API_KEY_SECRETA: API key para la ingesta
"""
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Tuple, Set

import requests
import xlsxwriter
from google.cloud import storage

from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkecs.v2.region.ecs_region import EcsRegion
from huaweicloudsdkecs.v2 import EcsClient, ListServersDetailsRequest
from huaweicloudsdkevs.v2.region.evs_region import EvsRegion
from huaweicloudsdkevs.v2 import EvsClient, ListVolumesRequest
from huaweicloudsdkvpc.v2.region.vpc_region import VpcRegion
from huaweicloudsdkvpc.v2 import VpcClient, ListVpcsRequest
from huaweicloudsdkcbr.v1.region.cbr_region import CbrRegion
from huaweicloudsdkcbr.v1 import CbrClient, ListBackupsRequest


CL_TZ = timezone(timedelta(hours=-3))


class HuaweiPricingService:
    def __init__(self) -> None:
        self.prices = {
            "SATA": 0.03,
            "SAS": 0.06,
            "SSD": 0.12,
            "GPSSD": 0.10,
            "ESSD": 0.15,
        }

    def get_disk_price(self, volume_type: str, size_gb: int) -> float:
        if not volume_type or not size_gb:
            return 0.0
        vtype = volume_type.upper()
        unit_price = 0.03
        if "SSD" in vtype and "GP" in vtype:
            unit_price = self.prices["GPSSD"]
        elif "SSD" in vtype:
            unit_price = self.prices["SSD"]
        elif "SAS" in vtype:
            unit_price = self.prices["SAS"]
        elif "SATA" in vtype:
            unit_price = self.prices["SATA"]
        return unit_price * float(size_gb)


class HuaweiMultiReporter:
    def __init__(self, config_path: str, cliente_nombre: str) -> None:
        self.config_path = config_path
        self.config_full = self._load_config()

        if cliente_nombre not in self.config_full:
            raise ValueError(f"Cliente no encontrado en config.json: {cliente_nombre}")

        self.config = self.config_full[cliente_nombre]
        self.client_name = cliente_nombre
        self.account_name = self.config.get("account_name", cliente_nombre)

        self.project_id = self.config["project_id"]
        self.region = self.config["region"]
        self.credentials = BasicCredentials(
            self.config["access_key"],
            self.config["secret_key"],
        ).with_project_id(self.project_id)

        self.ecs_client = EcsClient.new_builder() \
            .with_credentials(self.credentials) \
            .with_region(EcsRegion.value_of(self.region)) \
            .build()
        self.evs_client = EvsClient.new_builder() \
            .with_credentials(self.credentials) \
            .with_region(EvsRegion.value_of(self.region)) \
            .build()
        self.vpc_client = VpcClient.new_builder() \
            .with_credentials(self.credentials) \
            .with_region(VpcRegion.value_of(self.region)) \
            .build()
        self.cbr_client = CbrClient.new_builder() \
            .with_credentials(self.credentials) \
            .with_region(CbrRegion.value_of(self.region)) \
            .build()

        self.pricing_service = HuaweiPricingService()
        self.db_vms: List[Dict[str, Any]] = []
        self.db_disks: List[Dict[str, Any]] = []

    def _load_config(self) -> Dict[str, Any]:
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"No se encontro el archivo: {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def subir_excel_a_gcp(self, archivo_excel: str) -> str:
        print("Subiendo reporte a Google Cloud Storage...")
        try:
            bucket_name = os.environ.get("BUCKET_REPORTES")
            if not bucket_name:
                print("No se encontro variable de entorno BUCKET_REPORTES.")
                return ""

            storage_client = storage.Client()
            bucket = storage_client.bucket(bucket_name)

            filename = os.path.basename(archivo_excel)
            ruta_destino = f"{datetime.now(CL_TZ).strftime('%Y/%m')}/{filename}"
            blob = bucket.blob(ruta_destino)
            blob.upload_from_filename(
                archivo_excel,
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            public_url = blob.public_url
            print(f"Excel subido exitosamente: {public_url}")
            return public_url
        except Exception as e:
            print(f"Error al subir a GCS: {e}")
            return ""

    def enviar_datos_a_api(self, stats_globales: Dict[str, Any], client_name: str, proyectos: list) -> None:
        api_url = os.environ.get("API_INGESTA_URL")
        api_key = os.environ.get("API_KEY_SECRETA")

        if not api_url or not api_key:
            print("Faltan variables de entorno API_INGESTA_URL o API_KEY_SECRETA.")
            return

        print(f"Enviando datos a Cloud Function: {api_url}")
        try:
            stats = stats_globales
            payload = {
                "origen": client_name,
                "proveedor": "Huawei",
                "fecha": datetime.now(CL_TZ).strftime("%Y-%m-%dT%H:%M:%S"),
                "estadisticas": {
                    "total_vms": stats.get("total_vms", 0),
                    "active": stats.get("active", 0),
                    "stopped": stats.get("stopped", 0),
                    "total_disks": stats.get("total_disks", 0),
                    "disks_unattached": stats.get("disks_unattached", 0),
                    "wasted_money": stats.get("wasted_money", 0.0),
                    "total_compromisos": 0,
                    "vms_protected": stats.get("vms_protected", 0),
                    "vms_unprotected": stats.get("vms_unprotected", 0),
                    "vms_ignored_backup": stats.get("vms_ignored_backup", 0),
                    "vms_linux": stats.get("linux", 0),
                    "vms_windows": stats.get("windows", 0),
                },
                "archivoUrl": stats_globales.get("archivoUrl", ""),
                "vms": self.db_vms,
                "discos": self.db_disks,
                "compromisos": [],
                "proyectos": proyectos,
            }

            headers = {
                "Content-Type": "application/json",
                "x-api-key": api_key,
            }

            response = requests.post(api_url, json=payload, headers=headers, timeout=60)

            if response.status_code == 200:
                print(f"Datos procesados y guardados en BD. Respuesta: {response.text}")
            else:
                print(f"Error de la API (Codigo {response.status_code}): {response.text}")
        except Exception as e:
            print(f"Error de red intentando enviar datos a la API: {e}")

    def listar_vpcs(self, workbook: xlsxwriter.Workbook, fmt_head, fmt_cell) -> int:
        try:
            request = ListVpcsRequest()
            response = self.vpc_client.list_vpcs(request)
            vpcs = response.vpcs or []

            ws = workbook.add_worksheet("VPC")
            headers = [
                "VPC ID",
                "Name",
                "Cidr",
                "Status",
                "Created",
                "Description",
            ]
            ws.write_row(0, 0, headers, fmt_head)

            for row, vpc in enumerate(vpcs, start=1):
                ws.write(row, 0, vpc.id, fmt_cell)
                ws.write(row, 1, vpc.name, fmt_cell)
                ws.write(row, 2, vpc.cidr, fmt_cell)
                ws.write(row, 3, vpc.status, fmt_cell)
                ws.write(row, 4, str(vpc.created_at), fmt_cell)
                ws.write(row, 5, vpc.description, fmt_cell)
            return len(vpcs)
        except Exception as e:
            print(f"Error en VPCs: {e}")
            return 0

    def obtener_vms_protegidas(self) -> Set[str]:
        protected_ids: Set[str] = set()
        try:
            request = ListBackupsRequest()
            response = self.cbr_client.list_backups(request)
            data = response.to_dict()

            today = datetime.now(CL_TZ).date().isoformat()
            backups = data.get("backups", []) or []
            for backup in backups:
                created_at = str(backup.get("created_at", ""))
                created_date = created_at.split("T")[0].split(" ")[0]
                if created_date == today:
                    resource_id = backup.get("resource_id")
                    if resource_id:
                        protected_ids.add(resource_id)
        except Exception as e:
            print(f"Error consultando CBR backups: {e}")
        return protected_ids

    def _list_all_servers(self) -> List[Any]:
        servers: List[Any] = []
        offset = 0
        limit = 100
        while True:
            request = ListServersDetailsRequest()
            request.limit = limit
            request.offset = offset
            response = self.ecs_client.list_servers_details(request)
            batch = response.servers or []
            servers.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
        return servers

    def listar_instancias(
        self,
        workbook: xlsxwriter.Workbook,
        fmt_head,
        fmt_cell,
        protected_ids: Set[str],
    ) -> Tuple[int, int, int, int, int, int, int, int, List[Tuple[str, str]]]:
        servers = self._list_all_servers()

        ws = workbook.add_worksheet("ECS")
        headers = [
            "Server ID",
            "Name",
            "Status",
            "Created",
            "Flavor",
            "CPU",
            "Memory (GB)",
            "Image Name",
            "Os Type",
            "IP Address",
            "Security Group",
            "AZ",
            "Tags",
            "VPC ID",
            "DEH ID",
        ]
        ws.write_row(0, 0, headers, fmt_head)

        stats = {
            "active": 0,
            "stopped": 0,
            "windows": 0,
            "linux": 0,
            "protected": 0,
            "unprotected": 0,
            "ignored_backup": 0,
        }
        server_info_list: List[Tuple[str, str]] = []
        self.db_vms = []

        for row, server in enumerate(servers, start=1):
            s_dict = server.to_dict() if hasattr(server, "to_dict") else {}
            status = s_dict.get("status", getattr(server, "status", "UNKNOWN"))

            db_status = "UNKNOWN"
            if status == "ACTIVE":
                stats["active"] += 1
                db_status = "RUNNING"
            elif status == "SHUTOFF":
                stats["stopped"] += 1
                db_status = "STOPPED"

            server_id = s_dict.get("id", "")
            is_protected = server_id in protected_ids
            if is_protected:
                stats["protected"] += 1
            elif db_status == "RUNNING":
                stats["unprotected"] += 1
            else:
                stats["ignored_backup"] += 1

            metadata = s_dict.get("metadata", {}) or {}
            os_type = metadata.get("os_type", "Unknown")
            if os_type == "Windows":
                stats["windows"] += 1
            elif os_type == "Linux":
                stats["linux"] += 1

            flavor = s_dict.get("flavor", {}) or {}
            flavor_id = flavor.get("id", "")
            vcpus = int(flavor.get("vcpus", 0) or 0)
            ram_gb = float(flavor.get("ram", 0) or 0) / 1024
            image_name = metadata.get("image_name", "")
            vpc_id = metadata.get("vpc_id", "")
            az = s_dict.get("os_ext_a_zavailability_zone", "")

            deh_id = ""
            hints = s_dict.get("osscheduler_hints")
            if hints and isinstance(hints, dict):
                deh_id = hints.get("dedicated_host_id", "")
                if isinstance(deh_id, list) and len(deh_id) > 0:
                    deh_id = deh_id[0]

            ip_addresses: List[str] = []
            if getattr(server, "addresses", None):
                for _, addrs in server.addresses.items():
                    for addr in addrs:
                        if hasattr(addr, "addr"):
                            ip_addresses.append(addr.addr)
                        elif isinstance(addr, dict):
                            ip_addresses.append(addr.get("addr", ""))

            sg_names: List[str] = []
            if getattr(server, "security_groups", None):
                for sg in server.security_groups:
                    if hasattr(sg, "name"):
                        sg_names.append(sg.name)
                    elif isinstance(sg, dict):
                        sg_names.append(sg.get("name", ""))

            tags = str(s_dict.get("tags", []))
            server_info_list.append((server_id, s_dict.get("name", "")))

            data = [
                server_id,
                s_dict.get("name", ""),
                status,
                str(s_dict.get("created", "")),
                flavor_id,
                vcpus,
                ram_gb,
                image_name,
                os_type,
                ", ".join([ip for ip in ip_addresses if ip]),
                ", ".join([sg for sg in sg_names if sg]),
                str(az),
                tags,
                str(vpc_id),
                str(deh_id),
            ]
            ws.write_row(row, 0, data, fmt_cell)

            self.db_vms.append(
                {
                    "nombre": s_dict.get("name", ""),
                    "estado": db_status,
                    "tipoInstancia": flavor_id,
                    "ipPrivada": ip_addresses[0] if ip_addresses else "",
                    "so": os_type,
                    "tieneRespaldo": is_protected,
                    "metodoRespaldo": "Huawei CBR" if is_protected else "Ninguno",
                    "evidenciaRespaldo": "Vault CBR" if is_protected else "",
                    "resourceGroup": str(az),
                }
            )

        total = len(servers)
        return (
            total,
            stats["active"],
            stats["stopped"],
            stats["windows"],
            stats["linux"],
            stats["protected"],
            stats["unprotected"],
            stats["ignored_backup"],
            server_info_list,
        )

    def listar_disks(
        self,
        workbook: xlsxwriter.Workbook,
        fmt_head,
        fmt_cell,
        fmt_money,
        fmt_money_red,
        server_info_list: List[Tuple[str, str]],
    ) -> Tuple[int, int, int, float]:
        try:
            request = ListVolumesRequest()
            response = self.evs_client.list_volumes(request)
            volumes = response.volumes or []
        except Exception as e:
            print(f"Error listando discos EVS: {e}")
            return 0, 0, 0, 0.0

        ws = workbook.add_worksheet("EVS")
        headers = [
            "Volume ID",
            "Name",
            "Size (GB)",
            "Status",
            "Type",
            "Monthly Cost",
            "Wasted Cost",
            "AZ",
            "Attached Server",
            "Created",
            "Service Type",
            "Device",
        ]
        ws.write_row(0, 0, headers, fmt_head)

        stats = {"available": 0, "in_use": 0, "wasted_money": 0.0}
        self.db_disks = []

        server_name_by_id = {sid: sname for sid, sname in server_info_list}

        for row, volume in enumerate(volumes, start=1):
            if volume.status == "available":
                stats["available"] += 1
            elif volume.status == "in-use":
                stats["in_use"] += 1

            monthly_price = self.pricing_service.get_disk_price(volume.volume_type, volume.size)

            wasted = 0.0
            if volume.status == "available":
                wasted = monthly_price
                stats["wasted_money"] += wasted

            server_id = ""
            device = ""
            ecs_server_name = ""

            if volume.attachments:
                server_id = volume.attachments[0].server_id
                device = volume.attachments[0].device
                ecs_server_name = server_name_by_id.get(server_id, "")

            ws.write(row, 0, volume.id, fmt_cell)
            ws.write(row, 1, volume.name, fmt_cell)
            ws.write(row, 2, volume.size, fmt_cell)
            ws.write(row, 3, volume.status, fmt_cell)
            ws.write(row, 4, volume.volume_type, fmt_cell)
            ws.write(row, 5, monthly_price, fmt_money)
            ws.write(row, 6, wasted, fmt_money_red if wasted > 0 else fmt_money)
            ws.write(row, 7, volume.availability_zone, fmt_cell)
            ws.write(row, 8, ecs_server_name if ecs_server_name else server_id, fmt_cell)
            ws.write(row, 9, str(volume.created_at), fmt_cell)
            ws.write(row, 10, volume.service_type, fmt_cell)
            ws.write(row, 11, device, fmt_cell)

            self.db_disks.append(
                {
                    "nombre": volume.name,
                    "estado": volume.status,
                    "tamanoGB": volume.size,
                    "resourceGroup": volume.availability_zone,
                }
            )

        total = len(volumes)
        return total, stats["available"], stats["in_use"], stats["wasted_money"]


def ejecutar_recoleccion(request):
    """
    Entry point para Google Cloud Functions (HTTP).
    Payload esperado: {"cliente": "NOMBRE_CLIENTE"} (debe existir en config.json).
    """
    try:
        request_json = request.get_json(silent=True) or {}
        if isinstance(request_json, dict) and "cliente" in request_json:
            cliente_nombre = request_json["cliente"]
        else:
            cliente_nombre = os.environ.get("CLIENTE_DEFAULT", "")

        if not cliente_nombre:
            return ("Falta cliente en request y CLIENTE_DEFAULT no esta definido", 400)

        print(f"Iniciando recoleccion Huawei para cliente: {cliente_nombre}")

        reporter = HuaweiMultiReporter("config.json", cliente_nombre)
        fecha = datetime.now(CL_TZ).strftime("%Y-%m-%d")
        safe_name = cliente_nombre.replace(" ", "_")
        archivo_excel = f"/tmp/Inventario_FinOps_Huawei_{safe_name}_{fecha}.xlsx"

        wb = xlsxwriter.Workbook(archivo_excel)
        fmt_head = wb.add_format({"bold": True, "bg_color": "#B8CCE4", "border": 1})
        fmt_cell = wb.add_format({"bg_color": "#DCE6F1", "border": 1})
        fmt_money = wb.add_format({"num_format": "$#,##0.00", "bg_color": "#DCE6F1", "border": 1})
        fmt_money_red = wb.add_format(
            {"num_format": "$#,##0.00", "bg_color": "#FFC7CE", "font_color": "#9C0006", "border": 1, "bold": True}
        )

        reporter.listar_vpcs(wb, fmt_head, fmt_cell)
        print("Buscando backups en Huawei CBR...")
        protected_ids = reporter.obtener_vms_protegidas()
        print(f"VMs con backup detectadas en CBR: {len(protected_ids)}")

        (
            total_vms,
            active,
            stopped,
            windows,
            linux,
            protected_count,
            unprotected_count,
            ignored_backup_count,
            server_info_list,
        ) = reporter.listar_instancias(wb, fmt_head, fmt_cell, protected_ids)

        total_disks, available, in_use, wasted_money = reporter.listar_disks(
            wb, fmt_head, fmt_cell, fmt_money, fmt_money_red, server_info_list
        )

        wb.close()

        stats = {
            "total_vms": total_vms,
            "active": active,
            "stopped": stopped,
            "windows": windows,
            "linux": linux,
            "vms_protected": protected_count,
            "vms_unprotected": unprotected_count,
            "vms_ignored_backup": ignored_backup_count,
            "total_disks": total_disks,
            "disks_unattached": available,
            "disks_in_use": in_use,
            "wasted_money": wasted_money,
        }

        url_reporte = reporter.subir_excel_a_gcp(archivo_excel)
        stats["archivoUrl"] = url_reporte

        proyectos = [{"nombre": reporter.account_name, "proveedor": "Huawei"}]
        reporter.enviar_datos_a_api(stats, cliente_nombre, proyectos)

        return (f"Ejecucion exitosa para {cliente_nombre}", 200)
    except Exception as e:
        error_msg = f"Fallo la ejecucion de Huawei: {e}"
        print(error_msg)
        return (error_msg, 500)

