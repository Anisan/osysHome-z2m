from app.database import Column, Model, SurrogatePK, db

class ZigbeeDevices(SurrogatePK, db.Model):
    __tablename__ = 'zigbeedevices'
    title = Column(db.String(100))
    ieeaddr = Column(db.String(255))
    description = Column(db.String(255))
    is_hub = Column(db.Integer)
    is_battery = Column(db.Integer)
    battery_level = Column(db.Integer)
    full_path = Column(db.String(255))
    updated = Column(db.DateTime)
    manufacturer_id = Column(db.String(255))
    model = Column(db.String(255))
    model_name = Column(db.String(255))
    model_description = Column(db.String(255))
    vendor = Column(db.String(255))

class ZigbeeProperties(SurrogatePK, db.Model):
    __tablename__ = 'zigbeeproperties'
    device_id = Column(db.Integer)
    title = Column(db.String(100))
    value = Column(db.String(255))
    converted = Column(db.String(255))
    converter = Column(db.Integer)
    min_period = Column(db.Integer)
    round = Column(db.Integer)
    read_only = Column(db.Integer)
    process_type = Column(db.Integer)
    linked_object = Column(db.String(255))
    linked_property = Column(db.String(255))
    linked_method = Column(db.String(255))
    updated = Column(db.DateTime)

