import os
import time
import mysql.connector
import logging
from datetime import datetime
import pytz  # Para manejar la zona horaria de Colombia
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By

# --- CONFIGURACIÓN ---
DB_CONFIG = {
    'host': os.getenv('DB_HOST'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASS'),
    'database': os.getenv('DB_NAME'),
    'connection_timeout': 30
}

logging.basicConfig(filename="OK_Check.log", level=logging.INFO, format="%(asctime)s: %(message)s", encoding='utf-8')

# --- FUNCIONES DE APOYO ---

def get_db_connection():
    return mysql.connector.connect(**DB_CONFIG)

def get_curdate_time():
    """Obtiene la fecha y hora actual en el formato de MySQL para Colombia"""
    tz_bogota = pytz.timezone('America/Bogota')
    return datetime.now(tz_bogota).strftime('%Y-%m-%d %H:%M:%S')

def get_config_value(key):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT valor FROM config WHERE variable = %s", (key,))
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    return result['valor'] if result else None

def update_video_status(video_id, estado):
    """Actualiza el estado y la fecha/hora del bloqueo"""
    fecha_colombia = get_curdate_time()
    conn = get_db_connection()
    cursor = conn.cursor()
    # Actualizamos el estado Y la fecha
    sql = "UPDATE videos2024 SET estado = %s, fecha = %s WHERE id = %s"
    cursor.execute(sql, (estado, fecha_colombia, video_id))
    conn.commit()
    cursor.close()
    conn.close()

def update_checkpoint(new_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE config SET valor = %s WHERE variable = 'last_id_check'", (str(new_id),))
    conn.commit()
    cursor.close()
    conn.close()

# --- PROCESO PRINCIPAL ---

# 1. Obtener punto de control (last_id_check)
try:
    raw_id = get_config_value('last_id_check')
    last_id = int(raw_id) if raw_id is not None else 0
except Exception as e:
    print(f"Error de conexión inicial: {e}")
    exit(1)

# Si es 0 (primera vez), empezamos desde el ID más alto
if last_id == 0:
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT MAX(id) as max_id FROM videos2024")
    res = cursor.fetchone()
    last_id = res['max_id'] + 1 if res['max_id'] else 0
    cursor.close()
    conn.close()
    print(f"Iniciando desde el tope de la tabla: ID {last_id}")

# 2. Consultar lote de 100 videos (Descendente)
conn = get_db_connection()
cursor = conn.cursor(dictionary=True)
sql = "SELECT id, idok, titulo FROM videos2024 WHERE id < %s AND estado = '' ORDER BY id DESC LIMIT 200"
cursor.execute(sql, (last_id,))
batch = cursor.fetchall()
cursor.close()
conn.close()

if not batch:
    print("No hay más videos para procesar en orden descendente.")
    exit()

# 3. Configurar Selenium
options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
prefs = {"profile.managed_default_content_settings.images": 2}
options.add_experimental_option("prefs", prefs)
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

# 4. Bucle de procesamiento
for row in batch:
    curr_id = row["id"]
    idok = row["idok"]
    url = f'https://ok.ru/videoembed/{idok}?autoplay=0&quality=lowest'
    
    try:
        driver.get(url)
        time.sleep(3) # Espera técnica para carga de ok.ru
        
        stubs = driver.find_elements(By.CLASS_NAME, 'vp_video_stub_txt')
        if stubs:
            stext_ru = stubs[0].text
            status_map = {
                "Видео заблокировано из-за нарушений авторских прав": "Video bloqueado por copyright",
                "Видео заблокировано по требованию правообладателя": "Video bloqueado por copyright",
                "Видео заблокировано": "Vídeo bloqueado",
                "Автор данного видео не найден или заблокирован": "Autor bloqueado",
                "Видео не найдено": "Vídeo no encontrado"
            }
            estado = status_map.get(stext_ru, stext_ru[:45])
            
            # ACTUALIZACIÓN: Estado + Fecha de Colombia
            update_video_status(curr_id, estado)
            logging.info(f"ID {curr_id} BLOQUEADO: {estado} (Fecha registrada)")
        
        # Guardamos el progreso ID por ID para evitar perder el hilo si hay error
        update_checkpoint(curr_id)
        print(f"Procesado ID: {curr_id} (Fecha: {get_curdate_time()})")

    except Exception as e:
        print(f"Error en ID {curr_id}: {e}")
        break 

driver.quit()
print("Proceso completado exitosamente.")
