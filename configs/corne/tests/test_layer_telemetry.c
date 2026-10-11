/* Exercise the production callbacks with fake ZMK/GATT/workqueue boundaries. */
#include "mock_zmk.h"
#include "../telemetry/src/layer_telemetry.c"

static uint32_t raw_state;
static uint8_t default_layer;
static uint32_t queried_layers;
static struct bt_conn profiles[] = {{.profile = 0}, {.profile = 1}};
static struct bt_conn *active_conn = &profiles[0];
static bool subscribed;
static unsigned int lookups, releases, notifications;
static int notify_result;
static uint8_t notified[4];
static struct k_work *pending;
static bool in_event;

static bool zmk_keymap_layer_active(uint8_t layer) {
    assert(layer < 3);
    queried_layers |= 1U << layer;
    return layer == default_layer || (raw_state & (1U << layer));
}

static struct bt_conn *zmk_ble_active_profile_conn(void) {
    assert(!in_event);
    lookups++;
    return active_conn;
}

static void bt_conn_unref(struct bt_conn *conn) {
    assert(conn != NULL && conn == active_conn);
    releases++;
}

static bool bt_gatt_is_subscribed(struct bt_conn *conn, const struct bt_gatt_attr *attr, uint16_t ccc) {
    assert(conn != NULL && conn == active_conn);
    assert(attr == &corne_layer_service.attrs[2]);
    assert(ccc == BT_GATT_CCC_NOTIFY);
    return subscribed;
}

static int bt_gatt_notify(struct bt_conn *conn, const struct bt_gatt_attr *attr, const void *value,
                          uint16_t len) {
    assert(!in_event);
    assert(conn != NULL && conn == active_conn);
    assert(attr == &corne_layer_service.attrs[2]);
    assert(len == sizeof(notified));
    memcpy(notified, value, len);
    notifications++;
    return notify_result;
}

static ssize_t bt_gatt_attr_read(struct bt_conn *conn, const struct bt_gatt_attr *attr, void *buf,
                                 uint16_t len, uint16_t offset, const void *value, uint16_t value_len) {
    assert(conn == active_conn);
    assert(attr == &corne_layer_service.attrs[2]);
    assert(value_len == 4);
    if (offset > value_len) {
        return -7; /* BT_ATT_ERR_INVALID_OFFSET */
    }
    uint16_t count = value_len - offset;
    if (count > len) {
        count = len;
    }
    memcpy(buf, (const uint8_t *)value + offset, count);
    return count;
}

static int k_work_submit(struct k_work *work) {
    pending = work;
    return 1;
}

static void event(void) {
    unsigned int before = lookups;
    in_event = true;
    assert(corne_layer_telemetry(NULL) == ZMK_EV_EVENT_BUBBLE);
    in_event = false;
    assert(lookups == before); /* No BLE calls from ZMK event dispatch. */
    assert(pending != NULL);
}

static void run_work(void) {
    struct k_work *work = pending;
    assert(work != NULL);
    pending = NULL;
    work->handler(work);
}

int main(void) {
    const struct bt_gatt_attr *attrs = corne_layer_service.attrs;
    const uint64_t service_uuid[] = {0x32dcce45, 0xae11, 0x451c, 0x8b48, 0x7a3f083f6afe};
    const uint64_t layer_uuid[] = {0x32dcce45, 0xae11, 0x451c, 0x8b48, 0x7a3f083f6aff};
    assert(sizeof(corne_layer_service_attrs) / sizeof(*attrs) == 4);
    assert(memcmp(attrs[0].uuid->parts, service_uuid, sizeof(service_uuid)) == 0);
    assert(memcmp(attrs[2].uuid->parts, layer_uuid, sizeof(layer_uuid)) == 0);
    assert(attrs[1].properties == (BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY));
    assert(attrs[2].permissions == BT_GATT_PERM_READ_ENCRYPT);
    assert(attrs[2].write == NULL);
    assert(attrs[3].permissions == (BT_GATT_PERM_READ_ENCRYPT | BT_GATT_PERM_WRITE_ENCRYPT));
    assert(strcmp(subscribed_event, "zmk_layer_state_changed") == 0);

    uint8_t buf[8];
    for (default_layer = 0; default_layer < 3; default_layer++) {
        for (raw_state = 0; raw_state < 8; raw_state++) {
            uint8_t expected = raw_state | (1U << default_layer);
            queried_layers = 0;
            memset(buf, 0xff, sizeof(buf));
            assert(attrs[2].read(active_conn, &attrs[2], buf, sizeof(buf), 0) == 4);
            assert(buf[0] == expected && buf[1] == 0 && buf[2] == 0 && buf[3] == 0);
            assert(buf[4] == 0xff && queried_layers == 7);
        }
    }
    default_layer = 0;
    raw_state = 6;
    assert(attrs[2].read(active_conn, &attrs[2], buf, 1, 0) == 1 && buf[0] == 7);
    assert(attrs[2].read(active_conn, &attrs[2], buf, sizeof(buf), 1) == 3);
    assert(buf[0] == 0 && buf[1] == 0 && buf[2] == 0);
    assert(attrs[2].read(active_conn, &attrs[2], buf, sizeof(buf), 4) == 0);
    assert(attrs[2].read(active_conn, &attrs[2], buf, sizeof(buf), 5) == -7);
    raw_state = 0; /* Fresh reads do not depend on a previous event or cached notification. */
    assert(attrs[2].read(active_conn, &attrs[2], buf, sizeof(buf), 0) == 4 && buf[0] == 1);

    active_conn = NULL;
    event();
    run_work();
    assert(notifications == 0 && releases == 0);
    active_conn = &profiles[0];
    subscribed = false;
    event();
    run_work();
    assert(notifications == 0 && releases == 1);

    subscribed = true;
    raw_state = 2;
    event();
    raw_state = 4;
    event();
    active_conn = &profiles[1]; /* Resolve the active host in the worker, not the callback. */
    run_work();
    assert(notifications == 1 && releases == 2);
    assert(memcmp(notified, (uint8_t[]){5, 0, 0, 0}, 4) == 0);

    notify_result = -11; /* Failed notifications must still release the connection. */
    raw_state = 6;
    event();
    run_work();
    assert(notifications == 2 && releases == 3);
    assert(memcmp(notified, (uint8_t[]){7, 0, 0, 0}, 4) == 0);
    raw_state = 0;
    event();
    run_work();
    assert(notifications == 3 && releases == 4);
    assert(memcmp(notified, (uint8_t[]){1, 0, 0, 0}, 4) == 0);
    return 0;
}
