# z2m - Zigbee2MQTT Integration

![z2m Icon](static/z2m.png)

MQTT-based integration with Zigbee2MQTT bridge for managing Zigbee devices through MQTT protocol.

## Description

The `z2m` module provides integration with Zigbee2MQTT bridge for the osysHome platform. It enables discovery, control, and monitoring of Zigbee devices via MQTT protocol.

## Main Features

- ✅ **MQTT Integration**: MQTT-based device communication
- ✅ **Device Discovery**: Automatic Zigbee device discovery
- ✅ **Property Management**: Manage device properties
- ✅ **Property Linking**: Link device properties to object properties
- ✅ **Battery Monitoring**: Monitor device battery levels
- ✅ **Availability Tracking**: Track device availability
- ✅ **Search Integration**: Search devices and properties
- ✅ **Widget Support**: Dashboard widget with device statistics

## Admin Panel

The module provides an admin interface for:
- Viewing Zigbee devices
- Configuring device properties
- Linking properties to objects
- Monitoring device status

## Configuration

- **MQTT Broker**: MQTT broker connection settings
- **Topic**: MQTT topic prefix (default: zigbee2mqtt)
- **Device Management**: Configure device properties and links

## Usage

### Adding Zigbee Device

1. Ensure Zigbee2MQTT is running and connected to MQTT broker
2. Navigate to z2m module
3. Devices discovered automatically via MQTT
4. Configure device properties
5. Link properties to object properties

## Technical Details

- **Protocol**: MQTT
- **Bridge**: Zigbee2MQTT
- **Device Types**: All Zigbee device types supported by Zigbee2MQTT
- **Property Mapping**: Automatic property mapping from Zigbee2MQTT

## Version

Current version: **1.0**

## Category

Devices

## Actions

The module provides the following actions:
- `cycle` - Background MQTT communication
- `search` - Search devices and properties
- `widget` - Dashboard widget

## Requirements

- Flask
- paho-mqtt
- SQLAlchemy
- Zigbee2MQTT bridge
- osysHome core system

## Author

osysHome Team

## License

See the main osysHome project license

