import sqlite3

conn = sqlite3.connect("contabilidad.db")
cursor = conn.cursor()

# Limpiar correos y teléfonos erróneos o de prueba
cursor.execute("""
    UPDATE usuarios 
    SET email = NULL, telefono = NULL 
    WHERE email = 'CEO' OR telefono = 'CEO' OR email LIKE '%Jedidiah%' OR telefono LIKE '%Wueuxux%';
""")

conn.commit()
conn.close()
print("Base de datos limpiada de valores erróneos.")