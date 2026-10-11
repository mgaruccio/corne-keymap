/* Minimal host test doubles; the real module is also built against pinned ZMK. */
#pragma once
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <sys/types.h>

#define ARG_UNUSED(value) (void)(value)
#define BT_GATT_CHRC_READ 0x02
#define BT_GATT_CHRC_NOTIFY 0x10
#define BT_GATT_PERM_READ_ENCRYPT 0x04
#define BT_GATT_PERM_WRITE_ENCRYPT 0x08
#define BT_GATT_CCC_NOTIFY 1
#define ZMK_EV_EVENT_BUBBLE 0

typedef int zmk_event_t;
struct bt_conn { int profile; };
struct mock_uuid { uint64_t parts[5]; };
struct bt_gatt_attr {
    const struct mock_uuid *uuid;
    int properties;
    int permissions;
    ssize_t (*read)(struct bt_conn *, const struct bt_gatt_attr *, void *, uint16_t, uint16_t);
    void *write;
};
struct bt_gatt_service_static { const struct bt_gatt_attr *attrs; };
struct k_work { void (*handler)(struct k_work *); };

#define BT_UUID_128_ENCODE(a, b, c, d, e) {a, b, c, d, e}
#define BT_UUID_DECLARE_128(...) (&(const struct mock_uuid){.parts = __VA_ARGS__})
#define BT_GATT_PRIMARY_SERVICE(id) {.uuid = id}
#define BT_GATT_CHARACTERISTIC(id, props, perms, reader, writer, data) \
    {.properties = props}, {.uuid = id, .permissions = perms, .read = reader, .write = writer}
#define BT_GATT_CCC(callback, perms) {.permissions = perms}
#define BT_GATT_SERVICE_DEFINE(name, ...) \
    static const struct bt_gatt_attr name##_attrs[] = {__VA_ARGS__}; \
    static const struct bt_gatt_service_static name = {.attrs = name##_attrs}
#define K_WORK_DEFINE(name, callback) struct k_work name = {.handler = callback}
#define ZMK_LISTENER(name, callback) static int (*name)(const zmk_event_t *) = callback
#define ZMK_SUBSCRIPTION(name, event) static const char *subscribed_event = #event

static bool zmk_keymap_layer_active(uint8_t layer);
static struct bt_conn *zmk_ble_active_profile_conn(void);
static void bt_conn_unref(struct bt_conn *conn);
static bool bt_gatt_is_subscribed(struct bt_conn *conn, const struct bt_gatt_attr *attr, uint16_t ccc);
static int bt_gatt_notify(struct bt_conn *conn, const struct bt_gatt_attr *attr, const void *value,
                          uint16_t len);
static ssize_t bt_gatt_attr_read(struct bt_conn *conn, const struct bt_gatt_attr *attr, void *buf,
                                 uint16_t len, uint16_t offset, const void *value, uint16_t value_len);
static int k_work_submit(struct k_work *work);

static inline void sys_put_le32(uint32_t value, uint8_t *buf) {
    for (unsigned int i = 0; i < 4; i++) {
        buf[i] = (uint8_t)(value >> (8 * i));
    }
}
