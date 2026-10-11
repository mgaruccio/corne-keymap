/* SPDX-License-Identifier: MIT */
#include <stdint.h>
#include <zephyr/kernel.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/sys/byteorder.h>
#include <zmk/ble.h>
#include <zmk/event_manager.h>
#include <zmk/events/layer_state_changed.h>
#include <zmk/keymap.h>

#include "corne_layout.h"

static uint32_t active_layer_mask(void) {
    uint32_t mask = 0;

    /* The raw state can omit the default layer. Query ZMK's effective state. */
    for (uint8_t layer = 0; layer < CORNE_LAYER_COUNT; layer++) {
        if (zmk_keymap_layer_active(layer)) {
            mask |= UINT32_C(1) << layer;
        }
    }
    return mask;
}

static ssize_t read_layer_mask(struct bt_conn *conn, const struct bt_gatt_attr *attr, void *buf,
                               uint16_t len, uint16_t offset) {
    uint8_t value[sizeof(uint32_t)];

    sys_put_le32(active_layer_mask(), value);
    return bt_gatt_attr_read(conn, attr, buf, len, offset, value, sizeof(value));
}

BT_GATT_SERVICE_DEFINE(
    corne_layer_service, BT_GATT_PRIMARY_SERVICE(BT_UUID_DECLARE_128(CORNE_SERVICE_UUID)),
    BT_GATT_CHARACTERISTIC(BT_UUID_DECLARE_128(CORNE_LAYER_UUID),
                           BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY, BT_GATT_PERM_READ_ENCRYPT,
                           read_layer_mask, NULL, NULL),
    BT_GATT_CCC(NULL, BT_GATT_PERM_READ_ENCRYPT | BT_GATT_PERM_WRITE_ENCRYPT));

static void notify_layer_mask(struct k_work *work) {
    ARG_UNUSED(work);
    struct bt_conn *conn = zmk_ble_active_profile_conn();

    if (conn == NULL) {
        return;
    }

    /* Attribute 2 is the characteristic value (after service and declaration).
     * Never broadcast to other profiles or to the split peripheral. */
    const struct bt_gatt_attr *attr = &corne_layer_service.attrs[2];
    if (bt_gatt_is_subscribed(conn, attr, BT_GATT_CCC_NOTIFY)) {
        uint8_t value[sizeof(uint32_t)];

        sys_put_le32(active_layer_mask(), value);
        /* GATT also enforces the value's encryption permission. A disconnect or
         * exhausted TX buffers can fail this snapshot; a fresh read resyncs. */
        (void)bt_gatt_notify(conn, attr, value, sizeof(value));
    }
    bt_conn_unref(conn);
}

static K_WORK_DEFINE(layer_notify_work, notify_layer_mask);

static int layer_state_changed(const zmk_event_t *event) {
    ARG_UNUSED(event);
    /* Coalesce bursts to the current mask; keep BLE work out of event dispatch. */
    k_work_submit(&layer_notify_work);
    return ZMK_EV_EVENT_BUBBLE;
}

ZMK_LISTENER(corne_layer_telemetry, layer_state_changed);
ZMK_SUBSCRIPTION(corne_layer_telemetry, zmk_layer_state_changed);
