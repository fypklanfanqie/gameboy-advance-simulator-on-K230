#include "touch_async.h"
#include <assert.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static pthread_mutex_t fake_mutex = PTHREAD_MUTEX_INITIALIZER;
static int fake_count = 0, fake_event = 2, fake_reads = 0, fake_in_read = 0;
static int fake_custom = 0, fake_generation = 0, fake_completed = 0, fake_published = -1;
static struct drv_touch_data fake_points[DRV_TOUCH_POINT_NUMBER_MAX];
static int failures = 0, checks = 0;

#define CHECK(expr) do { ++checks; if (!(expr)) { \
    ++failures; fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #expr); } } while (0)

int drv_touch_read(drv_touch_inst_t *inst, struct drv_touch_data *points, int max_points)
{
    assert(inst != NULL && inst->fd == 42);
    pthread_mutex_lock(&fake_mutex);
    ++fake_reads;
    ++fake_in_read;
    /* The next read proves the worker published the previous read result. */
    fake_published = fake_completed;
    assert(fake_in_read == 1); /* One sole owner, including across stop/restart. */
    pthread_mutex_unlock(&fake_mutex);
    usleep(33000);
    pthread_mutex_lock(&fake_mutex);
    int count = fake_count, event = fake_event;
    int custom = fake_custom;
    if (custom && count > 0 && count <= max_points) {
        memcpy(points, fake_points, count * sizeof(points[0]));
    }
    fake_completed = fake_generation;
    --fake_in_read;
    pthread_mutex_unlock(&fake_mutex);
    if (!custom && count > 0 && count <= max_points) {
        for (int i = 0; i < count; ++i) {
            memset(&points[i], 0, sizeof(points[i]));
            points[i].event = event;
            points[i].track_id = i;
            points[i].x_coordinate = 123 + i;
            points[i].y_coordinate = 234 + i;
            points[i].timestamp = 30000 + i;
        }
    }
    return count;
}

static void fake_set(int count, int event)
{
    pthread_mutex_lock(&fake_mutex);
    fake_count = count; fake_event = event;
    fake_custom = 0; ++fake_generation;
    pthread_mutex_unlock(&fake_mutex);
}

static void publish_fake(int count, const struct drv_touch_data *points)
{
    pthread_mutex_lock(&fake_mutex);
    fake_count = count;
    fake_custom = points != NULL;
    if (count > 0 && points != NULL) {
        memcpy(fake_points, points, count * sizeof(points[0]));
    }
    int generation = ++fake_generation;
    pthread_mutex_unlock(&fake_mutex);
    for (int n = 0; n < 500; ++n) {
        pthread_mutex_lock(&fake_mutex);
        int done = fake_published >= generation;
        pthread_mutex_unlock(&fake_mutex);
        if (done) {
            return;
        }
        usleep(1000);
    }
    CHECK(0);
}

static int active_count(const struct drv_touch_data *points, int count)
{
    int active = 0;
    for (int i = 0; i < count; ++i) {
        active += points[i].event == 2 || points[i].event == 3;
    }
    return active;
}

static int wait_count(touch_async_state_t *state, int expected, struct drv_touch_data *points)
{
    for (int n = 0; n < 300; ++n) {
        int count = touch_async_snapshot(state, points, DRV_TOUCH_POINT_NUMBER_MAX);
        if (count == expected) { return count; }
        usleep(1000);
    }
    return -999;
}

static double seconds(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

int main(void)
{
    drv_touch_inst_t inst = { .fd = 42 };
    touch_async_state_t *state = NULL;
    struct drv_touch_data points[DRV_TOUCH_POINT_NUMBER_MAX];
    CHECK(touch_async_start(NULL, &state) != 0);
    CHECK(touch_async_start(&inst, NULL) != 0);
    CHECK(touch_async_snapshot(NULL, points, 1) < 0);
    CHECK(touch_async_stop(NULL) == 0);
    CHECK(touch_async_stop(&state) == 0);
    CHECK(touch_async_start(&inst, &state) == 0);
    CHECK(state != NULL);
    touch_async_state_t *saved = state;
    CHECK(touch_async_start(&inst, &state) == 0 && saved == state);
    CHECK(touch_async_snapshot(state, points, 0) < 0);
    CHECK(touch_async_snapshot(state, points, 11) < 0);
    CHECK(touch_async_snapshot(state, NULL, 1) < 0);
    CHECK(wait_count(state, 0, points) == 0);
    fake_set(2, 2);
    CHECK(wait_count(state, 2, points) == 2);
    CHECK(points[0].x_coordinate == 123 && points[1].x_coordinate == 124);
    CHECK(points[0].timestamp == 30000 && points[1].timestamp == 30001);
    CHECK(touch_async_snapshot(state, points, 1) == 1);
    double begin = seconds();
    for (int n = 0; n < 10000; ++n) {
        (void)touch_async_snapshot(state, points, 1);
    }
    double snapshot_time = seconds() - begin;
    CHECK(snapshot_time < 0.2); /* Blocking reads would require 330 seconds. */
    printf("10,000 cached reads: %.3f ms\n", snapshot_time * 1000);
    fake_set(2, 1);
    for (int n = 0; n < 300; ++n) {
        touch_async_snapshot(state, points, 10);
        if (points[0].event == 1) { break; }
        usleep(1000);
    }
    CHECK(points[0].event == 1);
    fake_set(0, 0);
    CHECK(wait_count(state, 0, points) == 0);
    fake_set(1, 2);
    CHECK(wait_count(state, 1, points) == 1);
    fake_set(-2, 0);
    CHECK(wait_count(state, 0, points) == 0);
    fake_set(11, 2);
    usleep(50000);
    CHECK(touch_async_snapshot(state, points, 10) == 0);
    begin = seconds();
    CHECK(touch_async_stop(&state) == 0 && state == NULL);
    CHECK(seconds() - begin < 0.2);
    pthread_mutex_lock(&fake_mutex);
    int reads_after_stop = fake_reads;
    CHECK(fake_in_read == 0);
    pthread_mutex_unlock(&fake_mutex);
    usleep(50000);
    pthread_mutex_lock(&fake_mutex);
    CHECK(fake_reads == reads_after_stop);
    pthread_mutex_unlock(&fake_mutex);
    CHECK(touch_async_stop(&state) == 0);
    fake_set(1, 2);
    for (int n = 0; n < 20; ++n) {
        CHECK(touch_async_start(&inst, &state) == 0);
        CHECK(wait_count(state, 1, points) == 1);
        CHECK(touch_async_stop(&state) == 0 && state == NULL);
    }
    fake_set(0, 0);
    CHECK(touch_async_start(&inst, &state) == 0);
    struct drv_touch_data events[10] = {{ .event = 2, .track_id = 4, .x_coordinate = 50 }};
    publish_fake(1, events);
    events[0].event = 1;
    publish_fake(1, events);
    publish_fake(0, NULL);
    CHECK(touch_async_snapshot(state, points, 10) == 1);
    CHECK(points[0].event == 2 && points[0].track_id == 4 && points[0].x_coordinate == 50);
    CHECK(touch_async_snapshot(state, points, 10) == 0);

    /* Current UP cannot accompany the guaranteed DOWN in its first snapshot. */
    events[0].event = 2;
    publish_fake(1, events);
    events[0].event = 1;
    publish_fake(1, events);
    CHECK(touch_async_snapshot(state, points, 10) == 1);
    CHECK(points[0].event == 2);
    CHECK(touch_async_snapshot(state, points, 10) == 1);
    CHECK(points[0].event == 1);
    publish_fake(0, NULL);

    /* Reusing a track between snapshots must retain both press coordinates. */
    events[0] = (struct drv_touch_data){ .event = 2, .track_id = 4, .x_coordinate = 100 };
    publish_fake(1, events);
    events[0].event = 1;
    publish_fake(1, events);
    events[0].event = 2; events[0].x_coordinate = 200;
    publish_fake(1, events);
    CHECK(touch_async_snapshot(state, points, 10) == 1);
    CHECK(points[0].event == 2 && points[0].x_coordinate == 100);
    CHECK(touch_async_snapshot(state, points, 10) == 0);
    CHECK(touch_async_snapshot(state, points, 10) == 1);
    CHECK(points[0].event == 2 && points[0].x_coordinate == 200);
    events[0].event = 1;
    publish_fake(1, events);
    CHECK(touch_async_snapshot(state, points, 10) == 1);
    CHECK(points[0].event == 1 && points[0].x_coordinate == 200);
    publish_fake(0, NULL);

    /* Same-button rapid clicks require an actual released mask in between. */
    events[0] = (struct drv_touch_data){ .event = 2, .track_id = 4, .x_coordinate = 200 };
    publish_fake(1, events);
    events[0].event = 1; publish_fake(1, events);
    events[0].event = 2; publish_fake(1, events);
    CHECK(touch_async_snapshot(state, points, 10) == 1);
    CHECK(points[0].event == 2 && points[0].x_coordinate == 200);
    CHECK(touch_async_snapshot(state, points, 10) == 0);
    CHECK(touch_async_snapshot(state, points, 10) == 1);
    CHECK(points[0].event == 2 && points[0].x_coordinate == 200);
    events[0].event = 1; publish_fake(1, events);
    CHECK(active_count(points, touch_async_snapshot(state, points, 10)) == 0);
    publish_fake(0, NULL);

    /* A held finger remains held while an ended second-finger tap is delivered. */
    events[0] = (struct drv_touch_data){ .event = 2, .track_id = 0 };
    publish_fake(1, events);
    CHECK(touch_async_snapshot(state, points, 10) == 1);
    events[0].event = 3;
    events[1] = (struct drv_touch_data){ .event = 2, .track_id = 1 };
    publish_fake(2, events);
    events[1].event = 1;
    publish_fake(2, events);
    CHECK(touch_async_snapshot(state, points, 10) == 2);
    CHECK(points[0].track_id == 0 && points[0].event == 3);
    CHECK(points[1].track_id == 1 && points[1].event == 2);
    int count = touch_async_snapshot(state, points, 10);
    CHECK(active_count(points, count) == 1);
    CHECK(points[0].track_id == 0 && points[0].event == 3);
    /* Repeated tap on the second finger inserts its gap without releasing hold. */
    events[1].event = 2; publish_fake(2, events);
    count = touch_async_snapshot(state, points, 2);
    CHECK(count == 2 && active_count(points, count) == 2);
    events[1].event = 1; publish_fake(2, events);
    events[1].event = 2; publish_fake(2, events);
    count = touch_async_snapshot(state, points, 2);
    CHECK(count == 1 && points[0].track_id == 0 && points[0].event == 3);
    count = touch_async_snapshot(state, points, 2);
    CHECK(count == 2 && active_count(points, count) == 2);
    CHECK(points[1].track_id == 1 && points[1].event == 2);
    publish_fake(0, NULL);
    CHECK(touch_async_snapshot(state, points, 10) == 0);

    /* A truncated-out track is not marked active; its queued sessions still
     * receive press/release/press after the held track vacates the one slot. */
    events[0] = (struct drv_touch_data){ .event = 2, .track_id = 0 };
    publish_fake(1, events);
    CHECK(touch_async_snapshot(state, points, 1) == 1);
    events[0].event = 3;
    events[1] = (struct drv_touch_data){ .event = 2, .track_id = 1, .x_coordinate = 111 };
    publish_fake(2, events);
    events[1].event = 1; publish_fake(2, events);
    events[1].event = 2; events[1].x_coordinate = 222; publish_fake(2, events);
    CHECK(touch_async_snapshot(state, points, 1) == 1 && points[0].track_id == 0);
    events[0].event = 1; publish_fake(2, events);
    CHECK(touch_async_snapshot(state, points, 1) == 1);
    CHECK(points[0].track_id == 1 && points[0].x_coordinate == 111);
    count = touch_async_snapshot(state, points, 1);
    CHECK(active_count(points, count) == 0);
    CHECK(touch_async_snapshot(state, points, 1) == 1);
    CHECK(points[0].track_id == 1 && points[0].x_coordinate == 222);
    publish_fake(0, NULL);
    CHECK(touch_async_snapshot(state, points, 10) == 0);

    /* Smaller snapshots retain each of 10 pending presses until delivered. */
    for (int i = 0; i < 10; ++i) {
        events[i] = (struct drv_touch_data){ .event = 2, .track_id = i };
    }
    publish_fake(10, events);
    for (int i = 0; i < 10; ++i) { events[i].event = 1; }
    publish_fake(10, events);
    publish_fake(0, NULL);
    unsigned seen = 0;
    for (int n = 0; n < 4; ++n) {
        count = touch_async_snapshot(state, points, 3);
        CHECK(count == (n == 3 ? 1 : 3));
        for (int i = 0; i < count; ++i) {
            CHECK(points[i].event == 2);
            CHECK((seen & (1u << points[i].track_id)) == 0);
            seen |= 1u << points[i].track_id;
        }
    }
    CHECK(seen == 1023);
    CHECK(touch_async_snapshot(state, points, 10) == 0);

    /* Errors release a pending tap after its one guaranteed press delivery. */
    events[0] = (struct drv_touch_data){ .event = 2, .track_id = 7 };
    publish_fake(1, events);
    publish_fake(-2, NULL);
    CHECK(touch_async_snapshot(state, points, 10) == 1 && points[0].event == 2);
    CHECK(touch_async_snapshot(state, points, 10) == 0);
    publish_fake(1, events);
    CHECK(touch_async_stop(&state) == 0);
    fake_set(0, 0);
    CHECK(touch_async_start(&inst, &state) == 0);
    publish_fake(0, NULL);
    CHECK(touch_async_snapshot(state, points, 10) == 0);
    CHECK(touch_async_stop(&state) == 0);
    printf("touch_async: %d/%d checks passed\n", checks - failures, checks);
    return failures != 0;
}
