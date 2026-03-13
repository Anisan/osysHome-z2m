""" Zigbee2Mqtt """
import time
import datetime
import json
import re
import queue
import threading
from flask import redirect, render_template, request, jsonify
from sqlalchemy import delete, update, or_
import paho.mqtt.client as mqtt
from app.database import session_scope, row2dict, db, get_now_to_utc
from app.extensions import cache
from app.authentication.handlers import handle_admin_required
from app.core.main.BasePlugin import BasePlugin
from plugins.z2m.models.z2m import ZigbeeDevices, ZigbeeProperties
from app.core.lib.object import callMethodThread, updatePropertyThread, setPropertyThread, setLinkToObject, removeLinkFromObject, getProperty
from app.core.lib.common import addNotify, CategoryNotify
from plugins.z2m.forms.SettingForms import SettingsForm

class z2m(BasePlugin):

    def __init__(self,app):
        super().__init__(app,__name__)
        self.title = "Zigbee2mqtt"
        self.version = 1
        self.description = """This is a test plugin"""
        self.category = "Devices"
        self.actions = ['cycle','search', "widget"]
        
        # MQTT message queue and worker thread
        queue_max_size = self.config.get("queue_max_size", 1000)
        self._msg_queue = queue.Queue(maxsize=queue_max_size)
        self._worker_thread = None
        self._worker_stop_event = threading.Event()
        self._last_worker_status_ts = 0

    def _is_connection_configured(self):
        """Проверка наличия обязательных параметров для подключения к MQTT"""
        host = (self.config.get("host") or "").strip()
        topic = (self.config.get("topic") or "").strip()
        return bool(host and topic)

    def _disconnect_mqtt(self):
        """Отключение от брокера MQTT"""
        if getattr(self, "_mqtt_started", False) and getattr(self, "_client", None):
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as e:
                self.logger.warning("MQTT disconnect error: %s", e)
            self._mqtt_started = False
            self.logger.info("MQTT disconnected")
        
        # Очистить очередь при дисконнекте
        try:
            while not self._msg_queue.empty():
                self._msg_queue.get_nowait()
                self._msg_queue.task_done()
        except Exception:
            pass

    def _send_connection_status(self, connected: bool, configured: bool = True):
        """Отправка статуса подключения через WebSocket"""
        self.sendDataToWebsocket("connectionStatus", {
            "connected": connected,
            "configured": configured,
        })

    def _connect_mqtt(self):
        """Подключение к брокеру MQTT с текущими настройками"""
        self._disconnect_mqtt()
        if not self._is_connection_configured():
            self.logger.info("MQTT: параметры подключения не заданы (host, topic)")
            self._send_connection_status(False, False)
            return
        try:
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
            self._client.on_connect = self.on_connect
            self._client.on_disconnect = self.on_disconnect
            self._client.on_message = self.on_message
            host = self.config.get("host", "").strip()
            port = int(self.config.get("port", 1883))
            login = (self.config.get("login") or "").strip()
            password = (self.config.get("password") or "").strip()
            if login and password:
                self._client.username_pw_set(login, password)
            # keepalive увеличен до 180 секунд, чтобы снизить риск разрыва соединения при пиках нагрузки
            self._client.connect(host, port, 180)
            self._client.loop_start()
            self._mqtt_started = True
            self.logger.info("MQTT: подключение к %s:%s", host, port)
        except Exception as e:
            self.logger.error("MQTT connect error: %s", e)
            addNotify("MQTT connect error", str(e), CategoryNotify.Error, self.name)
            self._send_connection_status(False, True)

    def initialization(self):
        self._client = None
        self._mqtt_started = False
        self._start_worker()
        self._connect_mqtt()
    
    def _start_worker(self):
        """Запуск воркер-потока для обработки MQTT-сообщений"""
        if self._worker_thread and self._worker_thread.is_alive():
            self.logger.debug("Worker thread already running")
            return
        
        self._worker_stop_event.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="z2m-worker")
        self._worker_thread.start()
        self.logger.info("Worker thread started")
        self._notify_worker_status(force=True)
    
    def _worker_loop(self):
        """Основной цикл воркера для обработки очереди MQTT-сообщений"""
        self.logger.info("Worker loop started")
        while not self._worker_stop_event.is_set():
            try:
                job = self._msg_queue.get(timeout=0.5)
                if job is None:
                    continue
                
                try:
                    self.processMessage(job['topic'], job['did'], job['payload'], job['from_hub'])
                except Exception as ex:
                    self.logger.error("Error processing message: %s", ex, exc_info=True)
                finally:
                    self._msg_queue.task_done()
                    self._notify_worker_status()
                    
            except queue.Empty:
                continue
            except Exception as ex:
                self.logger.error("Worker loop error: %s", ex, exc_info=True)
        
        self.logger.info("Worker loop stopped")
    
    def _stop_worker(self):
        """Остановка воркер-потока"""
        if not self._worker_thread or not self._worker_thread.is_alive():
            return
        
        self.logger.info("Stopping worker thread...")
        self._worker_stop_event.set()
        
        try:
            self._worker_thread.join(timeout=5.0)
            if self._worker_thread.is_alive():
                self.logger.warning("Worker thread did not stop gracefully")
            else:
                self.logger.info("Worker thread stopped")
        except Exception as ex:
            self.logger.error("Error stopping worker: %s", ex)
        finally:
            self._notify_worker_status(force=True)

    def _notify_worker_status(self, force: bool = False):
        """Отправка статуса воркера через WebSocket"""
        try:
            now = time.time()
            if not force:
                # Ограничиваем частоту отправки до 1 раза в секунду
                if now - getattr(self, "_last_worker_status_ts", 0) < 1.0:
                    return
            self._last_worker_status_ts = now

            status = {
                "running": self._worker_thread is not None and self._worker_thread.is_alive(),
                "queue_size": self._msg_queue.qsize() if hasattr(self, "_msg_queue") else 0,
                "queue_max": self._msg_queue.maxsize if hasattr(self, "_msg_queue") else 0,
            }
            self.sendDataToWebsocket("workerStatus", status)
        except Exception as ex:
            # Не мешаем основному потоку работы при ошибке отправки статуса
            self.logger.debug("Worker status notify error: %s", ex)

    def admin(self, request):
        op = request.args.get('op', '')
        id = request.args.get('id', '')

        if op == 'edit':
            return render_template("z2m_device.html", id=id)

        if op == 'delete':
            sql = delete(ZigbeeProperties).where(ZigbeeProperties.device_id == id)
            db.session.execute(sql)
            sql = delete(ZigbeeDevices).where(ZigbeeDevices.id == id)
            db.session.execute(sql)
            db.session.commit()
            return redirect(self.name)

        settings = SettingsForm()
        if request.method == 'GET':
            settings.host.data = self.config.get('host','')
            settings.port.data = self.config.get('port',1883)
            settings.topic.data = self.config.get('topic','')
            settings.login.data = self.config.get('login','')
            settings.password.data = self.config.get('password','')
        else:
            if settings.validate_on_submit():
                self.config["host"] = settings.host.data
                self.config["port"] = settings.port.data
                self.config["topic"] = settings.topic.data
                self.config["login"] = settings.login.data
                self.config["password"] = settings.password.data
                self.saveConfig()
                self._connect_mqtt()
        devs = ZigbeeDevices.query.order_by(ZigbeeDevices.title).all()
        devices = []
        props = ZigbeeProperties.query.order_by(ZigbeeProperties.title).all()

        for device in devs:
            vdev = row2dict(device)
            if device.is_battery:
                if device.battery_level < 30:
                    vdev['battery_warn'] = 'text-danger'
                elif device.battery_level < 360:
                    vdev['battery_warn'] = 'text-warning'
                else:
                    vdev['battery_warn'] = 'text-success'

            # availability: из кэша (MQTT), ZigbeeProperties не хранит value
            prop_cache = cache.get(f"z2m:prop_{device.id}_availability") or {}
            vdev["availability"] = prop_cache.get("value")

            # Данные по связанным свойствам: мета из БД, значения из кэша или из ObjectManager
            linked = []
            for p in props:
                if p.device_id == device.id and p.linked_object:
                    item = row2dict(p)
                    runtime = cache.get(f"z2m:prop_{device.id}_{p.title}") or {}
                    value = runtime.get('value')
                    converted = runtime.get('converted')
                    updated = runtime.get('updated')
                    # Если по MQTT ещё не было данных, пробуем взять текущее значение объекта
                    if value is None and p.linked_object and p.linked_property:
                        try:
                            obj_name = f"{p.linked_object}.{p.linked_property}"
                            value = getProperty(obj_name, 'value')
                        except Exception:
                            value = None
                    item['value'] = value
                    item['converted'] = converted
                    item['updated'] = updated
                    linked.append(item)
            vdev["data"] = linked

            # updated теперь живёт только в памяти/через WebSocket
            vdev['updated'] = cache.get(f"z2m_dev_updated:{device.id}")

            devices.append(vdev)

        client = getattr(self, "_client", None)
        mqtt_connected = (
            getattr(self, "_mqtt_started", False)
            and client
            and client.is_connected()
        )
        mqtt_configured = self._is_connection_configured()
        
        # Статус воркера для мониторинга
        worker_status = {
            "running": self._worker_thread and self._worker_thread.is_alive(),
            "queue_size": self._msg_queue.qsize() if hasattr(self, '_msg_queue') else 0,
            "queue_max": self._msg_queue.maxsize if hasattr(self, '_msg_queue') else 0,
        }
        
        settings_dict = {
            "host": self.config.get("host", ""),
            "port": self.config.get("port", 1883),
            "topic": self.config.get("topic", ""),
            "login": self.config.get("login", ""),
            "password": self.config.get("password", ""),
        }
        content = {
            "form": settings,
            "devices": devices,
            "settings": settings_dict,
            "mqtt_connected": mqtt_connected,
            "mqtt_configured": mqtt_configured,
            "worker_status": worker_status,
        }
        return self.render('z2m.html', content)

    def route_index(self):
        @self.blueprint.route('/z2m/device', methods=['POST'])
        @self.blueprint.route('/z2m/device/<device_id>', methods=['GET', 'POST'])
        @handle_admin_required
        def point_xi_device(device_id=None):
            with session_scope() as session:
                if request.method == "GET":
                    dev = session.query(ZigbeeDevices).filter(ZigbeeDevices.id == device_id).one()
                    device = row2dict(dev)
                    device['props'] = []
                    props = session.query(ZigbeeProperties).filter(ZigbeeProperties.device_id == device_id).order_by(ZigbeeProperties.title)
                    for prop in props:
                        item = row2dict(prop)
                        item['read_only'] = item['read_only'] == 1
                        # подтягиваем актуальные значения из кэша
                        runtime = cache.get(f"z2m:prop_{device_id}_{prop.title}") or {}
                        value = runtime.get('value')
                        converted = runtime.get('converted')
                        updated = runtime.get('updated')

                        # если в кэше ещё нет значения, а свойство связано с объектом — пробуем взять из ObjectManager
                        if value is None and prop.linked_object and prop.linked_property:
                            try:
                                obj_name = f"{prop.linked_object}.{prop.linked_property}"
                                value = getProperty(obj_name, 'value')
                            except Exception:
                                value = None

                        if value is not None:
                            item['value'] = value
                        if converted is not None:
                            item['converted'] = converted
                        if updated is not None:
                            item['updated'] = updated

                        device['props'].append(item)
                    return jsonify(device)
                if request.method == "POST":
                    data = request.get_json()
                    if data['id']:
                        device = session.query(ZigbeeDevices).where(ZigbeeDevices.id == int(data['id'])).one()
                    else:
                        device = ZigbeeDevices()
                        session.add(device)
                        session.commit()

                    device.title = data['title']
                    device.description = data['description']

                    for prop in data['props']:
                        prop_rec = session.query(ZigbeeProperties).filter(ZigbeeProperties.device_id == device.id,ZigbeeProperties.title == prop['title']).one()
                        if prop_rec.linked_object:
                            removeLinkFromObject(prop_rec.linked_object, prop_rec.linked_property, self.name)
                        prop_rec.linked_object = prop['linked_object']
                        prop_rec.linked_property = prop['linked_property']
                        prop_rec.linked_method = prop['linked_method']
                        prop_rec.converter = prop.get('converter',0)
                        prop_rec.read_only = 1 if prop['read_only'] else 0
                        prop_rec.round = prop['round']
                        prop_rec.min_period = prop['min_period']
                        prop_rec.process_type = prop['process_type']
                        if prop_rec.linked_object and prop_rec.read_only == 0:
                            setLinkToObject(prop_rec.linked_object, prop_rec.linked_property, self.name)

                        self.logger.debug(f"❌ Delete from cache - z2m:prop_{data['id']}_{prop['title']}")
                        cache.delete(f"z2m:prop_{data['id']}_{prop['title']}")

                    session.commit()

                    return 'Device updated successfully', 200

        @self.blueprint.route('/z2m/delete_prop/<prop_id>', methods=['GET', 'POST'])
        @handle_admin_required
        def point_delprop(prop_id=None):
            with session_scope() as session:
                sql = delete(ZigbeeProperties).where(ZigbeeProperties.id == int(prop_id))
                session.execute(sql)
                session.commit()

        @self.blueprint.route('/z2m/api/devices', methods=['GET'])
        @handle_admin_required
        def api_devices():
            devs = ZigbeeDevices.query.order_by(ZigbeeDevices.title).all()
            props = ZigbeeProperties.query.order_by(ZigbeeProperties.title).all()
            devices = []
            for device in devs:
                vdev = row2dict(device)
                if device.is_battery:
                    vdev['battery_warn'] = 'text-danger' if device.battery_level < 30 else ('text-warning' if device.battery_level < 360 else 'text-success')
                prop_cache = cache.get(f"z2m:prop_{device.id}_availability") or {}
                vdev['availability'] = prop_cache.get('value')

                linked = []
                for p in props:
                    if p.device_id == device.id and p.linked_object:
                        item = row2dict(p)
                        runtime = cache.get(f"z2m:prop_{device.id}_{p.title}") or {}
                        item['value'] = runtime.get('value')
                        item['converted'] = runtime.get('converted')
                        item['updated'] = runtime.get('updated')
                        linked.append(item)
                vdev["data"] = linked

                vdev['updated'] = cache.get(f"z2m_dev_updated:{device.id}")
                devices.append(vdev)
            return jsonify(devices)

        @self.blueprint.route('/z2m/api/settings', methods=['POST'])
        @handle_admin_required
        def api_settings():
            data = request.get_json() or {}
            self.config["host"] = data.get("host", "")
            self.config["port"] = int(data.get("port", 1883))
            self.config["topic"] = data.get("topic", "")
            self.config["login"] = data.get("login", "")
            self.config["password"] = data.get("password", "")
            self.saveConfig()
            self._connect_mqtt()
            return jsonify({"success": True})
        
        @self.blueprint.route('/z2m/api/worker_status', methods=['GET'])
        @handle_admin_required
        def api_worker_status():
            """API для мониторинга состояния воркера"""
            return jsonify({
                "running": self._worker_thread and self._worker_thread.is_alive(),
                "queue_size": self._msg_queue.qsize() if hasattr(self, '_msg_queue') else 0,
                "queue_max": self._msg_queue.maxsize if hasattr(self, '_msg_queue') else 0,
                "queue_percent": round((self._msg_queue.qsize() / self._msg_queue.maxsize * 100) if hasattr(self, '_msg_queue') and self._msg_queue.maxsize > 0 else 0, 1)
            })

    def widget(self):
        with session_scope() as session:
            devs = session.query(ZigbeeDevices).all()
            props = session.query(ZigbeeProperties).all()
            content = {}
            content['low_battery'] = ZigbeeDevices.query.filter(ZigbeeDevices.battery_level < 10).count()
            content['offline'] = 0
            content['count'] = len(devs)

            for device in devs:
                av = [av for av in props if (av.device_id == device.id and av.title == 'availability')]
                state = None
                if av:
                    prop_cache = cache.get(f"z2m:prop_{device.id}_availability") or {}
                    state = prop_cache.get('value')
                if state == 'offline':
                    content['offline'] = content['offline'] + 1

        return render_template("widget_z2m.html",**content)

    def cyclic_task(self):
        if self.event.is_set():
            self._stop_worker()
            self._disconnect_mqtt()
        else:
            self.event.wait(1.0)

    def mqttPublish(self, topic, value, qos=0, retain=0):
        client = getattr(self, "_client", None)
        if not client or not getattr(self, "_mqtt_started", False):
            self.logger.debug("MQTT: не подключен, пропуск publish %s", topic)
            return
        if not client.is_connected():
            self.logger.debug("MQTT: соединение потеряно, пропуск publish %s", topic)
            return
        self.logger.info("⬅️ Publish: " + topic + " " + value)
        client.publish(topic, str(value), qos=qos, retain=retain)

    def changeLinkedProperty(self, obj, prop, val):
        with session_scope() as session:
            properties = session.query(ZigbeeProperties).filter(ZigbeeProperties.linked_object == obj, ZigbeeProperties.linked_property == prop).all()
            if len(properties) == 0:
                from app.core.lib.object import removeLinkFromObject
                removeLinkFromObject(obj, prop, self.name)
                return
            for property in properties:
                new_value = val
                runtime = cache.get(f"z2m:prop_{property.device_id}_{property.title}") or {}
                old_value = str(runtime.get('value', '')).lower()
                if property.converter == 0:
                    new_value = val
                    if old_value == 'true' or old_value == 'false':
                        if str(val) == '1':
                            new_value = "True"
                        else:
                            new_value = "False"
                    elif old_value == 'on' or old_value == 'off':
                        if val == 1:
                            new_value = 'ON'
                        else:
                            new_value = 'OFF'
                    elif old_value == 'close' or old_value == 'open':
                        if val:
                            new_value = 'CLOSE'
                        else:
                            new_value = 'OPEN'
                    elif isinstance(old_value, int):
                        new_value = int(val)
                    elif isinstance(old_value, float):
                        new_value = float(val)
                elif property.converter == 1:
                    new_value = val
                elif property.converter == 2:
                    if str(val) == '0':
                        new_value = 'offline'
                    elif str(val) == '1':
                        new_value = 'online'
                elif property.converter == 3:
                    color = val.replace('0x', '').replace('#', '')
                    rgb = {
                        'r': int(color[0:2], 16),
                        'g': int(color[2:4], 16),
                        'b': int(color[4:6], 16)
                    }
                    rgb = {k: v / 255 for k, v in rgb.items()}
                    rgb = {k: (v / 12.92) if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 * 100 for k, v in rgb.items()}
                    xyz = {
                        'x': rgb['r'] * 0.4124 + rgb['g'] * 0.3576 + rgb['b'] * 0.1805,
                        'y': rgb['r'] * 0.2126 + rgb['g'] * 0.7152 + rgb['b'] * 0.0722,
                        'z': rgb['r'] * 0.0193 + rgb['g'] * 0.1192 + rgb['b'] * 0.9505
                    }
                    if color != "000000":
                        xy = {
                            'x': xyz['x'] / (xyz['x'] + xyz['y'] + xyz['z']),
                            'y': xyz['y'] / (xyz['x'] + xyz['y'] + xyz['z'])
                        }
                        new_value = xy
                    else:
                        new_value = {"x": None, "y": None}
                elif property.converter == 5:
                    new_value = round(val * 254 / 100)

                device = session.get(ZigbeeDevices, property.device_id)
                # send  to zigbee device
                topic = device.full_path + "/set"
                payload = json.dumps({property.title:new_value})
                self.mqttPublish(topic, payload)

    def set_payload(self, device_name:str, payload:dict):
        """Set parameters for device

        Args:
            device_name (str): Device name
            payload (dict): Dict parameters 
        """
        with session_scope() as session:
            device = session.query(ZigbeeDevices).filter(ZigbeeDevices.title == device_name).one_or_none()
            if device:
                # send to zigbee device
                topic = device.full_path + "/set"
                payload = json.dumps(payload)
                self.mqttPublish(topic, payload)

    # Функция обратного вызова для подключения к брокеру MQTT
    def on_connect(self, client, userdata, flags, rc):
        self.logger.info('Connected with result code %s', rc)
        self._send_connection_status(rc == 0, True)
        topic_str = self.config.get("topic", "").strip()
        if topic_str:
            topics = topic_str.split(',')
            for topic in topics:
                self.logger.info("🔹 Subscribe: " + topic)
                self._client.subscribe(topic)

    def on_disconnect(self, client, userdata, rc):
        self._send_connection_status(False, True)
        addNotify("Disconnect MQTT",str(rc),CategoryNotify.Error,self.name)
        if rc == 0:
            self.logger.info("Disconnected gracefully.")
        elif rc == 1:
            self.logger.info("Client requested disconnection.")
        elif rc == 2:
            self.logger.info("Broker disconnected the client unexpectedly.")
        elif rc == 3:
            self.logger.info("Client exceeded timeout for inactivity.")
        elif rc == 4:
            self.logger.info("Broker closed the connection.")
        else:
            self.logger.warning("Unexpected disconnection with code: %s", rc)

    # Функция обратного вызова для получения сообщений
    def on_message(self,client, userdata, msg):
        """Лёгкий callback - только складывает сообщения в очередь для воркера"""
        try:
            from_hub = 0
            did = msg.topic
            payload = msg.payload.decode('utf-8')
            
            if '/set' in msg.topic:
                return
            
            # Парсим topic для вычисления did и from_hub (минимальная логика)
            if 'bridge/' not in msg.topic:
                topics = (self.config.get('topic') or '').split(',')
                for t in topics:
                    t = t.lower()
                    t = re.sub(r'#$', '', t)
                    did = did.replace(t, '')
                if not payload.startswith('{') and '/'.join(did.split('/')[1:]):
                    prop = did.split('/')[-1]
                    payload = json.dumps({prop: payload}, indent=None, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
                did = did.split('/')[0]
            else:
                from_hub = 1
                if not payload.startswith('{') and '/'.join(did.split('/')[1:]):
                    prop = did.split('/')[-1]
                    payload = json.dumps({prop: payload}, indent=None, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
                did = did.split('/')[0]
            
            # Кладём в очередь
            job = {
                'topic': msg.topic,
                'did': did,
                'payload': payload,
                'from_hub': from_hub
            }
            
            try:
                self._msg_queue.put_nowait(job)
                queue_size = self._msg_queue.qsize()
                self.logger.debug("➡️ Queued: %s = %s (queue size: %d)", msg.topic, payload[:100], queue_size)
                
                # Предупреждение если очередь заполнена более чем на 80%
                if queue_size > self._msg_queue.maxsize * 0.8:
                    self.logger.warning("⚠️ Message queue is %d%% full (%d/%d)", 
                                      int(queue_size / self._msg_queue.maxsize * 100),
                                      queue_size, self._msg_queue.maxsize)
            except queue.Full:
                self.logger.warning("⚠️ Message queue full (%d), dropping message from %s", 
                                  self._msg_queue.maxsize, msg.topic)
                
        except Exception as ex:
            self.logger.error("on_message error: %s", ex, exc_info=True)

    def processMessage(self, path, did, value, hub):
        if re.search(r'#$', path):
            return 0
        device_id = cache.get("z2m_dev:" + did)
        if not device_id:
            with session_scope() as session:
                device = session.query(ZigbeeDevices).filter(ZigbeeDevices.title == did).one_or_none()
                if device is None:
                    device = session.query(ZigbeeDevices).filter(ZigbeeDevices.ieeaddr == did).one_or_none()
                if device is None:
                    device = ZigbeeDevices()
                    device.title = did
                    device.ieeaddr = did
                    device.updated = get_now_to_utc()
                    device.full_path = re.sub(r'\/bridge.+|\/availability$', '', path)
                    if hub:
                        device.is_hub = 1
                    session.add(device)
                    session.commit()

                device.updated = get_now_to_utc()
                device.full_path = re.sub(r'\/bridge.+|\/availability$', '', path)
                session.commit()

                self.sendDataToWebsocket("updateDevice",row2dict(device))

                cache.set("z2m_dev:" + did, device.id, timeout=0)
                device_id = device.id

        if re.match(r'^{', value):
            ar = json.loads(value)
            if hub and ar.get('type') == 'devices' and isinstance(ar.get('message'), list):
                path = re.sub(r'bridge.+', '', path)
                self.process_list_of_devices(path, ar['message'])
                return
            if hub and isinstance(ar.get('devices'), str) and ar['devices']:
                path = re.sub(r'bridge.+', '', path)
                str_json = ar['devices']
                devices = json.loads(str_json)
                self.process_list_of_devices(path, devices)
                return
            if hub and ar.get('type') == 'device_announce' and isinstance(ar.get('meta'), dict):
                if ar['meta']['ieeeAddr']:
                    friendly_name = ar['meta'].get('friendly_name', ar['meta']['ieeeAddr'])
                    with session_scope() as session:
                        device = session.query(ZigbeeDevices).filter(ZigbeeDevices.ieeaddr == ar['meta']['ieeeAddr']).one_or_none()
                        if device is None:
                            device = ZigbeeDevices()
                            device.title = friendly_name
                            device.ieeaddr = ar['meta']['ieeeAddr']
                            device.updated = get_now_to_utc()
                            session.add(device)
                            session.commit()
                        elif device and device.title != friendly_name:
                            device.title = friendly_name
                            device.updated = get_now_to_utc()
                            session.commit()
            batch = {"prop_updates": [], "battery": [], "side_effects": []}
            if '/availability' in path:
                try:
                    v = json.loads(value)
                    self.process_data(device_id, 'availability', v['state'], batch=batch)
                except Exception as ex:
                    self.logger.error(ex, exc_info=True)
            else:
                for k, v in ar.items():
                    if isinstance(v, dict):
                        v = json.dumps(v, separators=(',', ':'), ensure_ascii=False)
                    if k == 'action':
                        try:
                            self.process_data(device_id, f'action:{v}', time.strftime('%Y-%m-%d %H:%M:%S'), batch=batch)
                        except Exception as ex:
                            self.logger.exception(ex, exc_info=True)
                    try:
                        self.process_data(device_id, k, v, batch=batch)
                    except Exception as ex:
                        self.logger.error(ex, exc_info=True)
            # Batch DB: одно сохранение для батареи; значения свойств храним только в кэше
            now_utc = get_now_to_utc()
            with session_scope() as session:
                for bu in batch["battery"]:
                    session.execute(update(ZigbeeDevices).where(ZigbeeDevices.id == bu["device_id"]).values(
                        is_battery=1, battery_level=bu["value"]))
                session.commit()
            # updated устройства только в памяти/через WebSocket
            cache.set(f"z2m_dev_updated:{device_id}", now_utc, timeout=0)
            for se in batch["side_effects"]:
                if se.get("linked_method"):
                    self.logger.info(f"🔹 Process data: call {se['linked_object']}.{se['linked_method']} Value:{se['new_value']}")
                    callMethodThread(se["linked_object"] + "." + se["linked_method"],
                        {'VALUE': se['new_value'], 'NEW_VALUE': se['new_value'], 'OLD_VALUE': se['old_value'], 'TITLE': se['prop']}, self.name)
                if se.get("linked_property"):
                    self.logger.info(f"🔹 Process data: set {se['linked_object']}.{se['linked_property']} Value:{se['new_value']}")
                    if se.get("process_type") == 1:
                        setPropertyThread(se["linked_object"] + "." + se["linked_property"], se["new_value"], self.name)
                    else:
                        updatePropertyThread(se["linked_object"] + "." + se["linked_property"], se["new_value"], self.name)
                if se.get("property_"):
                    self.sendDataToWebsocket("updateProperty", se["property_"])
            update_payload = {"id": device_id, "updated": now_utc}
            if "/availability" in path:
                prop_cache = cache.get(f"z2m:prop_{device_id}_availability") or {}
                av = prop_cache.get("value")
                if av is not None:
                    update_payload["availability"] = av
            self.sendDataToWebsocket("updateDevice", update_payload)

    def process_list_of_devices(self, path, data):
        with session_scope() as session:
            for device_data in data:
                if device_data.get('friendly_name'):
                    device_data['path'] = path + device_data['friendly_name']
                else:
                    device_data['path'] = path + device_data['ieeeAddr']

                ieeeAddr = device_data.get('ieeeAddr', device_data.get('ieee_address', ''))
                device = session.query(ZigbeeDevices).filter(ZigbeeDevices.ieeaddr == ieeeAddr).one_or_none()
                if device is None and device_data.get('friendly_name'):
                    device = session.query(ZigbeeDevices).filter(ZigbeeDevices.title == device_data['friendly_name']).one_or_none()

                if device is None:
                    device = ZigbeeDevices()
                    session.add(device)

                device.ieeaddr = ieeeAddr
                device.title = device_data.get('friendly_name', device.ieeaddr)
                device.full_path = device_data['path']
                device.manufacturer_id = str(device_data.get('manufacturerID', device_data.get('manufacturer', '')))
                if 'definition' not in device_data or device_data['definition'] is None:
                    device_data['definition'] = {}
                device.model = str(device_data.get('model', device_data.get('definition', {}).get('model', '')))
                device.model_name = str(device_data.get('modelID', device_data.get('model_id', '')))
                device.model_description = str(device_data.get('description', device_data.get('definition', {}).get('description', '')))
                device.vendor = str(device_data.get('vendor', device_data.get('definition', {}).get('vendor', '')))

                if not device.description or device.description.strip().startswith('-'):
                    device.description = device.model_description + ' - ' + device.title

                session.commit()

    def process_data(self, device_id, prop, value, batch=None):
        if isinstance(value, dict):
            value = ""  # TODO fix len for coordinator
        property_ = cache.get(f"z2m:prop_{device_id}_{prop}")
        if not property_:
            with session_scope() as session:
                property_ = session.query(ZigbeeProperties).filter(
                    ZigbeeProperties.device_id == device_id,
                    ZigbeeProperties.title == prop
                ).one_or_none()
                if not property_:
                    property_ = ZigbeeProperties()
                    property_.title = prop
                    property_.device_id = device_id
                    session.add(property_)
                    session.commit()
                property_ = row2dict(property_)

        # Гарантируем наличие runtime-полей в dict, даже если их нет в БД
        property_.setdefault('min_period', 0)
        property_.setdefault('round', None)
        property_.setdefault('process_type', 0)
        property_.setdefault('linked_object', None)
        property_.setdefault('linked_property', None)
        property_.setdefault('linked_method', None)
        property_.setdefault('value', '')
        property_.setdefault('converted', '')
        property_.setdefault('updated', None)
        if property_['min_period'] and property_['updated']:
            if isinstance(property_['updated'], str):
                from dateutil import parser
                property_['updated'] = parser.parse(property_['updated'])
            if time.time() - property_['updated'].timestamp() < property_['min_period']:  # todo fix
                return
        if isinstance(value, (dict, list)):
            value = json.dumps(value, separators=(',', ':'), ensure_ascii=False)
        elif isinstance(value, float):
            value = value
        elif isinstance(value, bool):
            if value is False:
                value = 'false'
            if value is True:
                value = 'true'
        elif value is None:
            value = ''
        elif isinstance(value, str) and len(value) > 255:
            value = value[:255]

        if property_['round'] and property_['round'] != -1:
            if isinstance(value, (int, float)):
                value = round(value, property_['round'])

        old_value = property_['value']
        property_['value'] = value

        converted = ''
        if property_['converter'] == 0 or property_['converter'] is None:
            if isinstance(value, str):
                value = value.lower()
            if value in ['false', 'off', 'no', 'open']:
                converted = '0'
            elif value in ['true', 'on', 'yes', 'close']:
                converted = '1'
        elif property_['converter'] == 2:
            if value == 'offline':
                converted = '0'
            elif value == 'online':
                converted = '1'
        elif property_['converter'] == 3:
            # bri = 254
            xy = json.loads(value)
            if 'hex' not in xy and 'x' in xy and 'y' in xy:
                _x = xy['x']
                _y = xy['y']
                _z = 1.0 - _x - _y
                Y = 1
                X = Y / _y * _x if _y != 0 else 0
                Z = Y / _y * _z if _y != 0 else 0

                r = X * 3.2406 + Y * -1.5372 + Z * -0.4986
                g = X * -0.9689 + Y * 1.8758 + Z * 0.0415
                b = X * 0.0557 + Y * -0.2040 + Z * 1.0570

                r = 12.92 * r if r <= 0.0031308 else (1.0 + 0.055) * pow(r, 1.0 / 2.4) - 0.055
                g = 12.92 * g if g <= 0.0031308 else (1.0 + 0.055) * pow(g, 1.0 / 2.4) - 0.055
                b = 12.92 * b if b <= 0.0031308 else (1.0 + 0.055) * pow(b, 1.0 / 2.4) - 0.055

                r = min(max(0, r), 1) * 255
                g = min(max(0, g), 1) * 255
                b = min(max(0, b), 1) * 255

                r = format(round(r), '02x')
                g = format(round(g), '02x')
                b = format(round(b), '02x')

                converted = r + g + b
            else:
                converted = value
        elif property_['converter'] == 4:
            converted = str(int(time.mktime(time.strptime(value, "%Y-%m-%d %H:%M:%S"))))
        elif property_['converter'] == 5:
            converted = str(round(float(value) * 100 / 254))

        property_['converted'] = converted
        property_['updated'] = get_now_to_utc()

        new_value = converted if converted else value

        if str(property_['value']) != str(old_value) or prop == 'action' or property_['process_type'] == 1:
            if batch is not None:
                if property_['linked_object']:
                    batch["side_effects"].append({
                        "linked_object": property_['linked_object'],
                        "linked_property": property_['linked_property'] or "",
                        "linked_method": property_['linked_method'] or "",
                        "new_value": new_value,
                        "old_value": old_value,
                        "prop": prop,
                        "process_type": property_['process_type'] or 0,
                        "property_": dict(property_),
                    })
            else:
                if property_['linked_object']:
                    if property_['linked_method']:
                        self.logger.info(f"🔹 Process data: call {property_['linked_object']}.{property_['linked_method']} Value:{new_value}")
                        callMethodThread(property_['linked_object'] + '.' + property_['linked_method'], {'VALUE': new_value, 'NEW_VALUE': new_value, 'OLD_VALUE': old_value, 'TITLE': prop}, self.name)
                    if property_['linked_property']:
                        self.logger.info(f"🔹 Process data: set {property_['linked_object']}.{property_['linked_property']} Value:{new_value}")
                        if property_['process_type'] == 1:
                            setPropertyThread(property_['linked_object'] + '.' + property_['linked_property'], new_value, self.name)
                        else:
                            updatePropertyThread(property_['linked_object'] + '.' + property_['linked_property'], new_value, self.name)
                    # Только в WebSocket/кэше, без записи в БД
                    self.sendDataToWebsocket("updateProperty", property_)

        self.logger.debug(f"💾 Save in cache - z2m:prop_{device_id}_{prop} = {property_}")
        cache.set(f"z2m:prop_{device_id}_{prop}", property_, timeout=0)

        if prop == 'battery':
            if batch is not None:
                batch["battery"].append({"device_id": device_id, "value": value})
            else:
                with session_scope() as session:
                    session.execute(update(ZigbeeDevices).where(ZigbeeDevices.id == device_id).values(is_battery=1, battery_level=value))

    def search(self, query: str) -> str:
        res = []
        devices = ZigbeeDevices.query.filter(or_(ZigbeeDevices.title.contains(query),
                                                 ZigbeeDevices.description.contains(query),
                                                 ZigbeeDevices.ieeaddr.contains(query))).all()
        for device in devices:
            res.append({"url":f'z2m?view=device&op=edit&id={device.id}',
                        "title":f'{device.title} #{device.ieeaddr} ({device.description})',
                        "tags":[{"name":"z2m","color":"primary"},{"name":"Device","color":"danger"}]})
        props = ZigbeeProperties.query.filter(or_(ZigbeeProperties.title.contains(query),
                                                  ZigbeeProperties.linked_object.contains(query),
                                                  ZigbeeProperties.linked_property.contains(query),
                                                  ZigbeeProperties.linked_method.contains(query))).all()
        for prop in props:
            device = ZigbeeDevices.get_by_id(prop.device_id)
            runtime = cache.get(f"z2m:prop_{prop.device_id}_{prop.title}") or {}
            val = runtime.get('value', '')
            res.append({"url":f'z2m?view=device&op=edit&id={prop.device_id}&tab=properties',
                        "title":f'{device.title}->{val} ({prop.linked_object}.{prop.linked_property}{prop.linked_method})',
                        "tags":[{"name":"z2m","color":"primary"},{"name":"Property","color":"warning"}]})
        return res

    def changeObject(self, event, object_name, property_name, method_name, new_value):
        with session_scope() as session:
            devices = session.query(ZigbeeProperties).filter(ZigbeeProperties.linked_object == object_name).all()
            for device in devices:
                if new_value is None:
                    device.linked_object = None
                    device.linked_property = None
                    device.linked_method = None
                elif property_name is None and method_name is None:
                    device.linked_object = new_value
                elif property_name:
                    device.linked_property = new_value
                elif method_name:
                    device.linked_method = new_value

            session.commit()
