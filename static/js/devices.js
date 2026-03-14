new Vue({
    el: '#z2m_devices',
    delimiters: ['[[', ']]'],
    data: {
        devices: [],
        loadingDevices: true,
        mqttConnected: false,
        mqttConfigured: false,
        settings: { host: '', port: 1883, topic: '', login: '', password: '' },
        workerStatus: { running: false, queue_size: 0, queue_max: 0 },
        filterSearch: '',
        filterStatus: '',
        cardsVisible: true,
        sortColumn: 'title',
        sortOrder: 'asc',
        i18n: {
            connected: 'Connected',
            disconnected: 'Disconnected',
            notConfigured: 'Not configured',
            online: 'Online',
            offline: 'Offline',
            n_a: 'n/a',
            hub: 'Hub',
            battery: 'Battery',
            ac: 'AC',
            filterPlaceholder: 'Filter by title, description, model...',
            loading: 'Loading...'
        }
    },
    computed: {
        filteredDevices() {
            var search = (this.filterSearch || '').toLowerCase();
            var statusFilter = this.filterStatus || '';
            return this.devices.filter(function(dev) {
                var title = (dev.title || '').toLowerCase();
                var description = (dev.description || '').toLowerCase();
                var model = ((dev.model_name || dev.model || '') + '').toLowerCase();
                var availability = (dev.availability || 'n/a').toLowerCase();
                var matchSearch = !search || title.indexOf(search) >= 0 ||
                    description.indexOf(search) >= 0 || model.indexOf(search) >= 0;
                var matchStatus = true;
                if (statusFilter === 'battery') {
                    if (!(dev.is_battery == 1 || dev.is_battery === true)) matchStatus = false;
                    else {
                        var bl = dev.battery_level;
                        var p = (bl != null) ? ((bl <= 100) ? bl : Math.round(bl / 254 * 100)) : null;
                        matchStatus = p != null && p > 0 && p <= 30;
                    }
                } else if (statusFilter) {
                    matchStatus = availability === statusFilter;
                }
                return matchSearch && matchStatus;
            });
        },
        stats() {
            var total = this.devices.length;
            var online = 0, offline = 0, batteryCount = 0, lowBattery = 0, hubs = 0;
            this.devices.forEach(function(dev) {
                if (dev.is_hub === 1 || dev.title === 'Coordinator') {
                    hubs++;
                } else {
                    var av = (dev.availability || '').toLowerCase();
                    if (av === 'online') online++;
                    else if (av === 'offline') offline++;
                    if (dev.is_battery == 1 || dev.is_battery === true) {
                        var l = dev.battery_level;
                        var pct = (l != null) ? ((l <= 100) ? l : Math.round(l / 254 * 100)) : null;
                        if (pct != null && pct > 0 && pct <= 30) { batteryCount++; lowBattery++; }
                    }
                }
            });
            return { total: total, online: online, offline: offline, batteryCount: batteryCount, lowBattery: lowBattery, hubs: hubs };
        },
        sortedDevices() {
            var list = this.filteredDevices.slice();
            var col = this.sortColumn;
            var asc = this.sortOrder === 'asc';
            list.sort(function(a, b) {
                var va, vb;
                if (col === 'description') { va = (a.description || ''); vb = (b.description || ''); }
                else if (col === 'title') { va = (a.title || ''); vb = (b.title || ''); }
                else if (col === 'model') { va = (a.model_name || a.model || ''); vb = (b.model_name || b.model || ''); }
                else if (col === 'type') {
                    var ta = (a.is_hub === 1 ? 2 : (a.is_battery ? 1 : 0));
                    var tb = (b.is_hub === 1 ? 2 : (b.is_battery ? 1 : 0));
                    if (ta !== tb) { va = ta; vb = tb; }
                    else { va = (a.battery_level != null ? a.battery_level : -1); vb = (b.battery_level != null ? b.battery_level : -1); }
                }
                else if (col === 'availability') { va = (a.availability || ''); vb = (b.availability || ''); }
                else if (col === 'updated') { va = a.updated || ''; vb = b.updated || ''; }
                else return 0;
                if (typeof va === 'string') { va = va.toLowerCase(); vb = (vb || '').toLowerCase(); }
                var cmp = va < vb ? -1 : (va > vb ? 1 : 0);
                return asc ? cmp : -cmp;
            });
            return list;
        }
    },
    created() {
        this.mqttConnected = window.z2mInitialData.mqtt_connected || false;
        this.mqttConfigured = window.z2mInitialData.mqtt_configured || false;
        this.settings = window.z2mInitialData.settings || this.settings;
        this.workerStatus = window.z2mInitialData.worker_status || this.workerStatus;
        this.i18n = window.z2mInitialData.i18n || this.i18n;
        try {
            var stored = localStorage.getItem('z2m_cardsVisible');
            if (stored !== null) this.cardsVisible = stored === 'true';
        } catch (e) {}
        this.connectSocket();
        this.fetchDevices();
    },
    watch: {
        cardsVisible: function(val) {
            try { localStorage.setItem('z2m_cardsVisible', val); } catch (e) {}
        }
    },
    beforeDestroy() {
        if (this._visibilityHandler) {
            document.removeEventListener('visibilitychange', this._visibilityHandler);
        }
        if (typeof socket !== 'undefined') {
            socket.emit('unsubscribeData', ['z2m']);
        }
    },
    methods: {
        connectSocket() {
            if (typeof socket === 'undefined') return;
            var vm = this;
            function setSubscribed(visible) {
                if (typeof socket === 'undefined') return;
                if (visible) socket.emit('subscribeData', ['z2m']);
                else socket.emit('unsubscribeData', ['z2m']);
            }
            vm._visibilityHandler = function() { setSubscribed(!document.hidden); };
            setSubscribed(!document.hidden);
            document.addEventListener('visibilitychange', vm._visibilityHandler);
            socket.on('z2m', (data) => {
                if (data.operation === 'connectionStatus') {
                    this.mqttConnected = data.data.connected;
                    this.mqttConfigured = data.data.configured !== false;
                }
                if (data.operation === 'workerStatus') {
                    this.workerStatus = data.data || this.workerStatus;
                }
                if (data.operation === 'updateProperty') {
                    var prop = data.data || {};
                    var targetDeviceId = prop.device_id;
                    var found = false;
                    for (var i = 0; i < this.devices.length; i++) {
                        var dev = this.devices[i];
                        if (targetDeviceId != null && dev.id !== targetDeviceId) continue;
                        if (targetDeviceId == null && prop.title === 'availability') return; // без device_id не обновляем availability
                        if (prop.title === 'availability') {
                            if (prop.value !== undefined && prop.value !== null && String(prop.value).trim() !== '') {
                                this.$set(dev, 'availability', prop.value);
                            }
                            return;
                        }
                        if (!dev.data) dev.data = [];
                        for (var j = 0; j < dev.data.length; j++) {
                            if (dev.data[j].id === prop.id) {
                                this.$set(dev.data[j], 'value', prop.value);
                                this.$set(dev.data[j], 'converted', prop.converted);
                                found = true;
                                break;
                            }
                        }
                        if (!found && prop.linked_object) {
                            dev.data.push(Object.assign({}, prop));
                            found = true;
                        }
                        if (found) break;
                    }
                }
                if (data.operation === 'updateDevice') {
                    var d = data.data;
                    for (var k = 0; k < this.devices.length; k++) {
                        if (this.devices[k].id === d.id) {
                            this.$set(this.devices[k], 'updated', d.updated);
                            // Обновлять availability только при валидном значении — не затирать на н/д
                            var av = d.availability;
                            if (av !== undefined && av !== null && String(av).trim() !== '') {
                                this.$set(this.devices[k], 'availability', av);
                            }
                            return;
                        }
                    }
                }
            });
        },
        openSettings() {
            var modalEl = document.getElementById('settingsModal');
            if (modalEl) (new bootstrap.Modal(modalEl)).show();
        },
        async saveSettings() {
            try {
                await axios.post('/z2m/api/settings', this.settings);
                var modalEl = document.getElementById('settingsModal');
                if (modalEl) bootstrap.Modal.getInstance(modalEl).hide();
            } catch (e) {
                console.error('Save settings error:', e);
            }
        },
        async fetchDevices() {
            this.loadingDevices = true;
            try {
                var r = await axios.get('/z2m/api/devices');
                var newDevices = r.data;
                // Если API вернул availability: null (кэш пуст — другой worker / рестарт), сохраняем
                // уже известный клиенту статус, иначе столбец «В сети» станет н/д
                var oldById = {};
                this.devices.forEach(function(d) { oldById[d.id] = d; });
                newDevices.forEach(function(d) {
                    var old = oldById[d.id];
                    if (old && (d.availability === undefined || d.availability === null || d.availability === '') && (old.availability === 'online' || old.availability === 'offline')) {
                        d.availability = old.availability;
                    }
                });
                this.devices = newDevices;
            } catch (e) {
                console.error('Fetch devices error:', e);
            } finally {
                this.loadingDevices = false;
            }
        },
        setSort(column) {
            if (this.sortColumn === column) {
                this.sortOrder = this.sortOrder === 'asc' ? 'desc' : 'asc';
            } else {
                this.sortColumn = column;
                this.sortOrder = 'asc';
            }
        },
        sortIcon(column) {
            if (this.sortColumn !== column) return '';
            return this.sortOrder === 'asc' ? '▲' : '▼';
        },
        getQueueBadgeClass() {
            var percent = this.workerStatus.queue_max > 0 
                ? (this.workerStatus.queue_size / this.workerStatus.queue_max * 100) 
                : 0;
            if (percent >= 80) return 'bg-danger';
            if (percent >= 50) return 'bg-warning';
            return 'bg-secondary';
        },
        getBatteryBadgeClass(device) {
            var l = device.battery_level;
            if (l == null) return 'bg-secondary';
            var pct = (l <= 100) ? l : Math.round(l / 254 * 100);
            if (pct >= 60) return 'bg-success';
            if (pct >= 30) return 'bg-warning text-dark';
            return 'bg-danger';
        },
        getBatteryIcon(device) {
            var l = device.battery_level;
            if (l == null) return 'fa-battery-empty';
            var pct = (l <= 100) ? l : Math.round(l / 254 * 100);
            if (pct <= 10) return 'fa-battery-empty';
            if (pct <= 25) return 'fa-battery-quarter';
            if (pct <= 50) return 'fa-battery-half';
            if (pct <= 75) return 'fa-battery-three-quarters';
            return 'fa-battery-full';
        },
        deleteDevice(device) {
            if (confirm('Are you sure? Please confirm.')) {
                location.href = '?op=delete&id=' + device.id;
            }
        },
        applyFilterCard(filterValue) {
            this.filterStatus = this.filterStatus === filterValue ? '' : filterValue;
        }
    }
});
