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
    'connection_timeout': 300
}

logging.basicConfig(filename="OK_Check.log", level=logging.INFO, format="%(asctime)s: %(message)s", encoding='utf-8')

def connectDB():
    return mysql.connector.connect(**DB_CONFIG)

def get_config_value(cursor, key):
    cursor.execute("SELECT valor FROM config WHERE variable = %s", (key,))
    result = cursor.fetchone()
    return result['valor'] if result else None

def update_config_value(cursor, mydb, key, value):
    cursor.execute("UPDATE config SET valor = %s WHERE variable = %s", (str(value), key))
    mydb.commit()

# --- INICIO ---
mydb = connectDB()
cursor = mydb.cursor(dictionary=True)

# 1. Obtener punto de control
raw_id = get_config_value(cursor, 'last_id_check')
last_id = int(raw_id) if raw_id is not None else 0

# LÓGICA DESCENDENTE: Si last_id es 0, empezamos desde el ID más alto de la tabla
if last_id == 0:
    cursor.execute("SELECT MAX(id) as max_id FROM videos2024")
    res = cursor.fetchone()
    last_id = res['max_id'] + 1 if res['max_id'] else 0
    print(f"Iniciando desde el tope de la tabla: ID {last_id}")

# 2. Consultar los 100 vídeos ANTERIORES al último procesado (ID < last_id)
sql = "SELECT id, idok, titulo FROM videos2024 WHERE id < %s AND estado = '' ORDER BY id DESC LIMIT 100"
cursor.execute(sql, (last_id,))
batch = cursor.fetchall()

if not batch:
    print("No hay vídeos más antiguos para procesar (o llegaste al final).")
    cursor.close()
    mydb.close()
    exit()

# 3. Configurar Selenium
options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
prefs = {"profile.managed_default_content_settings.images": 2}
options.add_experimental_option("prefs", prefs)
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

# IMPORTANTE: En orden descendente, el final_id será el más PEQUEÑO del lote
final_id = last_id 

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
            
            up_cursor = mydb.cursor()
            up_cursor.execute("UPDATE videos2024 SET estado = %s WHERE id = %s", (estado, curr_id))
            mydb.commit()
            up_cursor.close()
            logging.info(f"ID {curr_id} BLOQUEADO: {estado}")
        
        # El progreso ahora baja
        final_id = curr_id
        print(f"Procesado ID: {curr_id} (Descendiendo)")

    except Exception as e:
        logging.error(f"Error en ID {curr_id}: {e}")
        break

# 4. Actualizar el checkpoint con el ID más bajo alcanzado
update_config_value(cursor, mydb, 'last_id_check', final_id)

driver.quit()
cursor.close()
mydb.close()
print(f"Progreso guardado. Siguiente tanda empezará debajo de ID: {final_id}")
