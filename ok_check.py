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
from dotenv import load_dotenv

# Cargar variables desde el archivo .env si existe
load_dotenv()

# --- CONFIGURACIÓN ---
DB_CONFIG = {
    'host': os.getenv('DB_HOST'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASS'),
    'database': os.getenv('DB_NAME'),
    'connection_timeout': 60
}

BATCH_SIZE = 20 # Sincronizamos con la DB cada 20 videos procesados

logging.basicConfig(filename="OK_Check.log", level=logging.INFO, format="%(asctime)s: %(message)s", encoding='utf-8', filemode = "w")

def get_db_connection(retries=3, delay=5):
    """Intenta conectar a la DB con un sistema de reintentos"""
    for i in range(retries):
        try:
            conn = mysql.connector.connect(**DB_CONFIG)
            
            msg = f"✅ Conexión exitosa a la DB (Intento {i+1}/{retries})"
            print(msg)
            logging.info(msg) 
            return conn
        except mysql.connector.Error as err:
            if i < retries - 1: # Si no es el último intento
                print(f"⚠️ Intento {i+1} fallido (Error: {err}). Reintentando en {delay}s...")
                logging.error(f"⚠️ Intento {i+1} fallido (Error: {err}). Reintentando en {delay}s...")
                time.sleep(delay)
            else:
                logging.error(f"❌ Error definitivo tras {retries} intentos: {err}")
                raise err # Lanza el error si ya agotó los reintentos

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

def commit_batch(results_list, last_id_to_save):
    """
    Sincronización selectiva:
    - Actualiza videos2024 SOLO si el estado no es vacío.
    - Actualiza config SIEMPRE para avanzar el puntero.
    """
    if not results_list:
        return
    
    # Filtramos: Solo registros que tengan un estado detectado (Bloqueados)
    bloqueados = [r for r in results_list if r[0] != ""]
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. ACTUALIZACIÓN SELECTIVA: Solo si hay bloqueos en esta tanda
        if bloqueados:
            sql_videos = "UPDATE videos2024 SET estado = %s, fecha = %s WHERE id = %s"
            cursor.executemany(sql_videos, bloqueados)
            print(f"--- DB: {len(bloqueados)} bloqueos registrados en esta tanda. ---")
        
        # 2. ACTUALIZACIÓN OBLIGATORIA: El checkpoint debe avanzar siempre
        sql_checkpoint = "UPDATE config SET valor = %s WHERE variable = 'last_id_check'"
        cursor.execute(sql_checkpoint, (str(last_id_to_save),))
        
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Error al sincronizar con la DB: {e}")

# --- INICIO ---
raw_id = get_config_value('last_id_check')
last_id = int(raw_id) if raw_id is not None else 0
limit_val = int(get_config_value('limit_check') or 1000)

if last_id == 0:
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    # Buscamos el ID más alto de la tabla
    cursor.execute("SELECT MAX(id) as max_id FROM videos2024")
    res = cursor.fetchone()
    cursor.close()
    conn.close()
    # Le sumamos 1 para que el primer lote (id < last_id) incluya al video más nuevo
    last_id = (res['max_id'] + 1) if res['max_id'] else 0
    print(f"--- INICIANDO DESDE EL TOPE DE LA TABLA: ID {last_id} ---")

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

options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
prefs = {"profile.managed_default_content_settings.images": 2}
options.add_experimental_option("prefs", prefs)
options.page_load_strategy = 'eager'
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

# Buffer de memoria
results_buffer = []
processed_count = 0

for row in batch_to_process:
    curr_id = row["id"]
    idok = row["idok"]
    titulo = row["titulo"]
    url = f'https://ok.ru/videoembed/{idok}?autoplay=0&quality=lowest'
    
    status_db = ""
    status_log = "ALIVE"
    
    try:
        driver.get(url)
        
        # ESPERA DE CARRERA (Race Condition)
        try:
            # Esperamos a que aparezca el error O el título del video (vid-card_n)
            elemento = WebDriverWait(driver, 3).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".vp_video_stub_txt, .vid-card_n"))
            )
            
            clase = elemento.get_attribute("class")
            
            if "vp_video_stub_txt" in clase:
                stext_ru = elemento.text
                status_map = {
                    "Видео заблокировано из-за нарушений авторских прав": "Bloqueado: Copyright",
                    "Видео заблокировано": "Bloqueado: General",
                    "The video is blocked": "Bloqueado: General",
                    "This video is not available in your region": "Bloqueado: Region",
                    "Видео не найдено": "Eliminado: No encontrado",
                    "Video has not been found": "Eliminado: No encontrado",
                    "Автор данного видео не найден или заблокирован": "Bloqueado: Autor",
                    "Video has been blocked due to author's rights infingement": "Bloqueado: Autor",
                    "Во время обработки видео произошла ошибка.": "Bloqueado: Error procesamiento"
                }
                status_db = status_map.get(stext_ru, f"{stext_ru[:100]}")
                status_log = f"BLOQUEADO ({status_db}) | {titulo[:30]}"
            else:
                # Se detectó .vid-card_n -> El video está bien
                status_db = ""
                status_log = "OK"
        except:
            # Si nada aparece en 3s, lo consideramos ALIVE para no marcar errores falsos
            status_db = ""
            status_log = "Timeout"
        
        # Agregamos al buffer (se usará para filtrar después)
        results_buffer.append((status_db, get_curdate_time(), curr_id))
        processed_count += 1
        
        print(f"ID: {curr_id} | {get_curdate_time()}  | {status_log}")
        logging.info(f"ID: {curr_id} | {get_curdate_time()}  | {status_log}")

        if len(results_buffer) >= BATCH_SIZE:
            commit_batch(results_buffer, curr_id)
            results_buffer = []

    except Exception as e:
        print(f"Error en ID {curr_id}: {e}")
        break

# Sincronización final
if results_buffer:
    commit_batch(results_buffer, results_buffer[-1][2])

driver.quit()
print(f"--- FIN: {processed_count} videos revisados ---")
