# z2m - User Guide

![z2m Icon](../static/z2m.png "z2m plugin")

## Purpose

`z2m` connects osysHome to Zigbee devices through Zigbee2MQTT and an MQTT broker.

The module is designed to:

- subscribe to Zigbee2MQTT topics;
- discover and update devices automatically;
- keep property values in runtime cache with live UI updates;
- link Zigbee properties to osysHome object properties and methods;
- send control payloads from osysHome to Zigbee devices.

> [!IMPORTANT]
> Integration is bidirectional: Zigbee2MQTT -> osysHome and osysHome -> Zigbee2MQTT.

---

## What You Get

| Capability | What it does |
| --- | --- |
| MQTT bridge | Connects to configured broker and topics |
| Auto discovery | Creates device records from incoming Zigbee2MQTT traffic |
| Device editor | Lets you configure links and property processing |
| Live dashboard | Shows MQTT status, worker status, online/offline, battery |
| Property push | Sends `/set` payloads to device topics |
| Search integration | Finds devices and linked properties via global search |

---

## Interface Overview

Admin page:

```text
/admin/z2m
```

### Labels from module localization (`plugins/z2m/translations/en.json`)

The interface description below uses exact labels from the module translation file:

- `Zigbee2mqtt`
- `MQTT`
- `Worker`
- `Topics`
- `Set value`
- `Send`
- `Filter by title, description, model...`
- `Show cards` / `Hide cards`
- `Not configured`
- `Hub`
- `Data`
- `Model name`
- `Model description`
- `Vendor`
- `Full path`
- `Converter`
- `Update value`
- `Round`
- `Minimal period (ms)`

> [!NOTE]
> Buttons such as `Settings`, `Refresh`, `Edit`, `Delete`, `Save`, `Cancel`, `Close` are rendered through shared platform localization, not the module translation file.

### Main page sections

| Section | Description |
| --- | --- |
| Toolbar | `Zigbee2mqtt`, search (`Filter by title, description, model...`), status filter, `Topics` in settings modal |
| Status cards | `MQTT`, `Worker`, plus show/hide cards (`Show cards` / `Hide cards`) |
| Devices table | `Data`, `Model name`, `Model description`, `Vendor`, online/offline and battery indicators |
| Device editor | `Full path`, `Set value`, `Converter`, `Update value`, `Round`, `Minimal period (ms)` |

> [!TIP]
> In `Topics`, you can provide multiple comma-separated subscriptions.

---

## Quick Start Checklist

- [ ] Make sure Zigbee2MQTT works and publishes to your MQTT broker.
- [ ] Open `/admin/z2m`.
- [ ] Open `Settings`.
- [ ] Fill in `Host`, `Port`, and `Topics`.
- [ ] Save settings.
- [ ] Wait for MQTT status to become `Connected`.
- [ ] Open a device and link needed properties to osysHome objects.

---

## MQTT Settings

Configure these fields in the modal:

| Field | Required | Description |
| --- | --- | --- |
| `Host` | Yes | MQTT broker hostname or IP |
| `Port` | Yes | Broker port, default `1883` |
| `Username` | No | Broker username |
| `Password` | No | Broker password |
| `Topics` | Yes | Subscription topic(s), comma-separated |

Example:

```text
zigbee2mqtt/#
```

or:

```text
zigbee2mqtt/#,custom_z2m/#
```

---

## Device Workflow

### 1. Discovery

Devices are created or updated automatically from incoming MQTT messages, including bridge announcements.

### 2. Open device editor

Click `Edit` in the device table. You can:

- change `Description`;
- inspect metadata (`IEEADDR`, model, vendor, full path);
- configure links for each property;
- set converter and processing options;
- send test value with `Set value`.

### 3. Save

`Save` updates links and processing rules for the device properties.

---

## Linking to osysHome Objects

Each Zigbee property can be linked to:

- `Linked object` + `Linked property`
- `Linked object` + `Linked method`

Examples:

```text
Climate.outdoor_temp
```

```text
Alarm.motionDetected
```

When new values arrive:

- linked property is updated (`update` or `set`, based on processing mode);
- linked method is called with value context.

---

## Converters and Processing Options

### Converter types

| Converter | Meaning |
| --- | --- |
| `0` (`Default`) | Auto map common bool-like values (`on/off`, `true/false`, `open/close`) |
| `1` (`No convert`) | Keep value as is |
| `2` | `online/offline -> 1/0` |
| `3` | `ColorXY <-> Hex RGB` |
| `4` | `DateTime -> timestamp` |
| `5` | `Value <-> %` (0..254 <-> 0..100) |
| `6` | `ON/OFF -> 1/0` |
| `7` | `OPEN/CLOSE -> 1/0` |
| `8` | `LOCK/UNLOCK -> 1/0` |

### Other options

| Option | What it does |
| --- | --- |
| `Readonly` | Prevents reverse link write registration |
| `Update value` = `Only new` | Send updates only when value changed |
| `Update value` = `All` | Send updates even for same value |
| `Round` | Rounds numeric values before processing |
| `Minimal period (ms)` | Throttles processing for frequent updates (comparison is done in milliseconds) |

---

## Sending Values to Device

From device editor, use `Set value` for a property.

The module sends payload to:

```text
<device_full_path>/set
```

If cached availability is `offline`, module first sends a wake/read request:

```text
<device_full_path>/get
```

then sends `/set`.

---

## Widget

`z2m` provides a dashboard widget with:

- total device count;
- low battery count;
- offline count.

Widget template:

```text
plugins/z2m/templates/widget_z2m.html
```

---

## Troubleshooting

### MQTT stays disconnected

Check:

- broker host and port;
- credentials;
- topic string is not empty;
- network reachability from osysHome server.

### Devices not appearing

Check:

- Zigbee2MQTT publishes into subscribed topics;
- topic mask includes device messages;
- worker queue is running (status card).

### Property links do nothing

Check:

- link points to existing object property or method;
- converter does not break expected value format;
- `Readonly`/`Update value` behavior matches your scenario.

---

## See Also

- [Technical Reference](TECHNICAL_REFERENCE.md)
- [Module index](index.md)
