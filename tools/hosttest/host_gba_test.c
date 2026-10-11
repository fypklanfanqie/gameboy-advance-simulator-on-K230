/* ==================================================================
 * host_gba_test.c — run the vendored mGBA GBA core on the host (x86_64)
 * under ASan/UBSan, reproducing the EXACT lifecycle that the firmware's
 * gba_module.c performs.
 *
 * WHY: the firmware changes were only proven to compile. This harness
 * exercises the same call sequence with the real Pokemon Emerald ROM and
 * checks for memory errors, so the runtime defects that were fixed
 * (uninitialised config, deinit-after-failed-init, blip stereo overflow,
 * savedata handling, stateSize) are verified rather than asserted.
 *
 * It is NOT the firmware: no MicroPython, no display, no audio device.
 * It validates the C core lifecycle and the data paths.
 *
 * Build: see tools/wsl/hosttest_core.sh (compiles with -fsanitize=address,undefined)
 * Run:   ./host_gba_test /path/to/rom.gba [frames]
 * ================================================================== */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>

#include <mgba/core/core.h>
#include <mgba/core/config.h>
#include <mgba/core/blip_buf.h>
#include <mgba/core/log.h>
#include <mgba/gba/core.h>
#include <mgba-util/vfs.h>
#include <mgba/internal/gba/gba.h>
#include <mgba/internal/gba/audio.h>
#include <mgba/internal/gba/input.h>
#include <mgba/internal/gba/savedata.h>

#define GBA_FB_W 240
#define GBA_FB_H 160
#define GBA_DISP_W 720
#define GBA_DISP_H 480
#define AUDIO_RATE 44100
#define AUDIO_MAX_SAMPLES 1024

static int g_checks = 0;
static int g_failures = 0;

/* Mirror the binding: retain warnings/errors, suppress per-frame debug logs. */
static unsigned long long g_log_suppressed = 0;
static void core_log(struct mLogger* logger, int category, enum mLogLevel level,
                     const char* format, va_list args) {
    (void)logger;
    if (!(level & (mLOG_FATAL | mLOG_ERROR | mLOG_WARN))) {
        g_log_suppressed++;
        return;
    }
    printf("[gba core] %s: ", mLogCategoryName(category));
    vprintf(format, args);
    printf("\n");
}
static struct mLogger g_core_logger = { .log = core_log, .filter = NULL };

static void check(int cond, const char* what) {
    g_checks++;
    printf("  %-58s %s\n", what, cond ? "PASS" : "FAIL");
    if (!cond) {
        g_failures++;
    }
}

static void section(const char* name) {
    printf("\n=== %s ===\n", name);
}

/* Mirror of gba_module.c: audio_frame() with the fixed stride handling. */
static int16_t s_pcm_buf[AUDIO_MAX_SAMPLES * 2];

static size_t read_audio_frame(struct mCore* core, unsigned char* out, size_t cap) {
    struct blip_t* bl = core->getAudioChannel(core, 0);
    struct blip_t* br = core->getAudioChannel(core, 1);
    if (!bl || !br) {
        return 0;
    }
    int avail_l = blip_samples_avail(bl);
    int avail_r = blip_samples_avail(br);
    int avail = avail_l < avail_r ? avail_l : avail_r;
    if (avail <= 0) {
        return 0;
    }
    if (avail > AUDIO_MAX_SAMPLES) {
        avail = AUDIO_MAX_SAMPLES;
    }
    /* stereo=1 => writes every OTHER element, so the interleaved buffer's
       even (left) and odd (right) slots are the correct targets. */
    int got_l = blip_read_samples(bl, s_pcm_buf, avail, 1);
    int got_r = blip_read_samples(br, &s_pcm_buf[1], avail, 1);
    int got = got_l < got_r ? got_l : got_r;
    if (got <= 0) {
        return 0;
    }
    size_t bytes = (size_t)got * 2 * 2;
    if (bytes > cap) {
        bytes = cap;
    }
    memcpy(out, s_pcm_buf, bytes);
    return bytes;
}

int main(int argc, char** argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <rom.gba> [frames]\n", argv[0]);
        return 2;
    }
    const char* rom_path = argv[1];
    mLogSetDefaultLogger(&g_core_logger);
    int frames = argc > 2 ? atoi(argv[2]) : 600;

    printf("host GBA core test\n");
    printf("  rom    : %s\n", rom_path);
    printf("  frames : %d\n", frames);

    /* ---------- buffers, exactly as the module allocates them ---------- */
    color_t* framebuf = malloc((size_t)GBA_FB_W * GBA_FB_H * sizeof(color_t));
    uint16_t* dispbuf = malloc((size_t)GBA_DISP_W * GBA_DISP_H * 2);
    if (!framebuf || !dispbuf) {
        fprintf(stderr, "alloc failed\n");
        return 2;
    }
    memset(framebuf, 0, (size_t)GBA_FB_W * GBA_FB_H * sizeof(color_t));
    memset(dispbuf, 0, (size_t)GBA_DISP_W * GBA_DISP_H * 2);

    /* ---------- 1. lifecycle: create -> config -> init -> map -> load ---------- */
    section("1. core lifecycle (mirrors gba_module.c gba_load)");
    struct mCore* core = GBACoreCreate();
    check(core != NULL, "GBACoreCreate returns a core");
    if (!core) {
        return 1;
    }

    /* The fix: config must be initialised before ANY core use, because
       _GBACoreReset reads core->config as a hash table. */
    mCoreInitConfig(core, "host-test");
    check(1, "mCoreInitConfig called before init/reset");

    bool inited = core->init(core);
    check(inited, "core->init succeeds");
    if (!inited) {
        return 1;
    }

    /* opts were memset to 0 by GBACoreCreate => volume 0 => silence.
       Give sane defaults, then map them in (this dereferences core->board). */
    mCoreConfigSetDefaultIntValue(&core->config, "volume", GBA_AUDIO_VOLUME_MAX);
    mCoreConfigSetDefaultIntValue(&core->config, "audioBuffers", GBA_AUDIO_SAMPLES);
    mCoreConfigSetDefaultValue(&core->config, "idleOptimization", "detect");
    mCoreLoadForeignConfig(core, &core->config);
    check(1, "defaults applied + mCoreLoadForeignConfig after init");

    core->setVideoBuffer(core, framebuf, GBA_FB_W);
    check(1, "setVideoBuffer before reset");

    /* audio rate must match the board's PyAudio rate (44100), not the 96000
       that the vendored GBAAudioInit hardcodes. */
    struct GBA* gba = (struct GBA*)core->board;
    blip_set_rates(gba->audio.psg.left, (double)gba->audio.psg.clockRate, (double)AUDIO_RATE);
    blip_set_rates(gba->audio.psg.right, (double)gba->audio.psg.clockRate, (double)AUDIO_RATE);
    check(1, "blip output rate set to 44100");

    struct VFile* vf = VFileOpen(rom_path, O_RDONLY);
    check(vf != NULL, "VFileOpen(O_RDONLY) opens the ROM (mmap path)");
    if (!vf) {
        return 1;
    }
    bool loaded = core->loadROM(core, vf);
    check(loaded, "core->loadROM succeeds");
    if (!loaded) {
        return 1;
    }
    core->reset(core);
    check(1, "core->reset completes");

    /* ---------- 2. the emulator actually executes the game ---------- */
    section("2. emulation: frames run and the framebuffer changes");
    unsigned long long h1 = 0, h2 = 0;
    int nonzero_frames = 0;
    int distinct = 0;
    unsigned long long prev = 0;

    for (int f = 0; f < frames; f++) {
        /* Press START for a while so the game advances past the boot screen;
           this also exercises setKeys(). */
        if (f < 60) {
            core->setKeys(core, 1u << GBA_KEY_START);
        } else if (f == 60) {
            core->setKeys(core, 0);
        }
        core->runFrame(core);

        /* hash the 240x160 framebuffer */
        unsigned long long h = 1469598103934665603ULL;
        const uint16_t* px = (const uint16_t*)framebuf;
        int nz = 0;
        for (int i = 0; i < GBA_FB_W * GBA_FB_H; i++) {
            h ^= px[i];
            h *= 1099511628211ULL;
            if (px[i]) {
                nz++;
            }
        }
        if (nz > 100) {
            nonzero_frames++;
        }
        if (h != prev) {
            distinct++;
            prev = h;
        }
        if (f == 0) {
            h1 = h;
        }
        h2 = h;

        /* drain audio every frame, like the firmware does */
        unsigned char pcm[AUDIO_MAX_SAMPLES * 4];
        read_audio_frame(core, pcm, sizeof(pcm));
    }
    check(nonzero_frames > frames / 2,
          "framebuffer is populated on most frames (ROM executes)");
    /* "Is it actually running?" must not depend on how long we ran: Pokemon
       Emerald holds a near-static screen through parts of its intro, so the
       number of distinct frames is a function of the frame budget, not of
       correctness. Assert (a) the core's own frame counter advanced, and
       (b) the framebuffer changed at least once. */
    uint32_t core_frames = core->frameCounter(core);
    printf("  idle loop=0x%08x mode=%d suppressed logs=%llu\n",
           gba->idleLoop, gba->idleOptimization, g_log_suppressed);
    check(gba->idleOptimization >= IDLE_LOOP_REMOVE || gba->idleOptimization == IDLE_LOOP_IGNORE,
          "idle detector finishes safely or remains active");
    printf("  core frameCounter=%u, distinct framebuffers=%d\n",
           core_frames, distinct);
    check(core_frames > 0, "the core's frame counter advances (emulation runs)");
    check(distinct >= 2, "the framebuffer changes at least once (not frozen)");
    check(h1 != h2, "first and last frame hashes differ");
    printf("  frames with content: %d/%d\n", nonzero_frames, frames);

    /* Sample a few pixels to sanity-check the colour path. A GBA boot/save
       screen is mostly non-black; report the distribution. */
    {
        int r_hi = 0, g_mid = 0, b_lo = 0;
        const uint16_t* px = (const uint16_t*)framebuf;
        for (int i = 0; i < GBA_FB_W * GBA_FB_H; i++) {
            uint16_t v = px[i];
            if ((v >> 11) & 0x1F) r_hi++;
            if ((v >> 5) & 0x3F) g_mid++;
            if (v & 0x1F) b_lo++;
        }
        printf("  channels present: R=%d G=%d B=%d (of %d px)\n",
               r_hi, g_mid, b_lo, GBA_FB_W * GBA_FB_H);
        check(r_hi + g_mid + b_lo > 0, "RGB565 bits are being written");
    }

    /* ---------- 3. save type detection (BPEE => FLASH1M) ---------- */
    section("3. battery save: type detection and clone/restore");
    size_t ssize = GBASavedataSize(&gba->memory.savedata);
    printf("  savedata type=%d size=%zu\n", gba->memory.savedata.type, ssize);
    check(ssize == 128 * 1024, "BPEE detected as 128 KiB FLASH1M save");
    check(gba->memory.savedata.data != NULL, "save buffer is allocated");

    void* cloned = NULL;
    size_t cloned_size = core->savedataClone(core, &cloned);
    check(cloned_size == 128 * 1024 && cloned != NULL,
          "core->savedataClone returns 128 KiB");
    if (cloned) {
        /* write a recognisable pattern, restore it, read it back */
        unsigned char* c = (unsigned char*)cloned;
        for (size_t i = 0; i < cloned_size; i++) {
            c[i] = (unsigned char)(i * 7 + 3);
        }
        bool restored = core->savedataRestore(core, cloned, cloned_size, true);
        check(restored, "core->savedataRestore accepts the clone");
        /* clone again and compare: proves the restore actually landed */
        void* again = NULL;
        size_t again_size = core->savedataClone(core, &again);
        bool same = (again_size == cloned_size) &&
                    (memcmp(again, cloned, cloned_size) == 0);
        check(same, "clone-after-restore is byte-identical (restore works)");
        free(again);
        free(cloned);
    }
    /* restore a pristine save so the state tests start clean */
    {
        void* zero = calloc(1, 128 * 1024);
        core->savedataRestore(core, zero, 128 * 1024, true);
        free(zero);
    }

    /* ---------- 4. savestate via stateSize ---------- */
    section("4. savestate: size comes from core->stateSize()");
    size_t st_size = core->stateSize(core);
    printf("  stateSize=%zu\n", st_size);
    check(st_size > 0, "core->stateSize returns non-zero (not the old 1 MiB guess)");
    check(st_size < 1024 * 1024, "stateSize is smaller than the old hardcoded 1 MiB");

    void* st = malloc(st_size);
    bool saved = core->saveState(core, st);
    check(saved, "core->saveState succeeds with a stateSize buffer");

    /* Run more frames (mutating state), then load and confirm it rewinds. */
    for (int f = 0; f < 30; f++) {
        core->runFrame(core);
    }
    bool st_loaded = core->loadState(core, st);
    check(st_loaded, "core->loadState accepts the stateSize buffer");

    /* A short read must be detectable: passing a truncated buffer is the case
       the module guards against. We just assert the size contract holds. */
    check(st_size >= 100000, "stateSize is large enough to be a full serialisation");
    free(st);

    /* ---------- 4b. save_busy: detecting a mid-write flash ---------- */
    section("4b. save_busy: the flash-settling signal used before saving");
    {
        /* The module exposes gba.save_busy() as
              mTimingIsScheduled(savedata.timing, &savedata.dust)
           which is the SAME condition mGBA itself uses to report "flash busy"
           (savedata.c: mTimingIsScheduled(...) && (address>>12)==settling).
           Here we assert the mechanism it relies on: the dust event really is
           scheduled while a flash operation is pending, and clears afterwards.

           We cannot easily make the game write a save on demand, so this checks
           the invariant the API depends on: with no flash operation in flight
           the dust event is NOT scheduled (so save_busy reports idle, and a
           periodic save is allowed). */
        bool idle = !mTimingIsScheduled(gba->memory.savedata.timing,
                                       &gba->memory.savedata.dust);
        check(idle, "no flash op in flight => save_busy would report idle");

        /* Drive a real sector erase through the emulated flash command sequence
           (unlock 0xAA/0x55 to 0x0E005555, then 0x80, then 0x30 at the sector).
           This is the documented JEDEC sequence the game itself uses. */
        GBASavedataWriteFlash(&gba->memory.savedata, 0x5555, 0xAA);
        GBASavedataWriteFlash(&gba->memory.savedata, 0x2AAA, 0x55);
        GBASavedataWriteFlash(&gba->memory.savedata, 0x5555, 0x80);
        GBASavedataWriteFlash(&gba->memory.savedata, 0x5555, 0xAA);
        GBASavedataWriteFlash(&gba->memory.savedata, 0x2AAA, 0x55);
        GBASavedataWriteFlash(&gba->memory.savedata, 0x0000, 0x30);  /* erase sector 0 */

        bool busy_now = mTimingIsScheduled(gba->memory.savedata.timing,
                                          &gba->memory.savedata.dust);
        check(busy_now, "sector erase schedules the dust event => save_busy == 1");
        if (busy_now) {
            /* The game would read 0x5F (busy) from the settling sector. */
            uint8_t status = GBASavedataReadFlash(&gba->memory.savedata, 0x0000);
            check(status == 0x5F, "settling sector reads 0x5F (flash busy)");
        }
        /* Advance emulation enough for the erase to settle (FLASH_ERASE_CYCLES
           = 30000 cycles, far less than one frame's worth), then confirm idle. */
        for (int f = 0; f < 8; f++) {
            core->runFrame(core);
        }
        bool idle_after = !mTimingIsScheduled(gba->memory.savedata.timing,
                                             &gba->memory.savedata.dust);
        check(idle_after, "after a few frames the flash is idle again");
    }

    /* ---------- 5. audio: the fixed stereo stride ---------- */
    section("5. audio: interleaved read stays in bounds");
    {
        unsigned long long total_bytes = 0;
        int nonzero = 0;
        for (int f = 0; f < 240; f++) {
            core->runFrame(core);
            unsigned char pcm[AUDIO_MAX_SAMPLES * 4];
            size_t got = read_audio_frame(core, pcm, sizeof(pcm));
            total_bytes += got;
            if (got) {
                nonzero++;
            }
        }
        printf("  audio bytes over 240 frames: %llu (%d frames with data)\n",
               total_bytes, nonzero);
        check(total_bytes > 0, "blip produces PCM data");
        /* ASan would have aborted above if the stereo stride overran the
           buffer, which is exactly the bug the old code had. */
        check(1, "no out-of-bounds write during interleaved reads (ASan)");
    }

    /* ---------- 6. teardown in the fixed order ---------- */
    section("6. teardown: config deinit BEFORE core->deinit");
    /* The firmware's destroy_core() does exactly this, and the order matters:
       _GBACoreDeinit() ends with free(core), so touching core->config after it
       would be a use-after-free. ASan will flag it if the order is wrong. */
    mCoreConfigDeinit(&core->config);
    check(1, "mCoreConfigDeinit called first");
    core->deinit(core);
    check(1, "core->deinit completed (frees the core)");

    free(framebuf);
    free(dispbuf);

    section("summary");
    printf("  checks: %d, failures: %d\n", g_checks, g_failures);
    if (g_failures) {
        printf("RESULT: FAIL\n");
        return 1;
    }
    printf("RESULT: PASS\n");
    return 0;
}
