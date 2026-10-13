# 固件构建与部署

本项目是 CanMV 用户态扩展。构建所需的完整 SDK、RT-Smart 库及 RISC-V 工具链由厂商提供，本仓库保留模拟器核心和项目修改源码。已有匹配固件时，只更新四个 Python 文件即可。

## 精确版本

已验证的 SDK 顶层提交为 `44b88b30f3c0dc418c146346e292e5ef7334a7c9`，CanMV 为 `f57cc2c426e92d21f96623116db4d5edec169b00`，MicroPython 为 `0295137e60c0de862c8ee72d81c88d9e57e3973b`。全部组件记录在 [source-pins.json](../firmware/source-pins.json)。厂商镜像、内核、媒体 API 和用户态库需要匹配。

SDK 自带的 `BUILD.md` 使用以下获取方式（`repo` 工具需预先安装）：

```bash
mkdir k230-sdk
cd k230-sdk
repo init -u https://github.com/canmv-k230/manifest -b master --repo-url=https://github.com/canmv-k230/git-repo.git
repo sync
```

该命令获取上游当前 manifest，**不会自动锁定为本项目版本**。请从厂商对应版本的源码包，或组件仓库可获取的历史提交准备 checkout，并按照本项目 `source-pins.json` 核对各组件。不同组件是独立 Git 仓库；仅切换 SDK 顶层提交不足以同步全部版本。所需历史提交是否仍可从上游获取取决于上游保留情况。

SDK 官方构建说明：[K230 RTOS 编译文档](https://www.kendryte.com/k230_rtos/zh/main/userguide/how_to_build.html)。Linux 或 WSL Ubuntu 环境安装 SDK 所列系统依赖、Python 包与工具链。第一次准备 SDK 按其说明运行 `make dl_toolchain`，选择 `k230_canmv_hiwonder_defconfig`，完成厂商基线及依赖构建。

## 安装与编译用户态

在本项目根目录运行：

```bash
python3 tools/check_source_pins.py --sdk /path/to/k230-sdk
SDK_PATH=/path/to/k230-sdk bash tools/wsl/install_and_build.sh
```

安装脚本先检查版本，再复制公开的 `firmware/gba/`，应用 CanMV Makefile 集成补丁，安装异步触摸绑定与 mGBA 优化标志，构建 CanMV 用户态。触摸原始基线和修改后的绑定同时保留，安装器核对哈希并备份厂商原文件。`-O3` 限定于 mGBA 核心；eReader 源码保留 `-Os`。核心同时定义 `HAVE_LOCALTIME_R=1`，避免占位函数覆盖 musl 的真实日期转换。安装器升级旧标志块后会触碰核心源码，确保 make 重编译；未知改动则拒绝覆盖。脚本不写 TF 卡、不替换厂商内核。

## RTAPP 打包

```bash
python3 tools/rtapp_tool.py generate --sdk /path/to/k230-sdk --defconfig k230_canmv_hiwonder_defconfig
python3 tools/rtapp_tool.py verify /path/to/rtapp.elf.gz --expected /path/to/micropython
```

`generate` 从指定 SDK 的 `output/<defconfig>/canmv/micropython` 读取已编译 ELF，生成 `output/<defconfig>/images/rtapp/rtapp.elf.gz`。SDK 的 `.config` 需保留正确的 `CONFIG_FAST_BOOT_FILE_PATH`，该工具会把 ELF 放到厂商打包器要求的位置。上面的安装构建脚本已经自动执行打包并把产物复制到本项目 `build/firmware/`。

厂商打包器在 genuine 和 fake 两条路径都会输出成功文字。工具额外要求 `images/bin/preload` 存在，并核对外层头、uImage 头及数据 CRC、私有 gzip 和解包后的 ELF 与输入一致。标准 gzip method `0x08` 和厂商 `0x09` 均可验证，未知 method 拒绝。

## 裸区安装

本仓库未提供针对某张开发卡的固定偏移刷写脚本。实际布局及启动槽需要读取卡的分区、TOC 和 OTA 元数据确认。已经工作的厂商内核与面板驱动应保留，只使用与其匹配的用户态 RTAPP。

已验证开发卡采用 A/B 布局，RTAPP A 位于 30 MiB、槽长 30 MiB；这属于该卡的实测信息，不能套用到其他镜像。FAT 上复制名为 `micropython` 的 ELF 不会替换快速启动加载的裸区 RTAPP。写裸区时使用适配本卡布局的部署工具，先备份目标槽，写后从卡独立回读校验。完成后断电冷启动，观察启动模块身份、显示、输入、音频及存档。

需要分发编译后的完整固件时，另行准备 SDK 依赖许可与 LGPL 修改/重链接材料；本次发布为源码。
