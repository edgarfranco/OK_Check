import os
import time
import mysql.connector
import logging
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

# Función para obtener una nueva conexión limpia
def get_db_connection():
    return mysql.connector.connect(**DB_CONFIG)

def get_config_value(key):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT valor FROM config WHERE variable = %s", (key,))
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    return result['valor'] if result else None

def update_video_status(video_id, estado):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE videos2024 SET estado = %s WHERE id = %s", (estado, video_id))
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

# --- INICIO ---

# 1. Obtener punto de control
try:
    raw_id = get_config_value('last_id_check')
    last_id = int(raw_id) if raw_id is not None else 0
except Exception as e:
    print(f"Error al conectar inicialmente: {e}")
    exit(1)

# Si es 0, buscamos el máximo
if last_id == 0:
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT MAX(id) as max_id FROM videos2024")
    res = cursor.fetchone()
    last_id = res['max_id'] + 1 if res['max_id'] else 0
    cursor.close()
    conn.close()
    print(f"Iniciando desde el tope: ID {last_id}")

# 2. Obtener lote de videos y CERRAR conexión inmediatamente
conn = get_db_connection()
cursor = conn.cursor(dictionary=True)
sql = "SELECT id, idok, titulo FROM videos2024 WHERE id < %s AND estado = '' ORDER BY id DESC LIMIT 100"
cursor.execute(sql, (last_id,))
batch = cursor.fetchall()
cursor.close()
conn.close()

if not batch:
    print("No hay videos para procesar.")
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
        time.sleep(3)
        
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
            
            # Actualizamos en el momento (abre y cierra conexión)
            update_video_status(curr_id, estado)
            logging.info(f"ID {curr_id} BLOQUEADO: {estado}")
        
        # ACTUALIZAMOS EL CHECKPOINT VIDEO POR VIDEO
        # Esto es más seguro: si el script falla en el video 50, 
        # la DB ya sabe que llegó al 50.
        update_checkpoint(curr_id)
        print(f"Procesado ID: {curr_id} (Descendiendo)")

    except Exception as e:
        print(f"Error procesando ID {curr_id}: {e}")
        # Si hay un error de red o Selenium, mejor parar y dejar que el cron reintente luego
        break

driver.quit()
print("Proceso finalizado con éxito.")
