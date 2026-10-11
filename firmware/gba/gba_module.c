// ==================================================================
// gba_module.c：K230 GBA 模拟器 MicroPython 绑定（mGBA core 真 API 版）
//
//   gba.load(path)        -> 0=ok，负数=错误码
//   gba.run_frame()       -> 帧号
//   gba.keys(mask)        -> 设置按键位掩码（bit i = GBA_KEY_i）
//   gba.set_keys(mask)    -> gba.keys 的别名
//   gba.audio_frame()     -> 可用的交错立体声 PCM（int16 LE），可能为空
//   gba.set_audio_rate(hz)-> 设置 blip 输出率，必须与板端 PyAudio 的 rate 一致
//   gba.save_write/load   -> 电池存档（走官方 savedataClone/savedataRestore）
//   gba.save_state/load_state -> 快速存档（长度用 core->stateSize()）
//   gba.disp_bytes()      -> 720x480 RGB565 缩放缓冲的引用型 bytearray
//   gba.info()            -> 自检信息
//
// 本文件依据厂商 SDK 精确版本重写，修正旧实现中的多个运行期缺陷：
//   1) load() 未调用 mCoreInitConfig -> core->config 是未初始化内存，
//      reset()/loadROM 会通过 mCoreConfigGet* 读取它（见 core/gba/core.c: _GBACoreReset）。
//   2) core->init() 失败时不能调用 core->deinit()：精确源码在 cpu/board 分配失败时
//      提前 return，此时 core->cpu/core->board 仍为 NULL，ARMDeinit(NULL) 会解引用空指针。
//   3) config 必须在 core->deinit() 之前 mCoreConfigDeinit()：
//      _GBACoreDeinit() 末尾 free(core)，之后再访问 core->config 即 use-after-free。
//   4) audio_frame() 用 blip_read_samples(..., count=512, stereo=1) 写入 tmp[512]，
//      而 stereo=1 每写一个样本步进 2 个元素 -> 越界。改为直接写入交错目标缓冲的
//      左右偏移，并按“实际可用样本数”读取，避免积压。
//   5) 电池存档改为官方 savedataClone/savedataRestore，不再直接碰 savedata.data。
//   6) 快速存档改用 core->stateSize()，不再硬编码 1 MiB。
//   7) backlight()：厂商内核没有用户态背光 ioctl（kd_display_control_backlight 是
//      TODO 空桩），旧实现的 ioctl 常量是臆造的。此函数现在明确返回“不支持”。
//   8) 所有分配都检查失败；进程级缓冲不释放（引用型 bytearray 仍指向它们）。
// ==================================================================
#include "py/obj.h"
#include "py/runtime.h"
#include "py/objstr.h"

#include <fcntl.h>   /* O_RDONLY for VFileOpen */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <mgba/core/core.h>
#include <mgba/core/config.h>
#include <mgba/core/blip_buf.h>
#include <mgba/core/log.h>
#include <mgba/gba/core.h>
#include <mgba-util/vfs.h>
/* USE_VFS_FILE 未定义，vfs.h 里没有这句声明；vfs-file.o 已编译进固件，
   链接没问题，但缺声明会 -Werror=implicit-function-declaration。 */
struct VFile* VFileFOpen(const char* path, const char* mode);
#include <mgba/internal/gba/gba.h>        /* struct GBA, GBA_ARM7TDMI_FREQUENCY */
#include <mgba/internal/gba/audio.h>     /* struct GBAAudio, psg.clockRate */
#include <mgba/internal/gba/input.h>     /* enum GBAKey */
#include <mgba/internal/gba/savedata.h>  /* GBASavedataSize, savedata types */

/* 缩放实现（纯 C，便于宿主测试在 ASan 下执行） */
#include "gba_scale.h"

#define GBA_FB_W 240
#define GBA_FB_H 160

/* 与 Python 壳对齐的缩放输出：240x160 最近邻 3 倍 -> 720x480，正好铺满 LCD */
#define GBA_DISP_W 720
#define GBA_DISP_H 480

/* blip 输出率必须与板端 PyAudio 的 rate 一致（官方 audio.py 例程用 44100）。
   旧实现写死 96000，直接喂给 44100Hz 的 AO 会变成慢速低音。 */
#define GBA_AUDIO_RATE_DEFAULT 44100
/* 单次 audio_frame() 最多返回的样本数（每样本 2 声道 x 2 字节） */
#define GBA_AUDIO_MAX_SAMPLES 1024

static struct mCore* s_core = NULL;
static int s_frame_count = 0;
/* 跟踪生命周期，避免对未初始化的对象调用 deinit / config deinit */
static int s_config_inited = 0;   /* mCoreInitConfig 是否已成功执行 */
static int s_core_inited = 0;     /* core->init() 是否已成功执行 */

static color_t* s_framebuf = NULL;
static uint16_t* s_dispbuf = NULL;

static void* s_state_buf = NULL;
static size_t s_state_size = 0;

static int16_t s_pcm_buf[GBA_AUDIO_MAX_SAMPLES * 2];
static int s_audio_rate = GBA_AUDIO_RATE_DEFAULT;

/* Without a logger mGBA prints every DMA/RTC/debug message. On an embedded
   console that can dominate the frame time. Keep actionable diagnostics. */
static uint32_t s_log_suppressed = 0;
static uint32_t s_log_reported = 0;
static void gba_core_log(struct mLogger* logger, int category, enum mLogLevel level,
                         const char* format, va_list args) {
    (void)logger;
    if (!(level & (mLOG_FATAL | mLOG_ERROR | mLOG_WARN))) {
        s_log_suppressed++;
        return;
    }
    s_log_reported++;
    char message[256];
    vsnprintf(message, sizeof(message), format, args);
    const char* name = mLogCategoryName(category);
    mp_printf(&mp_plat_print, "[gba core] %s: %s\n", name ? name : "core", message);
}
static struct mLogger s_gba_logger = { .log = gba_core_log, .filter = NULL };

// ==================================================================
// 内部辅助
// ==================================================================

/* 分配进程级缓冲。刻意不提供 free 路径：disp_bytes() 返回的是引用型 bytearray，
   Python 侧可能长期持有该指针，释放会造成 use-after-free。这些缓冲是单例、
   固定大小（76800B + 691200B），重复 load 不会重复分配。 */
static int ensure_buffers(void) {
    if (!s_framebuf) {
        s_framebuf = (color_t*)malloc((size_t)GBA_FB_W * GBA_FB_H * sizeof(color_t));
        if (!s_framebuf) {
            mp_printf(&mp_plat_print, "[gba] framebuf malloc failed\n");
            return -1;
        }
        memset(s_framebuf, 0, (size_t)GBA_FB_W * GBA_FB_H * sizeof(color_t));
    }
    if (!s_dispbuf) {
        s_dispbuf = (uint16_t*)malloc((size_t)GBA_DISP_W * GBA_DISP_H * 2);
        if (!s_dispbuf) {
            mp_printf(&mp_plat_print, "[gba] dispbuf malloc failed\n");
            return -2;
        }
        memset(s_dispbuf, 0, (size_t)GBA_DISP_W * GBA_DISP_H * 2);
    }
    return 0;
}

/* 把 blip 输出率同步到 s_audio_rate。必须与 Python 侧 PyAudio 的 rate 一致。 */
static void apply_audio_rate(void) {
    if (!s_core || !s_core->board) {
        return;
    }
    struct GBA* gba = (struct GBA*)s_core->board;
    double clock = (double)gba->audio.psg.clockRate;
    if (clock <= 0.0) {
        clock = (double)GBA_ARM7TDMI_FREQUENCY;
    }
    blip_set_rates(gba->audio.psg.left, clock, (double)s_audio_rate);
    blip_set_rates(gba->audio.psg.right, clock, (double)s_audio_rate);
}

/* 统一销毁 core。
   initialized=1 表示 core->init() 成功过，可以安全调用 core->deinit()（它会 free(core)）。
   initialized=0 表示 init 失败，此时只能手动 mCoreConfigDeinit + free(core)。
   顺序要点：config deinit 必须在 free(core) 之前。 */
static void destroy_core(int initialized) {
    if (!s_core) {
        s_config_inited = 0;
        s_core_inited = 0;
        return;
    }
    if (s_config_inited) {
        mCoreConfigDeinit(&s_core->config);
        s_config_inited = 0;
    }
    if (initialized && s_core_inited) {
        s_core->deinit(s_core);   /* 内部 free(core)，之后 s_core 不可再用 */
    } else {
        /* init 未完成：ARMDeinit/GBADestroy 会遇到 NULL，不能调用 deinit */
        free(s_core);
    }
    s_core = NULL;
    s_core_inited = 0;
    s_frame_count = 0;
}

static void reset_state_buf(void) {
    if (s_state_buf) {
        free(s_state_buf);
        s_state_buf = NULL;
    }
    s_state_size = 0;
}

// ==================================================================
// 核心生命周期
// ==================================================================

// gba.load(path) -> 0=ok，负数=错误码
STATIC mp_obj_t gba_load(mp_obj_t path) {
    const char* p = mp_obj_str_get_str(path);

    /* 允许重复 load：先彻底销毁上一个核心 */
    destroy_core(1);
    reset_state_buf();
    s_log_suppressed = 0;
    s_log_reported = 0;
    mLogSetDefaultLogger(&s_gba_logger);

    if (ensure_buffers() != 0) {
        return mp_obj_new_int(-1);
    }

    s_core = GBACoreCreate();
    if (!s_core) {
        mp_printf(&mp_plat_print, "[gba] GBACoreCreate failed\n");
        return mp_obj_new_int(-2);
    }

    /* (1) 必须先初始化 config：GBACoreCreate 只 memset 了 opts，config 是脏内存，
       而 _GBACoreReset / _GBACoreLoadConfig 都会读 core->config。 */
    mCoreInitConfig(s_core, "k230-gba");
    s_config_inited = 1;

    if (!s_core->init(s_core)) {
        mp_printf(&mp_plat_print, "[gba] core init failed\n");
        destroy_core(0);          /* (2) init 失败时不能走 core->deinit() */
        return mp_obj_new_int(-3);
    }
    s_core_inited = 1;

    /* opts 全 0 => volume=0 => 静音。显式给一个可用默认值，再映射进 opts。
       注意 mCoreLoadForeignConfig 会调用 _GBACoreLoadConfig，它解引用 core->board，
       所以必须在 init() 之后调用。 */
    mCoreConfigSetDefaultIntValue(&s_core->config, "volume", GBA_AUDIO_VOLUME_MAX);
    mCoreConfigSetDefaultIntValue(&s_core->config, "audioBuffers", GBA_AUDIO_SAMPLES);
    /* ROM hacks can bypass the fixed-address overrides. Let mGBA verify an
       idle loop from actual instructions instead of assuming a stock address. */
    mCoreConfigSetDefaultValue(&s_core->config, "idleOptimization", "detect");
    mCoreLoadForeignConfig(s_core, &s_core->config);

    /* 渲染目标：必须在 reset() 之前设置，_GBACoreReset 用它选择软件渲染器 */
    s_core->setVideoBuffer(s_core, s_framebuf, GBA_FB_W);

    /* 让 blip 输出率与板端一致 */
    apply_audio_rate();

    /* 用 VFileFOpen("rb")（stdio 路径），不用 VFileOpen(O_RDONLY)（fd 路径）。
       真机证据：fd 路径在 RT-Smart 上 loadROM 返回 false（屏幕显示 -5），
       而同一张卡同一文件用 Python 的 fread 逐块读完全正常。
       根因：RT-Smart 工具链上 _POSIX_MAPPED_FILES 未定义（gcc -E 已验证），
       fd 路径的 _vfdMap/_vfdSize/_vfdRead 走 read()/lseek()/fstat() 回退分支，
       这条分支在本平台未经验证；stdio 路径的 _vffMap 用
       anonymousMemoryMap()+fread()、_vffSize 用 fseek/ftell，
       正是 MicroPython 文件层与官方例程都在用的接口。
       VFileFOpen 的声明在 vfs.h 里被 #ifdef USE_VFS_FILE 包住（本构建未定义），
       所以在文件头补一个显式声明。 */
    struct VFile* vf = VFileFOpen(p, "rb");
    if (!vf) {
        mp_printf(&mp_plat_print, "[gba] cannot open ROM: %s\n", p);
        destroy_core(1);
        return mp_obj_new_int(-4);
    }
    if (!s_core->loadROM(s_core, vf)) {
        mp_printf(&mp_plat_print, "[gba] loadROM failed\n");
        vf->close(vf);            /* _GBACoreLoadROM 成功时才自行接管 vf */
        destroy_core(1);
        return mp_obj_new_int(-5);
    }
    s_core->reset(s_core);
    s_frame_count = 0;
    mp_printf(&mp_plat_print, "[gba] ROM loaded: %s\n", p);
    return mp_obj_new_int(0);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_1(gba_load_obj, gba_load);

// gba.run_frame() -> 帧号；-1=核心未加载
STATIC mp_obj_t gba_run_frame(void) {
    if (!s_core || !s_core_inited) {
        return mp_obj_new_int(-1);
    }
    s_core->runFrame(s_core);
    s_frame_count++;
    return mp_obj_new_int(s_frame_count);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_run_frame_obj, gba_run_frame);

// gba.keys(mask) -> 设置按键位掩码（bit i = GBA_KEY_i，1=按下）
STATIC mp_obj_t gba_keys(mp_obj_t mask) {
    if (!s_core || !s_core_inited) {
        return mp_obj_new_int(-1);
    }
    uint32_t keys = (uint32_t)mp_obj_get_int(mask);
    s_core->setKeys(s_core, keys);
    return mp_obj_new_int((mp_int_t)keys);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_1(gba_keys_obj, gba_keys);

// gba.frame_ptr() -> 帧缓冲地址
STATIC mp_obj_t gba_frame_ptr(void) {
    return mp_obj_new_int((mp_int_t)(uintptr_t)s_framebuf);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_frame_ptr_obj, gba_frame_ptr);

// gba.frame_size() -> 字节数
STATIC mp_obj_t gba_frame_size(void) {
    return mp_obj_new_int(GBA_FB_W * GBA_FB_H * (int)sizeof(color_t));
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_frame_size_obj, gba_frame_size);

// gba.frame_dims() -> (240, 160)
STATIC mp_obj_t gba_frame_dims(void) {
    mp_obj_t t[2] = { mp_obj_new_int(GBA_FB_W), mp_obj_new_int(GBA_FB_H) };
    return mp_obj_new_tuple(2, t);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_frame_dims_obj, gba_frame_dims);

// gba.unload()
STATIC mp_obj_t gba_unload(void) {
    destroy_core(1);
    reset_state_buf();
    return mp_obj_new_int(0);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_unload_obj, gba_unload);

// ==================================================================
// 显示：3 倍最近邻缩放到 s_dispbuf，Python 用
//   image.Image(720, 480, image.RGB565, alloc=4, data=gba.disp_bytes())
// 做零拷贝包装。已核对：mGBA 的 color_t（COLOR_16_BIT + COLOR_5_6_5，
// mColorFrom555）与 CanMV PIXEL_FORMAT_RGB_565_LE 位序完全相同
// （R bits15-11 / G bits10-5 / B bits4-0，小端 uint16），不需要字节交换。
// ==================================================================

// gba.display_alloc() -> 分配 720x480 缩放缓冲，返回地址
STATIC mp_obj_t gba_display_alloc(void) {
    if (ensure_buffers() != 0) {
        return mp_obj_new_int(0);
    }
    return mp_obj_new_int((mp_int_t)(uintptr_t)s_dispbuf);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_display_alloc_obj, gba_display_alloc);

// gba.display_scale() -> 240x160 最近邻 3 倍缩放到 720x480（整块缓冲）
STATIC mp_obj_t gba_display_scale(void) {
    if (!s_framebuf || !s_dispbuf) {
        return mp_obj_new_int(-1);
    }
    for (int sy = 0; sy < GBA_FB_H; sy++) {
        const uint16_t* src_row = (const uint16_t*)s_framebuf + (size_t)sy * GBA_FB_W;
        /* 源行 sy -> 目标行 sy*3 .. sy*3+2 */
        uint16_t* dst_row = s_dispbuf + (size_t)(sy * 3) * GBA_DISP_W;
        for (int rep = 0; rep < 3; rep++) {
            uint16_t* dst = dst_row + (size_t)rep * GBA_DISP_W;
            for (int x = 0; x < GBA_FB_W; x++) {
                uint16_t v = src_row[x];
                dst[x * 3] = v;
                dst[x * 3 + 1] = v;
                dst[x * 3 + 2] = v;
            }
        }
    }
    return mp_obj_new_int(0);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_display_scale_obj, gba_display_scale);

// gba.scale_into(scale, [off_x, off_y]) -> 0=ok
//
// 为什么需要它：s_dispbuf 的**行距固定是 720 像素**（GBA_DISP_W）。
// Python 侧用 image.Image(w, h, RGB565, alloc=4, data=ba) 包装时，
// 显示的步长取的是 image->w（见 modmedia.display.c: buffer->vf_info.v_frame.stride[0]
// = image->w * bpp）。所以**只有 w == 720 时才是正确的**：
// 用 w=480 包装同一块缓冲，会因为行距不匹配而把画面斜切
// （py_image_alloc 只检查 size > buflen，307200 < 691200 会通过，所以是静默出错）。
//
// 本函数允许 Python 侧在**不改变行距**的前提下，把画面按任意整数倍
// 缩放并放到缓冲内的指定位置（off_x/off_y 以**缓冲内像素**为单位，
// 即相对 (0,0) 而非屏幕）。这样：
//   * full  布局 = scale_into(3, 0, 0) 等价于 display_scale()
//   * split 布局 = scale_into(2, 0, 80)，仍然是 720 宽的图像，
//     但游戏画面只占左侧 480x320，右侧留白给虚拟按键。
// Python 侧始终用 image.Image(720, 480, ...) 包装，步长永远正确。
//
// 实现放在 gba_scale.c（纯 C，无 MicroPython 依赖），
// 便于宿主测试在 ASan 下真正执行它。
STATIC mp_obj_t gba_scale_into(size_t n_args, const mp_obj_t* args) {
    if (!s_framebuf || !s_dispbuf) {
        return mp_obj_new_int(-1);
    }
    int scale = mp_obj_get_int(args[0]);
    int off_x = 0, off_y = 0;
    if (n_args >= 3) {
        off_x = mp_obj_get_int(args[1]);
        off_y = mp_obj_get_int(args[2]);
    }
    /* 缩放器负责按布局清理留白：full (3x at 0,0) 不需要清空，
       split/其它布局只清空一次。包装层不要再无条件 memset 691 KB，
       否则会抵消 full 布局的覆盖优化，并让 split 每帧双重清零。 */
    return mp_obj_new_int(gba_scale_into_pixels(s_dispbuf, (const uint16_t*)s_framebuf,
                                                scale, off_x, off_y));
}
/* 宏的参数顺序是 (obj_name, n_args_min, n_args_max, fun_name) */
STATIC MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(gba_scale_into_obj, 1, 3, gba_scale_into);

// gba.display_size() -> (720, 480)
STATIC mp_obj_t gba_display_size(void) {
    mp_obj_t t[2] = { mp_obj_new_int(GBA_DISP_W), mp_obj_new_int(GBA_DISP_H) };
    return mp_obj_new_tuple(2, t);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_display_size_obj, gba_display_size);

// gba.disp_bytes() -> 引用型 bytearray，长度 720*480*2
STATIC mp_obj_t gba_disp_bytes(void) {
    if (ensure_buffers() != 0) {
        return mp_const_none;
    }
    return mp_obj_new_bytearray_by_ref((size_t)GBA_DISP_W * GBA_DISP_H * 2, s_dispbuf);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_disp_bytes_obj, gba_disp_bytes);

// ==================================================================
// 音频
// ==================================================================

// gba.set_audio_rate(hz) -> 生效后的速率
STATIC mp_obj_t gba_set_audio_rate(mp_obj_t hz_obj) {
    int hz = mp_obj_get_int(hz_obj);
    if (hz < 8000 || hz > 192000) {
        return mp_obj_new_int(-1);
    }
    s_audio_rate = hz;
    apply_audio_rate();
    return mp_obj_new_int(s_audio_rate);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_1(gba_set_audio_rate_obj, gba_set_audio_rate);

// gba.audio_rate() -> 当前 blip 输出率
STATIC mp_obj_t gba_audio_rate(void) {
    return mp_obj_new_int(s_audio_rate);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_audio_rate_obj, gba_audio_rate);

// gba.audio_avail() -> blip 中可读的立体声样本数（用于诊断积压）
STATIC mp_obj_t gba_audio_avail(void) {
    if (!s_core || !s_core_inited) {
        return mp_obj_new_int(-1);
    }
    struct blip_t* bl = s_core->getAudioChannel(s_core, 0);
    struct blip_t* br = s_core->getAudioChannel(s_core, 1);
    if (!bl || !br) {
        return mp_obj_new_int(-2);
    }
    int a = blip_samples_avail(bl);
    int b = blip_samples_avail(br);
    return mp_obj_new_int(a < b ? a : b);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_audio_avail_obj, gba_audio_avail);

// gba.audio_frame() -> 交错 int16 LE 立体声 PCM bytes（可为空）
// 返回“当前可用”的样本，最多 GBA_AUDIO_MAX_SAMPLES，避免固定块长造成积压/丢帧。
STATIC mp_obj_t gba_audio_frame(void) {
    if (!s_core || !s_core_inited) {
        return mp_obj_new_int(-1);
    }
    struct blip_t* blip_l = s_core->getAudioChannel(s_core, 0);
    struct blip_t* blip_r = s_core->getAudioChannel(s_core, 1);
    if (!blip_l || !blip_r) {
        return mp_obj_new_int(-2);
    }

    int avail_l = blip_samples_avail(blip_l);
    int avail_r = blip_samples_avail(blip_r);
    int avail = avail_l < avail_r ? avail_l : avail_r;
    if (avail <= 0) {
        return mp_obj_new_bytes((const byte*)s_pcm_buf, 0);
    }
    if (avail > GBA_AUDIO_MAX_SAMPLES) {
        avail = GBA_AUDIO_MAX_SAMPLES;
    }

    /* stereo=1 表示"每写一个样本步进 2 个 short"，正好可以直接写进
       交错缓冲的偶数位（左）与奇数位（右），无需中间 temp 数组。 */
    int got_l = blip_read_samples(blip_l, s_pcm_buf, avail, 1);
    int got_r = blip_read_samples(blip_r, &s_pcm_buf[1], avail, 1);
    int got = got_l < got_r ? got_l : got_r;
    if (got <= 0) {
        return mp_obj_new_bytes((const byte*)s_pcm_buf, 0);
    }
    return mp_obj_new_bytes((const byte*)s_pcm_buf, (size_t)got * 2 * 2);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_audio_frame_obj, gba_audio_frame);

// ==================================================================
// 存档：电池存档走官方 savedataClone / savedataRestore
// （GBASavedataInit 在本构建里以 vf=NULL 调用，savedata.data 由核心自管，
//  类型在运行时由 memory.c 自动探测，所以不能假设固定尺寸/类型）
// ==================================================================

// gba.save_write(path) -> 写入字节数，负数=错误
STATIC mp_obj_t gba_save_write(mp_obj_t path) {
    if (!s_core || !s_core_inited) {
        return mp_obj_new_int(-1);
    }
    const char* p = mp_obj_str_get_str(path);

    void* buf = NULL;
    size_t size = s_core->savedataClone(s_core, &buf);
    if (!size || !buf) {
        return mp_obj_new_int(-2);   /* 存档类型未知/尚无存档 */
    }
    FILE* f = fopen(p, "wb");
    if (!f) {
        free(buf);
        return mp_obj_new_int(-3);
    }
    size_t wrote = fwrite(buf, 1, size, f);
    int bad = ferror(f);
    fclose(f);
    free(buf);
    if (wrote != size || bad) {
        return mp_obj_new_int(-4);
    }
    return mp_obj_new_int((mp_int_t)size);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_1(gba_save_write_obj, gba_save_write);

// gba.save_load(path) -> 恢复的字节数，负数=错误
STATIC mp_obj_t gba_save_load(mp_obj_t path) {
    if (!s_core || !s_core_inited) {
        return mp_obj_new_int(-1);
    }
    const char* p = mp_obj_str_get_str(path);

    FILE* f = fopen(p, "rb");
    if (!f) {
        return mp_obj_new_int(-3);
    }
    if (fseek(f, 0, SEEK_END) != 0) {
        fclose(f);
        return mp_obj_new_int(-4);
    }
    long len = ftell(f);
    if (len <= 0) {
        fclose(f);
        return mp_obj_new_int(-5);
    }
    if (fseek(f, 0, SEEK_SET) != 0) {
        fclose(f);
        return mp_obj_new_int(-4);
    }
    void* buf = malloc((size_t)len);
    if (!buf) {
        fclose(f);
        return mp_obj_new_int(-6);
    }
    size_t rd = fread(buf, 1, (size_t)len, f);
    fclose(f);
    if (rd != (size_t)len) {
        free(buf);
        return mp_obj_new_int(-7);   /* 短读：不做部分恢复，保护原存档 */
    }
    bool ok = s_core->savedataRestore(s_core, buf, (size_t)len, true);
    free(buf);
    return ok ? mp_obj_new_int((mp_int_t)len) : mp_obj_new_int(-8);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_1(gba_save_load_obj, gba_save_load);

// gba.save_type() -> 存档类型名（诊断用）
STATIC mp_obj_t gba_save_type(void) {
    if (!s_core || !s_core_inited) {
        return mp_obj_new_int(-1);
    }
    struct GBA* gba = (struct GBA*)s_core->board;
    if (!gba) {
        return mp_obj_new_int(-1);
    }
    const char* name;
    switch (gba->memory.savedata.type) {
    case SAVEDATA_SRAM:       name = "SRAM"; break;
    case SAVEDATA_SRAM512:    name = "SRAM512"; break;
    case SAVEDATA_FLASH512:   name = "FLASH512"; break;
    case SAVEDATA_FLASH1M:    name = "FLASH1M"; break;
    case SAVEDATA_EEPROM:     name = "EEPROM"; break;
    case SAVEDATA_EEPROM512:  name = "EEPROM512"; break;
    case SAVEDATA_FORCE_NONE: name = "NONE"; break;
    case SAVEDATA_AUTODETECT:
    default:                  name = "AUTODETECT"; break;
    }
    return mp_obj_new_str(name, strlen(name));
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_save_type_obj, gba_save_type);

// gba.save_size() -> 当前存档字节数（0=尚未确定类型）
STATIC mp_obj_t gba_save_size(void) {
    if (!s_core || !s_core_inited) {
        return mp_obj_new_int(-1);
    }
    struct GBA* gba = (struct GBA*)s_core->board;
    if (!gba) {
        return mp_obj_new_int(-1);
    }
    return mp_obj_new_int((mp_int_t)GBASavedataSize(&gba->memory.savedata));
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_save_size_obj, gba_save_size);

// gba.save_busy() -> 1 = 游戏正在写 FLASH/EEPROM（不要在此刻抓存档快照）
//
// 为什么需要：本构建里 GBASavedataInit(&savedata, NULL) 的 vf 是 NULL，
// 因此 GBASavedataClean() 在第一行就 return，存档的 dirty 标志永远不会被清除，
// 也没有任何后台线程把存档刷到文件。存档落盘完全由 Python 侧调 save_write() 完成。
//
// FLASH 的一次扇区擦除/编程会排一个 dust 定时事件（FLASH_ERASE_CYCLES /
// FLASH_PROGRAM_CYCLES）；在这段时间里游戏读回的是 0x5F（"忙"），而
// savedata.data 正处于中间状态（例如整扇区已被填成 0xFF 但新数据还没写回）。
// 此刻抓快照，就可能把一个"半写"的存档存进 .sav，覆盖掉用户的有效进度。
//
// 所以周期性保存前先查这个标志：忙就跳过这一轮，下一轮再存。
// 这与 mGBA 自己判断"忙"用的是同一个条件（savedata.c 的
// mTimingIsScheduled(timing, &dust) && (address>>12)==settling）。
STATIC mp_obj_t gba_save_busy(void) {
    if (!s_core || !s_core_inited) {
        return mp_obj_new_int(-1);
    }
    struct GBA* gba = (struct GBA*)s_core->board;
    if (!gba) {
        return mp_obj_new_int(-1);
    }
    struct GBASavedata* sd = &gba->memory.savedata;
    if (!sd->timing) {
        return mp_obj_new_int(0);
    }
    return mp_obj_new_int(mTimingIsScheduled(sd->timing, &sd->dust) ? 1 : 0);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_save_busy_obj, gba_save_busy);

// ==================================================================
// 快速存档：长度由 core->stateSize() 决定，不再硬编码 1 MiB
// ==================================================================

// gba.state_size() -> 快速存档字节数
STATIC mp_obj_t gba_state_size(void) {
    if (!s_core || !s_core_inited) {
        return mp_obj_new_int(-1);
    }
    return mp_obj_new_int((mp_int_t)s_core->stateSize(s_core));
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_state_size_obj, gba_state_size);

/* 按需把状态缓冲调整到 size 字节 */
static int ensure_state_buf(size_t size) {
    if (s_state_buf && s_state_size >= size) {
        return 0;
    }
    void* nb = malloc(size);
    if (!nb) {
        return -1;
    }
    if (s_state_buf) {
        free(s_state_buf);
    }
    s_state_buf = nb;
    s_state_size = size;
    return 0;
}

// gba.save_state(path) -> 写入字节数，负数=错误
STATIC mp_obj_t gba_save_state(mp_obj_t path) {
    if (!s_core || !s_core_inited) {
        return mp_obj_new_int(-1);
    }
    const char* p = mp_obj_str_get_str(path);

    size_t size = s_core->stateSize(s_core);
    if (!size) {
        return mp_obj_new_int(-2);
    }
    if (ensure_state_buf(size) != 0) {
        return mp_obj_new_int(-5);
    }
    if (!s_core->saveState(s_core, s_state_buf)) {
        return mp_obj_new_int(-6);
    }
    FILE* f = fopen(p, "wb");
    if (!f) {
        return mp_obj_new_int(-3);
    }
    size_t wrote = fwrite(s_state_buf, 1, size, f);
    int bad = ferror(f);
    fclose(f);
    if (wrote != size || bad) {
        return mp_obj_new_int(-4);
    }
    return mp_obj_new_int((mp_int_t)size);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_1(gba_save_state_obj, gba_save_state);

// gba.load_state(path) -> 读取的字节数，负数=错误
STATIC mp_obj_t gba_load_state(mp_obj_t path) {
    if (!s_core || !s_core_inited) {
        return mp_obj_new_int(-1);
    }
    const char* p = mp_obj_str_get_str(path);

    size_t size = s_core->stateSize(s_core);
    if (!size) {
        return mp_obj_new_int(-2);
    }
    if (ensure_state_buf(size) != 0) {
        return mp_obj_new_int(-5);
    }
    FILE* f = fopen(p, "rb");
    if (!f) {
        return mp_obj_new_int(-3);
    }
    size_t rd = fread(s_state_buf, 1, size, f);
    fclose(f);
    if (rd != size) {
        /* 短读：绝不能把不完整状态喂给 loadState */
        return mp_obj_new_int(-7);
    }
    if (!s_core->loadState(s_core, s_state_buf)) {
        return mp_obj_new_int(-8);
    }
    return mp_obj_new_int((mp_int_t)rd);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_1(gba_load_state_obj, gba_load_state);

// gba.rtc_time() -> 当前系统时间戳（验证 RTC 可用）
STATIC mp_obj_t gba_rtc_time(void) {
    return mp_obj_new_int((mp_int_t)time(NULL));
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_rtc_time_obj, gba_rtc_time);

// ==================================================================
// 背光：厂商内核未提供用户态背光控制
//   - kd_display_control_backlight() 是 TODO 空桩，直接 return K_FAILED
//   - connector 驱动里没有 SET_PANEL_BACKLIGHT ioctl 命令
//   - 面板 *_set_backlight 只在 panel enable/disable 内部调用
// 因此本函数不再臆造 ioctl，明确返回“不支持”。
// ==================================================================

// gba.backlight(on) -> -2 = 本内核不支持用户态背光控制
STATIC mp_obj_t gba_backlight(mp_obj_t on_obj) {
    (void)on_obj;
    return mp_obj_new_int(-2);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_1(gba_backlight_obj, gba_backlight);

// ==================================================================
// 自检
// ==================================================================

// gba.info() -> dict 自检信息
// 注意：这里用 mp_obj_new_str 的字面量键，而不是 MP_ROM_QSTR(MP_QSTR_xxx)。
// 用 QSTR 会为每个键新建一个 interned 字符串，而厂商树里已有大量模块
// （例如 frozen_content.c 的 OpenMV 侧）占用了 framesize 之类的名字，会造成
// "redeclaration of enumerator 'MP_QSTR_framesize'" 编译失败。
#define GBA_DICT_SET(d, key, val) \
    mp_obj_dict_store(d, mp_obj_new_str((key), strlen(key)), (val))

STATIC mp_obj_t gba_info(void) {
    mp_obj_t d = mp_obj_new_dict(20);
    GBA_DICT_SET(d, "core_loaded", mp_obj_new_int((s_core && s_core_inited) ? 1 : 0));
    GBA_DICT_SET(d, "config_inited", mp_obj_new_int(s_config_inited ? 1 : 0));
    GBA_DICT_SET(d, "frame", gba_frame_dims());
    GBA_DICT_SET(d, "disp", gba_display_size());
    GBA_DICT_SET(d, "dispsize", mp_obj_new_int(GBA_DISP_W * GBA_DISP_H * 2));
    GBA_DICT_SET(d, "gba_framesize", gba_frame_size());
    GBA_DICT_SET(d, "framebuf", mp_obj_new_int((mp_int_t)(uintptr_t)s_framebuf));
    GBA_DICT_SET(d, "dispbuf", mp_obj_new_int((mp_int_t)(uintptr_t)s_dispbuf));
    GBA_DICT_SET(d, "audio_rate", mp_obj_new_int(s_audio_rate));
    GBA_DICT_SET(d, "state_size", gba_state_size());
    GBA_DICT_SET(d, "save_type", gba_save_type());
    GBA_DICT_SET(d, "save_size", gba_save_size());
    GBA_DICT_SET(d, "log_suppressed", mp_obj_new_int_from_uint(s_log_suppressed));
    GBA_DICT_SET(d, "log_reported", mp_obj_new_int_from_uint(s_log_reported));
    if (s_core && s_core_inited && s_core->board) {
        struct GBA* gba = (struct GBA*)s_core->board;
        GBA_DICT_SET(d, "rom_crc32", mp_obj_new_int_from_uint(gba->romCrc32));
        GBA_DICT_SET(d, "idle_loop", mp_obj_new_int_from_uint(gba->idleLoop));
        GBA_DICT_SET(d, "idle_mode", mp_obj_new_int(gba->idleOptimization));
    }
    return d;
}
STATIC MP_DEFINE_CONST_FUN_OBJ_0(gba_info_obj, gba_info);

// ==================================================================
// 模块表
// 注意：按键是"位掩码"，bit i 对应 GBA_KEY_i（与 GBA KEYINPUT 位序一致）。
// 这里导出常量，避免 Python 侧硬编码魔法数字而搞反方向。
// ==================================================================
STATIC const mp_rom_map_elem_t gba_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_gba) },

    /* 生命周期 */
    { MP_ROM_QSTR(MP_QSTR_load), MP_ROM_PTR(&gba_load_obj) },
    { MP_ROM_QSTR(MP_QSTR_unload), MP_ROM_PTR(&gba_unload_obj) },
    { MP_ROM_QSTR(MP_QSTR_run_frame), MP_ROM_PTR(&gba_run_frame_obj) },

    /* 输入 */
    { MP_ROM_QSTR(MP_QSTR_keys), MP_ROM_PTR(&gba_keys_obj) },
    { MP_ROM_QSTR(MP_QSTR_set_keys), MP_ROM_PTR(&gba_keys_obj) },
    { MP_ROM_QSTR(MP_QSTR_GBA_KEY_A), MP_ROM_INT(GBA_KEY_A) },
    { MP_ROM_QSTR(MP_QSTR_GBA_KEY_B), MP_ROM_INT(GBA_KEY_B) },
    { MP_ROM_QSTR(MP_QSTR_GBA_KEY_SELECT), MP_ROM_INT(GBA_KEY_SELECT) },
    { MP_ROM_QSTR(MP_QSTR_GBA_KEY_START), MP_ROM_INT(GBA_KEY_START) },
    { MP_ROM_QSTR(MP_QSTR_GBA_KEY_RIGHT), MP_ROM_INT(GBA_KEY_RIGHT) },
    { MP_ROM_QSTR(MP_QSTR_GBA_KEY_LEFT), MP_ROM_INT(GBA_KEY_LEFT) },
    { MP_ROM_QSTR(MP_QSTR_GBA_KEY_UP), MP_ROM_INT(GBA_KEY_UP) },
    { MP_ROM_QSTR(MP_QSTR_GBA_KEY_DOWN), MP_ROM_INT(GBA_KEY_DOWN) },
    { MP_ROM_QSTR(MP_QSTR_GBA_KEY_R), MP_ROM_INT(GBA_KEY_R) },
    { MP_ROM_QSTR(MP_QSTR_GBA_KEY_L), MP_ROM_INT(GBA_KEY_L) },

    /* 显示 */
    { MP_ROM_QSTR(MP_QSTR_frame_ptr), MP_ROM_PTR(&gba_frame_ptr_obj) },
    { MP_ROM_QSTR(MP_QSTR_frame_size), MP_ROM_PTR(&gba_frame_size_obj) },
    { MP_ROM_QSTR(MP_QSTR_frame_dims), MP_ROM_PTR(&gba_frame_dims_obj) },
    { MP_ROM_QSTR(MP_QSTR_display_alloc), MP_ROM_PTR(&gba_display_alloc_obj) },
    { MP_ROM_QSTR(MP_QSTR_display_scale), MP_ROM_PTR(&gba_display_scale_obj) },
    { MP_ROM_QSTR(MP_QSTR_scale_into), MP_ROM_PTR(&gba_scale_into_obj) },
    { MP_ROM_QSTR(MP_QSTR_display_size), MP_ROM_PTR(&gba_display_size_obj) },
    { MP_ROM_QSTR(MP_QSTR_disp_bytes), MP_ROM_PTR(&gba_disp_bytes_obj) },

    /* 音频 */
    { MP_ROM_QSTR(MP_QSTR_audio_frame), MP_ROM_PTR(&gba_audio_frame_obj) },
    { MP_ROM_QSTR(MP_QSTR_audio_avail), MP_ROM_PTR(&gba_audio_avail_obj) },
    { MP_ROM_QSTR(MP_QSTR_audio_rate), MP_ROM_PTR(&gba_audio_rate_obj) },
    { MP_ROM_QSTR(MP_QSTR_set_audio_rate), MP_ROM_PTR(&gba_set_audio_rate_obj) },

    /* 电池存档 */
    { MP_ROM_QSTR(MP_QSTR_save_write), MP_ROM_PTR(&gba_save_write_obj) },
    { MP_ROM_QSTR(MP_QSTR_save_load), MP_ROM_PTR(&gba_save_load_obj) },
    { MP_ROM_QSTR(MP_QSTR_save_type), MP_ROM_PTR(&gba_save_type_obj) },
    { MP_ROM_QSTR(MP_QSTR_save_size), MP_ROM_PTR(&gba_save_size_obj) },
    { MP_ROM_QSTR(MP_QSTR_save_busy), MP_ROM_PTR(&gba_save_busy_obj) },

    /* 快速存档 */
    { MP_ROM_QSTR(MP_QSTR_state_size), MP_ROM_PTR(&gba_state_size_obj) },
    { MP_ROM_QSTR(MP_QSTR_save_state), MP_ROM_PTR(&gba_save_state_obj) },
    { MP_ROM_QSTR(MP_QSTR_load_state), MP_ROM_PTR(&gba_load_state_obj) },

    /* 其它 */
    { MP_ROM_QSTR(MP_QSTR_rtc_time), MP_ROM_PTR(&gba_rtc_time_obj) },
    { MP_ROM_QSTR(MP_QSTR_backlight), MP_ROM_PTR(&gba_backlight_obj) },
    { MP_ROM_QSTR(MP_QSTR_info), MP_ROM_PTR(&gba_info_obj) },
};
STATIC MP_DEFINE_CONST_DICT(gba_module_globals, gba_module_globals_table);

const mp_obj_module_t gba_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t*)&gba_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_gba, gba_module);
