import os
import time
import mysql.connector
import logging
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By

# --- CONFIGURACIÓN DESDE GITHUB SECRETS ---
DB_CONFIG = {
    'host': os.getenv('DB_HOST'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASS'),
    'database': os.getenv('DB_NAME'),
    'connection_timeout': 300
}

# Configuración de Logs
logging.basicConfig(
    filename="OK_Check.log", 
    level=logging.INFO, 
    format="%(asctime)s: %(message)s",
    encoding='utf-8'
)

def connectDB():
    return mysql.connector.connect(**DB_CONFIG)

def get_config_value(cursor, key):
    """Obtiene un valor de la tabla config"""
    cursor.execute("SELECT valor FROM config WHERE variable = %s", (key,))
    result = cursor.fetchone()
    return result['valor'] if result else None

def update_config_value(cursor, mydb, key, value):
    """Actualiza un valor en la tabla config"""
    cursor.execute("UPDATE config SET valor = %s WHERE variable = %s", (str(value), key))
    mydb.commit()

# --- INICIO DEL PROCESO ---
try:
    mydb = connectDB()
    cursor = mydb.cursor(dictionary=True)

    # 1. Obtener el punto de control usando el nuevo nombre 'last_id_check'
    raw_id = get_config_value(cursor, 'last_id_check')
    last_id = int(raw_id) if raw_id is not None else 0
    print(f">>> Iniciando desde ID: {last_id}")

    # 2. Consultar lote de videos (puedes subir el LIMIT si quieres procesar más)
    sql = "SELECT id, idok, titulo FROM videos2024 WHERE id > %s AND estado = '' ORDER BY id ASC LIMIT 100"
    cursor.execute(sql, (last_id,))
    batch = cursor.fetchall()

    if not batch:
        print(">>> No hay videos nuevos para procesar.")
        cursor.close()
        mydb.close()
        exit()

    # 3. Configuración de Selenium Headless
    options = Options()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    # Optimización: No cargar imágenes
    prefs = {"profile.managed_default_content_settings.images": 2}
    options.add_experimental_option("prefs", prefs)

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    last_successfully_processed_id = last_id

    # 4. Bucle de verificación
    for row in batch:
        current_id = row["id"]
        idok = row["idok"]
        url = f'https://ok.ru/videoembed/{idok}?autoplay=0&quality=lowest'
        
        try:
            driver.get(url)
            time.sleep(3) # Espera para que cargue el DOM
            
            # Buscar el elemento de video bloqueado/eliminado
            stubs = driver.find_elements(By.CLASS_NAME, 'vp_video_stub_txt')
            
            if stubs:
                stext_ru = stubs[0].text
                # Diccionario de traducción de estados comunes de OK.ru
                status_map = {
                    "Видео заблокировано из-за нарушений авторских прав": "Video bloqueado por copyright",
                    "Видео заблокировано по требованию правообладателя": "Video bloqueado por copyright",
                    "Видео заблокировано": "Vídeo bloqueado",
                    "Автор данного видео не найден или заблокирован": "Autor bloqueado",
                    "Видео не найдено": "Vídeo no encontrado",
                    "Видео не прошло модерацию": "Video sin moderación",
                    "Доступ к этому видео ограничен": "Acceso restringido"
                }
                
                estado = status_map.get(stext_ru, stext_ru[:45]) # Truncar si es un error desconocido
                
                # Actualizar la tabla de videos
                up_cursor = mydb.cursor()
                up_cursor.execute("UPDATE videos2024 SET estado = %s WHERE id = %s", (estado, current_id))
                mydb.commit()
                up_cursor.close()
                
                logging.info(f"ID {current_id} BLOQUEADO: {estado}")
                print(f"ID {current_id}: {estado}")
            else:
                # El video está bien
                print(f"ID {current_id}: OK")

            # Actualizamos nuestro marcador de progreso
            last_successfully_processed_id = current_id

        except Exception as e:
            logging.error(f"Error procesando ID {current_id}: {e}")
            # Si hay un error crítico (timeout de red, etc.), guardamos hasta donde llegamos y salimos
            break

    # 5. Guardar el nuevo checkpoint en la tabla config
    update_config_value(cursor, mydb, 'last_id_check', last_successfully_processed_id)
    print(f">>> Progreso guardado. last_id_check actualizado a: {last_successfully_processed_id}")

    driver.quit()
    cursor.close()
    mydb.close()

except Exception as e:
    print(f"CRITICAL ERROR: {e}")
    if 'logging' in locals():
        logging.critical(f"Error crítico en el script: {e}")
