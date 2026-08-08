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
    TOKEN = os.getenv("TOKEN", "8656036159:AAEkZ9srVuHecDFFAMUY7mZmzFQ2-lVdxBQ")
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

# Estados para la gestión de aprobación/rechazo de depósitos por parte del admin
ADMIN_DEP_APROBAR_COMPROBANTE = range(35, 36)

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
    """Asegura que las columnas de wallet, pin, estado_animo, referido_por y ganancias_referidos existan."""
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute("ALTER TABLE usuarios ADD COLUMN wallet TEXT")
        conn.commit()
    except Exception:
        conn.rollback()
        
    try:
        cursor.execute("ALTER TABLE usuarios ADD COLUMN pin TEXT")
        conn.commit()
    except Exception:
        conn.rollback()
        
    try:
        cursor.execute("ALTER TABLE usuarios ADD COLUMN estado_animo TEXT")
        conn.commit()
    except Exception:
        conn.rollback()
        
    try:
        cursor.execute("ALTER TABLE usuarios ADD COLUMN referido_por INTEGER")
        conn.commit()
    except Exception:
        conn.rollback()
        
    try:
        cursor.execute("ALTER TABLE usuarios ADD COLUMN ganancias_referidos REAL DEFAULT 0.0")
        conn.commit()
    except Exception:
        conn.rollback()
        
    cursor.close()
    conn.close()

def asegurar_tabla_kyc():
    """Crea la tabla para las solicitudes KYC de recuperación de wallet."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS kyc_wallets (
            id SERIAL PRIMARY KEY,
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
        "📸 <b>Captura de pantalla</b>\n\nAhora envía la foto del comprobante:",
        parse_mode="HTML",
    )
    return COMPROBANTE_DEP


async def recibir_comprobante_dep(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not update.message.photo:
        await update.message.reply_text("⚠️ Envía una imagen válida.")
        return COMPROBANTE_DEP

    tx_hash = context.user_data.get("hash_dep", "N/A")
    file_id = update.message.photo[-1].file_id
    return await procesar_deposito_final(update, context, tx_hash, file_id)


async def procesar_deposito_final(update: Update, context: ContextTypes.DEFAULT_TYPE, tx_hash: str, file_id: str):
    user = update.effective_user
    monto = context.user_data.get("monto_dep", 0.0)

    tx_id = registrar_deposito(user.id, monto, tx_hash, file_id)

    await update.message.reply_text(
        "✅ <b>¡Depósito registrado!</b> En revisión.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML",
    )

    if ADMIN_ID:
        try:
            admin_msg = (
                "🔔 <b>NUEVO DEPÓSITO</b>\n\n"
                f"• ID: <code>{tx_id}</code>\n"
                f"• Usuario: {user.first_name} (<code>{user.id}</code>)\n"
                f"• Monto: <b>{monto} USDT</b>\n"
                f"• TXID: <code>{tx_hash}</code>"
            )
            keyboard = [[
                InlineKeyboardButton("✅ Aprobar", callback_data=f"admin_dep_ok_{tx_id}"),
                InlineKeyboardButton("❌ Rechazar", callback_data=f"admin_dep_no_{tx_id}"),
            ]]
            await context.bot.send_photo(
                chat_id=int(ADMIN_ID),
                photo=file_id,
                caption=admin_msg,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error notificando depósito al admin: {e}")

    await start(update, context)
    return ConversationHandler.END


# --- FLUJO ADMIN: GESTIÓN DE DEPÓSITOS (APROBAR / RECHAZAR) ---
async def admin_deposito_iniciar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if not es_administrador(query.from_user.id):
        await query.answer("❌ Sin permisos.", show_alert=True)
        return ConversationHandler.END

    partes = data.split("_")
    accion = partes[2]  # ok o no
    tx_id = partes[3]

    context.user_data["admin_dep_tx_id"] = tx_id

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, monto FROM transacciones WHERE id = ? AND tipo = 'deposito'", (tx_id,))
    tx = cursor.fetchone()
    conn.close()

    if not tx:
        await query.answer("Transacción no encontrada.", show_alert=True)
        return ConversationHandler.END

    target_user = tx["telegram_id"]
    monto = tx["monto"]

    if accion == "ok":
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE transacciones SET estado = 'completado' WHERE id = ?", (tx_id,))
        
        # Verificar si el usuario tiene referido y aplicar comisión del 5% si aplica
        cursor.execute("SELECT referido_por FROM usuarios WHERE telegram_id = ?", (target_user,))
        user_row = cursor.fetchone()
        if user_row and user_row["referido_por"]:
            referrer_id = user_row["referido_por"]
            comision_ref = monto * 0.05
            cursor.execute(
                "INSERT INTO transacciones (telegram_id, tipo, monto, estado) VALUES (?, 'comision_referido', ?, 'completado')",
                (referrer_id, comision_ref)
            )
            cursor.execute(
                "UPDATE usuarios SET ganancias_referidos = ganancias_referidos + ? WHERE telegram_id = ?",
                (comision_ref, referrer_id)
            )
            try:
                await context.bot.send_message(
                    chat_id=referrer_id,
                    text=f"🎉 ¡Has recibido una comisión de referido del <b>5% (${comision_ref:.2f} USDT)</b> por el depósito de tu invitado!",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        conn.commit()
        conn.close()

        try:
            if query.message.photo:
                await query.message.edit_caption(caption=f"✅ Depósito #{tx_id} <b>APROBADO</b> exitosamente.", parse_mode="HTML")
            else:
                await query.message.edit_text(text=f"✅ Depósito #{tx_id} <b>APROBADO</b> exitosamente.", parse_mode="HTML")
        except Exception:
            pass

        try:
            await context.bot.send_message(
                chat_id=target_user,
                text=f"🎉 ¡Tu depósito por <b>{monto} USDT</b> ha sido aprobado exitosamente! Tu inversión ha comenzado a operar.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error notificando aprobación de depósito al usuario: {e}")

        return ConversationHandler.END

    else:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE transacciones SET estado = 'rechazado' WHERE id = ?", (tx_id,))
        conn.commit()
        conn.close()

        try:
            if query.message.photo:
                await query.message.edit_caption(caption=f"❌ Depósito #{tx_id} <b>RECHAZADO</b>.", parse_mode="HTML")
            else:
                await query.message.edit_text(text=f"❌ Depósito #{tx_id} <b>RECHAZADO</b>.", parse_mode="HTML")
        except Exception:
            pass

        try:
            await context.bot.send_message(
                chat_id=target_user,
                text=f"❌ Tu solicitud de depósito por <b>{monto} USDT</b> fue rechazada por el administrador.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error notificando rechazo de depósito al usuario: {e}")

        return ConversationHandler.END


# --- FLUJO DE RETIRO ---
async def iniciar_retiro_flujo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    user_row = obtener_datos_usuario(user_id)
    if not user_row or not user_row["wallet"]:
        msg_error = "⚠️ No tienes una <b>Wallet registrada</b>. Por favor, registra tu billetera antes de solicitar un retiro."
        kb_error = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧰 Ir a Wallet", callback_data="menu_wallet")],
            [InlineKeyboardButton("🔙 Volver", callback_data="volver_inicio")]
        ])
        try:
            if query.message.photo:
                await query.message.reply_text(msg_error, reply_markup=kb_error, parse_mode="HTML")
            else:
                await query.message.edit_text(msg_error, reply_markup=kb_error, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(msg_error, reply_markup=kb_error, parse_mode="HTML")
        return ConversationHandler.END

    dia_actual = datetime.now().weekday()
    if dia_actual >= 5 and not es_administrador(user_id):
        msg_error = "⚠️ Retiros habilitados solo de <b>Lunes a Viernes</b>."
        kb_error = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver_inicio")]])
        try:
            if query.message.photo:
                await query.message.reply_text(msg_error, reply_markup=kb_error, parse_mode="HTML")
            else:
                await query.message.edit_text(msg_error, reply_markup=kb_error, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(msg_error, reply_markup=kb_error, parse_mode="HTML")
        return ConversationHandler.END

    if (
        obtener_configuracion("retiros_activos") == 0
        and not es_administrador(user_id)
    ):
        msg_error = "❌ Retiros desactivados temporalmente."
        kb_error = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver_inicio")]])
        try:
            if query.message.photo:
                await query.message.reply_text(msg_error, reply_markup=kb_error, parse_mode="HTML")
            else:
                await query.message.edit_text(msg_error, reply_markup=kb_error, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(msg_error, reply_markup=kb_error, parse_mode="HTML")
        return ConversationHandler.END

    resumen = obtener_resumen_financiero(user_id)
    balance = resumen["balance_disponible"]

    if balance <= 0:
        msg_error = "⚠️ No tienes balance disponible para retirar."
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
        "📤 <b>Solicitud de Retiro</b>\n\n"
        f"• Comisión: <b>{COMISION_RETIRO} USDT</b>\n"
        f"• Disponible: <b>${balance:.2f} USDT</b>\n"
        f"• Wallet Guardada (BEP-20): <code>{user_row['wallet']}</code>\n\n"
        "Escribe la cantidad exacta que deseas retirar:\n"
        "<i>(/cancelar para anular)</i>",
        parse_mode="HTML",
    )
    return MONTO_RET


async def recibir_monto_ret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        monto = float(update.message.text.strip().replace(",", "."))
        if monto <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Ingresa una cantidad válida.")
        return MONTO_RET

    user = update.effective_user
    resumen = obtener_resumen_financiero(user.id)
    balance = resumen["balance_disponible"]

    if (monto + COMISION_RETIRO) > balance:
        await update.message.reply_text(
            f"⚠️ El monto + comisión supera tu balance (${balance:.2f} USDT). Ingresa otro valor:"
        )
        return MONTO_RET

    user_row = obtener_datos_usuario(user.id)
    billetera = user_row["wallet"] if user_row and user_row["wallet"] else "N/A"

    tx_id = registrar_retiro(user.id, monto, COMISION_RETIRO, billetera)

    await update.message.reply_text(
        "✅ <b>¡Retiro solicitado!</b> En revisión del administrador.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML",
    )

    if ADMIN_ID:
        try:
            nombre_completo = user_row["nombre_completo"] if user_row and user_row["nombre_completo"] else (user.full_name or "N/A")
            email = user_row["email"] if user_row and user_row["email"] else "N/A"
            telefono = user_row["telefono"] if user_row and user_row["telefono"] else "N/A"
            username = f"@{user.username}" if user.username else "N/A"

            admin_msg = (
                "🔔 <b>NUEVO RETIRO SOLICITADO</b>\n\n"
                f"• ID Transacción: <code>{tx_id}</code>\n"
                f"• <b>Datos del Usuario:</b>\n"
                f"  - Nombre: {nombre_completo}\n"
                f"  - Usuario TG: {username} (<code>{user.id}</code>)\n"
                f"  - Correo: {email}\n"
                f"  - Teléfono: {telefono}\n\n"
                f"• <b>Detalles Financieros:</b>\n"
                f"  - Monto a retirar: <b>{monto} USDT</b>\n"
                f"  - Comisión: <b>{COMISION_RETIRO} USDT</b>\n"
                f"  - Total descontado: <b>{monto + COMISION_RETIRO} USDT</b>\n\n"
                f"• <b>Wallet de Destino (BNB Smart Chain - BEP-20):</b>\n"
                f"<code>{billetera}</code>"
            )
            keyboard = [[
                InlineKeyboardButton("✅ Aprobar", callback_data=f"admin_ret_ok_{tx_id}"),
                InlineKeyboardButton("❌ Rechazar", callback_data=f"admin_ret_no_{tx_id}"),
            ]]
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


# --- FLUJO ADMIN: GESTIÓN DE RETIROS (APROBAR / RECHAZAR) ---
async def admin_retirar_iniciar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if not es_administrador(query.from_user.id):
        await query.answer("❌ Sin permisos.", show_alert=True)
        return ConversationHandler.END

    partes = data.split("_")
    accion = partes[2]
    tx_id = partes[3]

    context.user_data["admin_ret_tx_id"] = tx_id

    if accion == "ok":
        await query.message.edit_text(
            f"📸 <b>Aprobar Retiro #{tx_id}</b>\n\n"
            "Envía la <b>captura de pantalla del comprobante de pago</b> para enviársela al usuario:\n"
            "<i>(/cancelar para anular)</i>",
            parse_mode="HTML"
        )
        return ADMIN_RET_APROBAR_COMPROBANTE
    else:
        keyboard = [
            [InlineKeyboardButton("⚠️ Wallet incorrecta", callback_data="admin_motivo_wallet")],
            [InlineKeyboardButton("✍️ Otro motivo", callback_data="admin_motivo_otro")],
            [InlineKeyboardButton("🔙 Cancelar", callback_data="admin_motivo_cancelar")]
        ]
        await query.message.edit_text(
            f"❌ <b>Rechazar Retiro #{tx_id}</b>\n\nSelecciona el motivo del rechazo:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return ADMIN_RET_OTRO_MOTIVO


async def admin_retirar_recibir_comprobante(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("⚠️ Por favor, envía una imagen válida como comprobante de pago:")
        return ADMIN_RET_APROBAR_COMPROBANTE

    file_id = update.message.photo[-1].file_id
    tx_id = context.user_data.get("admin_ret_tx_id")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, monto FROM transacciones WHERE id = ? AND tipo = 'retiro'", (tx_id,))
    tx = cursor.fetchone()

    if not tx:
        conn.close()
        await update.message.reply_text("⚠️ Transacción no encontrada.")
        return ConversationHandler.END

    target_user = tx["telegram_id"]
    monto = tx["monto"]

    cursor.execute("UPDATE transacciones SET estado = 'completado' WHERE id = ?", (tx_id,))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"✅ Retiro #{tx_id} <b>APROBADO</b> y comprobante enviado al usuario.", parse_mode="HTML")

    try:
        await context.bot.send_photo(
            chat_id=target_user,
            photo=file_id,
            caption=f"🎉 ¡Tu retiro por <b>{monto} USDT</b> ha sido aprobado y procesado exitosamente! Aquí tienes el comprobante de pago:",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error enviando comprobante al usuario: {e}")

    return ConversationHandler.END


async def admin_retirar_motivo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    tx_id = context.user_data.get("admin_ret_tx_id")

    if data == "admin_motivo_cancelar":
        await query.message.edit_text("❌ Operación de rechazo cancelada.")
        return ConversationHandler.END

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, monto FROM transacciones WHERE id = ? AND tipo = 'retiro'", (tx_id,))
    tx = cursor.fetchone()

    if not tx:
        conn.close()
        await query.answer("Transacción no encontrada.", show_alert=True)
        return ConversationHandler.END

    target_user = tx["telegram_id"]
    monto = tx["monto"]

    if data == "admin_motivo_wallet":
        cursor.execute("UPDATE transacciones SET estado = 'rechazado' WHERE id = ?", (tx_id,))
        conn.commit()
        conn.close()

        await query.message.edit_text(f"❌ Retiro #{tx_id} <b>RECHAZADO</b> por motivo: <i>Wallet incorrecta</i>.", parse_mode="HTML")
        try:
            await context.bot.send_message(
                chat_id=target_user,
                text=f"❌ Tu solicitud de retiro por <b>{monto} USDT</b> fue rechazada.\nMotivo: <b>Wallet incorrecta</b>.",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return ConversationHandler.END

    elif data == "admin_motivo_otro":
        await query.message.edit_text(
            "✍️ Ingresa por favor el motivo detallado del rechazo para notificar al usuario:\n"
            "<i>(/cancelar para anular)</i>",
            parse_mode="HTML"
        )
        return ADMIN_RET_OTRO_MOTIVO


async def admin_retirar_recibir_motivo_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    motivo_texto = update.message.text.strip()
    if len(motivo_texto) < 3:
        await update.message.reply_text("⚠️ Ingresa un motivo válido:")
        return ADMIN_RET_OTRO_MOTIVO

    tx_id = context.user_data.get("admin_ret_tx_id")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, monto FROM transacciones WHERE id = ? AND tipo = 'retiro'", (tx_id,))
    tx = cursor.fetchone()

    if not tx:
        conn.close()
        await update.message.reply_text("⚠️ Transacción no encontrada.")
        return ConversationHandler.END

    target_user = tx["telegram_id"]
    monto = tx["monto"]

    cursor.execute("UPDATE transacciones SET estado = 'rechazado' WHERE id = ?", (tx_id,))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"❌ Retiro #{tx_id} <b>RECHAZADO</b>. Motivo enviado al usuario.", parse_mode="HTML")

    try:
        await context.bot.send_message(
            chat_id=target_user,
            text=f"❌ Tu solicitud de retiro por <b>{monto} USDT</b> fue rechazada.\nMotivo: <b>{motivo_texto}</b>"
        )
    except Exception:
        pass

    return ConversationHandler.END


# --- FLUJO ADMIN: DIFUSIÓN MASIVA CON MULTIMEDIA ---
async def iniciar_broadcast_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not es_administrador(query.from_user.id):
        await query.answer("❌ Sin permisos.", show_alert=True)
        return ConversationHandler.END

    texto = (
        "📢 <b>Difusión de Mensaje Masivo con Multimedia</b>\n\n"
        "Envía el mensaje que deseas transmitir a <b>todos los usuarios registrados</b> (puede incluir texto, foto, video, documento, etc.):\n\n"
        "<i>(/cancelar para anular)</i>"
    )
    try:
        if query.message.photo:
            await query.message.reply_text(texto, parse_mode="HTML")
        else:
            await query.message.edit_text(texto, parse_mode="HTML")
    except Exception:
        await query.message.reply_text(texto, parse_mode="HTML")
    return ADMIN_BROADCAST


async def procesar_broadcast_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not es_administrador(user.id):
        return ConversationHandler.END

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id FROM usuarios")
    usuarios = cursor.fetchall()
    conn.close()

    enviados = 0
    fallidos = 0

    msg = update.message
    for u in usuarios:
        target_id = u["telegram_id"]
        try:
            await context.bot.copy_message(
                chat_id=target_id,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id
            )
            enviados += 1
        except Exception as e:
            logger.error(f"Error enviando broadcast a {target_id}: {e}")
            fallidos += 1

    await update.message.reply_text(
        f"✅ <b>Difusión completada</b>\n\n"
        f"• Enviados exitosamente: {enviados}\n"
        f"• Fallidos: {fallidos}",
        parse_mode="HTML"
    )
    await start(update, context)
    return ConversationHandler.END


async def cancelar_operacion(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "❌ Operación cancelada.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# --- TAREA AUTOMÁTICA DE CONTABILIDAD ---
async def contabilidad_diaria_job(context: ContextTypes.DEFAULT_TYPE):
    nyse_tz = pytz.timezone('America/New_York')
    ahora = datetime.now(nyse_tz)
    
    if ahora.weekday() < 5:
        try:
            aplicar_rendimiento_diario(porcentaje_diario=0.5)
            logger.info(f"✅ Contabilidad automática ejecutada: {ahora.strftime('%Y-%m-%d')}")
            
            if ADMIN_ID:
                await context.bot.send_message(
                    chat_id=int(ADMIN_ID),
                    text="✅ <b>Contabilidad Automática:</b> Los rendimientos diarios (0.5%) han sido aplicados con éxito.",
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Error ejecutando contabilidad automática: {e}")
    else:
        logger.info(f"Fin de semana detectado ({ahora.strftime('%A')}). No se aplican rendimientos.")


# --- EJECUTAR CONTABILIDAD MANUAL ---
async def ejecutar_contabilidad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        await query.answer()
    else:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

    if not es_administrador(user_id):
        if update.callback_query:
            await query.answer("❌ Sin autorización.", show_alert=True)
        else:
            await update.message.reply_text("❌ No tienes permisos de administrador.")
        return
        
    nyse_tz = pytz.timezone('America/New_York')
    ahora = datetime.now(nyse_tz)
    
    advertencia = ""
    if ahora.weekday() >= 5:
        advertencia = "\n⚠️ <i>Nota: Se ha ejecutado manualmente durante un fin de semana.</i>"

    try:
        aplicar_rendimiento_diario(porcentaje_diario=0.5)
        texto = f"✅ <b>Contabilidad ejecutada con éxito (0.5%).</b> Se han repartido los rendimientos diarios a las inversiones activas.{advertencia}"
    except Exception as e:
        logger.error(f"Error ejecutando contabilidad: {e}")
        texto = f"❌ Error al ejecutar contabilidad: {e}"

    if update.callback_query:
        await context.bot.send_message(chat_id=chat_id, text=texto, parse_mode="HTML")
    else:
        await update.message.reply_text(texto, parse_mode="HTML")


# --- EXPORTAR EXCEL FINANCIERO (GLOBAL) ---
async def exportar_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        await query.answer()
    else:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

    if not es_administrador(user_id):
        if update.callback_query:
            await query.answer("❌ Sin autorización.", show_alert=True)
        else:
            await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    conn = get_db()
    
    query_sql = """
        SELECT 
            u.telegram_id AS 'ID Telegram',
            COALESCE(u.nombre_completo, u.first_name, 'Sin Nombre') AS 'Nombre',
            COALESCE(u.username, 'N/A') AS 'Usuario de Telegram',
            COALESCE(u.email, 'N/A') AS 'Correo',
            COALESCE(u.telefono, 'N/A') AS 'Telefono',
            COALESCE(u.wallet, 'N/A') AS 'Wallet BEP-20',
            COALESCE(u.estado_animo, 'No definido') AS 'Estado de Ánimo',
            COALESCE(u.ganancias_referidos, 0.0) AS 'Ganancias por Comisiones',
            COALESCE(i.monto_inicial, 0.0) AS 'Capital Activos',
            CASE WHEN i.estado = 'vencido' THEN 1 ELSE 0 END AS 'Planes vencidos',
            COALESCE(i.ganancias_acumuladas, 0.0) AS 'Ganancia realizadas por plan',
            COALESCE(i.tope_ganancia, 0.0) AS 'Tope de ganancia por plan',
            COALESCE(i.tope_ganancia - i.ganancias_acumuladas, 0.0) AS 'Pendiente de ganar por plan',
            COALESCE((
                SELECT SUM(t.monto) 
                FROM transacciones t 
                WHERE t.telegram_id = u.telegram_id AND t.tipo = 'retiro' AND t.estado = 'completado'
            ), 0.0) AS 'Retiros hechos total',
            COALESCE(i.estado, 'Sin Inversión') AS 'Estado de inversión',
            COALESCE(i.fecha_activacion, 'N/A') AS 'Fecha de activación de cada plan'
        FROM usuarios u
        LEFT JOIN inversiones i ON u.telegram_id = i.telegram_id
    """
    
    try:
        df = pd.read_sql_query(query_sql, conn)
    except Exception as e:
        logger.error(f"Error leyendo datos para Excel Global: {e}")
        df = pd.DataFrame()
    finally:
        conn.close()

    archivo_excel = "Reporte_Financiero_Global.xlsx"
    df.to_excel(archivo_excel, index=False, engine='openpyxl')

    try:
        with open(archivo_excel, "rb") as doc:
            await context.bot.send_document(
                chat_id=chat_id,
                document=doc,
                filename="Reporte_Financiero_Global.xlsx",
                caption="📊 <b>Reporte Financiero Global</b>\nListado completo desglosado por plan.",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error enviando documento Excel Global: {e}")
    
    if os.path.exists(archivo_excel):
        os.remove(archivo_excel)


# --- EXPORTAR REPORTE DE CONCILIACIÓN ---
async def exportar_conciliacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        await query.answer()
    else:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

    if not es_administrador(user_id):
        if update.callback_query:
            await query.answer("❌ Sin autorización.", show_alert=True)
        else:
            await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    conn = get_db()
    cursor = conn.cursor()

    try:
        # 1. Inversión total (Suma del monto inicial de todas las inversiones registradas)
        cursor.execute("SELECT COALESCE(SUM(monto_inicial), 0.0) FROM inversiones")
        inversion_total = cursor.fetchone()[0]

        # 2. Comisiones pagadas totales (Comisiones de referidos pagadas + comisiones de retiros)
        cursor.execute("SELECT COALESCE(SUM(monto), 0.0) FROM transacciones WHERE tipo = 'comision_referido' AND estado = 'completado'")
        comisiones_referidos = cursor.fetchone()[0]

        cursor.execute("SELECT COALESCE(SUM(comision), 0.0) FROM transacciones WHERE tipo = 'retiro' AND estado = 'completado'")
        comisiones_retiros = cursor.fetchone()[0]

        comisiones_pagadas_totales = comisiones_referidos + comisiones_retiros

        # 3. Saldo por pagar (Suma del pendiente por ganar en planes activos: tope_ganancia - ganancias_acumuladas)
        cursor.execute("SELECT COALESCE(SUM(tope_ganancia - ganancias_acumuladas), 0.0) FROM inversiones WHERE estado = 'activa'")
        saldo_por_pagar = cursor.fetchone()[0]

        data = {
            "Concepto de Conciliación": [
                "Inversión Total",
                "Comisiones Pagadas Totales",
                "Saldo por Pagar (Pendiente en Planes Activos)"
            ],
            "Monto Total (USDT)": [
                inversion_total,
                comisiones_pagadas_totales,
                saldo_por_pagar
            ]
        }
        df = pd.DataFrame(data)

    except Exception as e:
        logger.error(f"Error generando datos de conciliación: {e}")
        df = pd.DataFrame()
    finally:
        conn.close()

    archivo_excel = "Reporte_Conciliacion.xlsx"
    df.to_excel(archivo_excel, index=False, engine='openpyxl')

    try:
        with open(archivo_excel, "rb") as doc:
            await context.bot.send_document(
                chat_id=chat_id,
                document=doc,
                filename="Reporte_Conciliacion.xlsx",
                caption=(
                    "📊 <b>Reporte de Conciliación Financiera</b>\n\n"
                    f"• <b>Inversión Total:</b> ${inversion_total:,.2f} USDT\n"
                    f"• <b>Comisiones Pagadas Totales:</b> ${comisiones_pagadas_totales:,.2f} USDT\n"
                    f"• <b>Saldo por Pagar:</b> ${saldo_por_pagar:,.2f} USDT"
                ),
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error enviando documento Excel de Conciliación: {e}")
    
    if os.path.exists(archivo_excel):
        os.remove(archivo_excel)


# --- GENERAR Y ENVIAR EXCEL DE UN USUARIO ESPECÍFICO ---
async def generar_excel_usuario(chat_id, target_user_id, context):
    conn = get_db()
    
    query_sql = """
        SELECT 
            u.telegram_id AS 'ID Telegram',
            COALESCE(u.nombre_completo, u.first_name, 'Sin Nombre') AS 'Nombre',
            COALESCE(u.username, 'N/A') AS 'Usuario de Telegram',
            COALESCE(u.email, 'N/A') AS 'Correo',
            COALESCE(u.telefono, 'N/A') AS 'Telefono',
            COALESCE(u.wallet, 'N/A') AS 'Wallet BEP-20',
            COALESCE(u.estado_animo, 'No definido') AS 'Estado de Ánimo',
            COALESCE(u.ganancias_referidos, 0.0) AS 'Ganancias por Comisiones',
            COALESCE(i.monto_inicial, 0.0) AS 'Capital Activos',
            CASE WHEN i.estado = 'vencido' THEN 1 ELSE 0 END AS 'Planes vencidos',
            COALESCE(i.ganancias_acumuladas, 0.0) AS 'Ganancia realizadas por plan',
            COALESCE(i.tope_ganancia, 0.0) AS 'Tope de ganancia por plan',
            COALESCE(i.tope_ganancia - i.ganancias_acumuladas, 0.0) AS 'Pendiente de ganar por plan',
            COALESCE((
                SELECT SUM(t.monto) 
                FROM transacciones t 
                WHERE t.telegram_id = u.telegram_id AND t.tipo = 'retiro' AND t.estado = 'completado'
            ), 0.0) AS 'Retiros hechos total',
            COALESCE(i.estado, 'Sin Inversión') AS 'Estado de inversión',
            COALESCE(i.fecha_activacion, 'N/A') AS 'Fecha de activación de cada plan'
        FROM usuarios u
        LEFT JOIN inversiones i ON u.telegram_id = i.telegram_id
        WHERE u.telegram_id = ?
    """
    
    try:
        df = pd.read_sql_query(query_sql, conn, params=(target_user_id,))
    except Exception as e:
        logger.error(f"Error leyendo datos de usuario para Excel: {e}")
        df = pd.DataFrame()
    finally:
        conn.close()

    if df.empty:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ No se encontraron datos económicos para el ID de usuario especificado o el usuario no está registrado.",
            parse_mode="HTML"
        )
        return

    archivo_excel = f"Estado_De_Cuenta_{target_user_id}.xlsx"
    df.to_excel(archivo_excel, index=False, engine='openpyxl')

    try:
        with open(archivo_excel, "rb") as doc:
            await context.bot.send_document(
                chat_id=chat_id,
                document=doc,
                filename=f"Estado_De_Cuenta_{target_user_id}.xlsx",
                caption=f"📄 <b>Estado de Cuenta Individual</b>\nDatos económicos para el usuario <code>{target_user_id}</code>.",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error enviando documento Excel de usuario: {e}")
    
    if os.path.exists(archivo_excel):
        os.remove(archivo_excel)


# --- FLUJO ADMIN: BUSCAR ESTADO DE CUENTA DE UN USUARIO INDIVIDUAL ---
async def iniciar_buscar_usuario_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not es_administrador(query.from_user.id):
        await query.answer("❌ Sin autorización.", show_alert=True)
        return ConversationHandler.END

    await query.message.reply_text(
        "🔍 <b>Búsqueda de Estado de Cuenta Individual</b>\n\n"
        "Escribe el <b>ID de Telegram</b> del usuario que deseas consultar:\n"
        "<i>(/cancelar para anular)</i>",
        parse_mode="HTML",
    )
    return BUSCAR_USUARIO_ADMIN


async def recibir_id_usuario_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    try:
        target_user_id = int(texto)
    except ValueError:
        await update.message.reply_text("⚠️ Por favor, ingresa un ID de Telegram válido en números enteros:")
        return BUSCAR_USUARIO_ADMIN

    await generar_excel_usuario(update.effective_chat.id, target_user_id, context)
    await start(update, context)
    return ConversationHandler.END


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "menu_wallet":
        await menu_wallet(update, context)

    elif data == "kyc_no_permitido":
        await query.answer("⚠️ Debes tener una dirección de billetera registrada previamente para usar el Setteo.", show_alert=True)

    elif data == "menu_referidos":
        if not usuario_tiene_plan_activo(user_id):
            texto = (
                "❌ <b>Link de Referido no disponible</b>\n\n"
                "Para generar tu enlace de referido y ganar el <b>5% de comisión</b> de las inversiones de tus invitados, "
                "debes tener al menos un <b>plan de inversión activo</b>."
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
            return

        bot_username = context.bot.username
        link_referido = f"https://t.me/{bot_username}?start=ref_{user_id}"

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*), COALESCE(ganancias_referidos, 0.0) FROM usuarios WHERE referido_por = ?", (user_id,))
        row = cursor.fetchone()
        total_referidos = row[0] if row else 0
        ganancias_totales = row[1] if row else 0.0
        conn.close()

        texto = (
            "🤝 <b>Sistema de Referidos</b>\n\n"
            "Invita a nuevos usuarios con tu enlace personal. Recibirás el <b>5% de la inversión</b> de cada persona que se registre y active un depósito.\n"
            "<i>(Este enlace funciona porque posees planes de inversión activos).</i>\n\n"
            f"🔗 <b>Tu enlace de referido:</b>\n<code>{link_referido}</code>\n\n"
            f"📊 <b>Tus Estadísticas de Referidos:</b>\n"
            f"• Total de referidos invitados: <b>{total_referidos}</b>\n"
            f"• Ganancias totales por comisiones: <b>${ganancias_totales:.2f} USDT</b>\n\n"
            "💡 <i>Las comisiones se acreditan de forma automática a tu saldo disponible cuando el administrador aprueba el depósito de tu referido.</i>"
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

    elif data == "menu_estado_animo":
        user_row = obtener_datos_usuario(user_id)
        animo_actual = user_row["estado_animo"] if user_row and user_row["estado_animo"] else "No seleccionado 🔄"
        
        text = (
            "⭐ <b>Calificación y Estado de Ánimo del Proyecto</b>\n\n"
            f"• Tu estado de ánimo actual: <b>{animo_actual}</b>\n\n"
            "Selecciona cómo te sientes con respecto al proyecto (puedes cambiarlo cuando quieras):"
        )
        keyboard = [
            [InlineKeyboardButton("🤩 Súper Optimista / Excelente", callback_data="animo_🤩 Súper Optimista")],
            [InlineKeyboardButton("😊 Satisfecho y Confiado", callback_data="animo_😊 Satisfecho")],
            [InlineKeyboardButton("😐 Neutral / Expectante", callback_data="animo_😐 Neutral")],
            [InlineKeyboardButton("😕 Preocupado / Inseguro", callback_data="animo_😕 Preocupado")],
            [InlineKeyboardButton("😡 Molesto / Insatisfecho", callback_data="animo_😡 Molesto")],
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

    elif data.startswith("animo_"):
        animo_elegido = data.replace("animo_", "")
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE usuarios SET estado_animo = ? WHERE telegram_id = ?", (animo_elegido, user_id))
        conn.commit()
        conn.close()

        await query.answer(f"✅ Estado de ánimo actualizado: {animo_elegido}", show_alert=True)
        await start(update, context)

    elif data == "menu_balance":
        resumen = obtener_resumen_financiero(user_id)
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, monto_inicial, ganancias_acumuladas, tope_ganancia, fecha_activacion FROM inversiones WHERE telegram_id = ? AND estado = 'activa'",
            (user_id,)
        )
        inversiones_activas = cursor.fetchall()
        conn.close()

        text = "📊 <b>Tu Balance y Planes de Inversión</b>\n\n"

        if resumen["deposito_pendiente"] > 0:
            text += f"⏳ Depósito pendiente: ${resumen['deposito_pendiente']:.2f} USDT\n"

        if inversiones_activas:
            text += "📦 <b>Tus planes activos:</b>\n"
            for inv in inversiones_activas:
                # Dependiendo si usas sqlite3.Row o no, ajustamos la obtención del valor. Asumo formato dict/row
                pendiente = inv['tope_ganancia'] - inv['ganancias_acumuladas']
                text += (
                    f"🔹 <b>Plan #{inv['id']}</b>\n"
                    f"   Capital: ${inv['monto_inicial']:.2f} USDT\n"
                    f"   Ganado: ${inv['ganancias_acumuladas']:.2f} USDT\n"
                    f"   Pendiente: ${pendiente:.2f} USDT\n"
                    f"   Activado: {inv['fecha_activacion']}\n"
                )
        else:
            text += "⚠️ No tienes planes de inversión activos.\n"

        text += (
            f"\n💰 <b>Balance Disponible (Retirable):</b> ${resumen['balance_disponible']:.2f} USDT\n\n"
            "<i>(Tus ganancias se acumulan de lunes a viernes automáticamente tras 24h del depósito)</i>"
        )
        
        keyboard = [[InlineKeyboardButton("🔙 Volver", callback_data="volver_inicio")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            if query.message.photo:
                await query.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")
            else:
                await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")

    elif data == "descargar_mi_excel":
        await query.message.reply_text("⏳ Generando tu estado de cuenta, por favor espera...", parse_mode="HTML")
        await generar_excel_usuario(query.message.chat_id, user_id, context)

    elif data == "volver_inicio":
        await start(update, context)

    elif data == "admin_panel":
        if es_administrador(user_id):
            texto = "⚙️ <b>Panel de Control de Administrador</b>\nSelecciona una acción:"
            keyboard = [
                [InlineKeyboardButton("🔍 Buscar Usuario por ID", callback_data="admin_buscar_usuario")],
                [InlineKeyboardButton("📢 Difusión Masiva", callback_data="admin_broadcast_start")],
                [InlineKeyboardButton("⚡ Ejecutar Contabilidad", callback_data="ejecutar_contabilidad")],
                [InlineKeyboardButton("📊 Reporte Global (Excel)", callback_data="exportar_excel")],
                [InlineKeyboardButton("📉 Reporte de Conciliación", callback_data="exportar_conciliacion")],
                [InlineKeyboardButton("🔙 Volver al Inicio", callback_data="volver_inicio")]
            ]
            try:
                if query.message.photo:
                    await query.message.reply_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
                else:
                    await query.message.edit_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
            except Exception:
                await query.message.reply_text(texto, reply_markup=reply_markup, parse_mode="HTML")


# --- FUNCIÓN PRINCIPAL MAIN ---
def main():
    init_db()
    asegurar_columnas_usuarios()
    asegurar_tabla_kyc()
    
    app = ApplicationBuilder().token(TOKEN).build()

    nyse_tz = pytz.timezone('America/New_York')
    hora_ejecucion = time(hour=17, minute=0, tzinfo=nyse_tz)
    
    app.job_queue.run_daily(
        contabilidad_diaria_job,
        time=hora_ejecucion,
        days=(0, 1, 2, 3, 4) 
    )

    registro_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                iniciar_registro, pattern="^iniciar_registro$"
            )
        ],
        states={
            NOMBRE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_nombre)
            ],
            EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_email)
            ],
            TELEFONO: [
                MessageHandler(
                    filters.CONTACT | (filters.TEXT & ~filters.COMMAND),
                    recibir_telefono,
                )
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar_operacion)],
    )

    registro_wallet_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                iniciar_registro_wallet, pattern="^registrar_wallet$"
            )
        ],
        states={
            WALLET_DIR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_wallet_dir)
            ],
            WALLET_PIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_wallet_pin)
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar_operacion)],
    )

    cambio_wallet_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                iniciar_cambio_wallet, pattern="^cambiar_wallet$"
            )
        ],
        states={
            CAMBIO_WALLET_PIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_cambio_wallet_pin)
            ],
            CAMBIO_WALLET_DIR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_cambio_wallet_dir)
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar_operacion)],
    )

    kyc_wallet_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                iniciar_kyc_wallet, pattern="^iniciar_kyc_wallet$"
            )
        ],
        states={
            KYC_WALLET_DIR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_kyc_wallet_dir)
            ],
            KYC_NOMBRE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_kyc_nombre)
            ],
            KYC_FOTO: [
                MessageHandler(filters.PHOTO & ~filters.COMMAND, recibir_kyc_foto)
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar_operacion)],
    )

    deposito_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                iniciar_deposito_flujo, pattern="^menu_depositar$"
            )
        ],
        states={
            MONTO_DEP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_monto_dep)
            ],
            HASH_DEP: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, recibir_hash_dep
                )
            ],
            COMPROBANTE_DEP: [
                MessageHandler(
                    filters.PHOTO & ~filters.COMMAND, recibir_comprobante_dep
                )
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar_operacion)],
    )

    retiro_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                iniciar_retiro_flujo, pattern="^menu_retirar$"
            )
        ],
        states={
            MONTO_RET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_monto_ret)
            ]
        },
        fallbacks=[CommandHandler("cancelar", cancelar_operacion)],
    )
    
    admin_retiro_flujo_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_retirar_iniciar, pattern="^admin_ret_(ok|no)_")
        ],
        states={
            ADMIN_RET_APROBAR_COMPROBANTE: [
                MessageHandler(filters.PHOTO & ~filters.COMMAND, admin_retirar_recibir_comprobante)
            ],
            ADMIN_RET_OTRO_MOTIVO: [
                CallbackQueryHandler(admin_retirar_motivo_callback, pattern="^admin_motivo_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_retirar_recibir_motivo_manual)
            ]
        },
        fallbacks=[CommandHandler("cancelar", cancelar_operacion)],
    )

    admin_buscar_usuario_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                iniciar_buscar_usuario_admin, pattern="^admin_buscar_usuario$"
            )
        ],
        states={
            BUSCAR_USUARIO_ADMIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_id_usuario_admin)
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar_operacion)],
    )
    
    admin_broadcast_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(iniciar_broadcast_admin, pattern="^admin_broadcast_start$")
        ],
        states={
            ADMIN_BROADCAST: [
                MessageHandler(filters.ALL & ~filters.COMMAND, procesar_broadcast_admin)
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar_operacion)],
    )

    # Añadir los CommandHandlers iniciales
    app.add_handler(CommandHandler("start", start))
    
    # Añadir todos los ConversationHandlers
    app.add_handler(registro_handler)
    app.add_handler(registro_wallet_handler)
    app.add_handler(cambio_wallet_handler)
    app.add_handler(kyc_wallet_handler)
    app.add_handler(deposito_handler)
    app.add_handler(retiro_handler)
    app.add_handler(admin_retiro_flujo_handler)
    app.add_handler(admin_buscar_usuario_handler)
    app.add_handler(admin_broadcast_handler)

    # Añadir CallbackQueryHandlers dedicados y de admin antes del bloqueador global
    app.add_handler(CallbackQueryHandler(admin_kyc_callback, pattern="^admin_kyc_"))
    app.add_handler(CallbackQueryHandler(admin_deposito_iniciar, pattern="^admin_dep_(ok|no)_"))
    app.add_handler(CallbackQueryHandler(ejecutar_contabilidad, pattern="^ejecutar_contabilidad$"))
    app.add_handler(CallbackQueryHandler(exportar_excel, pattern="^exportar_excel$"))
    app.add_handler(CallbackQueryHandler(exportar_conciliacion, pattern="^exportar_conciliacion$"))

    # Finalmente, el CallbackQueryHandler que sirve de atrapalotodo (Catch-All) para el menú
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Bot en ejecución...")
    app.run_polling()

if __name__ == "__main__":
    main()
