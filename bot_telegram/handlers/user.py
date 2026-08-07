# handlers/user.py
import sqlite3
from config import ADMIN_ID
from database import DB_NAME, usuario_registrado
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

# --- HELPER DE SEGURIDAD PARA ADMINISTRADOR ---
def es_administrador(user_id: int) -> bool:
    try:
        admin_config = int(ADMIN_ID)
    except (TypeError, ValueError):
        admin_config = 0

    if admin_config == 0:
        return False
    return user_id == admin_config


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = user.id
    username = user.username or ""
    first_name = user.first_name or ""

    # Conectar a la base de datos y asegurar el registro inicial del usuario
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            INSERT OR IGNORE INTO usuarios (telegram_id, username, first_name)
            VALUES (?, ?, ?)
            """,
            (telegram_id, username, first_name)
        )
        conn.commit()
    except Exception as e:
        print(f"Error al registrar usuario en base de datos: {e}")
    finally:
        conn.close()

    # Verificar si el usuario ya completó su registro con nombre completo
    es_registrado = usuario_registrado(telegram_id)

    if not es_registrado:
        welcome_text = (
            f"¡Hola, <b>{first_name}</b>! Bienvenido al sistema de gestión de inversiones.\n\n"
            "Para poder operar y acceder al sistema, es necesario completar tu registro de usuario."
        )
        botones = [
            [InlineKeyboardButton("📝 Completar Registro", callback_data="iniciar_registro")]
        ]
    else:
        welcome_text = (
            f"¡Hola de nuevo, <b>{first_name}</b>!\n\n"
            "Selecciona una opción del menú:"
        )
        botones = [
            [InlineKeyboardButton("💰 Depositar", callback_data="menu_depositar")],
            [InlineKeyboardButton("📊 Mi Balance / Inversiones", callback_data="menu_balance")],
            [InlineKeyboardButton("📤 Solicitar Retiro", callback_data="menu_retirar")]
        ]

        if es_administrador(telegram_id):
            botones.append([InlineKeyboardButton("⚙️ Panel de Administrador", callback_data="admin_panel")])

    teclado = InlineKeyboardMarkup(botones)

    # Manejar respuesta si viene de un CallbackQuery (como el botón "Volver" o un menú) o del comando /start
    if update.callback_query:
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass
            
        try:
            await query.message.edit_text(welcome_text, reply_markup=teclado, parse_mode="HTML")
        except Exception:
            try:
                await query.message.reply_text(welcome_text, reply_markup=teclado, parse_mode="HTML")
            except Exception as e:
                print(f"Error al editar/enviar mensaje en start_command: {e}")
    else:
        try:
            await update.message.reply_text(welcome_text, reply_markup=teclado, parse_mode="HTML")
        except Exception as e:
            print(f"Error al enviar mensaje inicial: {e}")