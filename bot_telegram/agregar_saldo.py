from datetime import datetime
import pytz
from database import get_db

def agregar_capital_activo(telegram_id: int, monto_ingresar: float):
    conn = get_db()
    cursor = conn.cursor()

    # 1. Asegurar que el usuario exista en la tabla usuarios (por si es una inserción manual directa)
    cursor.execute(
        """
        INSERT OR IGNORE INTO usuarios (telegram_id, username, first_name, nombre_completo)
        VALUES (?, 'admin_added', 'Usuario', 'Registro Manual')
        """,
        (telegram_id,),
    )

    # 2. Registrar la transacción real de depósito completado
    cursor.execute(
        """
        INSERT INTO transacciones (telegram_id, tipo, monto, estado, tx_hash)
        VALUES (?, 'deposito', ?, 'completado', 'MANUAL_ADMIN')
        """,
        (telegram_id, monto_ingresar),
    )
    transaccion_id = cursor.lastrowid

    # 3. Calcular el tope de ganancia del 200% basado en el monto ingresado
    tope_ganancia = monto_ingresar * 2.0

    # Obtener fecha y hora actual en la zona horaria de la bolsa (NYSE)
    nyse_tz = pytz.timezone('America/New_York')
    ahora_nyse = datetime.now(nyse_tz).isoformat()

    # 4. Crear la inversión activa directamente con el capital asignado
    cursor.execute(
        """
        INSERT INTO inversiones (
            telegram_id, 
            transaccion_id, 
            monto_inicial, 
            monto_acumulado, 
            ganancias_acumuladas, 
            tope_ganancia, 
            fecha_activacion, 
            ultima_actualizacion, 
            estado
        ) 
        VALUES (?, ?, ?, ?, 0.0, ?, ?, ?, 'activa')
        """,
        (
            telegram_id, 
            transaccion_id, 
            monto_ingresar, 
            monto_ingresar, 
            tope_ganancia, 
            ahora_nyse, 
            ahora_nyse
        )
    )

    conn.commit()
    conn.close()
    
    print(f"\n¡Éxito! Se han agregado ${monto_ingresar:.2f} USDT al capital activo del usuario {telegram_id}.")
    print(f"• Tope de ganancia establecido (200%): ${tope_ganancia:.2f} USDT.")

if __name__ == "__main__":
    # ⚠️ REEMPLAZA ESTE NÚMERO POR TU ID REAL DE TELEGRAM ⚠️
    TU_TELEGRAM_ID = 7063310482  
    
    print("--- ASIGNADOR DE CAPITAL ACTIVO ---")
    try:
        entrada = input("¿Cuántos USDT deseas agregar al capital activo?: ")
        monto_deseado = float(entrada.strip().replace(",", "."))
        
        if monto_deseado <= 0:
            print("⚠️ El monto debe ser mayor a 0.")
        else:
            agregar_capital_activo(TU_TELEGRAM_ID, monto_deseado)
            
    except ValueError:
        print("⚠️ Por favor, ingresa un número válido.")