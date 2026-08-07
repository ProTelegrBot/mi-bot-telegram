import os
import pandas as pd
from database import get_db

async def generar_excel_financiero_async(chat_id, context):
    """Genera un archivo Excel con el reporte financiero global y lo envía por Telegram de forma segura."""
    conn = get_db()
    
    # Consulta optimizada: Usamos una subconsulta o agregación para evitar duplicar usuarios 
    # en caso de que tengan múltiples registros en la tabla inversiones.
    query = """
        SELECT 
            u.telegram_id AS 'ID Telegram',
            COALESCE(u.nombre_completo, u.first_name, 'Sin Nombre') AS 'Nombre Completo',
            COALESCE(u.username, 'N/A') AS 'Usuario Telegram',
            COALESCE(u.email, 'N/A') AS 'Correo',
            COALESCE(u.telefono, 'N/A') AS 'Teléfono',
            COALESCE(i.monto_inicial, 0.0) AS 'Capital Activo ($)',
            COALESCE(i.ganancias_acumuladas, 0.0) AS 'Ganancias ($)',
            COALESCE(i.tope_ganancia, 0.0) AS 'Tope de Ganancia ($)',
            COALESCE(i.estado, 'Sin Inversión') AS 'Estado de Inversión',
            COALESCE(i.fecha_activacion, 'N/A') AS 'Fecha de Activación'
        FROM usuarios u
        LEFT JOIN (
            -- Selecciona la inversión más reciente o activa de cada usuario para evitar filas duplicadas
            SELECT * FROM inversiones 
            WHERE estado = 'activa' OR id IN (SELECT MAX(id) FROM inversiones GROUP BY telegram_id)
        ) i ON u.telegram_id = i.telegram_id
    """
    
    try:
        df = pd.read_sql_query(query, conn)
    except Exception as e:
        print(f"Error leyendo datos para Excel: {e}")
        df = pd.DataFrame()
    finally:
        conn.close()

    archivo_excel = "reporte_financiero_admin.xlsx"
    
    try:
        # Asegurar formato limpio en el DataFrame antes de exportar
        if not df.empty:
            df.fillna('N/A', inplace=True)
            
        df.to_excel(archivo_excel, index=False, engine='openpyxl')
    except Exception as e:
        print(f"Error al generar el archivo Excel: {e}")
        return

    try:
        with open(archivo_excel, "rb") as doc:
            await context.bot.send_document(
                chat_id=chat_id,
                document=doc,
                filename="Reporte_Financiero_Global.xlsx",
                caption="📊 <b>Reporte Financiero Global</b>\nAquí tienes el detalle contable completo y actualizado de los usuarios.",
                parse_mode="HTML"
            )
    except Exception as e:
        print(f"Error al enviar el archivo en Telegram: {e}")
    
    # Limpieza segura del archivo local temporal
    if os.path.exists(archivo_excel):
        try:
            os.remove(archivo_excel)
        except Exception as e:
            print(f"No se pudo eliminar el archivo temporal: {e}")