# 当前源码与平台契约

本文整理 2026-10-10 至 2026-10-11 的源码核对和真机修正，适用于 [source-pins.json](../firmware/source-pins.json) 中的精确版本。后续硬件验证优先于早期推断。

## 模拟器生命周期与文件

`firmware/gba/gba_module.c` 在 `GBACoreCreate()` 后初始化 config，再初始化核心、加载配置和视频缓冲、读取 ROM、reset。释放时先销毁 config，最后调用会释放 core 自身的 `deinit`；初始化失败与完整销毁走不同路径。

ROM 采用 **`VFileFOpen` 的 stdio 路径**。RT-Smart 上的 fd/mmap 路径 `VFileOpen` 曾使真机 `loadROM` 失败，因此保留已经验证的 stdio 实现。宿主 Linux 可用的 mmap 不能作为替换它的依据。电池存档采用 `savedataClone`/`savedataRestore`，短读和短写需报错；不接受零长度存档成功。状态缓冲按 `stateSize()` 分配。

核心的 `log.c` 空 categoryId、`gui.c` 路径终止符和 `string.c` 定长复制修正已经包含在公开源码中，原 mGBA 许可头保留。进程级帧缓冲继续存活，避免 Python 引用型 bytearray 悬空。

## RGB565 与显示

mGBA `COLOR_16_BIT`/`COLOR_5_6_5` 与 CanMV RGB565 LE 一致，无需交换红蓝或字节。显示缓冲固定 720×480；即使 `split` 布局只使用其中 480×320 区域，仍须用 `image.Image(720, 480, image.RGB565, alloc=4, data=ba)` 包装。厂商显示步长取自 `image->w`，错误宽度会斜切且可能通过长度检查。

`alloc=4` 是 `ALLOC_REF`，只引用原始缓冲；这不代表 `Display.show_image()` 完全免拷贝，后者仍复制到内部 VB 并提交 VO。该版本没有 `direct_show`。半透明按键和音量窗口放在 ARGB OSD 层，游戏帧仍为 RGB565。初始化先提交全屏 ARGB 图像，为厂商旋转缓冲准备最大所需空间。

## 触摸、摇杆与速度

厂商同步 `TOUCH.read()` 可能等候约 33 ms。`firmware/touch/touch_async.c` 使用独立原生线程读取驱动；游戏循环通过 `read_cached()` 读取快照，避免每帧阻塞。关闭时先停止并 join 线程，再销毁触摸实例。`point.x/.y/.event` 为对象属性，触摸释放事件不能当作按住。

ADC 数据为 12 位右对齐，正常 0–4095；错误经过 `&0xFFFF` 后为 65535，该值必须丢弃。GPIO18 输入内部上拉实际生效，K 按下为低电平。Python 使用稳定采样去抖，长按产生一次切换边沿。每帧只采样一次每个轴，音量窗口消费横向事件。

分段实测的瓶颈是同步触摸等待和核心编译优化，显示提交约 1 ms。仅依据每帧音频样本数无法推断模拟部分的真实墙钟速度。目标节流保持 59.7275 Hz，避免优化后游戏与音乐快放。

## 音频

PCM 为 44100 Hz、int16 LE、双声道交错；mGBA 与 PyAudio 采样率同步。`blip_read_samples(..., stereo=1)` 每隔一个 int16 写一次，直接写交错缓冲的奇偶位置，避免单声道临时数组越界。

厂商 PyAudio 每次实际播放 `frames_per_buffer` 个样本，短块尾部会补零。因此 Python 累积足够 **1024 个立体声样本（4096 字节）** 再提交，启动前预存三整块。每帧约 738 个样本不等于每帧可以提交一整块。Micropython bytearray 不支持切片删除，余数用切片重建。

音量通过现有 `stream.volume(vol=...)` 调节，范围 0–100；底层明确失败时保持旧值。厂商映射为 `-43 + 0.5 × vol` dB，0% 为最低增益。统计 `failed=0`/`dropped=0` 只能证明软件提交情况，实际输出需要耳机试听。

## 启动与资源

此厂商版本冷启动执行 `/sdcard/main.py`，软复位不会重新运行。`MediaManager.init/deinit` 已为 deprecated 空壳；仍保留显示释放及 exitpoint 等清理顺序。用户态背光控制返回不支持，不能使用未经源码确认的 ioctl。`gba_boot.log` 记录启动身份、输入初始化、每 120 帧 FPS/分段耗时、音频提交和存档信息。
