# MCP — z2m (Zigbee2MQTT)

Устройства и свойства **не создаются вручную** — они появляются автоматически из Zigbee2MQTT через MQTT.

## Collections

| ID | binding_mode | creatable | deletable | Что может агент |
|----|--------------|-----------|-----------|-----------------|
| `devices` | `none` | нет | нет | Только изменить `description` |
| `properties` | `property` | нет | нет | Связка с объектами, настройка конвертеров |

### Фильтры list_entities

| collection | фильтр | описание |
|------------|--------|----------|
| `devices` | `query` | Поиск по title, description, ieeaddr |
| `devices` | `availability` | `online` или `offline` |
| `properties` | `query` | Поиск по title, linked_* |
| `properties` | `device_id` | Свойства конкретного устройства |
| `properties` | `linked_object` | Свойства, привязанные к объекту |
| `properties` | `has_binding` | true — только с привязкой, false — без |

## Операции

| operation | Описание |
|-----------|----------|
| `set_property` | Отправить команду устройству через MQTT `/set` |
| `get_property_value` | Прочитать текущее значение из кэша |
| `get_connection_status` | Статус MQTT-подключения |
| `get_worker_status` | Статус очереди обработки MQTT-сообщений |
| `reconnect` | Переподключиться к брокеру с текущими настройками |
| `set_via_binding` | Изменить связанное свойство объекта и отправить MQTT через привязку |

## Промпты

| name | Назначение |
|------|------------|
| `osys_z2m_binding` | Пошаговая привязка свойства Zigbee к объекту |
| `osys_z2m_entity_authoring` | Создание payload для upsert по схеме коллекции |

## Примеры

### Список устройств

```json
{
  "plugin": "z2m",
  "action": "list_entities",
  "args": {
    "collection": "devices",
    "query": "kitchen"
  }
}
```

### Список offline-устройств

```json
{
  "plugin": "z2m",
  "action": "list_entities",
  "args": {
    "collection": "devices",
    "availability": "offline"
  }
}
```

### Связать свойство Zigbee с объектом

```json
{
  "plugin": "z2m",
  "action": "upsert_entity",
  "args": {
    "collection": "properties",
    "entity_id": 42,
    "payload": {
      "linked_object": "KitchenLight",
      "linked_property": "status",
      "converter": 6,
      "read_only": false,
      "process_type": 0
    }
  }
}
```

### Изменить описание устройства

```json
{
  "plugin": "z2m",
  "action": "upsert_entity",
  "args": {
    "collection": "devices",
    "entity_id": 5,
    "payload": {
      "description": "Лампа на кухне"
    }
  }
}
```

### Отправить команду устройству

```json
{
  "plugin": "z2m",
  "action": "invoke",
  "args": {
    "operation": "set_property",
    "params": {
      "device_id": 5,
      "prop": "state",
      "value": "ON"
    }
  }
}
```

### Управление через привязку

```json
{
  "plugin": "z2m",
  "action": "invoke",
  "args": {
    "operation": "set_via_binding",
    "params": {
      "object_name": "KitchenLight",
      "property_name": "status",
      "value": 1
    }
  }
}
```

### Проверить связку

1. `osys_get_property` → `KitchenLight.status` → `linked` содержит `z2m`
2. Изменение `KitchenLight.status` через `osys_write_property` или `set_via_binding` отправит MQTT `/set`

### Настройки MQTT-брокера

`osys_get_plugin_config` / `osys_update_plugin_config` для `plugin: "z2m"`.

## Конвертеры свойств

| converter | Назначение |
|-----------|------------|
| 0 | auto |
| 1 | none |
| 2 | availability (online/offline) |
| 3 | color xy → hex |
| 4 | datetime → unix timestamp |
| 5 | brightness (0–254 → 0–100%) |
| 6 | on/off |
| 7 | open/close |
| 8 | lock/unlock |

## Ограничения

- **Нельзя** создавать или удалять устройства и свойства
- **Нельзя** менять `title`, `ieeaddr`, `model` и другие поля, приходящие из Zigbee2MQTT
- **Нельзя** писать `value`, `converted`, `updated` через upsert — это runtime-кэш
- Свойства появляются при получении MQTT-данных от устройства
- Для привязки action-событий используйте `linked_method` (например, `action:toggle`)
- `read_only=true` отключает исходящий MQTT при изменении связанного свойства
