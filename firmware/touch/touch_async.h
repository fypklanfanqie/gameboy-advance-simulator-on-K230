#ifndef CANMV_TOUCH_ASYNC_H
#define CANMV_TOUCH_ASYNC_H

#include "drv_touch.h"

/* Native worker state is outside the MicroPython heap and owns no Python data.
 * The caller must stop/join it before destroying the driver instance. */
typedef struct touch_async_state touch_async_state_t;

int touch_async_start(drv_touch_inst_t *inst, touch_async_state_t **state);
int touch_async_snapshot(touch_async_state_t *state, struct drv_touch_data *points, int max_points);
int touch_async_stop(touch_async_state_t **state);

#endif
