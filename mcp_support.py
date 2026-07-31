"""MCP integration helpers for z2m (Zigbee2mqtt) plugin."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Set, Tuple

from sqlalchemy import and_, or_

from app.core.lib.mcp_contract import (
    build_plugin_mcp_descriptors,
    revision_from_datetime,
    revision_from_dict,
    validate_entity_payload,
)
from app.core.lib.plugin_binding import (
    remove_property_link,
    sync_property_link,
    validate_object_exists,
    validate_object_property_exists,
)
from app.core.main.ObjectsStorage import objects_storage
from app.database import row2dict, session_scope
from app.extensions import cache

from plugins.z2m.models.z2m import ZigbeeDevices, ZigbeeProperties

DEVICES = "devices"
PROPERTIES = "properties"
PLUGIN_NAME = "z2m"

_DEVICE_WRITABLE_FIELDS = ("description",)
_PROPERTY_WRITABLE_FIELDS = (
    "linked_object",
    "linked_property",
    "linked_method",
    "converter",
    "min_period",
    "round",
    "read_only",
    "process_type",
)
_DEVICE_READONLY_FIELDS = ("id", "title", "ieeaddr", "availability", "full_path", "is_hub", "is_battery", "battery_level", "manufacturer_id", "model", "model_name", "model_description", "vendor", "updated")
_PROPERTY_READONLY_FIELDS = ("id", "device_id", "title", "value", "converted", "updated")

_CONVERTER_DESC = "0=auto, 1=none, 2=availability, 3=color xy, 4=datetime, 5=brightness, 6=on/off, 7=open/close, 8=lock"

_PLUGIN_NOTES = [
    "Devices and properties are auto-managed from Zigbee2MQTT via MQTT; do not create or delete entities.",
    "For devices only description may be edited. For properties configure bindings and conversion settings.",
    "Use set_property to send MQTT /set commands. Prefer set_via_binding when changing a linked osysHome property.",
    "Check get_connection_status before publishing or changing broker config.",
    "process_type: 0=update linked property on inbound MQTT, 1=set linked property.",
    "read_only=true skips outbound MQTT when linked property changes.",
    "For action events bind linked_method to action:toggle (or other action:title values).",
    "converter auto-detects bool/on-off from last MQTT value; use explicit converter for lights, locks, colors.",
    "Offline devices: plugin sends /get before /set to wake Zigbee end devices.",
    "Property values (value, converted, updated) are runtime cache fields; not writable via upsert.",
    "Prefer osys_bind_device over manual upsert plus manage_property_links.",
]

_BINDING_PROMPT = "osys_z2m_binding"
_ENTITY_AUTHORING_PROMPT = "osys_z2m_entity_authoring"


def _plugin_instance():
    try:
        from app.core.main.PluginsHelper import plugins
        return plugins.get(PLUGIN_NAME, {}).get("instance")
    except Exception:
        return None


def validate_object_method_exists(object_name: Optional[str], method_name: Optional[str]) -> bool:
    obj_name = str(object_name or "").strip()
    meth_name = str(method_name or "").strip()
    if not obj_name or not meth_name:
        return False
    obj = objects_storage.getObjectByName(obj_name)
    if obj is None:
        return False
    return meth_name in obj.methods


def _parse_optional_bool(value) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def mcp_capabilities() -> dict:
    return {
        "mcp_version": 1,
        "entities": True,
        "config_schema": True,
        "notes": list(_PLUGIN_NOTES),
        "collections": [
            {
                "id": DEVICES,
                "title": "Zigbee Devices",
                "binding_mode": "none",
                "writable": True,
                "creatable": False,
                "deletable": False,
                "has_code": False,
                "list_filters": ["query", "availability"],
                "default_sort": "title asc, id asc",
                "writable_fields": list(_DEVICE_WRITABLE_FIELDS),
                "description": "Auto-discovered from Zigbee2MQTT. Agent may only update description.",
            },
            {
                "id": PROPERTIES,
                "title": "Zigbee Properties",
                "binding_mode": "property",
                "writable": True,
                "creatable": False,
                "deletable": False,
                "has_code": False,
                "list_filters": ["query", "device_id", "linked_object", "has_binding"],
                "default_sort": "title asc, id asc",
                "writable_fields": list(_PROPERTY_WRITABLE_FIELDS),
                "description": (
                    "Auto-created when MQTT data arrives. "
                    "Agent may configure bindings and converters, not create or delete properties."
                ),
            },
        ],
        "operations": [
            "set_property",
            "get_property_value",
            "get_connection_status",
            "get_worker_status",
            "reconnect",
            "set_via_binding",
        ],
        "operation_schemas": {
            "set_property": {
                "description": "Send MQTT /set command to a Zigbee device",
                "params": {
                    "type": "object",
                    "properties": {
                        "device_id": {"type": "integer", "description": "Internal device primary key"},
                        "device": {"type": "string", "description": "Device friendly name (title)"},
                        "prop": {"type": "string", "description": "Property title"},
                        "value": {"description": "Value to send via MQTT /set"},
                    },
                    "required": ["prop", "value"],
                },
            },
            "get_property_value": {
                "description": "Read current property value from runtime cache",
                "params": {
                    "type": "object",
                    "properties": {
                        "device_id": {"type": "integer"},
                        "prop": {"type": "string", "description": "Property title"},
                    },
                    "required": ["device_id", "prop"],
                },
            },
            "get_connection_status": {
                "description": "MQTT broker connection status and configured host/topic",
                "params": {"type": "object", "properties": {}},
            },
            "get_worker_status": {
                "description": "MQTT message worker queue size and running state",
                "params": {"type": "object", "properties": {}},
            },
            "reconnect": {
                "description": "Reconnect MQTT client with current plugin config",
                "params": {"type": "object", "properties": {}},
            },
            "set_via_binding": {
                "description": "Change linked osysHome property and publish through z2m bindings",
                "params": {
                    "type": "object",
                    "properties": {
                        "object_name": {"type": "string"},
                        "property_name": {"type": "string"},
                        "value": {"description": "Value to publish through linked Zigbee properties"},
                    },
                    "required": ["object_name", "property_name", "value"],
                },
            },
        },
    }


def mcp_config_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "host": {"type": "string", "description": "MQTT broker host"},
            "port": {"type": "integer", "default": 1883},
            "protocol": {
                "type": "string",
                "enum": ["3.1", "3.1.1", "5.0"],
                "default": "3.1.1",
            },
            "topic": {
                "type": "string",
                "description": "MQTT subscribe topic(s), comma-separated (Zigbee2MQTT base topic)",
            },
            "login": {"type": "string"},
            "password": {"type": "string", "writeOnly": True},
            "queue_max_size": {"type": "integer", "default": 1000, "minimum": 100},
        },
    }


def _collection_meta(collection: str) -> dict:
    for item in mcp_capabilities()["collections"]:
        if item["id"] == collection:
            return item
    raise ValueError(f"Unsupported collection: {collection}")


def _format_dt(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value)


def _device_to_dict(row: ZigbeeDevices) -> dict:
    data = row2dict(row)
    prop_cache = cache.get(f"z2m:prop_{row.id}_availability") or {}
    availability = prop_cache.get("value") or data.get("availability")
    if availability is not None:
        data["availability"] = availability
    updated = _format_dt(cache.get(f"z2m_dev_updated:{row.id}"))
    if updated is not None:
        data["updated"] = updated
    return data


def _property_to_dict(row: ZigbeeProperties, include_runtime: bool = True) -> dict:
    data = row2dict(row)
    data["read_only"] = bool(row.read_only)
    if include_runtime:
        runtime = cache.get(f"z2m:prop_{row.device_id}_{row.title}") or {}
        if runtime.get("value") is not None:
            data["value"] = runtime.get("value")
        if runtime.get("converted") is not None:
            data["converted"] = runtime.get("converted")
        updated = _format_dt(runtime.get("updated"))
        if updated is not None:
            data["updated"] = updated
    return data


def _query_filter_devices(query: str):
    like = f"%{query}%"
    return or_(
        ZigbeeDevices.title.ilike(like),
        ZigbeeDevices.description.ilike(like),
        ZigbeeDevices.ieeaddr.ilike(like),
    )


def _query_filter_properties(query: str):
    like = f"%{query}%"
    return or_(
        ZigbeeProperties.title.ilike(like),
        ZigbeeProperties.linked_object.ilike(like),
        ZigbeeProperties.linked_property.ilike(like),
        ZigbeeProperties.linked_method.ilike(like),
    )


def _device_availability(row: ZigbeeDevices) -> str:
    prop_cache = cache.get(f"z2m:prop_{row.id}_availability") or {}
    return str(prop_cache.get("value") or row.availability or "").strip().lower()


def _merge_device_payload(payload: dict, entity_id=None) -> dict:
    merged = dict(payload or {})
    if entity_id in (None, ""):
        return merged
    try:
        current = mcp_get_entity(DEVICES, entity_id)
    except ValueError:
        return merged
    for field in _DEVICE_WRITABLE_FIELDS:
        if field not in merged and field in current:
            merged[field] = current[field]
    return merged


def _merge_property_payload(payload: dict, entity_id=None) -> dict:
    merged = dict(payload or {})
    if entity_id not in (None, ""):
        try:
            current = mcp_get_entity(PROPERTIES, entity_id)
        except ValueError:
            current = None
    else:
        current = None
        device_id = merged.get("device_id")
        title = str(merged.get("title") or "").strip()
        if device_id not in (None, "") and title:
            with session_scope() as session:
                row = (
                    session.query(ZigbeeProperties)
                    .filter(ZigbeeProperties.device_id == int(device_id), ZigbeeProperties.title == title)
                    .one_or_none()
                )
                if row is not None:
                    current = _property_to_dict(row)
    if not current:
        return merged
    for field in _PROPERTY_WRITABLE_FIELDS:
        if field not in merged and field in current:
            merged[field] = current[field]
    if "device_id" not in merged and current.get("device_id") is not None:
        merged["device_id"] = current["device_id"]
    if "title" not in merged and current.get("title"):
        merged["title"] = current["title"]
    return merged


def _sync_z2m_property_link(
    row: ZigbeeProperties,
    old_object: Optional[str] = None,
    old_property: Optional[str] = None,
) -> None:
    if old_object and old_property:
        remove_property_link(PLUGIN_NAME, old_object, old_property)
    if row.linked_object and row.linked_property and not row.read_only:
        ok, err = sync_property_link(
            PLUGIN_NAME,
            row.linked_object,
            row.linked_property,
            old_object=old_object,
            old_property=old_property,
        )
        if not ok:
            raise ValueError(err or "property link validation failed")


def _get_existing_property(session, entity_id=None, payload: dict = None) -> ZigbeeProperties:
    if entity_id not in (None, ""):
        row = (
            session.query(ZigbeeProperties)
            .filter(ZigbeeProperties.id == int(entity_id))
            .one_or_none()
        )
        if row is None:
            raise ValueError(f"Property not found: {entity_id}")
        return row

    payload = payload or {}
    device_id = payload.get("device_id")
    title = str(payload.get("title") or "").strip()
    if device_id in (None, "") or not title:
        raise ValueError(
            "entity_id or existing (device_id + title) is required; "
            "properties cannot be created manually"
        )
    row = (
        session.query(ZigbeeProperties)
        .filter(ZigbeeProperties.device_id == int(device_id), ZigbeeProperties.title == title)
        .one_or_none()
    )
    if row is None:
        raise ValueError(
            f"Property not found: device_id={device_id}, title={title}. "
            "Properties are auto-created from Zigbee2MQTT MQTT data."
        )
    return row


def mcp_entity_schema(collection: str) -> dict:
    _collection_meta(collection)
    if collection == DEVICES:
        return {
            "type": "object",
            "description": "Zigbee device auto-discovered from Zigbee2MQTT.",
            "properties": {
                "id": {"type": "integer", "readOnly": True},
                "title": {"type": "string", "readOnly": True, "description": "Friendly name from Zigbee2MQTT"},
                "description": {"type": "string", "description": "User-editable description"},
                "ieeaddr": {"type": "string", "readOnly": True},
                "availability": {"type": "string", "readOnly": True, "description": "online/offline"},
                "full_path": {"type": "string", "readOnly": True, "description": "MQTT topic base path"},
                "is_hub": {"type": "boolean", "readOnly": True},
                "is_battery": {"type": "boolean", "readOnly": True},
                "battery_level": {"type": "integer", "readOnly": True},
                "manufacturer_id": {"type": "string", "readOnly": True},
                "model": {"type": "string", "readOnly": True},
                "model_name": {"type": "string", "readOnly": True},
                "model_description": {"type": "string", "readOnly": True},
                "vendor": {"type": "string", "readOnly": True},
                "updated": {"type": "string", "readOnly": True},
            },
            "required": [],
        }
    if collection == PROPERTIES:
        return {
            "type": "object",
            "description": "Zigbee device property with optional osysHome binding.",
            "properties": {
                "id": {"type": "integer", "readOnly": True},
                "device_id": {"type": "integer", "readOnly": True},
                "title": {"type": "string", "readOnly": True, "description": "MQTT property key"},
                "converter": {
                    "type": "integer",
                    "enum": [0, 1, 2, 3, 4, 5, 6, 7, 8],
                    "description": _CONVERTER_DESC,
                },
                "min_period": {"type": "integer", "description": "Minimum update period in ms"},
                "round": {"type": "integer", "description": "Decimal places for numeric values"},
                "read_only": {"type": "boolean", "description": "Skip outbound MQTT on linked property change"},
                "process_type": {"type": "integer", "enum": [0, 1], "description": "0=update, 1=set"},
                "linked_object": {"type": "string", "description": "osysHome object name"},
                "linked_property": {"type": "string", "description": "Property on linked object"},
                "linked_method": {"type": "string", "description": "Method called on inbound MQTT (e.g. action:toggle)"},
                "value": {"type": "string", "readOnly": True},
                "converted": {"type": "string", "readOnly": True},
                "updated": {"type": "string", "readOnly": True},
            },
            "required": [],
        }
    raise ValueError(f"Unsupported collection: {collection}")


def mcp_list_entities(
    collection: str,
    query: str = None,
    limit: int = 100,
    device_id: Optional[int] = None,
    linked_object: Optional[str] = None,
    has_binding: Optional[bool] = None,
    availability: Optional[str] = None,
) -> List[dict]:
    limit = max(1, min(int(limit or 100), 5000))
    if collection == DEVICES:
        availability_filter = str(availability or "").strip().lower()
        with session_scope() as session:
            q = session.query(ZigbeeDevices)
            if query:
                q = q.filter(_query_filter_devices(query))
            rows = q.order_by(ZigbeeDevices.title, ZigbeeDevices.id).limit(limit * 5 if availability_filter else limit).all()
            items = [_device_to_dict(row) for row in rows]
            if availability_filter:
                items = [
                    item for item, row in zip(items, rows)
                    if _device_availability(row) == availability_filter
                ][:limit]
            return items
    if collection == PROPERTIES:
        with session_scope() as session:
            q = session.query(ZigbeeProperties)
            if device_id not in (None, ""):
                q = q.filter(ZigbeeProperties.device_id == int(device_id))
            linked_obj = str(linked_object or "").strip()
            if linked_obj:
                q = q.filter(ZigbeeProperties.linked_object == linked_obj)
            binding_filter = _parse_optional_bool(has_binding)
            if binding_filter is True:
                q = q.filter(
                    or_(
                        and_(ZigbeeProperties.linked_property.isnot(None), ZigbeeProperties.linked_property != ""),
                        and_(ZigbeeProperties.linked_method.isnot(None), ZigbeeProperties.linked_method != ""),
                    )
                )
            elif binding_filter is False:
                q = q.filter(
                    or_(ZigbeeProperties.linked_property.is_(None), ZigbeeProperties.linked_property == ""),
                ).filter(
                    or_(ZigbeeProperties.linked_method.is_(None), ZigbeeProperties.linked_method == ""),
                )
            if query:
                q = q.filter(_query_filter_properties(query))
            rows = q.order_by(ZigbeeProperties.title, ZigbeeProperties.id).limit(limit).all()
            return [_property_to_dict(row) for row in rows]
    raise ValueError(f"Unsupported collection: {collection}")


def mcp_get_entity(collection: str, entity_id) -> dict:
    with session_scope() as session:
        if collection == DEVICES:
            row = session.query(ZigbeeDevices).filter(ZigbeeDevices.id == int(entity_id)).one_or_none()
            if row is None:
                raise ValueError(f"Device not found: {entity_id}")
            return _device_to_dict(row)
        if collection == PROPERTIES:
            row = session.query(ZigbeeProperties).filter(ZigbeeProperties.id == int(entity_id)).one_or_none()
            if row is None:
                raise ValueError(f"Property not found: {entity_id}")
            return _property_to_dict(row)
    raise ValueError(f"Unsupported collection: {collection}")


def mcp_upsert_entity(collection: str, payload: dict, entity_id=None) -> dict:
    meta = _collection_meta(collection)
    if not meta.get("writable"):
        raise ValueError(f"Collection '{collection}' is read-only")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if meta.get("creatable") is False and entity_id in (None, ""):
        raise ValueError(
            f"Collection '{collection}' does not allow manual creation; "
            "entities are auto-managed from Zigbee2MQTT"
        )

    clean_payload = dict(payload)
    readonly_fields = _DEVICE_READONLY_FIELDS if collection == DEVICES else _PROPERTY_READONLY_FIELDS
    for field in readonly_fields:
        clean_payload.pop(field, None)

    validation = mcp_validate_entity(collection, clean_payload, entity_id=entity_id)
    if not validation.get("ok"):
        raise ValueError(f"validation failed: {validation}")

    if collection == DEVICES:
        merged = _merge_device_payload(clean_payload, entity_id=entity_id)
        with session_scope() as session:
            row = session.query(ZigbeeDevices).filter(ZigbeeDevices.id == int(entity_id)).one_or_none()
            if row is None:
                raise ValueError(f"Device not found: {entity_id}")
            if "description" in merged:
                row.description = merged.get("description")
            session.commit()
            return _device_to_dict(row)

    if collection == PROPERTIES:
        merged = _merge_property_payload(clean_payload, entity_id=entity_id)
        with session_scope() as session:
            row = _get_existing_property(session, entity_id=entity_id, payload=merged)
            old_object = row.linked_object
            old_property = row.linked_property

            if "converter" in merged and merged["converter"] is not None:
                row.converter = int(merged["converter"])
            if "min_period" in merged:
                v = merged["min_period"]
                row.min_period = int(v) if v not in (None, "") else None
            if "round" in merged:
                v = merged["round"]
                row.round = int(v) if v not in (None, "") else None
            if "read_only" in merged:
                row.read_only = 1 if merged.get("read_only") else 0
            if "process_type" in merged and merged["process_type"] is not None:
                row.process_type = int(merged["process_type"])
            if "linked_object" in merged:
                row.linked_object = str(merged.get("linked_object") or "").strip() or None
            if "linked_property" in merged:
                row.linked_property = str(merged.get("linked_property") or "").strip() or None
            if "linked_method" in merged:
                row.linked_method = str(merged.get("linked_method") or "").strip() or None

            session.commit()
            session.refresh(row)
            _sync_z2m_property_link(row, old_object=old_object, old_property=old_property)

            if row.title != "availability":
                cache.delete(f"z2m:prop_{row.device_id}_{row.title}")

            return _property_to_dict(row)

    raise ValueError(f"Unsupported collection: {collection}")


def mcp_delete_entity(collection: str, entity_id) -> bool:
    _collection_meta(collection)
    raise ValueError(
        f"Collection '{collection}' does not allow deletion of entity {entity_id}; "
        "entities are auto-managed from Zigbee2MQTT"
    )


def mcp_validate_entity_code(collection: str, code: str) -> dict:
    raise ValueError(f"Collection '{collection}' does not support code validation")


def mcp_run_entity_dry(collection: str, code: str, context: dict = None) -> dict:
    raise ValueError(f"Collection '{collection}' does not support dry-run code")


def mcp_invoke(operation: str, params: dict = None) -> dict:
    params = params or {}
    if operation == "set_property":
        prop_title = str(params.get("prop") or "").strip()
        if not prop_title:
            raise ValueError("prop is required")
        if "value" not in params:
            raise ValueError("value is required")
        instance = _plugin_instance()
        if instance is None:
            raise ValueError("z2m plugin not loaded")
        device_id = params.get("device_id")
        device_name = str(params.get("device") or "").strip()
        with session_scope() as session:
            if device_id not in (None, ""):
                device = session.query(ZigbeeDevices).filter(ZigbeeDevices.id == int(device_id)).one_or_none()
            elif device_name:
                device = session.query(ZigbeeDevices).filter(ZigbeeDevices.title == device_name).one_or_none()
            else:
                raise ValueError("device_id or device is required")
            if device is None:
                raise ValueError("Device not found")
            instance.set_payload(device.title, {prop_title: params.get("value")})
            return {
                "ok": True,
                "operation": operation,
                "device_id": device.id,
                "device": device.title,
                "prop": prop_title,
            }
    if operation == "get_property_value":
        device_id = params.get("device_id")
        prop_title = str(params.get("prop") or "").strip()
        if device_id in (None, "") or not prop_title:
            raise ValueError("device_id and prop are required")
        runtime = cache.get(f"z2m:prop_{int(device_id)}_{prop_title}") or {}
        return {
            "ok": True,
            "operation": operation,
            "device_id": int(device_id),
            "prop": prop_title,
            "value": runtime.get("value"),
            "converted": runtime.get("converted"),
            "updated": _format_dt(runtime.get("updated")),
        }
    if operation == "get_connection_status":
        instance = _plugin_instance()
        if instance is None:
            raise ValueError("z2m plugin not loaded")
        client = getattr(instance, "_client", None)
        mqtt_connected = (
            getattr(instance, "_mqtt_started", False)
            and client is not None
            and client.is_connected()
        )
        mqtt_configured = instance._is_connection_configured()
        return {
            "ok": True,
            "operation": operation,
            "connected": mqtt_connected,
            "configured": mqtt_configured,
            "host": (instance.config.get("host") or "").strip(),
            "topic": (instance.config.get("topic") or "").strip(),
        }
    if operation == "get_worker_status":
        instance = _plugin_instance()
        if instance is None:
            raise ValueError("z2m plugin not loaded")
        queue = getattr(instance, "_msg_queue", None)
        queue_size = queue.qsize() if queue is not None else 0
        queue_max = queue.maxsize if queue is not None else 0
        return {
            "ok": True,
            "operation": operation,
            "running": instance._worker_thread is not None and instance._worker_thread.is_alive(),
            "queue_size": queue_size,
            "queue_max": queue_max,
            "queue_percent": round((queue_size / queue_max * 100) if queue_max > 0 else 0, 1),
        }
    if operation == "reconnect":
        instance = _plugin_instance()
        if instance is None:
            raise ValueError("z2m plugin not loaded")
        instance._connect_mqtt()
        client = getattr(instance, "_client", None)
        connected = (
            getattr(instance, "_mqtt_started", False)
            and client is not None
            and client.is_connected()
        )
        return {
            "ok": connected,
            "operation": operation,
            "connected": connected,
            "configured": instance._is_connection_configured(),
        }
    if operation == "set_via_binding":
        object_name = str(params.get("object_name") or "").strip()
        property_name = str(params.get("property_name") or "").strip()
        if not object_name or not property_name:
            raise ValueError("object_name and property_name are required")
        if "value" not in params:
            raise ValueError("value is required")
        instance = _plugin_instance()
        if instance is None:
            raise ValueError("z2m plugin not loaded")
        instance.changeLinkedProperty(object_name, property_name, params.get("value"))
        return {
            "ok": True,
            "operation": operation,
            "object_name": object_name,
            "property_name": property_name,
        }
    raise ValueError(f"Unsupported operation: {operation}")


def mcp_descriptors() -> Tuple[list, list, list]:
    return build_plugin_mcp_descriptors(PLUGIN_NAME, mcp_capabilities())


def mcp_get_prompt(name: str, arguments: dict = None) -> dict:
    arguments = arguments or {}
    notes_block = "\n".join(f"- {note}" for note in _PLUGIN_NOTES)

    if name == _BINDING_PROMPT:
        object_name = str(arguments.get("object_name") or "").strip()
        property_name = str(arguments.get("property_name") or "").strip()
        device_id = arguments.get("device_id")
        prop_title = str(arguments.get("prop") or "").strip()
        prompt_text = (
            "Bind a Zigbee property to an osysHome object property via z2m.\n"
            f"Plugin: {PLUGIN_NAME}\n"
            f"Object: {object_name or '-'}\n"
            f"Property: {property_name or '-'}\n"
            f"Device id: {device_id or '-'}\n"
            f"Zigbee prop: {prop_title or '-'}\n\n"
            f"Plugin notes:\n{notes_block}\n\n"
            "Flow:\n"
            "1. osys_plugin_list_entities collection=properties device_id=<id>\n"
            "2. osys_plugin_entity_schema collection=properties\n"
            "3. osys_plugin_validate_entity then osys_plugin_upsert_entity on collection=properties\n"
            "4. osys_get_property to confirm linked contains z2m\n"
            "5. Use set_via_binding or osys_write_property to publish outbound MQTT\n"
            f"Converter reference: {_CONVERTER_DESC}\n"
        )
        return {"messages": [{"role": "user", "content": {"type": "text", "text": prompt_text}}]}

    if name == _ENTITY_AUTHORING_PROMPT:
        task = str(arguments.get("task") or "").strip()
        collection = str(arguments.get("collection") or PROPERTIES).strip()
        if not task:
            raise ValueError("task is required")
        prompt_text = (
            "Create or update z2m plugin entity payload by schema.\n"
            f"Plugin: {PLUGIN_NAME}\nCollection: {collection}\nTask: {task}\n\n"
            f"Plugin notes:\n{notes_block}\n\n"
            "Flow: osys_plugin_entity_schema -> validate_entity -> upsert_entity.\n"
            "Devices: only description is writable.\n"
            "Properties: linked_object, linked_property, linked_method, converter, "
            "min_period, round, read_only, process_type.\n"
            "Do not create or delete entities; they are auto-managed from Zigbee2MQTT.\n"
        )
        return {"messages": [{"role": "user", "content": {"type": "text", "text": prompt_text}}]}

    raise ValueError(f"Unsupported prompt: {name}")


def mcp_entity_revision(collection: str, entity_id) -> str:
    entity = mcp_get_entity(collection, entity_id)
    if collection == DEVICES:
        updated = revision_from_datetime(entity.get("updated"))
        if updated:
            return updated
        return revision_from_dict(
            entity,
            keys=["id", "title", "description", "ieeaddr", "availability", "battery_level"],
        )
    if collection == PROPERTIES:
        updated = revision_from_datetime(entity.get("updated"))
        if updated:
            return updated
        return revision_from_dict(
            entity,
            keys=[
                "id",
                "device_id",
                "title",
                "converter",
                "min_period",
                "round",
                "read_only",
                "process_type",
                "linked_object",
                "linked_property",
                "linked_method",
                "value",
                "converted",
            ],
        )
    raise ValueError(f"Unsupported collection: {collection}")


def mcp_validate_entity(collection: str, payload: dict, entity_id=None) -> dict:
    if not isinstance(payload, dict):
        return {"ok": False, "errors": [{"field": "_", "message": "payload must be an object"}]}

    meta = _collection_meta(collection)
    if meta.get("creatable") is False and entity_id in (None, ""):
        return {
            "ok": False,
            "errors": [{
                "field": "id",
                "message": "entity_id is required; entities are auto-managed from Zigbee2MQTT",
            }],
        }

    merged = (
        _merge_device_payload(payload, entity_id=entity_id)
        if collection == DEVICES
        else _merge_property_payload(payload, entity_id=entity_id)
        if collection == PROPERTIES
        else dict(payload)
    )
    schema = mcp_entity_schema(collection)
    result = validate_entity_payload(merged, schema)
    if not result.get("ok"):
        return result

    errors = list(result.get("errors") or [])
    warnings: List[dict] = []

    readonly_fields = _DEVICE_READONLY_FIELDS if collection == DEVICES else _PROPERTY_READONLY_FIELDS
    disallowed = [key for key in payload if key in readonly_fields]
    if disallowed:
        return {
            "ok": False,
            "errors": [{"field": disallowed[0], "message": "field is read-only"}],
        }

    if collection == DEVICES:
        allowed = set(_DEVICE_WRITABLE_FIELDS)
        extra = [key for key in payload if key not in allowed and key != "id"]
        if extra:
            errors.append({
                "field": extra[0],
                "message": f"only {', '.join(sorted(allowed))} may be updated on devices",
            })
        if entity_id not in (None, ""):
            with session_scope() as session:
                row = session.query(ZigbeeDevices).filter(ZigbeeDevices.id == int(entity_id)).one_or_none()
                if row is None:
                    errors.append({"field": "id", "message": f"device not found: {entity_id}"})

    if collection == PROPERTIES:
        allowed = set(_PROPERTY_WRITABLE_FIELDS)
        extra = [key for key in payload if key not in allowed and key not in ("id", "device_id", "title")]
        if extra:
            errors.append({
                "field": extra[0],
                "message": "field is read-only or managed by Zigbee2MQTT",
            })

        with session_scope() as session:
            try:
                _get_existing_property(session, entity_id=entity_id, payload=merged)
            except ValueError as ex:
                errors.append({"field": "id", "message": str(ex)})

        linked_object = str(merged.get("linked_object") or "").strip()
        linked_property = str(merged.get("linked_property") or "").strip()
        linked_method = str(merged.get("linked_method") or "").strip()

        if linked_property and not linked_object:
            errors.append({"field": "linked_object", "message": "required when linked_property is set"})
        if linked_method and not linked_object:
            errors.append({"field": "linked_object", "message": "required when linked_method is set"})

        if linked_object and not validate_object_exists(linked_object):
            errors.append({"field": "linked_object", "message": f"Object not found: {linked_object}"})

        if linked_object and linked_property:
            if not validate_object_property_exists(linked_object, linked_property):
                errors.append({
                    "field": "linked_property",
                    "message": f"Object property not found: {linked_object}.{linked_property}",
                })

        if linked_object and linked_method and not validate_object_method_exists(linked_object, linked_method):
            errors.append({
                "field": "linked_method",
                "message": f"Object method not found: {linked_object}.{linked_method}",
            })

        converter = merged.get("converter")
        if converter is not None:
            try:
                converter_value = int(converter)
                if converter_value < 0 or converter_value > 8:
                    errors.append({"field": "converter", "message": "must be between 0 and 8"})
            except (TypeError, ValueError):
                errors.append({"field": "converter", "message": "must be an integer"})

        process_type = merged.get("process_type")
        if process_type is not None and process_type not in (0, 1):
            errors.append({"field": "process_type", "message": "must be 0 or 1"})

    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}

    response = {"ok": True, "errors": []}
    if warnings:
        response["warnings"] = warnings
    return response
