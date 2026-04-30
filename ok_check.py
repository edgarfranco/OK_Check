import os
import time
import mysql.connector
import logging
from datetime import datetime
import pytz
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
    """Hora actual de Colombia"""
    tz_bogota = pytz.timezone('America/Bogota')
    return datetime.now(tz_bogota).strftime('%Y-%m-%d %H:%M:%S')

def get_config_value(key):
    """Obtiene un valor de la tabla config"""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT valor FROM config WHERE variable = %s", (key,))
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    return result['valor'] if result else None

def update_video_status(video_id, estado):
    """Actualiza estado y fecha de Colombia en la base de datos"""
    fecha_colombia = get_curdate_time()
    conn = get_db_connection()
    cursor = conn.cursor()
    sql = "UPDATE videos2024 SET estado = %s, fecha = %s WHERE id = %s"
    cursor.execute(sql, (estado, fecha_colombia, video_id))
    conn.commit()
    cursor.close()
    conn.close()

def update_checkpoint(new_id):
    """Actualiza el last_id_check en la tabla config"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE config SET valor = %s WHERE variable = 'last_id_check'", (str(new_id),))
    conn.commit()
    cursor.close()
    conn.close()

# --- PROCESO PRINCIPAL ---

try:
    # 1. Obtener parámetros de la tabla 'config'
    raw_id = get_config_value('last_id_check')
    last_id = int(raw_id) if raw_id is not None else 0
    
    raw_limit = get_config_value('limit_check')
    limit_val = int(raw_limit) if raw_limit is not None else 100 # Default a 100 si no existe
    
    print(f"--- Lote configurado: {limit_val} videos ---")
except Exception as e:
    print(f"Error al obtener configuración inicial: {e}")
    exit(1)

# Lógica para empezar desde arriba si es la primera vez
if last_id == 0:
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT MAX(id) as max_id FROM videos2024")
    res = cursor.fetchone()
    last_id = res['max_id'] + 1 if res['max_id'] else 0
    cursor.close()
    conn.close()
    print(f"--- INICIANDO DESDE EL TOPE: ID {last_id} ---")

# 2. Consultar videos usando el LIMIT parametrizado
conn = get_db_connection()
cursor = conn.cursor(dictionary=True)
# Usamos el valor de limit_val obtenido de la DB
sql = "SELECT id, idok, titulo FROM videos2024 WHERE id < %s AND estado = '' ORDER BY id DESC LIMIT %s"
cursor.execute(sql, (last_id, limit_val))
batch = cursor.fetchall()
cursor.close()
conn.close()

if not batch:
    print("No hay más videos para procesar.")
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
    titulo = row["titulo"][:35]
    url = f'https://ok.ru/videoembed/{idok}?autoplay=0&quality=lowest'
    
    status_db = ""        
    status_log = "ALIVE"  
    
    try:
        driver.get(url)
        time.sleep(3)
        
        stubs = driver.find_elements(By.CLASS_NAME, 'vp_video_stub_txt')
        if stubs:
            stext_ru = stubs[0].text
            status_map = {
                "Видео заблокировано из-за нарушений авторских прав": "Bloqueado: Copyright",
                "Видео заблокировано по требованию правообладателя": "Bloqueado: Copyright",
                "Видео заблокировано": "Bloqueado: General",
                "Автор данного видео не найден или заблокирован": "Bloqueado: Autor",
                "Видео не найдено": "Eliminado: No encontrado"
            }
            status_db = status_map.get(stext_ru, f"Error: {stext_ru[:20]}")
            status_log = f"DEAD ({status_db})"
            logging.info(f"ID {curr_id} -> {status_db}")
        
        # ACTUALIZACIÓN EN DB: Siempre actualizamos estado (vacío o causa) y FECHA de Colombia
        update_video_status(curr_id, status_db)
        
        # Actualizar el punto de control
        update_checkpoint(curr_id)
        
        # Print detallado para los logs de GitHub
        print(f"ID: {curr_id} | Status: {status_log} | Title: {titulo}... | Time: {get_curdate_time()}")

    except Exception as e:
        print(f"Error procesando ID {curr_id}: {str(e)[:50]}")
        break 

driver.quit()
print(f"--- TANDA DE {len(batch)} VIDEOS FINALIZADA ---")
