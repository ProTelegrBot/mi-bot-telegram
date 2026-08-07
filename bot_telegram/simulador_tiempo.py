from datetime import datetime, date
import pytz
from database import get_db

def contar_dias_habiles(fecha_inicio: date, fecha_fin: date) -> int:
    """Calcula cuántos días hábiles (lunes a viernes) hay entre dos fechas."""
    dias_habiles = 0
    actual = fecha_inicio
    while actual <= fecha_fin:
        if actual.weekday() < 5:
            dias_habiles += 1
        actual = actual.fromordinal(actual.toordinal() + 1)
    return dias_habiles

def simular_por_fecha_global(fecha_inicio_str: str):
    conn = get_db()
    cursor = conn.cursor()

    # 1. Convertir la fecha ingresada por el usuario
    try:
        fecha_simulada_inicio = datetime.strptime(fecha_inicio_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        print("\n⚠️ Formato de fecha incorrecto. Debe ser AAAA-MM-DD (Ej: 2026-06-01).")
        conn.close()
        return

    hoy = date.today()

    if fecha_simulada_inicio > hoy:
        print("\n⚠️ La fecha simulada no puede ser en el futuro.")
        conn.close()
        return

    # 2. Calcular días totales y días hábiles transcurridos
    dias_totales = (hoy - fecha_simulada_inicio).days
    dias_habiles = contar_dias_habiles(fecha_simulada_inicio, hoy)

    # 3. Establecer la hora exacta con la zona horaria de Nueva York (NYSE) de forma segura
    nyse_tz = pytz.timezone('America/New_York')
    nueva_fecha_activacion = datetime.combine(fecha_simulada_inicio, datetime.min.time())
    nueva_fecha_activacion = nyse_tz.localize(nueva_fecha_activacion, is_dst=False)
    
    ahora_nyse = datetime.now(nyse_tz)

    # 4. Actualizar TODAS las inversiones que estén en estado 'activa' de una sola vez
    cursor.execute(
        """
        UPDATE inversiones 
        SET fecha_activacion = ?, ultima_actualizacion = ?
        WHERE estado = 'activa'
        """,
        (
            nueva_fecha_activacion.isoformat(),
            ahora_nyse.isoformat()
        )
    )

    # Contar cuántas inversiones fueron actualizadas
    inversiones_afectadas = cursor.rowcount

    conn.commit()
    conn.close()

    print("\n" + "="*45)
    print("⏳ ¡SIMULACIÓN GLOBAL APLICADA CON ÉXITO!")
    print("="*45)
    print(f"• Inversiones activas actualizadas: {inversiones_afectadas} usuario(s)")
    print(f"• Fecha de inicio asignada a todos: {fecha_simulada_inicio.strftime('%Y-%m-%d')}")
    print(f"• Días calendario transcurridos: {dias_totales} días")
    print(f"• 📊 Días hábiles (Lunes a Viernes): {dias_habiles} días")
    print("="*45)
    print("💡 Tip: Ahora cualquier usuario que consulte su balance verá reflejado el rendimiento acumulado masivo correspondiente a esta fecha.")

if __name__ == "__main__":
    print("--- SIMULADOR DE TIEMPO GLOBAL (TODOS LOS USUARIOS) ---")
    print("Permite adelantar en el tiempo las inversiones activas de TODO el sistema.")
    
    fecha_input = input("Ingresa la fecha de inicio a simular para todos (Formato AAAA-MM-DD): ")
    simular_por_fecha_global(fecha_input)