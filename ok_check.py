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
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# --- CONFIGURACIÓN ---
DB_CONFIG = {
    'host': os.getenv('DB_HOST'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASS'),
    'database': os.getenv('DB_NAME'),
    'connection_timeout': 30
}

# Tamaño del lote de actualización (Cada cuántos videos escribimos en la DB)
BATCH_SIZE = 10 

logging.basicConfig(filename="OK_Check.log", level=logging.INFO, format="%(asctime)s: %(message)s", encoding='utf-8')

def get_db_connection():
    return mysql.connector.connect(**DB_CONFIG)

def get_curdate_time():
    return datetime.now(pytz.timezone('America/Bogota')).strftime('%Y-%m-%d %H:%M:%S')

def get_config_value(key):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT valor FROM config WHERE variable = %s", (key,))
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    return result['valor'] if result else None

def commit_batch(results_list, last_id):
    """Envía todos los resultados acumulados a la DB en una sola conexión"""
    if not results_list:
        return
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. Actualización masiva de videos usando executemany
        sql_videos = "UPDATE videos2024 SET estado = %s, fecha = %s WHERE id = %s"
        cursor.executemany(sql_videos, results_list)
        
        # 2. Actualización del checkpoint
        sql_checkpoint = "UPDATE config SET valor = %s WHERE variable = 'last_id_check'"
        cursor.execute(sql_checkpoint, (str(last_id),))
        
        conn.commit()
        cursor.close()
        conn.close()
        print(f"--- DB Sincronizada: {len(results_list)} registros actualizados. Checkpoint: {last_id} ---")
    except Exception as e:
        print(f"Error al sincronizar con la DB: {e}")

# --- PROCESO PRINCIPAL ---

raw_id = get_config_value('last_id_check')
last_id = int(raw_id) if raw_id is not None else 0
limit_val = int(get_config_value('limit_check') or 100)

# Obtener lote de trabajo
conn = get_db_connection()
cursor = conn.cursor(dictionary=True)
sql = "SELECT id, idok, titulo FROM videos2024 WHERE id < %s AND estado = '' ORDER BY id DESC LIMIT %s"
cursor.execute(sql, (last_id, limit_val))
batch_to_process = cursor.fetchall()
cursor.close()
conn.close()

if not batch_to_process:
    print("Nada que procesar.")
    exit()

# Selenium Setup
options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
prefs = {"profile.managed_default_content_settings.images": 2}
options.add_experimental_option("prefs", prefs)
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

# --- BUCLE OPTIMIZADO ---
results_buffer = []  # Aquí guardamos temporalmente (estado, fecha, id)
processed_count = 0

for row in batch_to_process:
    curr_id = row["id"]
    idok = row["idok"]
    url = f'https://ok.ru/videoembed/{idok}?autoplay=0&quality=lowest'
    
    status_db = ""
    status_log = "OK"
    
    try:
        driver.get(url)

        # ESPERA DE CARRERA: Espera hasta x segundos a que aparezca 
        # el mensaje de error O el título del video.
        try:
            # El selector CSS coma (,) funciona como un "OR"
            elemento_detectado = WebDriverWait(driver, 2).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".vp_video_stub_txt, .vid-card_n"))
            )
            
            # Verificamos qué clase tiene el elemento que ganó la carrera
            clase_encontrada = elemento_detectado.get_attribute("class")
            
            if "vp_video_stub_txt" in clase_encontrada:
                # El video está BLOQUEADO
                stext_ru = elemento_detectado.text
                status_map = {
                    "Видео заблокировано из-за нарушений авторских прав": "Bloqueado: Copyright",
                    "Видео заблокировано": "Bloqueado: General",
                    "Видео не найдено": "Eliminado: No encontrado",
                    "Автор данного видео не найден или заблокирован": "Bloqueado: Autor"
                }
                status_db = status_map.get(stext_ru, f"{stext_ru[:20]}")
                status_log = f"DEAD ({status_db})"
            else:
                # El video está ALIVE (se encontró .vid-card_n)
                status_db = ""
                status_log = "OK"
                
        except:
            # Si en 3 segundos no aparece ninguno, podría ser un error de carga 
            # o un tercer estado. Lo marcamos como ALIVE por defecto o Error de carga.
            status_db = ""
            status_log = "Timeout"
        
        # Guardar en el buffer (siempre, para actualizar la fecha)
        results_buffer.append((status_db, get_curdate_time(), curr_id))
        processed_count += 1
        
        print(f"ID: {curr_id} | {get_curdate_time()} | Status: {status_log} ")

        # ¿Es momento de sincronizar con la DB?
        if len(results_buffer) >= BATCH_SIZE:
            commit_batch(results_buffer, curr_id)
            results_buffer = [] # Limpiar buffer después de commit

    except Exception as e:
        print(f"Fallo en ID {curr_id}: {e}")
        break

# Sincronización final (para los videos restantes que no completaron un lote de 10)
if results_buffer:
    commit_batch(results_buffer, results_buffer[-1][2])

driver.quit()
print(f"--- FIN: {processed_count} videos revisados ---")
