import sqlite3
from datetime import datetime

DB_NAME = "contabilidad.db"


def get_db():
    conn = sqlite3.connect(DB_NAME)
    # CLAVE: Permite que las filas se lean como diccionarios (ej. inv["monto_inicial"])
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # 1. Tabla de Usuarios
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS usuarios (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            nombre_completo TEXT NULL,
            email TEXT NULL,
            telefono TEXT NULL,
            fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # 2. Tabla de Transacciones / Depósitos y Retiros
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS transacciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            tipo TEXT CHECK(tipo IN ('deposito', 'retiro')),
            monto REAL DEFAULT 0.0,
            comision REAL DEFAULT 0.0,
            red TEXT DEFAULT 'BEP-20 (USDT)',
            tx_hash TEXT NULL,
            direccion_billetera TEXT NULL,
            comprobante_file_id TEXT NULL,
            estado TEXT DEFAULT 'pendiente',
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (telegram_id) REFERENCES usuarios(telegram_id)
        )
        """
    )

    # 3. Tabla de Inversiones / Rendimientos
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS inversiones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            transaccion_id INTEGER,
            monto_inicial REAL NOT NULL,
            monto_acumulado REAL DEFAULT 0.0,
            ganancias_acumuladas REAL DEFAULT 0.0,
            tope_ganancia REAL NOT NULL,
            fecha_carga TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            fecha_activacion TIMESTAMP NULL,
            ultima_actualizacion TIMESTAMP NULL,
            estado TEXT DEFAULT 'activa',
            FOREIGN KEY (telegram_id) REFERENCES usuarios(telegram_id),
            FOREIGN KEY (transaccion_id) REFERENCES transacciones(id)
        )
        """
    )

    # 4. Tabla de Configuración (Controles ON/OFF del Admin)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS configuracion (
            clave TEXT PRIMARY KEY,
            valor INTEGER DEFAULT 1
        )
        """
    )

    cursor.execute(
        "INSERT OR IGNORE INTO configuracion (clave, valor) VALUES"
        " ('depositos_activos', 1)"
    )
    cursor.execute(
        "INSERT OR IGNORE INTO configuracion (clave, valor) VALUES"
        " ('retiros_activos', 1)"
    )

    conn.commit()
    conn.close()


def usuario_registrado(telegram_id: int) -> bool:
    """Verifica si el usuario ya completó el flujo de registro con su nombre completo."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM usuarios WHERE telegram_id = ? AND nombre_completo IS"
        " NOT NULL",
        (telegram_id,),
    )
    existe = cursor.fetchone() is not None
    conn.close()
    return existe


def guardar_registro_completo(
    telegram_id: int,
    username: str,
    first_name: str,
    nombre_completo: str,
    email: str,
    telefono: str,
):
    """Guarda o actualiza la información personal del usuario."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO usuarios (telegram_id, username, first_name, nombre_completo, email, telefono)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username = COALESCE(NULLIF(excluded.username, ''), usuarios.username),
            first_name = COALESCE(NULLIF(excluded.first_name, ''), usuarios.first_name),
            nombre_completo = COALESCE(NULLIF(excluded.nombre_completo, ''), usuarios.nombre_completo),
            email = COALESCE(NULLIF(excluded.email, ''), usuarios.email),
            telefono = COALESCE(NULLIF(excluded.telefono, ''), usuarios.telefono)
        """,
        (telegram_id, username, first_name, nombre_completo, email, telefono),
    )
    conn.commit()
    conn.close()


def registrar_deposito(
    telegram_id: int, monto: float, tx_hash: str, file_id: str
) -> int:
    """Registra una solicitud de depósito en estado pendiente y retorna su ID de transacción."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO transacciones (telegram_id, tipo, monto, tx_hash, comprobante_file_id, estado)
        VALUES (?, 'deposito', ?, ?, ?, 'pendiente')
        """,
        (telegram_id, monto, tx_hash, file_id),
    )
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return tx_id


def registrar_retiro(
    telegram_id: int, monto: float, comision: float, direccion_billetera: str
) -> int:
    """Registra una solicitud de retiro en estado pendiente y retorna su ID de transacción."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO transacciones (telegram_id, tipo, monto, comision, direccion_billetera, estado)
        VALUES (?, 'retiro', ?, ?, ?, 'pendiente')
        """,
        (telegram_id, monto, comision, direccion_billetera),
    )
    tx_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return tx_id


def obtener_configuracion(clave: str) -> int:
    """Obtiene el estado de una configuración (1 para Activo, 0 para Desactivado)."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT valor FROM configuracion WHERE clave = ?", (clave,))
    res = cursor.fetchone()
    conn.close()
    return res[0] if res else 1


def actualizar_configuracion(clave: str, valor: int):
    """Actualiza una opción del panel de administración."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE configuracion SET valor = ? WHERE clave = ?", (valor, clave)
    )
    conn.commit()
    conn.close()


def aplicar_rendimiento_diario(porcentaje_diario: float = 1.0):
    """
    Aplica un porcentaje de rendimiento diario a todas las inversiones activas,
    acumulando ganancias sin superar el tope establecido.
    """
    conn = get_db()
    cursor = conn.cursor()
    
    # Seleccionar todas las inversiones activas
    cursor.execute("""
        SELECT id, monto_inicial, monto_acumulado, ganancias_acumuladas, tope_ganancia 
        FROM inversiones WHERE estado = 'activa'
    """)
    inversiones = cursor.fetchall()
    
    ahora = datetime.now().isoformat()
    
    for inv in inversiones:
        inv_id = inv["id"]
        monto_inicial = inv["monto_inicial"]
        ganancias_actuales = inv["ganancias_acumuladas"]
        tope_ganancia = inv["tope_ganancia"]
        monto_acumulado = inv["monto_acumulado"]
        
        # Calcular ganancia del día en base al monto inicial
        ganancia_generada = monto_inicial * (porcentaje_diario / 100.0)
        
        nueva_ganancia = ganancias_actuales + ganancia_generada
        nuevo_monto_acumulado = monto_acumulado + ganancia_generada
        
        # Verificar si alcanza o supera el tope
        if nuevo_monto_acumulado >= tope_ganancia:
            nuevo_monto_acumulado = tope_ganancia
            nueva_ganancia = tope_ganancia - monto_inicial
            cursor.execute("""
                UPDATE inversiones 
                SET ganancias_acumuladas = ?, monto_acumulado = ?, ultima_actualizacion = ?, estado = 'completada'
                WHERE id = ?
            """, (nueva_ganancia, nuevo_monto_acumulado, ahora, inv_id))
        else:
            cursor.execute("""
                UPDATE inversiones 
                SET ganancias_acumuladas = ?, monto_acumulado = ?, ultima_actualizacion = ?
                WHERE id = ?
            """, (nueva_ganancia, nuevo_monto_acumulado, ahora, inv_id))
            
    conn.commit()
    conn.close()


def obtener_resumen_financiero(telegram_id: int) -> dict:
    """Calcula y retorna un resumen completo del estado de cuenta del usuario."""
    conn = get_db()
    cursor = conn.cursor()

    # 1. Sumar depósitos pendientes
    cursor.execute(
        """
        SELECT COALESCE(SUM(monto), 0.0) FROM transacciones 
        WHERE telegram_id = ? AND tipo = 'deposito' AND estado = 'pendiente'
    """,
        (telegram_id,),
    )
    deposito_pendiente = cursor.fetchone()[0]

    # 2. Consultar inversiones activas y sus montos/ganancias
    cursor.execute(
        """
        SELECT id, monto_inicial, monto_acumulado, ganancias_acumuladas, tope_ganancia 
        FROM inversiones WHERE telegram_id = ? AND estado = 'activa'
    """,
        (telegram_id,),
    )
    inversiones_activas = cursor.fetchall()

    # 3. Sumar retiros realizados (completados)
    cursor.execute(
        """
        SELECT COALESCE(SUM(monto + comision), 0.0) FROM transacciones 
        WHERE telegram_id = ? AND tipo = 'retiro' AND estado = 'completado'
    """,
        (telegram_id,),
    )
    retiros_realizados = cursor.fetchone()[0]

    # 4. Sumar retiros pendientes
    cursor.execute(
        """
        SELECT COALESCE(SUM(monto + comision), 0.0) FROM transacciones 
        WHERE telegram_id = ? AND tipo = 'retiro' AND estado = 'pendiente'
    """,
        (telegram_id,),
    )
    retiros_pendientes = cursor.fetchone()[0]

    conn.close()

    total_invertido = sum(inv["monto_inicial"] for inv in inversiones_activas)
    ganancia_actual = sum(inv["ganancias_acumuladas"] for inv in inversiones_activas)

    capital_liberado_total = 0.0
    pendiente_por_ganar_total = 0.0

    for inv in inversiones_activas:
        m_inicial = inv["monto_inicial"]
        g_acumuladas = inv["ganancias_acumuladas"]
        t_ganancia = inv["tope_ganancia"]
        
        if g_acumuladas >= m_inicial:
            capital_liberado_total += m_inicial
            pendiente_por_ganar_total += max(0.0, t_ganancia - inv["monto_acumulado"])
        else:
            pendiente_por_ganar_total += max(0.0, t_ganancia - inv["monto_acumulado"])

    fondos_retirables = ganancia_actual + capital_liberado_total
    balance_disponible = max(0.0, fondos_retirables - retiros_realizados - retiros_pendientes)

    return {
        "deposito_pendiente": deposito_pendiente,
        "inversiones_activas": inversiones_activas,
        "total_invertido": total_invertido,
        "ganancia_actual": ganancia_actual,
        "pendiente_por_ganar": pendiente_por_ganar_total,
        "retiros_realizados": retiros_realizados,
        "retiros_pendientes": retiros_pendientes,
        "balance_disponible": balance_disponible,
    }


if __name__ == "__main__":
    init_db()
    print("Base de datos inicializada correctamente.")