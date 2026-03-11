const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
});

exports.recibirInventario = async (req, res) => {
  // 1. Validar Seguridad
  const apiKeyHeader = req.headers['x-api-key'];
  if (apiKeyHeader !== process.env.API_KEY_SECRETA) {
    return res.status(401).send('Acceso Denegado: API Key inválida.');
  }

  const payload = req.body;
  if (!payload || !payload.origen || !payload.proveedor) {
    return res.status(400).send('Bad Request: Faltan datos en el JSON.');
  }

  // Obtenemos un cliente dedicado del pool para nuestra "Transacción"
  const client = await pool.connect();

  try {
    console.log(`Iniciando ingesta transaccional para: ${payload.origen}`);
    
    // INICIAR TRANSACCIÓN SEGURA
    await client.query('BEGIN');

    // --- 1. PROVEEDOR ---
    let provRes = await client.query('SELECT id FROM "Proveedor" WHERE nombre = $1', [payload.proveedor]);
    let proveedorId;
    if (provRes.rows.length > 0) {
      proveedorId = provRes.rows[0].id;
    } else {
      let insProv = await client.query('INSERT INTO "Proveedor" (nombre) VALUES ($1) RETURNING id', [payload.proveedor]);
      proveedorId = insProv.rows[0].id;
    }

    // --- 2. CLIENTE ---
    const slugCliente = payload.origen.toLowerCase().replace(/ /g, "-");
    let cliRes = await client.query('SELECT id FROM "Cliente" WHERE slug = $1', [slugCliente]);
    let clienteId;
    if (cliRes.rows.length > 0) {
      clienteId = cliRes.rows[0].id;
    } else {
      let insCli = await client.query('INSERT INTO "Cliente" (nombre, slug) VALUES ($1, $2) RETURNING id', [payload.origen, slugCliente]);
      clienteId = insCli.rows[0].id;
    }

    // --- 3. PROYECTO (principal para el reporte) ---
    const nombreProyecto = `Infra-${payload.proveedor}-${payload.origen}`;
    let proyRes = await client.query('SELECT id FROM "Proyecto" WHERE nombre = $1 AND "clienteId" = $2', [nombreProyecto, clienteId]);
    let proyectoId;
    if (proyRes.rows.length > 0) {
      proyectoId = proyRes.rows[0].id;
    } else {
      let insProy = await client.query('INSERT INTO "Proyecto" (nombre, "clienteId", "proveedorId") VALUES ($1, $2, $3) RETURNING id', [nombreProyecto, clienteId, proveedorId]);
      proyectoId = insProy.rows[0].id;
    }

    // --- 3b. PROYECTOS/SUSCRIPCIONES del cliente ---
    if (payload.proyectos && payload.proyectos.length > 0) {
      for (let proy of payload.proyectos) {
        const nombreSub = proy.nombre;
        let subProvId = proveedorId;
        if (proy.proveedor && proy.proveedor !== payload.proveedor) {
          let spRes = await client.query('SELECT id FROM "Proveedor" WHERE nombre = $1', [proy.proveedor]);
          if (spRes.rows.length > 0) {
            subProvId = spRes.rows[0].id;
          } else {
            let insSprov = await client.query('INSERT INTO "Proveedor" (nombre) VALUES ($1) RETURNING id', [proy.proveedor]);
            subProvId = insSprov.rows[0].id;
          }
        }
        let existRes = await client.query('SELECT id FROM "Proyecto" WHERE nombre = $1 AND "clienteId" = $2', [nombreSub, clienteId]);
        if (existRes.rows.length === 0) {
          await client.query('INSERT INTO "Proyecto" (nombre, "clienteId", "proveedorId") VALUES ($1, $2, $3)', [nombreSub, clienteId, subProvId]);
        }
      }
    }
    
    // --- 4. REPORTE ---
    const stats = payload.estadisticas;
    let insRep = await client.query(`
      INSERT INTO "Reporte" 
      ("fecha", "proyectoId", "totalVMs", "vmsEncendidas", "vmsApagadas", "totalDiscos", "discosHuerfanos", "costoDesperdicio", "archivoUrl", "totalCompromisos", "vmsProtegidas", "vmsSinRespaldo", "vmsIgnoradasRespaldo", "vmsLinux", "vmsWindows") 
      VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15) RETURNING id
    `, [
      payload.fecha || new Date(),
      proyectoId, stats.total_vms, stats.active, stats.stopped, 
      stats.total_disks, stats.disks_unattached, stats.wasted_money, payload.archivoUrl || null,
      stats.total_compromisos || 0, stats.vms_protected || 0, stats.vms_unprotected || 0,
      stats.vms_ignored_backup || 0, stats.vms_linux || 0, stats.vms_windows || 0
    ]);
    const reporteId = insRep.rows[0].id;

    // --- 5. DETALLE VMs ---
    if (payload.vms && payload.vms.length > 0) {
      const vmQuery = `INSERT INTO "DetalleVM" ("reporteId", "nombre", "estado", "tipoInstancia", "ipPrivada", "so", "tieneRespaldo", "metodoRespaldo", "evidenciaRespaldo", "resourceGroup") VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)`;
      for (let vm of payload.vms) {
        await client.query(vmQuery, [
          reporteId, vm.nombre, vm.estado, vm.tipoInstancia, vm.ipPrivada, vm.so,
          vm.tieneRespaldo || false, vm.metodoRespaldo || 'Ninguno', vm.evidenciaRespaldo || '', vm.resourceGroup || ''
        ]);
      }
    }

    // --- 6. DETALLE DISCOS ---
    if (payload.discos && payload.discos.length > 0) {
      const diskQuery = `INSERT INTO "DetalleDisco" ("reporteId", "nombre", "estado", "tamanoGB", "resourceGroup") VALUES ($1, $2, $3, $4, $5)`;
      for (let disco of payload.discos) {
        await client.query(diskQuery, [reporteId, disco.nombre, disco.estado, disco.tamanoGB, disco.resourceGroup]);
      }
    }

    // --- 7. DETALLE COMPROMISOS ---
    if (payload.compromisos && payload.compromisos.length > 0) {
      const compQuery = `INSERT INTO "DetalleCompromiso" ("reporteId", "nombre", "region", "estado", "fechaFin", "diasRestantes", "creadoEn") VALUES ($1, $2, $3, $4, $5, $6, $7)`;
      for (let comp of payload.compromisos) {
        await client.query(compQuery, [
          reporteId, comp.nombre, comp.region, comp.estado,
          comp.fechaFin || null, comp.diasRestantes || 0, comp.creadoEn || null
        ]);
      }
    }

    // SI TODO SALIÓ BIEN, GUARDAMOS LOS CAMBIOS
    await client.query('COMMIT');
    console.log("Transacción exitosa. Datos guardados en ITAM.");
    res.status(200).send(`Inventario de ${payload.origen} procesado y guardado correctamente.`);

  } catch (error) {
    // SI HAY UN ERROR, REVERTIMOS TODO LO DE ESTE INTENTO
    await client.query('ROLLBACK');
    console.error("Error en la transacción DB. Cambios revertidos:", error);
    res.status(500).send(`Error interno procesando los datos: ${error.message}`);
  } finally {
    // Liberamos la conexión para que otro pueda usarla
    client.release();
  }
};

