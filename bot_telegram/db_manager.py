from datetime import datetime
import pytz
from database import get_db  # Importas la conexión desde tu archivo database.py

def aplicar_interes_diario_habil():
    nyse_tz = pytz.timezone('America/New_York')
    ahora_nyse = datetime.now(nyse_tz)
    
    # Validar si es fin de semana
    if ahora_nyse.weekday() >= 5:
        print("⏸️ Fin de semana: La bolsa está cerrada.")
        return False

    conn = get_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT id, monto_inicial, monto_acumulado, ganancias_acumuladas, tope_ganancia FROM inversiones WHERE estado = 'activa'")
        inversiones = cursor.fetchall()
        
        for inv in inversiones:
            ganancia_diaria = inv["monto_inicial"] * 0.005
            nuevo_acumulado = inv["monto_acumulado"] + ganancia_diaria
            nuevas_ganancias = inv["ganancias_acumuladas"] + ganancia_diaria
            
            cursor.execute("""
                UPDATE inversiones 
                SET monto_acumulado = ?, ganancias_acumuladas = ?
                WHERE id = ?
            """, (nuevo_acumulado, nuevas_ganancias, inv["id"]))
            
        conn.commit()
        print("✅ Interés del 0.5% aplicado con éxito.")
        return True
    except Exception as e:
        print(f"❌ Error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()