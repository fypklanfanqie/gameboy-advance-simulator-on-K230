#ifndef TEST_DRV_TOUCH_H
#define TEST_DRV_TOUCH_H
#include <stdint.h>
#define DRV_TOUCH_POINT_NUMBER_MAX 10
#define DRV_TOUCH_EVENT_NONE 0
#define DRV_TOUCH_EVENT_UP 1
#define DRV_TOUCH_EVENT_DOWN 2
#define DRV_TOUCH_EVENT_MOVE 3
struct drv_touch_data {
    uint8_t event, track_id, width;
    uint16_t x_coordinate, y_coordinate;
    uint32_t timestamp;
};
typedef struct { int fd; } drv_touch_inst_t;
int drv_touch_read(drv_touch_inst_t *, struct drv_touch_data *, int);
#endif
