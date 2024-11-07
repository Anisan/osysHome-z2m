""" Zigbee2Mqtt """
import time
import datetime
import json
import re
from flask import redirect, render_template, request, jsonify
from sqlalchemy import delete, update, or_
import paho.mqtt.client as mqtt
from app.database import session_scope, row2dict, db
from app.extensions import cache
from app.authentication.handlers import handle_admin_required
from app.core.main.BasePlugin import BasePlugin
from plugins.z2m.models.z2m import ZigbeeDevices, ZigbeeProperties
from app.core.lib.object import callMethodThread, updatePropertyThread, setPropertyThread, setLinkToObject, removeLinkFromObject
from app.core.lib.common import addNotify, CategoryNotify
from plugins.z2m.forms.SettingForms import SettingsForm

class z2m(BasePlugin):

    def __init__(self,app):
        super().__init__(app,__name__)
        self.title = "Zigbee2Mqtt"
        self.version = 1
        self.description = """This is a test plugin"""
        self.category = "Devices"
        self.actions = ['cycle','search', "widget"]

    def initialization(self):
        # Создаем клиент MQTT
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
        # Назначаем функции обратного вызова
        self._client.on_connect = self.on_connect
        self._client.on_disconnect = self.on_disconnect
        self._client.on_message = self.on_message

        if "host" in self.config:
            if self.config.get("login",'') != '' and self.config.get("password",'') != '':
                self._client.username_pw_set(self.config["login"], self.config["password"])
            # Подключаемся к брокеру MQTT
            self._client.connect(self.config.get("host",""), 1883, 0)
            # Запускаем цикл обработки сообщений в отдельном потоке
            self._client.loop_start()

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
            av = [av for av in props if (av.device_id == device.id and av.title == 'availability')]
            if av:
                vdev['availability'] = av[0].value
            linked = [av for av in props if (av.device_id == device.id and av.linked_object)]
            vdev["data"] = linked

            devices.append(vdev)

        content = {
            "form": settings,
            "devices": devices,
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
                        cache.delete(f"z2m:prop_{data['id']}_{prop}")
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
                        if prop_rec.linked_object:
                            setLinkToObject(prop_rec.linked_object, prop_rec.linked_property, self.name)

                    session.commit()

                    return 'Device updated successfully', 200

        @self.blueprint.route('/z2m/delete_prop/<prop_id>', methods=['GET', 'POST'])
        @handle_admin_required
        def point_delprop(prop_id=None):
            with session_scope() as session:
                sql = delete(ZigbeeProperties).where(ZigbeeProperties.id == int(prop_id))
                session.execute(sql)
                session.commit()

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
                if av and av[0].value == 'offline':
                    content['offline'] = content['offline'] + 1

        return render_template("widget_z2m.html",**content)

    def cyclic_task(self):
        if self.event.is_set():
            # Останавливаем цикл обработки сообщений
            self._client.loop_stop()
            # Отключаемся от брокера MQTT
            self._client.disconnect()
        else:
            self.event.wait(1.0)

    def mqttPublish(self, topic, value, qos=0, retain=0):
        self.logger.info("Publish: " + topic + " " + value)
        self._client.publish(topic, str(value), qos=qos, retain=retain)

    def changeLinkedProperty(self, obj, prop, val):
        with session_scope() as session:
            properties = session.query(ZigbeeProperties).filter(ZigbeeProperties.linked_object == obj, ZigbeeProperties.linked_property == prop).all()
            if len(properties) == 0:
                from app.core.lib.object import removeLinkFromObject
                removeLinkFromObject(obj, prop, self.name)
                return
            for property in properties:
                new_value = val
                old_value = property.value.lower()
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
    def on_connect(self,client, userdata, flags, rc):
        self.logger.info('Connected with result code %s',rc)
        # Подписываемся на топик
        if self.config["topic"]:
            self._client.subscribe(self.config["topic"])

    def on_disconnect(self, client, userdata, rc):
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
        self.logger.debug(msg.topic + " " + str(msg.payload))
        from_hub = 0
        did = msg.topic
        payload = msg.payload.decode('utf-8')

        if '/set' in msg.topic:
            return

        if 'bridge/' not in msg.topic:
            topics = self.config['topic'].split(',')
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

        if not payload:
            return False

        self.processMessage(msg.topic, did, payload, from_hub)

    def processMessage(self, path, did, value, hub):
        if re.search(r'#$', path):
            return 0
        self.logger.debug(path + " " + str(value))
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
                    device.updated = datetime.datetime.now()
                    device.full_path = re.sub(r'\/bridge.+|\/availability$', '', path)
                    if hub:
                        device.is_hub = 1
                    session.add(device)
                    session.commit()

                device.updated = datetime.datetime.now()
                device.full_path = re.sub(r'\/bridge.+|\/availability$', '', path)
                session.commit()

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
                            device.updated = datetime.datetime.now()
                            session.add(device)
                            session.commit()
                        elif device and device.title != friendly_name:
                            device.title = friendly_name
                            device.updated = datetime.datetime.now()
                            session.commit()

            for k, v in ar.items():
                if isinstance(v, dict):
                    v = json.dumps(v, separators=(',', ':'), ensure_ascii=False)
                if k == 'action':
                    try:
                        self.process_data(device_id, f'action:{v}', time.strftime('%Y-%m-%d %H:%M:%S'))
                    except Exception as ex:
                        self.logger.exception(ex, exc_info=True)
                try:
                    self.process_data(device_id, k, v)
                except Exception as ex:
                    self.logger.error(ex, exc_info=True)
            with session_scope() as session:
                sql = update(ZigbeeDevices).where(ZigbeeDevices.id == device_id).values(updated=datetime.datetime.now())
                session.execute(sql)
                session.commit()

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

                device.updated = datetime.datetime.now()
                session.commit()

    def process_data(self, device_id, prop, value):
        if isinstance(value, dict):
            value = ""  # TODO fix len for coordinator
        property_ = cache.get(f"z2m:prop_{device_id}_{prop}")
        if not property_:
            with session_scope() as session:
                property_ = session.query(ZigbeeProperties).filter(ZigbeeProperties.device_id == device_id, ZigbeeProperties.title == prop).one_or_none()
                if not property_:
                    property_ = ZigbeeProperties()
                    property_.title = prop
                    property_.device_id = device_id
                    session.add(property_)
                    session.commit()
                property_ = row2dict(property_)
            cache.set(f"z2m:prop_{device_id}_{prop}",property_, timeout=0)
        if property_['min_period']:
            if time.time() - property_['updated'].timestamp() < property_['min_period']:  # todo fix
                return
        if isinstance(value, dict):
            value = json.dumps(value)
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
            if 'hex' not in xy:
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
        elif property_['converter'] == 4:
            converted = str(int(time.mktime(time.strptime(value, "%Y-%m-%d %H:%M:%S"))))
        elif property_['converter'] == 5:
            converted = str(round(float(value) * 100 / 254))

        property_['converted'] = converted
        property_['updated'] = datetime.datetime.now()

        new_value = converted if converted else value

        if str(property_['value']) != str(old_value) or prop == 'action' or property_['process_type'] == 1:
            with session_scope() as session:
                sql = update(ZigbeeProperties).where(ZigbeeProperties.id == property_['id']).values(value=property_['value'],
                                                                                                    converted=converted,
                                                                                                    updated=datetime.datetime.now())
                session.execute(sql)
                if property_['linked_object']:
                    if property_['linked_method']:
                        callMethodThread(property_['linked_object'] + '.' + property_['linked_method'], {'VALUE': new_value, 'NEW_VALUE': new_value, 'OLD_VALUE': old_value, 'TITLE': prop}, self.name)
                    if property_['linked_property']:
                        if property_['process_type'] == 1:
                            setPropertyThread(property_['linked_object'] + '.' + property_['linked_property'], new_value, self.name)
                        else:
                            updatePropertyThread(property_['linked_object'] + '.' + property_['linked_property'], new_value, self.name)
                    session.commit()

        cache.set(f"z2m:prop_{device_id}_{prop}",property_, timeout=0)

        if prop == 'battery':
            with session_scope() as session:
                sql = update(ZigbeeDevices).where(ZigbeeDevices.id == device_id).values(is_battery=1, battery_level=value)
                session.execute(sql)

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
            res.append({"url":f'z2m?view=device&op=edit&id={prop.device_id}&tab=properties',
                        "title":f'{device.title}->{prop.value} ({prop.linked_object}.{prop.linked_property}{prop.linked_method})',
                        "tags":[{"name":"z2m","color":"primary"},{"name":"Property","color":"warning"}]})
        return res
