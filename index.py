#!/bin/python3

from datetime import datetime

import sqlite3
import time
import logging

import paho.mqtt.client as mqtt
import yaml
import sys
import json
import requests

mqtt_client = None
config = None
first_message = True
_LOGGER = None

VERSION = '1.3.4'

CONFIG_PATH = './config/config.yml'
DB_PATH = './config/frigate_plate_recogizer.db'
LOG_FILE = './config/frigate_plate_recogizer.log'

PLATE_RECOGIZER_BASE_URL = 'https://api.platerecognizer.com/v1/plate-reader'
CODE_PROJECT_AI_BASE_URL = 'http://127.0.0.1:32168'
valid_objects = ['car', 'motorcycle', 'bus']


# you will likely need to create multiple iterations for each vehicle
# 0/8/B for example are often mixed up
known_plates = {
    # Bob's Car
    "ABC128": "Bob's Car",
    "ABC12B": "Bob's Car",
    
    # Steve's Truck
    "123TR0": "Steve's Truck",
    "123TRO": "Steve's Truck",
}


def on_connect(mqtt_client, userdata, flags, rc):
    _LOGGER.info("MQTT Connected")
    mqtt_client.subscribe(config['frigate']['main_topic'] + "/events")


def on_disconnect(mqtt_client, userdata, rc):
    if rc != 0:
        _LOGGER.warning("Unexpected disconnection, trying to reconnect")
        while True:
            try:
                mqtt_client.reconnect()
                break
            except Exception as e:
                _LOGGER.warning(f"Reconnection failed due to {e}, retrying in 60 seconds")
                time.sleep(60)
    else:
        _LOGGER.error("Expected disconnection")


def set_sublabel(frigate_url, frigate_event, sublabel):
    post_url = f"{frigate_url}/api/events/{frigate_event}/sub_label"
    _LOGGER.debug(f'sublabel: {sublabel}')
    _LOGGER.debug(f'sublabel url: {post_url}')

    # frigate limits sublabels to 20 characters currently
    if len(sublabel) > 20:
        sublabel = sublabel[:20]

    # Submit the POST request with the JSON payload
    payload = { "subLabel": sublabel }
    headers = { "Content-Type": "application/json" }
    response = requests.post(post_url, data=json.dumps(payload), headers=headers)

    # Check for a successful response
    if response.status_code == 200:
        _LOGGER.info(f"Sublabel set successfully to: {sublabel}")
    else:
        _LOGGER.error(f"Failed to set sublabel. Status code: {response.status_code}")


def plate_recognizer(image):
    pr_url = config['plate_recognizer'].get('api_url') or PLATE_RECOGIZER_BASE_URL
    token = config['plate_recognizer']['token']

    response = requests.post(
        pr_url,
        data=dict(regions=config['plate_recognizer']['regions']),
        files=dict(upload=image),
        headers={'Authorization': f'Token {token}'}
    )

    response = response.json()
    _LOGGER.debug(f"response: {response}")

    if response.get('results') is None:
        _LOGGER.error(f"Failed to get plate number. Response: {response}")
        return None, None
    
    if len(response['results']) == 0:
        return None, None

    plate_number = response['results'][0].get('plate')
    score = response['results'][0].get('score')

    return plate_number, score

def code_project_ai_recognize(image):
    ai_url = config['code_proejct_ai'].get('api_url') or CODE_PROJECT_AI_BASE_URL
    
    response = requests.post(
        ai_url,
        files=dict(upload=image)
    )

    _LOGGER.debug(f"response: {response}")
    plates = response.json()
    plate = None

    if len(plates["predictions"]) > 0 and plates["predictions"][0].get("plate"):
        plate = str(plates["predictions"][0]["plate"]).replace(" ", "")
        score = plates["predictions"][0]["confidence"]
        _LOGGER.debug(f"Checking plate: {plate} in {known_plates.keys()}")
        _LOGGER.debug(f"[{datetime.datetime.now()}]: {camera} - detected {plate} as {known_plates.get(plate)} with a score of {score}\n")

        if plate in known_plates.keys():
            _LOGGER.debug(f"{camera} - Found a known plate: {known_plates[plate]}")
            return plate, score
        else:
            return plate, score
    else:
        _LOGGER.debug(f"[{datetime.datetime.now()}]: {camera} - No plates detected in run: {plates}\n")

    if plate is None:
        print(f"No valid results found: {plates['predictions']}")
        return None, None

def send_mqtt_message(message):
    _LOGGER.debug(f"Sending MQTT message: {message}")

    main_topic = config['frigate']['main_topic']
    return_topic = config['frigate']['return_topic']
    topic = f'{main_topic}/{return_topic}'

    mqtt_client.publish(topic, json.dumps(message))

def on_message(client, userdata, message):
    global first_message
    if first_message:
        first_message = False
        _LOGGER.debug("skipping first message")
        return

    # get frigate event payload
    payload_dict = json.loads(message.payload)
    _LOGGER.debug(f'mqtt message: {payload_dict}')
    after_data = payload_dict.get('after', {})
    before_data = payload_dict.get('before', {})

    if not after_data['camera'] in config['frigate']['camera']:
        _LOGGER.debug(f"Skipping event: {after_data['id']} because it is from the wrong camera: {after_data['camera']}")
        return

    if config['frigate']['zones']:
        entered_zones = set(after_data['entered_zones'])
        allowed_zones = set(config['frigate'].get('zones'))
        if not entered_zones.intersection(allowed_zones):
            _LOGGER.debug(f"Skipping event: {after_data['id']} because it is from the wrong zones: {after_data['entered_zones']}")
            return

    # check if it is a valid object like a car, motorcycle, or bus
    if(after_data['label'] not in valid_objects):
        _LOGGER.debug(f"is not a correct label: {after_data['label']}")
        return

    # check if Frigate has updated the snapshot
    if(before_data['top_score'] == after_data['top_score']):
        _LOGGER.debug(f"duplicated snapshot from Frigate as top_score from before and after are the same: {after_data['top_score']}")
        return

    # get frigate event
    frigate_event = after_data['id']
    frigate_url = config['frigate']['frigate_url']

    # see if we have already processed this event
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM plates WHERE frigate_event = ?
    """, (frigate_event,))
    row = cursor.fetchone()
    conn.close()

    if row is not None:
        _LOGGER.debug(f"Skipping event: {frigate_event} because it has already been processed")
        return

    snapshot_url = f"{frigate_url}/api/events/{frigate_event}/snapshot.jpg"
    _LOGGER.debug(f"Getting image for event: {frigate_event}" )
    _LOGGER.debug(f"event URL: {snapshot_url}")

    response = requests.get(snapshot_url, params={ "crop": 1, "quality": 95 })

    # Check if the request was successful (HTTP status code 200)
    if response.status_code != 200:
        _LOGGER.error(f"Error getting snapshot: {response.status_code}")
        return

    # try to get plate number
    plate_number = None
    score = None
    if config.get('plate_recognizer'):
        plate_number, score = plate_recognizer(response.content)
    elif config.get('code_proejct_ai'):
        plate_number, score = code_project_ai_recognize(response.content)
    else:
        _LOGGER.error("Plate Recognizer is not configured. You must configure either code_proejct_ai or plate_recongizer")
        return

    if plate_number is None:
        _LOGGER.info(f'No plate number found for event {frigate_event}')
        return

    min_score = config['frigate'].get('min_score')
    if min_score and score < min_score:
        _LOGGER.info(f"Score is below minimum: {score}")
        return

    start_time = datetime.fromtimestamp(after_data['start_time'])
    formatted_start_time = start_time.strftime("%Y-%m-%d %H:%M:%S")

    # get db connection
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Insert a new record of plate number
    _LOGGER.info(f"Storing plate number in database: {plate_number}")
    cursor.execute("""
        INSERT INTO plates (detection_time, score, plate_number, frigate_event, camera_name) VALUES (?, ?, ?, ?, ?)
    """, (formatted_start_time, score, plate_number, frigate_event, after_data['camera']))
    conn.commit()
    conn.close()

    # set the sublabel
    set_sublabel(frigate_url, frigate_event, plate_number)

    # send mqtt message
    if config['frigate'].get('return_topic'):
        send_mqtt_message({
            'plate_number': plate_number,
            'score': score,
            'frigate_event': frigate_event,
            'camera_name': after_data['camera'],
            'start_time': formatted_start_time
        })


def setup_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""    
        CREATE TABLE IF NOT EXISTS plates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detection_time TIMESTAMP NOT NULL,
            score TEXT NOT NULL,
            plate_number TEXT NOT NULL,
            frigate_event TEXT NOT NULL UNIQUE,
            camera_name TEXT NOT NULL
        )    
    """)
    conn.commit()
    conn.close()


def load_config():
    global config
    with open(CONFIG_PATH, 'r') as config_file:
        config = yaml.safe_load(config_file)


def run_mqtt_client():
    global mqtt_client
    _LOGGER.info(f"Starting MQTT client. Connecting to: {config['frigate']['mqtt_server']}")
    now = datetime.now()
    current_time = now.strftime("%Y%m%d%H%M%S")

    # setup mqtt client
    mqtt_client = mqtt.Client("FrigatePlateRecognizer" + current_time)
    mqtt_client.on_message = on_message
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_connect = on_connect

    # check if we are using authentication and set username/password if so
    if config['frigate']['mqtt_auth']:
        username = config['frigate']['mqtt_username']
        password = config['frigate']['mqtt_password']
        mqtt_client.username_pw_set(username, password)

    mqtt_client.connect(config['frigate']['mqtt_server'])
    mqtt_client.loop_forever()


def load_logger():
    global _LOGGER
    _LOGGER = logging.getLogger(__name__)
    _LOGGER.setLevel(config['logger_level'])

    # Create a formatter to customize the log message format
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # Create a console handler and set the level to display all messages
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)

    # Create a file handler to log messages to a file
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # Add the handlers to the logger
    _LOGGER.addHandler(console_handler)
    _LOGGER.addHandler(file_handler)
    

def main():
    load_config()
    setup_db()
    load_logger()

    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    _LOGGER.info(f"Time: {current_time}")
    _LOGGER.info(f"Python Version: {sys.version}")
    _LOGGER.info(f"Frigate Plate Recognizer Version: {VERSION}")
    _LOGGER.debug(f"config: {config}")

    run_mqtt_client()


if __name__ == '__main__':
    main()
