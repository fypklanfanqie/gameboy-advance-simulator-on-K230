/* Optional native touch reader: keep the vendor driver's bounded wait outside
 * the interpreter thread. No Python API, GC allocation, or object access is
 * permitted on this worker. */
#include "touch_async.h"

#include <errno.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

struct touch_async_state {
    drv_touch_inst_t *inst;
    pthread_t thread;
    pthread_mutex_t mutex;
    int stop;
    int count;
    struct drv_touch_data points[DRV_TOUCH_POINT_NUMBER_MAX];
    int pending_count;
    struct drv_touch_data pending[DRV_TOUCH_POINT_NUMBER_MAX];
    int previous_count;
    struct drv_touch_data previous[DRV_TOUCH_POINT_NUMBER_MAX];
};

static int find_track(const struct drv_touch_data *points, int count, unsigned track, int active_only)
{
    for (int i = 0; i < count; ++i) {
        if (points[i].track_id == track
            && (!active_only || points[i].event == DRV_TOUCH_EVENT_DOWN || points[i].event == DRV_TOUCH_EVENT_MOVE)) {
            return i;
        }
    }
    return -1;
}

/* All cache operations below run under the state mutex. Pending press events
 * form a bounded FIFO and survive an UP/empty/error until consumed. Reused
 * track IDs may have several sessions queued with distinct original coordinates.
 * Repeated reports for a held track do not create extra press edges. */
static void publish(touch_async_state_t *state, const struct drv_touch_data *points, int count)
{
    for (int i = 0; i < count; ++i) {
        if (points[i].event == DRV_TOUCH_EVENT_DOWN
            && find_track(state->points, state->count, points[i].track_id, 1) < 0
            && state->pending_count < DRV_TOUCH_POINT_NUMBER_MAX) {
            state->pending[state->pending_count++] = points[i];
        }
    }
    state->count = count;
    if (count > 0) {
        memcpy(state->points, points, count * sizeof(points[0]));
    }
}

static void consume_pending(touch_async_state_t *state, int index)
{
    --state->pending_count;
    memmove(state->pending + index, state->pending + index + 1,
            (state->pending_count - index) * sizeof(state->pending[0]));
}

static void *touch_async_worker(void *arg)
{
    touch_async_state_t *state = arg;
    struct drv_touch_data points[DRV_TOUCH_POINT_NUMBER_MAX];
    for (;;) {
        pthread_mutex_lock(&state->mutex);
        int stop = state->stop;
        pthread_mutex_unlock(&state->mutex);
        if (stop) {
            break;
        }

        int count = drv_touch_read(state->inst, points, DRV_TOUCH_POINT_NUMBER_MAX);
        if (count < 0 || count > DRV_TOUCH_POINT_NUMBER_MAX) {
            count = 0;
        }
        pthread_mutex_lock(&state->mutex);
        publish(state, points, count);
        pthread_mutex_unlock(&state->mutex);

        /* The normal vendor read waits up to 33 ms. Limit retry rate if another
         * driver returns empty/error immediately. */
        if (count == 0) {
            usleep(1000);
        }
    }
    return NULL;
}

int touch_async_start(drv_touch_inst_t *inst, touch_async_state_t **out)
{
    if (inst == NULL || out == NULL) {
        return EINVAL;
    }
    if (*out != NULL) {
        return 0;
    }
    touch_async_state_t *state = calloc(1, sizeof(*state));
    if (state == NULL) {
        return ENOMEM;
    }
    state->inst = inst;
    int rc = pthread_mutex_init(&state->mutex, NULL);
    if (rc != 0) {
        free(state);
        return rc;
    }
    pthread_attr_t attr;
    rc = pthread_attr_init(&attr);
    if (rc != 0) {
        pthread_mutex_destroy(&state->mutex);
        free(state);
        return rc;
    }
    size_t stack_size = 16384;
#ifdef PTHREAD_STACK_MIN
    if (stack_size < PTHREAD_STACK_MIN) {
        stack_size = PTHREAD_STACK_MIN;
    }
#endif
    rc = pthread_attr_setstacksize(&attr, stack_size);
    if (rc == 0) {
        rc = pthread_create(&state->thread, &attr, touch_async_worker, state);
    }
    pthread_attr_destroy(&attr);
    if (rc != 0) {
        pthread_mutex_destroy(&state->mutex);
        free(state);
        return rc;
    }
    *out = state;
    return 0;
}

int touch_async_snapshot(touch_async_state_t *state, struct drv_touch_data *points, int max_points)
{
    if (state == NULL || points == NULL || max_points <= 0 || max_points > DRV_TOUCH_POINT_NUMBER_MAX) {
        return -1;
    }
    pthread_mutex_lock(&state->mutex);
    int count = 0;
    struct drv_touch_data blocked[DRV_TOUCH_POINT_NUMBER_MAX];
    int blocked_count = 0;
    /* Two press sessions sharing a track must have a released snapshot between
     * them, so the GBA sees separate edges even for the same button. Only tracks
     * actually returned active in the preceding snapshot need this gap. */
    for (int i = 0; i < state->pending_count; ++i) {
        unsigned track = state->pending[i].track_id;
        if (find_track(state->previous, state->previous_count, track, 0) >= 0
            && find_track(blocked, blocked_count, track, 0) < 0) {
            blocked[blocked_count++] = state->pending[i];
        }
    }
    /* Current held points take priority, keeping them held while short ended
     * taps await a slot. An unread DOWN for a held point is delivered once. */
    for (int i = 0; i < state->count && count < max_points; ++i) {
        const struct drv_touch_data *point = &state->points[i];
        if (point->event != DRV_TOUCH_EVENT_DOWN && point->event != DRV_TOUCH_EVENT_MOVE) {
            continue;
        }
        if (find_track(points, count, point->track_id, 0) >= 0
            || find_track(blocked, blocked_count, point->track_id, 0) >= 0) {
            continue;
        }
        points[count] = *point;
        int pending = find_track(state->pending, state->pending_count, point->track_id, 0);
        if (pending >= 0) {
            /* Preserve the oldest unread session's coordinates, even if the
             * current point is a newer press reusing the same track ID. */
            points[count] = state->pending[pending];
            consume_pending(state, pending);
        }
        ++count;
    }
    /* Ended taps receive one pressed snapshot, then release on the next read.
     * Only delivered items are consumed, so max_points cannot drop a press. */
    for (int i = 0; i < state->pending_count && count < max_points;) {
        if (find_track(points, count, state->pending[i].track_id, 0) >= 0
            || find_track(blocked, blocked_count, state->pending[i].track_id, 0) >= 0) {
            ++i;
            continue;
        }
        points[count++] = state->pending[i];
        consume_pending(state, i);
    }
    /* Never return UP alongside the pending DOWN for the same track. */
    for (int i = 0; i < state->count && count < max_points; ++i) {
        const struct drv_touch_data *point = &state->points[i];
        if (point->event == DRV_TOUCH_EVENT_UP && find_track(points, count, point->track_id, 0) < 0) {
            points[count++] = *point;
        }
    }
    state->previous_count = 0;
    for (int i = 0; i < count; ++i) {
        if (points[i].event == DRV_TOUCH_EVENT_DOWN || points[i].event == DRV_TOUCH_EVENT_MOVE) {
            state->previous[state->previous_count++] = points[i];
        }
    }
    pthread_mutex_unlock(&state->mutex);
    return count;
}

int touch_async_stop(touch_async_state_t **owner)
{
    if (owner == NULL || *owner == NULL) {
        return 0;
    }
    touch_async_state_t *state = *owner;
    pthread_mutex_lock(&state->mutex);
    state->stop = 1;
    state->count = 0;
    state->pending_count = 0;
    state->previous_count = 0;
    pthread_mutex_unlock(&state->mutex);
    int rc = pthread_join(state->thread, NULL);
    if (rc != 0) {
        /* Do not free state or permit driver teardown unless join succeeded. */
        return rc;
    }
    pthread_mutex_destroy(&state->mutex);
    free(state);
    *owner = NULL;
    return 0;
}
