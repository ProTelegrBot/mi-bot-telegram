from datetime import datetime, time, timedelta
import logging
import sqlite3
import pytz
import os
import re
import pandas as pd
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import openpyxl

# --- IMPORTACIÓN CENTRALIZADA DESDE CONFIG.PY ---
try:
    from config import (
        ADMIN_ID,
        TOKEN,
        WALLET_ADDRESS,
        SMTP_SERVER,
        SMTP_PORT,
        SMTP_USER,
        SMTP_PASSWORD,
        COMISION_RETIRO,
    )
except ImportError:
    # Respaldos por defecto en caso de que alguna variable falte en config.py
    ADMIN_ID = os.getenv("ADMIN_ID", "0")
    TOKEN = os.getenv("TOKEN", "")
    WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
    SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER", "tu_correo@gmail.com")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "tu_contraseña_o_app_password")
    COMISION_RETIRO = 1.3

from database import (
    actualizar_configuracion,
    aplicar_rendimiento_diario,
    get_db,
    guardar_registro_completo,
    init_db,
    obtener_configuracion,
    obtener_resumen_financiero,
    registrar_deposito,
    registrar_retiro,
    usuario_registrado,
)
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Estados para la conversación de registro
NOMBRE, EMAIL, TELEFONO = range(3)

# Estados para la conversación de depósito
MONTO_DEP, HASH_DEP, COMPROBANTE_DEP = range(10, 13)

# Estados para la conversación de retiro
MONTO_RET = range(20, 21)

# Estados para la conversación de registro de wallet
WALLET_DIR, WALLET_PIN = range(50, 52)

# Estados para la conversación de cambio de wallet
CAMBIO_WALLET_PIN, CAMBIO_WALLET_DIR = range(60, 62)

# Estados para la conversación KYC de recuperación/seteo de wallet
KYC_WALLET_DIR, KYC_NOMBRE, KYC_FOTO = range(70, 73)

# Estado para la búsqueda de usuario individual del admin
BUSCAR_USUARIO_ADMIN = range(30, 31)

# Estados para la gestión de aprobación/rechazo de retiros por parte del admin
ADMIN_RET_APROBAR_COMPROBANTE, ADMIN_RET_OTRO_MOTIVO = range(40, 42)

# Estado para difusión masiva de admin
ADMIN_BROADCAST = range(80, 81)


def es_administrador(user_id: int) -> bool:
    try:
        admin_config = int(ADMIN_ID)
    except (TypeError, ValueError):
        admin_config = 0

    if admin_config == 0:
        return False
    return user_id == admin_config


def asegurar_columnas_usuarios():
    """Asegura que las columnas de wallet, pin, estado_animo, referido_por y ganancias_referidos existan en la tabla usuarios."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE usuarios ADD COLUMN wallet TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE usuarios ADD COLUMN pin TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE usuarios ADD COLUMN estado_animo TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE usuarios ADD COLUMN referido_por INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE usuarios ADD COLUMN ganancias_referidos REAL DEFAULT 0.0")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def asegurar_tabla_kyc():
    """Crea la tabla para las solicitudes KYC de recuperación de wallet."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS kyc_wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            nueva_wallet TEXT,
            nombre_kyc TEXT,
            foto_barbilla TEXT,
            estado TEXT DEFAULT 'pendiente',
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def obtener_datos_usuario(telegram_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM usuarios WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def usuario_tiene_plan_activo(telegram_id: int) -> bool:
    """Verifica si el usuario tiene al menos un plan de inversión activo."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM inversiones WHERE telegram_id = ? AND estado = 'activa'", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def guardar_wallet_usuario(telegram_id: int, wallet: str, pin: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE usuarios SET wallet = ?, pin = ? WHERE telegram_id = ?", (wallet, pin, telegram_id))
    conn.commit()
    conn.close()


def actualizar_wallet_usuario(telegram_id: int, wallet: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE usuarios SET wallet = ? WHERE telegram_id = ?", (wallet, telegram_id))
    conn.commit()
    conn.close()


def es_wallet_valida(billetera: str) -> bool:
    """Valida que la dirección tenga exactamente 42 caracteres, empiece con 0x y 40 caracteres hexadecimales."""
    pattern = r"^0x[a-fA-F0-9]{40}$"
    return bool(re.match(pattern, billetera))


def wallet_registrada_por_otro(wallet: str, telegram_id: int) -> bool:
    """Verifica si la wallet ya fue registrada por otro usuario."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id FROM usuarios WHERE wallet = ? AND telegram_id != ?", (wallet, telegram_id))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def enviar_correo_bienvenida(destinatario: str, nombre: str):
    """Envía un correo electrónico explicativo de bienvenida al nuevo usuario registrado."""
    if not SMTP_USER or not SMTP_PASSWORD or "tu_correo" in SMTP_USER:
        logger.warning("Credenciales SMTP no configuradas. Omitiendo envío de correo.")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "🚀 ¡Bienvenido a la plataforma! Cómo generar ganancias y tu capital"
        msg["From"] = SMTP_USER
        msg["To"] = destinatario

        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e0e0e0; border-radius: 10px; background-color: #f9f9f9;">
                <h2 style="color: #2c3e50; text-align: center;">¡Bienvenido a bordo, {nombre}! 🎉</h2>
                <p>Nos alegra mucho que hayas completado tu registro exitosamente en nuestro bot de Telegram.</p>
                
                <hr style="border: none; border-top: 1px solid #e0e0e0; margin: 20px 0;">
                
                <h3 style="color: #2980b9;">📈 ¿Cómo funciona tu capital invertido y las ganancias?</h3>
                <ul style="padding-left: 20px;">
                    <li><b>Depósitos en USDT (BEP-20):</b> Realiza tus aportes de inversión de forma segura transfiriendo a la billetera oficial asignada en el bot a través de la red <b>BNB Smart Chain (BEP-20)</b>.</li>
                    <li><b>Generación de Rendimientos:</b> Tu capital activo comienza a generar un rendimiento diario del <b>0.5%</b> (de lunes a viernes) una vez transcurridas las primeras 24 horas de la aprobación de tu depósito.</li>
                    <li><b>Tope de Ganancia (200%):</b> Cada plan de inversión cuenta con un límite o tope de ganancia del <b>200%</b> sobre tu capital inicial. Al alcanzarlo, el ciclo del plan concluye.</li>
                    <li><b>Retiros Flexibles:</b> Puedes solicitar retiros de tus balances disponibles en cualquier día hábil (lunes a viernes) directamente hacia tu billetera registrada.</li>
                </ul>
                
                <p style="margin-top: 30px;">Puedes gestionar tus fondos, ver tus balances y solicitar operaciones directamente desde el menú principal en Telegram.</p>
                
                <p style="text-align: center; color: #7f8c8d; font-size: 12px; margin-top: 40px;">
                    Este es un mensaje automático generado tras tu registro en el sistema. Por favor, no respondas a este correo.
                </p>
            </div>
        </body>
        </html>
        """

        msg.attach(MIMEText(html_content, "html"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, destinatario, msg.as_string())
        
        logger.info(f"Correo de bienvenida enviado exitosamente a {destinatario}")
        return True
    except Exception as e:
        logger.error(f"Error al enviar correo de bienvenida a {destinatario}: {e}")
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    # Capturar enlace de referido si viene en el comando start (ej: /start ref_123456)
    if args and args[0].startswith("ref_"):
        try:
            referrer_id = int(args[0].replace("ref_", ""))
            if referrer_id != user.id and not usuario_registrado(user.id):
                context.user_data["referido_por"] = referrer_id
        except ValueError:
            pass

    if es_administrador(user.id):
        keyboard = [[
            InlineKeyboardButton(
                "⚙️ Panel Admin", callback_data="admin_panel"
            )
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = f"👋 ¡Hola Administrador <b>{user.first_name}</b>! 🛡️\n\nAccede a tu panel de control:"
    else:
        es_registrado = usuario_registrado(user.id)

        if not es_registrado:
            keyboard = [[
                InlineKeyboardButton(
                    "📝 Completar Registro", callback_data="iniciar_registro"
                )
            ]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            text = (
                f"👋 ¡Hola <b>{user.first_name}</b>! ✨\n\n"
                "Para comenzar, por favor completa tu registro."
            )
        else:
            keyboard = [
                [
                    InlineKeyboardButton(
                        "💰 Depositar", callback_data="menu_depositar"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📊 Mi Balance", callback_data="menu_balance"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🤝 Link de Referido", callback_data="menu_referidos"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "💵 Solicitar Retiro", callback_data="menu_retirar"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "👛 Wallet", callback_data="menu_wallet"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⭐ Calificar / Estado de Ánimo", callback_data="menu_estado_animo"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "💸 Descargar Mi Estado de Cuenta", callback_data="descargar_mi_excel"
                    )
                ],
            ]

            reply_markup = InlineKeyboardMarkup(keyboard)
            text = f"👋 ¡Hola, <b>{user.first_name}</b>! ✨\n\nSelecciona una opción:"

    if update.callback_query:
        query = update.callback_query
        try:
            if query.message.photo:
                await query.message.reply_text(
                    text, reply_markup=reply_markup, parse_mode="HTML"
                )
            else:
                await query.message.edit_text(
                    text, reply_markup=reply_markup, parse_mode="HTML"
                )
        except Exception:
            await query.message.reply_text(
                text, reply_markup=reply_markup, parse_mode="HTML"
            )
    else:
        await update.message.reply_text(
            text, reply_markup=reply_markup, parse_mode="HTML"
        )


# --- FLUJO DE REGISTRO ---
async def iniciar_registro(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "📝 <b>Paso 1/3</b>\n\nEscribe tu nombre y tus dos apellidos:",
        parse_mode="HTML",
    )
    return NOMBRE


async def recibir_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if len(texto) < 3:
        await update.message.reply_text("⚠️ Por favor, ingresa un nombre y apellidos válidos:")
        return NOMBRE

    context.user_data["nombre_completo"] = texto
    await update.message.reply_text(
        "📧 <b>Paso 2/3</b>\n\nIngresa tu correo electrónico válido:",
        parse_mode="HTML",
    )
    return EMAIL


async def recibir_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    email = update.message.text.strip()
    if "@" not in email or "." not in email:
        await update.message.reply_text("⚠️ <b>Correo no válido.</b> Asegúrate de incluir '@' y un dominio correcto. Inténtalo de nuevo:", parse_mode="HTML")
        return EMAIL

    context.user_data["email"] = email
    contact_keyboard = ReplyKeyboardMarkup(
        [[
            KeyboardButton(
                "📱 Compartir mi teléfono", request_contact=True
            )
        ]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "📞 <b>Paso 3/3</b>\n\nComparte tu número de teléfono con el botón o escríbelo:",
        reply_markup=contact_keyboard,
        parse_mode="HTML",
    )
    return TELEFONO


async def recibir_telefono(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message.contact:
        telefono = update.message.contact.phone_number
    else:
        telefono = update.message.text.strip()
        if len(telefono) < 7:
            await update.message.reply_text("⚠️ Ingresa un número de teléfono válido.")
            return TELEFONO

    email_usuario = context.user_data.get("email", "")
    nombre_completo_usuario = context.user_data.get("nombre_completo", "")

    guardar_registro_completo(
        telegram_id=user.id,
        username=user.username or "",
        first_name=user.first_name or "",
        nombre_completo=nombre_completo_usuario,
        email=email_usuario,
        telefono=telefono,
    )

    # Asignar referido si existe en la sesión temporal
    ref_id = context.user_data.get("referido_por")
    if ref_id:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE usuarios SET referido_por = ? WHERE telegram_id = ?", (ref_id, user.id))
        conn.commit()
        conn.close()

    if email_usuario:
        enviar_correo_bienvenida(email_usuario, nombre_completo_usuario or user.first_name)

    await update.message.reply_text(
        "✅ <b>¡Registro exitoso!</b> Te hemos enviado un correo electrónico con los detalles sobre tu capital y cómo generar ganancias.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML",
    )
    
    # --- ALERTA INFORMATIVA AL ADMINISTRADOR ---
    if ADMIN_ID:
        try:
            username_str = f"@{user.username}" if user.username else "Sin alias"
            admin_alerta_msg = (
                "🔔 <b>¡NUEVO USUARIO REGISTRADO!</b>\n\n"
                f"• <b>Nombre completo:</b> {nombre_completo_usuario}\n"
                f"• <b>Nombre en Telegram:</b> {user.first_name}\n"
                f"• <b>Usuario TG:</b> {username_str}\n"
                f"• <b>ID Telegram:</b> <code>{user.id}</code>\n"
                f"• <b>Correo:</b> {email_usuario}\n"
                f"• <b>Teléfono:</b> {telefono}\n"
                f"• <b>Fecha/Hora:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            await context.bot.send_message(
                chat_id=int(ADMIN_ID),
                text=admin_alerta_msg,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error enviando alerta de nuevo usuario al administrador: {e}")

    await start(update, context)
    return ConversationHandler.END


# --- MENÚ Y FLUJOS DE WALLET ---
async def menu_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_row = obtener_datos_usuario(user_id)
    
    tiene_wallet = bool(user_row and user_row["wallet"])
    wallet_actual = user_row["wallet"] if tiene_wallet else "No registrada ❌"

    wallet_btn_text = "✏️ Cambiar Wallet (con PIN)" if tiene_wallet else "👛 Registrar Wallet"
    wallet_callback = "cambiar_wallet" if tiene_wallet else "registrar_wallet"

    text = (
        "👛 <b>Gestión de Billetera (Wallet)</b>\n\n"
        f"• Wallet actual (BEP-20): <code>{wallet_actual}</code>\n\n"
        "Selecciona una opción:"
    )

    if tiene_wallet:
        kyc_btn = InlineKeyboardButton("🔑 Olvidé mi PIN / Settear Wallet (KYC)", callback_data="iniciar_kyc_wallet")
    else:
        kyc_btn = InlineKeyboardButton("🔑 Settear Wallet (Requiere Wallet previa) 🔒", callback_data="kyc_no_permitido")

    keyboard = [
        [InlineKeyboardButton(wallet_btn_text, callback_data=wallet_callback)],
        [kyc_btn],
        [InlineKeyboardButton("🔙 Volver", callback_data="volver_inicio")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if query.message.photo:
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")
        else:
            await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except Exception:
        await query.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def iniciar_registro_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    texto = (
        "👛 <b>Registro de Billetera (Wallet)</b>\n\n"
        "Por favor, ingresa tu dirección de billetera USDT.\n\n"
        "⚠️ <b>IMPORTANTE - RED BEP-20:</b>\n"
        "Debes asegurarte al 100% de que la dirección corresponda a la red <b>BNB Smart Chain (BEP-20)</b>. "
        "Si utilizas una red diferente (como ERC-20, TRC-20, etc.), <b>los fondos se perderán permanentemente</b>.\n\n"
        "• Longitud fija: <b>42 caracteres</b> (comenzando con <code>0x</code> y 40 caracteres hexadecimales).\n\n"
        "<i>(/cancelar para anular)</i>"
    )
    try:
        if query.message.photo:
            await query.message.reply_text(texto, parse_mode="HTML")
        else:
            await query.message.edit_text(texto, parse_mode="HTML")
    except Exception:
        await query.message.reply_text(texto, parse_mode="HTML")
    return WALLET_DIR


async def recibir_wallet_dir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    billetera = update.message.text.strip()
    user = update.effective_user
    
    if not es_wallet_valida(billetera):
        await update.message.reply_text(
            "⚠️ <b>Dirección de Wallet no válida.</b>\n\n"
            "Verifica los siguientes requisitos:\n"
            "1. Debe empezar exactamente con <code>0x</code>.\n"
            "2. Debe tener una longitud exacta de <b>42 caracteres</b>.\n"
            "3. Pertenece estrictamente a la red <b>BNB Smart Chain (BEP-20)</b>.\n\n"
            "Inténtalo de nuevo:",
            parse_mode="HTML"
        )
        return WALLET_DIR

    if wallet_registrada_por_otro(billetera, user.id):
        await update.message.reply_text(
            "⚠️ <b>Billetera duplicada.</b> Esta dirección ya ha sido registrada por otro usuario en la plataforma. Ingresa otra dirección:",
            parse_mode="HTML"
        )
        return WALLET_DIR

    context.user_data["temp_wallet"] = billetera
    await update.message.reply_text(
        "🔐 <b>Clave de Seguridad (PIN)</b>\n\n"
        "Para proteger los cambios futuros de tu Wallet, crea una <b>clave de 4 dígitos</b> numéricos (ej. <code>1234</code>):\n\n"
        "<i>(/cancelar para anular)</i>",
        parse_mode="HTML"
    )
    return WALLET_PIN


async def recibir_wallet_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pin = update.message.text.strip()
    
    if not (pin.isdigit() and len(pin) == 4):
        await update.message.reply_text(
            "⚠️ <b>PIN no válido.</b> La clave debe ser exactamente de <b>4 dígitos numéricos</b>. Inténtalo de nuevo:",
            parse_mode="HTML"
        )
        return WALLET_PIN

    user = update.effective_user
    billetera = context.user_data.get("temp_wallet")

    guardar_wallet_usuario(user.id, billetera, pin)

    await update.message.reply_text(
        "✅ <b>¡Wallet registrada y PIN guardado exitosamente!</b>\n\n"
        f"Dirección: <code>{billetera}</code> (BEP-20)\n\n"
        "💡 <i>Te recomendamos eliminar el mensaje con tu PIN de este chat por seguridad.</i>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )
    await start(update, context)
    return ConversationHandler.END


async def iniciar_cambio_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    user_row = obtener_datos_usuario(user.id)
    
    if not user_row or not user_row["pin"]:
        texto = "⚠️ No tienes una clave de seguridad (PIN) configurada. Por favor, registra tu Wallet nuevamente o usa la opción de recuperación por KYC."
        keyboard = [[InlineKeyboardButton("🔙 Volver", callback_data="menu_wallet")]]
        await query.message.edit_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return ConversationHandler.END

    texto = (
        "🔐 <b>Seguridad para Cambio de Wallet</b>\n\n"
        "Para cambiar tu dirección de billetera, ingresa tu <b>clave de 4 dígitos</b>:\n\n"
        "<i>(/cancelar para anular)</i>"
    )
    try:
        if query.message.photo:
            await query.message.reply_text(texto, parse_mode="HTML")
        else:
            await query.message.edit_text(texto, parse_mode="HTML")
    except Exception:
        await query.message.reply_text(texto, parse_mode="HTML")
    return CAMBIO_WALLET_PIN


async def recibir_cambio_wallet_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    bloqueo_hasta = context.user_data.get("cambio_wallet_bloqueo_hasta")
    if bloqueo_hasta and datetime.now() < bloqueo_hasta:
        tiempo_restante = int((bloqueo_hasta - datetime.now()).total_seconds() / 60) + 1
        await update.message.reply_text(
            f"⏳ Has superado el límite de intentos incorrectos. Debes esperar aproximadamente <b>{tiempo_restante} minuto(s)</b> más para un nuevo intento.",
            parse_mode="HTML"
        )
        return CAMBIO_WALLET_PIN

    pin_ingresado = update.message.text.strip()
    user_row = obtener_datos_usuario(user.id)

    if not user_row or pin_ingresado != user_row["pin"]:
        intentos = context.user_data.get("cambio_wallet_intentos", 0) + 1
        context.user_data["cambio_wallet_intentos"] = intentos

        if intentos > 3:
            context.user_data["cambio_wallet_bloqueo_hasta"] = datetime.now() + timedelta(minutes=5)
            context.user_data["cambio_wallet_intentos"] = 0
            await update.message.reply_text(
                "❌ <b>PIN incorrecto.</b> Has superado los 3 intentos permitidos. Debes esperar una pausa de <b>5 minutos</b> para un nuevo intento.",
                parse_mode="HTML"
            )
            return CAMBIO_WALLET_PIN

        intentos_restantes = max(0, 3 - intentos)
        await update.message.reply_text(
            f"❌ <b>PIN incorrecto.</b> Clave de seguridad inválida. Intentos restantes antes del bloqueo: {intentos_restantes}. Inténtalo de nuevo o escribe /cancelar:",
            parse_mode="HTML"
        )
        return CAMBIO_WALLET_PIN

    context.user_data["cambio_wallet_intentos"] = 0
    context.user_data.pop("cambio_wallet_bloqueo_hasta", None)

    await update.message.reply_text(
        "👛 <b>Nueva Dirección de Wallet (BEP-20)</b>\n\n"
        "Ingresa la nueva dirección de tu billetera (42 caracteres, comenzando con <code>0x</code>):\n"
        "⚠️ Recuerda verificar que esté en la red <b>BNB Smart Chain (BEP-20)</b>.",
        parse_mode="HTML"
    )
    return CAMBIO_WALLET_DIR


async def recibir_cambio_wallet_dir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    billetera = update.message.text.strip()
    user = update.effective_user
    
    if not es_wallet_valida(billetera):
        await update.message.reply_text(
            "⚠️ <b>Dirección de Wallet no válida.</b>\n"
            "Debe empezar con <code>0x</code>, tener 42 caracteres y pertenecer al red <b>BNB Smart Chain (BEP-20)</b>. Inténtalo de nuevo:",
            parse_mode="HTML"
        )
        return CAMBIO_WALLET_DIR

    if wallet_registrada_por_otro(billetera, user.id):
        await update.message.reply_text(
            "⚠️ <b>Billetera duplicada.</b> Esta dirección ya ha sido registrada por otro usuario en la plataforma. Ingresa otra dirección:",
            parse_mode="HTML"
        )
        return CAMBIO_WALLET_DIR

    user_row = obtener_datos_usuario(user.id)
    wallet_anterior = user_row["wallet"] if user_row else None

    if billetera == wallet_anterior:
        await update.message.reply_text(
            "ℹ️ <b>Aviso:</b> La nueva dirección ingresada es igual a tu Wallet anterior. El proceso se ha completado.",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove()
        )
        await start(update, context)
        return ConversationHandler.END

    actualizar_wallet_usuario(user.id, billetera)

    await update.message.reply_text(
        "✅ <b>¡Wallet actualizada exitosamente!</b>\n\n"
        f"Nueva dirección: <code>{billetera}</code> (BEP-20)",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )
    await start(update, context)
    return ConversationHandler.END


# --- FLUJO KYC (RECUPERACIÓN / SETEO DE WALLET POR OLVIDO DE PIN) ---
async def iniciar_kyc_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    user_row = obtener_datos_usuario(user_id)
    if not user_row or not user_row["wallet"]:
        await query.answer("⚠️ Debes tener una dirección de billetera registrada previamente para usar el Setteo.", show_alert=True)
        return ConversationHandler.END

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM kyc_wallets WHERE telegram_id = ? AND estado = 'pendiente'", (user_id,))
    pendiente = cursor.fetchone()
    conn.close()

    if pendiente:
        texto = "⚠️ Ya tienes una solicitud KYC pendiente de revisión por el administrador."
        keyboard = [[InlineKeyboardButton("🔙 Volver", callback_data="menu_wallet")]]
        try:
            if query.message.photo:
                await query.message.reply_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
            else:
                await query.message.edit_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        except Exception:
            await query.message.reply_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return ConversationHandler.END

    texto = (
        "🔐 <b>Verificación KYC - Setteo de Wallet</b>\n\n"
        "Has solicitado configurar o restablecer tu Wallet por olvido de PIN.\n\n"
        "<b>Paso 1/3:</b> Ingresa la nueva dirección de tu billetera USDT (Red <b>BEP-20</b>, 42 caracteres, empezando con <code>0x</code>):\n\n"
        "<i>(/cancelar para anular)</i>"
    )
    try:
        if query.message.photo:
            await query.message.reply_text(texto, parse_mode="HTML")
        else:
            await query.message.edit_text(texto, parse_mode="HTML")
    except Exception:
        await query.message.reply_text(texto, parse_mode="HTML")
    return KYC_WALLET_DIR


async def recibir_kyc_wallet_dir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    billetera = update.message.text.strip()
    user = update.effective_user

    if not es_wallet_valida(billetera):
        await update.message.reply_text(
            "⚠️ <b>Dirección de Wallet no válida.</b>\n"
            "Debe empezar con <code>0x</code>, tener 42 caracteres y pertenecer a la red <b>BNB Smart Chain (BEP-20)</b>. Inténtalo de nuevo:",
            parse_mode="HTML"
        )
        return KYC_WALLET_DIR

    if wallet_registrada_por_otro(billetera, user.id):
        await update.message.reply_text(
            "⚠️ <b>Billetera duplicada.</b> Esta dirección ya ha sido registrada por otro usuario en la plataforma. Ingresa otra dirección:",
            parse_mode="HTML"
        )
        return KYC_WALLET_DIR

    context.user_data["kyc_wallet"] = billetera
    await update.message.reply_text(
        "📝 <b>Paso 2/3: Información Personal</b>\n\n"
        "Ingresa tu <b>nombre completo y número de documento de identidad (Cédula / Pasaporte)</b> para validar tu identidad:",
        parse_mode="HTML"
    )
    return KYC_NOMBRE


async def recibir_kyc_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nombre_kyc = update.message.text.strip()
    if len(nombre_kyc) < 5:
        await update.message.reply_text("⚠️ Por favor, ingresa un nombre y documento válidos:")
        return KYC_NOMBRE

    context.user_data["kyc_nombre"] = nombre_kyc
    await update.message.reply_text(
        "📸 <b>Paso 3/3: Verificación Fotográfica KYC</b>\n\n"
        "Envía una <b>foto tuya con tu documento de identidad colocado debajo de la barbilla</b> (rostro y documento claramente visibles):",
        parse_mode="HTML"
    )
    return KYC_FOTO


async def recibir_kyc_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("⚠️ Debes enviar una imagen válida con el documento debajo de la barbilla:")
        return KYC_FOTO

    file_id = update.message.photo[-1].file_id
    user = update.effective_user
    billetera = context.user_data.get("kyc_wallet")
    nombre_kyc = context.user_data.get("kyc_nombre")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO kyc_wallets (telegram_id, nueva_wallet, nombre_kyc, foto_barbilla, estado) VALUES (?, ?, ?, ?, 'pendiente')",
        (user.id, billetera, nombre_kyc, file_id)
    )
    kyc_id = cursor.lastrowid
    conn.commit()
    conn.close()

    await update.message.reply_text(
        "✅ <b>¡Solicitud KYC enviada con éxito!</b>\n\n"
        "Tus datos han sido remitidos al administrador para su revisión y validación. Te notificaremos el resultado próximamente.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML"
    )

    if ADMIN_ID:
        try:
            admin_msg = (
                "🔔 <b>NUEVA SOLICITUD KYC (SETEO DE WALLET)</b>\n\n"
                f"• Solicitud ID: <code>{kyc_id}</code>\n"
                f"• Usuario: {user.first_name} (<code>{user.id}</code>)\n"
                f"• Nombre / Documento: {nombre_kyc}\n"
                f"• Nueva Wallet (BEP-20): <code>{billetera}</code>"
            )
            keyboard = [
                [
                    InlineKeyboardButton("✅ Aceptar KYC", callback_data=f"admin_kyc_ok_{kyc_id}"),
                    InlineKeyboardButton("❌ Denegar KYC", callback_data=f"admin_kyc_no_{kyc_id}")
                ]
            ]
            await context.bot.send_photo(
                chat_id=int(ADMIN_ID),
                photo=file_id,
                caption=admin_msg,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error notificando KYC al admin: {e}")

    await start(update, context)
    return ConversationHandler.END


async def admin_kyc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if not es_administrador(query.from_user.id):
        await query.answer("❌ Sin permisos.", show_alert=True)
        return

    partes = data.split("_")
    accion = partes[2]  # ok o no
    kyc_id = partes[3]

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, nueva_wallet FROM kyc_wallets WHERE id = ? AND estado = 'pendiente'", (kyc_id,))
    kyc = cursor.fetchone()

    if not kyc:
        conn.close()
        try:
            if query.message.photo:
                await query.message.edit_caption(caption="⚠️ Solicitud KYC ya procesada o no encontrada.", parse_mode="HTML")
            else:
                await query.message.edit_text("⚠️ Solicitud KYC ya procesada o no encontrada.", parse_mode="HTML")
        except Exception:
            pass
        return

    target_user = kyc["telegram_id"]
    nueva_wallet = kyc["nueva_wallet"]

    if accion == "ok":
        cursor.execute("UPDATE kyc_wallets SET estado = 'aprobado' WHERE id = ?", (kyc_id,))
        cursor.execute("UPDATE usuarios SET wallet = ? WHERE telegram_id = ?", (nueva_wallet, target_user))
        conn.commit()
        conn.close()

        try:
            if query.message.photo:
                await query.message.edit_caption(caption=f"✅ Solicitud KYC #{kyc_id} <b>ACEPTADA</b>. Wallet actualizada para el usuario.", parse_mode="HTML")
            else:
                await query.message.edit_text(text=f"✅ Solicitud KYC #{kyc_id} <b>ACEPTADA</b>. Wallet actualizada para el usuario.", parse_mode="HTML")
        except Exception:
            pass

        try:
            await context.bot.send_message(
                chat_id=target_user,
                text="✅ ¡Tu solicitud KYC ha sido <b>ACEPTADA</b> por el administrador! Tu nueva Wallet ha sido configurada exitosamente.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    else:
        cursor.execute("UPDATE kyc_wallets SET estado = 'denegado' WHERE id = ?", (kyc_id,))
        conn.commit()
        conn.close()

        try:
            if query.message.photo:
                await query.message.edit_caption(caption=f"❌ Solicitud KYC #{kyc_id} <b>DENEGADA</b>.", parse_mode="HTML")
            else:
                await query.message.edit_text(text=f"❌ Solicitud KYC #{kyc_id} <b>DENEGADA</b>.", parse_mode="HTML")
        except Exception:
            pass

        try:
            await context.bot.send_message(
                chat_id=target_user,
                text="SETTEO DENEGADO: Los datos no coinciden."
            )
        except Exception:
            pass


# --- FLUJO DE DEPÓSITO ---
async def iniciar_deposito_flujo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    if (
        obtener_configuracion("depositos_activos") == 0
        and not es_administrador(query.from_user.id)
    ):
        msg_error = "❌ Los depósitos están desactivados temporalmente."
        kb_error = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver_inicio")]])
        try:
            if query.message.photo:
                await query.message.reply_text(msg_error, reply_markup=kb_error, parse_mode="HTML")
            else:
                await query.message.edit_text(msg_error, reply_markup=kb_error, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(msg_error, reply_markup=kb_error, parse_mode="HTML")
        return ConversationHandler.END

    await query.message.reply_text(
        "💰 <b>Paso 1/2: Monto</b>\n\n"
        f"Transfiere a la billetera (BEP-20):\n<code>{WALLET_ADDRESS}</code>\n\n"
        "Escribe la cantidad exacta en USDT que enviaste:\n"
        "<i>(/cancelar para anular)</i>",
        parse_mode="HTML",
    )
    return MONTO_DEP


async def recibir_monto_dep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        monto = float(update.message.text.strip().replace(",", "."))
        if monto <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Ingresa un monto válido en números.")
        return MONTO_DEP

    context.user_data["monto_dep"] = monto
    await update.message.reply_text(
        "🔗 <b>Paso 2/2: Comprobante</b>\n\n"
        "Envía el <b>Hash (TXID)</b> o la <b>captura de pantalla</b> de tu pago:",
        parse_mode="HTML",
    )
    return HASH_DEP


async def recibir_hash_dep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        return await procesar_deposito_final(update, context, tx_hash="N/A", file_id=update.message.photo[-1].file_id)

    tx_hash = update.message.text.strip()
    if len(tx_hash) < 5:
        await update.message.reply_text("⚠️ Hash no válido. Inténtalo de nuevo:")
        return HASH_DEP

    context.user_data["hash_dep"] = tx_hash
    await update.message.reply_text(
        "📸 <b>Captura de pantalla</b>\n\nAhora envía la captura de pantalla o foto del comprobante de pago:",
        parse_mode="HTML"
    )
    return COMPROBANTE_DEP


async def recibir_comprobante_dep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("⚠️ Debes enviar una captura de pantalla / imagen válida como comprobante:")
        return COMPROBANTE_DEP
    
    file_id = update.message.photo[-1].file_id
    tx_hash = context.user_data.get("hash_dep", "N/A")
    return await procesar_deposito_final(update, context, tx_hash=tx_hash, file_id=file_id)


async def procesar_deposito_final(update: Update, context: ContextTypes.DEFAULT_TYPE, tx_hash: str, file_id: str):
    user = update.effective_user
    monto = context.user_data.get("monto_dep")

    deposito_id = registrar_deposito(
        telegram_id=user.id,
        monto=monto,
        hash_transaccion=tx_hash,
        comprobante_file_id=file_id
    )

    await update.message.reply_text(
        "✅ <b>¡Depósito registrado con éxito!</b>\n\n"
        "Tu solicitud ha sido enviada al equipo de administración para su verificación. Te notificaremos en cuanto sea aprobado.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML"
    )

    if ADMIN_ID:
        try:
            admin_msg = (
                "🔔 <b>NUEVA SOLICITUD DE DEPÓSITO</b>\n\n"
                f"• Depósito ID: <code>{deposito_id}</code>\n"
                f"• Usuario: {user.first_name} (<code>{user.id}</code>)\n"
                f"• Monto: <b>{monto} USDT</b>\n"
                f"• Hash TXID: <code>{tx_hash}</code>"
            )
            keyboard = [
                [
                    InlineKeyboardButton("✅ Aprobar", callback_data=f"admin_dep_ok_{deposito_id}"),
                    InlineKeyboardButton("❌ Rechazar", callback_data=f"admin_dep_no_{deposito_id}")
                ]
            ]
            if file_id != "N/A":
                await context.bot.send_photo(
                    chat_id=int(ADMIN_ID),
                    photo=file_id,
                    caption=admin_msg,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML"
                )
            else:
                await context.bot.send_message(
                    chat_id=int(ADMIN_ID),
                    text=admin_msg,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Error notificando depósito al admin: {e}")

    await start(update, context)
    return ConversationHandler.END


# --- FLUJO DE RETIRO ---
async def iniciar_retiro_flujo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if obtener_configuracion("retiros_activos") == 0 and not es_administrador(user_id):
        msg_error = "❌ Los retiros están desactivados temporalmente."
        kb_error = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver_inicio")]])
        try:
            if query.message.photo:
                await query.message.reply_text(msg_error, reply_markup=kb_error, parse_mode="HTML")
            else:
                await query.message.edit_text(msg_error, reply_markup=kb_error, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(msg_error, reply_markup=kb_error, parse_mode="HTML")
        return ConversationHandler.END

    user_row = obtener_datos_usuario(user_id)
    if not user_row or not user_row["wallet"]:
        msg = "⚠️ Debes registrar tu dirección de Wallet USDT (BEP-20) antes de solicitar un retiro."
        kb = [[InlineKeyboardButton("👛 Registrar Wallet", callback_data="menu_wallet")]]
        try:
            if query.message.photo:
                await query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
            else:
                await query.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        except Exception:
            await query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        return ConversationHandler.END

    resumen = obtener_resumen_financiero(user_id)
    disponible = resumen["ganancias_disponibles"]

    if disponible <= 0:
        msg = f"⚠️ No tienes saldo disponible para retirar.\nSaldo actual: <b>{disponible:.2f} USDT</b>"
        kb = [[InlineKeyboardButton("🔙 Volver", callback_data="volver_inicio")]]
        try:
            if query.message.photo:
                await query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
            else:
                await query.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        except Exception:
            await query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        return ConversationHandler.END

    texto = (
        "💵 <b>Solicitud de Retiro</b>\n\n"
        f"• Saldo disponible: <b>{disponible:.2f} USDT</b>\n"
        f"• Comisión fija de retiro: <b>{COMISION_RETIRO} USDT</b>\n\n"
        "Ingresa la cantidad en USDT que deseas retirar:\n"
        "<i>(/cancelar para anular)</i>"
    )
    try:
        if query.message.photo:
            await query.message.reply_text(texto, parse_mode="HTML")
        else:
            await query.message.edit_text(texto, parse_mode="HTML")
    except Exception:
        await query.message.reply_text(texto, parse_mode="HTML")
    return MONTO_RET


async def recibir_monto_ret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        monto = float(update.message.text.strip().replace(",", "."))
    except ValueError:
        await update.message.reply_text("⚠️ Ingresa un monto válido en números:")
        return MONTO_RET

    user = update.effective_user
    resumen = obtener_resumen_financiero(user.id)
    disponible = resumen["ganancias_disponibles"]

    if monto <= 0:
        await update.message.reply_text("⚠️ El monto debe ser mayor a 0.")
        return MONTO_RET

    if monto > disponible:
        await update.message.reply_text(f"⚠️ El monto supera tus ganancias disponibles ({disponible:.2f} USDT). Inténtalo de nuevo:")
        return MONTO_RET

    user_row = obtener_datos_usuario(user.id)
    wallet = user_row["wallet"]

    retiro_id = registrar_retiro(user.id, monto, COMISION_RETIRO, wallet)

    await update.message.reply_text(
        "✅ <b>¡Solicitud de retiro creada con éxito!</b>\n\n"
        f"• Monto solicitado: {monto} USDT\n"
        f"• Comisión: {COMISION_RETIRO} USDT\n"
        f"• Recibirás: <code>{monto - COMISION_RETIRO} USDT</code>\n\n"
        "El administrador procesará tu pago a la brevedad.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML"
    )

    if ADMIN_ID:
        try:
            admin_msg = (
                "🔔 <b>NUEVA SOLICITUD DE RETIRO</b>\n\n"
                f"• Retiro ID: <code>{retiro_id}</code>\n"
                f"• Usuario: {user.first_name} (<code>{user.id}</code>)\n"
                f"• Monto bruto: <b>{monto} USDT</b>\n"
                f"• Comisión: {COMISION_RETIRO} USDT\n"
                f"• Neto a enviar: <b>{monto - COMISION_RETIRO} USDT</b>\n"
                f"• Wallet destino: <code>{wallet}</code>"
            )
            keyboard = [
                [
                    InlineKeyboardButton("✅ Aprobar Retiro", callback_data=f"admin_ret_aprobar_{retiro_id}"),
                    InlineKeyboardButton("❌ Rechazar Retiro", callback_data=f"admin_ret_rechazar_{retiro_id}")
                ]
            ]
            await context.bot.send_message(
                chat_id=int(ADMIN_ID),
                text=admin_msg,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error notificando retiro al admin: {e}")

    await start(update, context)
    return ConversationHandler.END


# --- MENÚS ADICIONALES USUARIO ---
async def menu_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    resumen = obtener_resumen_financiero(user_id)

    texto = (
        "📊 <b>Tu Resumen Financiero</b>\n\n"
        f"• Capital activo invertido: <b>{resumen['capital_activo']:.2f} USDT</b>\n"
        f"• Ganancias disponibles: <b>{resumen['ganancias_disponibles']:.2f} USDT</b>\n"
        f"• Ganancias totales generadas: <b>{resumen['ganancias_totales']:.2f} USDT</b>\n"
        f"• Ganancias de referidos: <b>{resumen['ganancias_referidos']:.2f} USDT</b>\n"
        f"• Total retirado: <b>{resumen['total_retirado']:.2f} USDT</b>"
    )
    keyboard = [[InlineKeyboardButton("🔙 Volver", callback_data="volver_inicio")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if query.message.photo:
            await query.message.reply_text(texto, reply_markup=reply_markup, parse_mode="HTML")
        else:
            await query.message.edit_text(texto, reply_markup=reply_markup, parse_mode="HTML")
    except Exception:
        await query.message.reply_text(texto, reply_markup=reply_markup, parse_mode="HTML")


async def menu_referidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    bot_username = context.bot.username

    link_referido = f"https://t.me/{bot_username}?start=ref_{user_id}"
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM usuarios WHERE referido_por = ?", (user_id,))
    total_referidos = cursor.fetchone()[0]
    conn.close()

    resumen = obtener_resumen_financiero(user_id)

    texto = (
        "🤝 <b>Sistema de Referidos</b>\n\n"
        "Invita a tus amigos y gana comisiones por sus inversiones en la plataforma.\n\n"
        f"• Total de referidos directos: <b>{total_referidos}</b>\n"
        f"• Ganancias por referidos: <b>{resumen['ganancias_referidos']:.2f} USDT</b>\n\n"
        "Tu enlace personal de referido:\n"
        f"<code>{link_referido}</code>"
    )
    keyboard = [[InlineKeyboardButton("🔙 Volver", callback_data="volver_inicio")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if query.message.photo:
            await query.message.reply_text(texto, reply_markup=reply_markup, parse_mode="HTML")
        else:
            await query.message.edit_text(texto, reply_markup=reply_markup, parse_mode="HTML")
    except Exception:
        await query.message.reply_text(texto, reply_markup=reply_markup, parse_mode="HTML")


async def menu_estado_animo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton("😄 Excelente", callback_data="animo_Excelente"),
            InlineKeyboardButton("🙂 Satisfecho", callback_data="animo_Satisfecho")
        ],
        [
            InlineKeyboardButton("😐 Neutral", callback_data="animo_Neutral"),
            InlineKeyboardButton("🤔 Con dudas", callback_data="animo_Con_dudas")
        ],
        [InlineKeyboardButton("🔙 Volver", callback_data="volver_inicio")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    texto = "⭐ <b>¿Cómo calificas tu experiencia o cuál es tu estado de ánimo actual con la plataforma?</b>"

    try:
        if query.message.photo:
            await query.message.reply_text(texto, reply_markup=reply_markup, parse_mode="HTML")
        else:
            await query.message.edit_text(texto, reply_markup=reply_markup, parse_mode="HTML")
    except Exception:
        await query.message.reply_text(texto, reply_markup=reply_markup, parse_mode="HTML")


async def registrar_estado_animo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("¡Gracias por tu feedback!")
    user_id = query.from_user.id
    animo = query.data.replace("animo_", "").replace("_", " ")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE usuarios SET estado_animo = ? WHERE telegram_id = ?", (animo, user_id))
    conn.commit()
    conn.close()

    texto = f"✅ ¡Hemos registrado tu calificación como: <b>{animo}</b>! Agradecemos tu opinión para seguir mejorando."
    keyboard = [[InlineKeyboardButton("🔙 Volver al Inicio", callback_data="volver_inicio")]]
    
    try:
        if query.message.photo:
            await query.message.reply_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        else:
            await query.message.edit_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    except Exception:
        await query.message.reply_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def descargar_mi_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    conn = get_db()
    df_inv = pd.read_sql_query("SELECT * FROM inversiones WHERE telegram_id = ?", conn, params=(user_id,))
    df_dep = pd.read_sql_query("SELECT * FROM depositos WHERE telegram_id = ?", conn, params=(user_id,))
    df_ret = pd.read_sql_query("SELECT * FROM retiros WHERE telegram_id = ?", conn, params=(user_id,))
    conn.close()

    filename = f"estado_cuenta_{user_id}.xlsx"
    with pd.ExcelWriter(filename, engine="openpyxl") as writer:
        df_inv.to_excel(writer, sheet_name="Inversiones", index=False)
        df_dep.to_excel(writer, sheet_name="Depositos", index=False)
        df_ret.to_excel(writer, sheet_name="Retiros", index=False)

    try:
        with open(filename, "rb") as doc:
            await context.bot.send_document(
                chat_id=user_id,
                document=doc,
                caption="📈 <b>Tu Estado de Cuenta Financiero en Excel</b>",
                parse_mode="HTML"
            )
        os.remove(filename)
        await query.answer("✅ Archivo enviado con éxito al chat.", show_alert=True)
    except Exception as e:
        logger.error(f"Error enviando Excel al usuario: {e}")
        await query.answer("❌ Error al generar el archivo.", show_alert=True)


# --- PANEL DE ADMINISTRACIÓN ---
async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if not es_administrador(user_id):
        await query.answer("❌ Acceso denegado.", show_alert=True)
        return

    dep_activo = obtener_configuracion("depositos_activos")
    ret_activo = obtener_configuracion("retiros_activos")

    text_dep = "🟢 Activos" if dep_activo == 1 else "🔴 Desactivados"
    text_ret = "🟢 Activos" if ret_activo == 1 else "🔴 Desactivados"

    keyboard = [
        [
            InlineKeyboardButton(f"Depósitos: {text_dep}", callback_data="admin_toggle_depositos"),
            InlineKeyboardButton(f"Retiros: {text_ret}", callback_data="admin_toggle_retiros")
        ],
        [
            InlineKeyboardButton("📊 Resumen Global", callback_data="admin_resumen_global"),
            InlineKeyboardButton("👥 Buscar Usuario", callback_data="admin_buscar_usuario_inicio")
        ],
        [
            InlineKeyboardButton("📢 Difusión Masiva", callback_data="admin_broadcast_inicio"),
            InlineKeyboardButton("📈 Aplicar Rendimiento Diario", callback_data="admin_ejecutar_rendimiento")
        ],
        [InlineKeyboardButton("🔄 Actualizar Panel", callback_data="admin_panel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    texto = "⚙️ <b>Panel de Control de Administración</b>\n\nSelecciona una opción de gestión:"

    try:
        if query.message.photo:
            await query.message.reply_text(texto, reply_markup=reply_markup, parse_mode="HTML")
        else:
            await query.message.edit_text(texto, reply_markup=reply_markup, parse_mode="HTML")
    except Exception:
        await query.message.reply_text(texto, reply_markup=reply_markup, parse_mode="HTML")


async def admin_toggle_feature(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not es_administrador(query.from_user.id):
        return

    data = query.data
    if "depositos" in data:
        actual = obtener_configuracion("depositos_activos")
        nuevo = 0 if actual == 1 else 1
        actualizar_configuracion("depositos_activos", nuevo)
    elif "retiros" in data:
        actual = obtener_configuracion("retiros_activos")
        nuevo = 0 if actual == 1 else 1
        actualizar_configuracion("retiros_activos", nuevo)

    await admin_panel_callback(update, context)


async def admin_resumen_global(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not es_administrador(query.from_user.id):
        return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM usuarios")
    total_usuarios = cursor.fetchone()[0]

    cursor.execute("SELECT SUM(monto) FROM inversiones WHERE estado = 'activa'")
    capital_total = cursor.fetchone()[0] or 0.0

    cursor.execute("SELECT SUM(monto) FROM depositos WHERE estado = 'aprobado'")
    depositos_totales = cursor.fetchone()[0] or 0.0

    cursor.execute("SELECT SUM(monto) FROM retiros WHERE estado = 'aprobado'")
    retiros_totales = cursor.fetchone()[0] or 0.0
    conn.close()

    texto = (
        "📈 <b>Resumen Global de la Plataforma</b>\n\n"
        f"• Total de usuarios registrados: <b>{total_usuarios}</b>\n"
        f"• Capital activo en inversiones: <b>{capital_total:.2f} USDT</b>\n"
        f"• Depósitos aprobados acumulados: <b>{depositos_totales:.2f} USDT</b>\n"
        f"• Retiros aprobados acumulados: <b>{retiros_totales:.2f} USDT</b>"
    )
    keyboard = [[InlineKeyboardButton("🔙 Volver al Panel", callback_data="admin_panel")]]
    
    try:
        if query.message.photo:
            await query.message.reply_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        else:
            await query.message.edit_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    except Exception:
        await query.message.reply_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def admin_ejecutar_rendimiento_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not es_administrador(query.from_user.id):
        return

    aplicar_rendimiento_diario()
    await query.answer("✅ ¡Rendimiento diario aplicado exitosamente a las inversiones activas!", show_alert=True)
    await admin_panel_callback(update, context)


# --- GESTIÓN DE DEPÓSITOS POR ADMIN ---
async def admin_deposito_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if not es_administrador(query.from_user.id):
        return

    partes = data.split("_")
    accion = partes[2]  # ok o no
    dep_id = partes[3]

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, monto FROM depositos WHERE id = ? AND estado = 'pendiente'", (dep_id,))
    dep = cursor.fetchone()

    if not dep:
        conn.close()
        try:
            if query.message.photo:
                await query.message.edit_caption(caption="⚠️ Este depósito ya fue procesado o no existe.", parse_mode="HTML")
            else:
                await query.message.edit_text(text="⚠️ Este depósito ya fue procesado o no existe.", parse_mode="HTML")
        except Exception:
            pass
        return

    target_user = dep["telegram_id"]
    monto = dep["monto"]

    if accion == "ok":
        cursor.execute("UPDATE depositos SET estado = 'aprobado' WHERE id = ?", (dep_id,))
        
        # Crear plan de inversión activa
        fecha_aprobacion = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "INSERT INTO inversiones (telegram_id, monto, ganancias_generadas, tope_ganancia, estado, fecha_inicio) VALUES (?, ?, 0.0, ?, 'activa', ?)",
            (target_user, monto, monto * 2.0, fecha_aprobacion)
        )
        
        # Procesar comisión de referidos (10%) si tiene referido
        cursor.execute("SELECT referido_por FROM usuarios WHERE telegram_id = ?", (target_user,))
        ref_row = cursor.fetchone()
        if ref_row and ref_row["referido_por"]:
            referrer_id = ref_row["referido_por"]
            comision_ref = monto * 0.10
            cursor.execute("UPDATE usuarios SET ganancias_referidos = ganancias_referidos + ? WHERE telegram_id = ?", (comision_ref, referrer_id))
            try:
                await context.bot.send_message(
                    chat_id=referrer_id,
                    text=f"🤝 <b>¡Nueva comisión por referido!</b> Has recibido <b>{comision_ref:.2f} USDT</b> (10%) por el depósito de tu referido.",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        conn.commit()
        conn.close()

        try:
            if query.message.photo:
                await query.message.edit_caption(caption=f"✅ Depósito #{dep_id} <b>APROBADO</b> exitosamente.", parse_mode="HTML")
            else:
                await query.message.edit_text(text=f"✅ Depósito #{dep_id} <b>APROBADO</b> exitosamente.", parse_mode="HTML")
        except Exception:
            pass

        try:
            await context.bot.send_message(
                chat_id=target_user,
                text=f"✅ ¡Tu depósito de <b>{monto} USDT</b> ha sido <b>APROBADO</b>! Tu plan de inversión se encuentra activo.",
                parse_mode="HTML"
            )
        except Exception:
            pass

    else:
        cursor.execute("UPDATE depositos SET estado = 'rechazado' WHERE id = ?", (dep_id,))
        conn.commit()
        conn.close()

        try:
            if query.message.photo:
                await query.message.edit_caption(caption=f"❌ Depósito #{dep_id} <b>RECHAZADO</b>.", parse_mode="HTML")
            else:
                await query.message.edit_text(text=f"❌ Depósito #{dep_id} <b>RECHAZADO</b>.", parse_mode="HTML")
        except Exception:
            pass

        try:
            await context.bot.send_message(
                chat_id=target_user,
                text=f"❌ Tu depósito de <b>{monto} USDT</b> ha sido <b>RECHAZADO</b> por el administrador.",
                parse_mode="HTML"
            )
        except Exception:
            pass


# --- GESTIÓN DE RETIROS POR ADMIN ---
async def admin_retiro_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if not es_administrador(query.from_user.id):
        return

    partes = data.split("_")
    accion = partes[2]  # aprobar o rechazar
    ret_id = partes[3]

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, monto, comision FROM retiros WHERE id = ? AND estado = 'pendiente'", (ret_id,))
    ret = cursor.fetchone()

    if not ret:
        conn.close()
        await query.edit_message_text("⚠️ Este retiro ya fue procesado o no existe.")
        return

    target_user = ret["telegram_id"]
    monto = ret["monto"]
    comision = ret["comision"]
    neto = monto - comision

    if accion == "aprobar":
        context.user_data["admin_ret_id_procesando"] = ret_id
        context.user_data["admin_ret_target_user"] = target_user
        context.user_data["admin_ret_monto"] = monto
        context.user_data["admin_ret_neto"] = neto

        await query.message.reply_text(
            f"📤 <b>Aprobación de Retiro #{ret_id}</b>\n\n"
            f"Monto neto a enviar: <b>{neto} USDT</b>\n"
            "Envía la <b>captura de pantalla del comprobante de transferencia</b> realizado a la wallet del usuario:",
            parse_mode="HTML"
        )
        return ADMIN_RET_APROBAR_COMPROBANTE
    else:
        context.user_data["admin_ret_id_procesando"] = ret_id
        context.user_data["admin_ret_target_user"] = target_user
        context.user_data["admin_ret_monto"] = monto

        await query.message.reply_text(
            f"❌ <b>Rechazo de Retiro #{ret_id}</b>\n\n"
            "Escribe el motivo del rechazo para informarlo al usuario:",
            parse_mode="HTML"
        )
        return ADMIN_RET_OTRO_MOTIVO


async def admin_recibir_comprobante_retiro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("⚠️ Debes enviar una captura de pantalla del comprobante de pago:")
        return ADMIN_RET_APROBAR_COMPROBANTE

    file_id = update.message.photo[-1].file_id
    ret_id = context.user_data.get("admin_ret_id_procesando")
    target_user = context.user_data.get("admin_ret_target_user")
    monto = context.user_data.get("admin_ret_monto")
    neto = context.user_data.get("admin_ret_neto")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE retiros SET estado = 'aprobado', comprobante_file_id = ? WHERE id = ?", (file_id, ret_id))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"✅ Retiro #{ret_id} marcado como aprobado y comprobante enviado al usuario.", parse_mode="HTML")

    try:
        await context.bot.send_photo(
            chat_id=target_user,
            photo=file_id,
            caption=(
                "✅ <b>¡Tu retiro ha sido APROBADO y PAGADO!</b> 🎉\n\n"
                f"• Monto bruto: {monto} USDT\n"
                f"• Neto transferido: <b>{neto} USDT</b>\n\n"
                "Adjuntamos el comprobante de la transacción."
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error enviando comprobante de retiro al usuario: {e}")

    await start(update, context)
    return ConversationHandler.END


async def admin_recibir_motivo_rechazo_retiro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    motivo = update.message.text.strip()
    ret_id = context.user_data.get("admin_ret_id_procesando")
    target_user = context.user_data.get("admin_ret_target_user")
    monto = context.user_data.get("admin_ret_monto")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE retiros SET estado = 'rechazado' WHERE id = ?", (ret_id,))
    
    # Devolver los fondos a ganancias disponibles del usuario
    cursor.execute("UPDATE inversiones SET ganancias_generadas = ganancias_generadas + ? WHERE telegram_id = ? AND estado = 'activa' LIMIT 1", (monto, target_user))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"❌ Retiro #{ret_id} rechazado y fondos devueltos al usuario.", parse_mode="HTML")

    try:
        await context.bot.send_message(
            chat_id=target_user,
            text=(
                f"❌ <b>Tu solicitud de retiro de {monto} USDT ha sido RECHAZADA.</b>\n\n"
                f"• Motivo: {motivo}\n\n"
                "Tus fondos han sido devueltos a tu balance disponible."
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error notificando rechazo de retiro al usuario: {e}")

    await start(update, context)
    return ConversationHandler.END


# --- BÚSQUEDA DE USUARIO POR ADMIN ---
async def admin_buscar_usuario_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not es_administrador(query.from_user.id):
        return

    await query.message.reply_text(
        "🔍 <b>Búsqueda de Usuario</b>\n\nIngresa el <b>ID de Telegram</b> o el <b>Nombre de Usuario (@username)</b> del cliente que deseas consultar:",
        parse_mode="HTML"
    )
    return BUSCAR_USUARIO_ADMIN


async def admin_recibir_busqueda_usuario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_busqueda = update.message.text.strip().replace("@", "")
    
    conn = get_db()
    cursor = conn.cursor()
    if texto_busqueda.isdigit():
        cursor.execute("SELECT * FROM usuarios WHERE telegram_id = ?", (int(texto_busqueda),))
    else:
        cursor.execute("SELECT * FROM usuarios WHERE username LIKE ?", (f"%{texto_busqueda}%",))
    
    user_row = cursor.fetchone()
    conn.close()

    if not user_row:
        await update.message.reply_text("⚠️ No se encontró ningún usuario con ese criterio. Inténtalo de nuevo o /cancelar:")
        return BUSCAR_USUARIO_ADMIN

    u_id = user_row["telegram_id"]
    resumen = obtener_resumen_financiero(u_id)

    info_msg = (
        "👤 <b>INFORMACIÓN DEL USUARIO</b>\n\n"
        f"• Nombre completo: {user_row['nombre_completo']}\n"
        f"• Telegram ID: <code>{u_id}</code>\n"
        f"• Username: @{user_row['username']}\n"
        f"• Correo: {user_row['email']}\n"
        f"• Teléfono: {user_row['telefono']}\n"
        f"• Wallet: <code>{user_row['wallet']}</code>\n"
        f"• Estado de ánimo: {user_row['estado_animo'] or 'No registrado'}\n\n"
        f"📊 <b>Balances:</b>\n"
        f"• Capital activo: {resumen['capital_activo']:.2f} USDT\n"
        f"• Ganancias disponibles: {resumen['ganancias_disponibles']:.2f} USDT\n"
        f"• Ganancias referidos: {resumen['ganancias_referidos']:.2f} USDT\n"
        f"• Total retirado: {resumen['total_retirado']:.2f} USDT"
    )
    await update.message.reply_text(info_msg, parse_mode="HTML")
    await start(update, context)
    return ConversationHandler.END


# --- DIFUSIÓN MASIVA (BROADCAST) ---
async def admin_broadcast_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not es_administrador(query.from_user.id):
        return

    await query.message.reply_text(
        "📢 <b>Difusión Masiva (Broadcast)</b>\n\nEnvía el mensaje (texto, foto o anuncio) que deseas difundir a <b>todos los usuarios registrados</b> en el bot:\n\n<i>(/cancelar para anular)</i>",
        parse_mode="HTML"
    )
    return ADMIN_BROADCAST


async def admin_ejecutar_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_administrador(update.effective_user.id):
        return ConversationHandler.END

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id FROM usuarios")
    usuarios = cursor.fetchall()
    conn.close()

    enviados = 0
    fallidos = 0

    for u in usuarios:
        chat_id = u["telegram_id"]
        try:
            if update.message.photo:
                photo_id = update.message.photo[-1].file_id
                caption = update.message.caption or ""
                await context.bot.send_photo(chat_id=chat_id, photo=photo_id, caption=caption, parse_mode="HTML")
            else:
                texto = update.message.text_html
                await context.bot.send_message(chat_id=chat_id, text=texto, parse_mode="HTML")
            enviados += 1
        except Exception:
            fallidos += 1

    await update.message.reply_text(
        f"📢 <b>Difusión completada</b>\n\n"
        f"• Enviados exitosamente: {enviados}\n"
        f"• Fallidos / Bloqueados: {fallidos}",
        parse_mode="HTML"
    )
    await start(update, context)
    return ConversationHandler.END


# --- CANCELAR Y VOLVER ---
async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ Operación cancelada.", reply_markup=ReplyKeyboardRemove()
    )
    await start(update, context)
    return ConversationHandler.END


async def volver_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await start(update, context)


async def kyc_no_permitido_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("⚠️ Debes tener una Wallet registrada previamente para usar el Setteo por KYC.", show_alert=True)


def main():
    init_db()
    asegurar_columnas_usuarios()
    asegurar_tabla_kyc()

    app = ApplicationBuilder().token(TOKEN).build()

    # Handlers de Conversación
    conv_registro = ConversationHandler(
        entry_points=[CallbackQueryHandler(iniciar_registro, pattern="^iniciar_registro$")],
        states={
            NOMBRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_nombre)],
            EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_email)],
            TELEFONO: [MessageHandler(filters.TEXT & ~filters.COMMAND | filters.CONTACT, recibir_telefono)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )

    conv_deposito = ConversationHandler(
        entry_points=[CallbackQueryHandler(iniciar_deposito_flujo, pattern="^menu_depositar$")],
        states={
            MONTO_DEP: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_monto_dep)],
            HASH_DEP: [
                MessageHandler(filters.PHOTO, recibir_hash_dep),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_hash_dep)
            ],
            COMPROBANTE_DEP: [MessageHandler(filters.PHOTO, recibir_comprobante_dep)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )

    conv_retiro = ConversationHandler(
        entry_points=[CallbackQueryHandler(iniciar_retiro_flujo, pattern="^menu_retirar$")],
        states={
            MONTO_RET: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_monto_ret)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )

    conv_wallet = ConversationHandler(
        entry_points=[CallbackQueryHandler(iniciar_registro_wallet, pattern="^registrar_wallet$")],
        states={
            WALLET_DIR: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_wallet_dir)],
            WALLET_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_wallet_pin)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )

    conv_cambio_wallet = ConversationHandler(
        entry_points=[CallbackQueryHandler(iniciar_cambio_wallet, pattern="^cambiar_wallet$")],
        states={
            CAMBIO_WALLET_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_cambio_wallet_pin)],
            CAMBIO_WALLET_DIR: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_cambio_wallet_dir)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )

    conv_kyc_wallet = ConversationHandler(
        entry_points=[CallbackQueryHandler(iniciar_kyc_wallet, pattern="^iniciar_kyc_wallet$")],
        states={
            KYC_WALLET_DIR: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_kyc_wallet_dir)],
            KYC_NOMBRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_kyc_nombre)],
            KYC_FOTO: [MessageHandler(filters.PHOTO, recibir_kyc_foto)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )

    conv_buscar_usuario = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_buscar_usuario_inicio, pattern="^admin_buscar_usuario_inicio$")],
        states={
            BUSCAR_USUARIO_ADMIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_recibir_busqueda_usuario)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )

    conv_admin_retiro = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_retiro_callback, pattern="^admin_ret_(aprobar|rechazar)_.*$")],
        states={
            ADMIN_RET_APROBAR_COMPROBANTE: [MessageHandler(filters.PHOTO, admin_recibir_comprobante_retiro)],
            ADMIN_RET_OTRO_MOTIVO: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_recibir_motivo_rechazo_retiro)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )

    conv_broadcast = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_broadcast_inicio, pattern="^admin_broadcast_inicio$")],
        states={
            ADMIN_BROADCAST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ejecutar_broadcast),
                MessageHandler(filters.PHOTO, admin_ejecutar_broadcast)
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )

    # Registrar handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_registro)
    app.add_handler(conv_deposito)
    app.add_handler(conv_retiro)
    app.add_handler(conv_wallet)
    app.add_handler(conv_cambio_wallet)
    app.add_handler(conv_kyc_wallet)
    app.add_handler(conv_buscar_usuario)
    app.add_handler(conv_admin_retiro)
    app.add_handler(conv_broadcast)

    # Callbacks generales
    app.add_handler(CallbackQueryHandler(volver_inicio, pattern="^volver_inicio$"))
    app.add_handler(CallbackQueryHandler(menu_wallet, pattern="^menu_wallet$"))
    app.add_handler(CallbackQueryHandler(menu_balance, pattern="^menu_balance$"))
    app.add_handler(CallbackQueryHandler(menu_referidos, pattern="^menu_referidos$"))
    app.add_handler(CallbackQueryHandler(menu_estado_animo, pattern="^menu_estado_animo$"))
    app.add_handler(CallbackQueryHandler(registrar_estado_animo_callback, pattern="^animo_.*$"))
    app.add_handler(CallbackQueryHandler(descargar_mi_excel, pattern="^descargar_mi_excel$"))
    app.add_handler(CallbackQueryHandler(kyc_no_permitido_callback, pattern="^kyc_no_permitido$"))

    # Callbacks de Admin
    app.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^admin_panel$"))
    app.add_handler(CallbackQueryHandler(admin_toggle_feature, pattern="^admin_toggle_.*$"))
    app.add_handler(CallbackQueryHandler(admin_resumen_global, pattern="^admin_resumen_global$"))
    app.add_handler(CallbackQueryHandler(admin_ejecutar_rendimiento_manual, pattern="^admin_ejecutar_rendimiento$"))
    app.add_handler(CallbackQueryHandler(admin_deposito_callback, pattern="^admin_dep_(ok|no)_.*$"))
    app.add_handler(CallbackQueryHandler(admin_kyc_callback, pattern="^admin_kyc_(ok|no)_.*$"))

    print("🤖 Bot iniciado correctamente...")
    app.run_polling()


if __name__ == "__main__":
    main()
