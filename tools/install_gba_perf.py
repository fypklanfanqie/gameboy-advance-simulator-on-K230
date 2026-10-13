"""Install target-scoped -O3 for mGBA and force changed flags to rebuild."""
import argparse
import json
import hashlib
import os
from pathlib import Path

BEGIN = '# BEGIN POKEMO MGBA PERF FLAGS'
END = '# END POKEMO MGBA PERF FLAGS'
BLOCK = '''# BEGIN POKEMO MGBA PERF FLAGS
# Keep CanMV and the MicroPython binding at their vendor optimization level.
# musl provides localtime_r; do not link mGBA's no-op fallback.
$(filter $(BUILD)/gba/core/%,$(OBJ)): CFLAGS += -O3 -DHAVE_LOCALTIME_R=1
# eReader conditional-header code retains the vendor optimization level.
$(BUILD)/gba/core/gba/cart/ereader.o: CFLAGS += -Os
# hle-bios.s is guest ARM code; compile its pre-generated C byte array.
$(BUILD)/gba/core/gba/hle-bios.o: gba/core/gba/hle-bios.c
	$(call compile_c)
# END POKEMO MGBA PERF FLAGS
'''
RTC_BLOCK = BLOCK.replace('# hle-bios.s is guest ARM code; compile its pre-generated C byte array.\n$(BUILD)/gba/core/gba/hle-bios.o: gba/core/gba/hle-bios.c\n\t$(call compile_c)\n', '')
OLD_BLOCK = RTC_BLOCK.replace("# musl provides localtime_r; do not link mGBA's no-op fallback.\n", '').replace(' -DHAVE_LOCALTIME_R=1', '')
PREVIOUS_BLOCK = OLD_BLOCK.replace('# eReader conditional-header code retains the vendor optimization level.\n$(BUILD)/gba/core/gba/cart/ereader.o: CFLAGS += -Os\n', '')
OPTIMIZATION_EXCEPTIONS = {'gba/core/gba/cart/ereader.o': '-Os'}
LOG_VENDOR_SHA256 = '4e5ec86ac64bdd32b5829b9b7adf49403e1abc0a95fcab7edf7c727e823bc5c1'
LOG_OLD = '\t\tchar configName[128] = {0};\n\t\tsnprintf(configName, sizeof(configName) - 1, "logLevel.%s", mLogCategoryId(i));'
LOG_NEW = '\t\tconst char* categoryId = mLogCategoryId(i);\n\t\tif (!categoryId) {\n\t\t\tcontinue;\n\t\t}\n\t\tchar configName[128] = {0};\n\t\tsnprintf(configName, sizeof(configName) - 1, "logLevel.%s", categoryId);'

GUI_VENDOR_SHA256 = 'bb64d9f2641d38b51c821e6ef499302baf0c1dd162a5378717f79446e2f024cf'
GUI_OLD = '\tstrncpy(params->currentPath, params->basePath, PATH_MAX);'
GUI_NEW = '\tstrncpy(params->currentPath, params->basePath, PATH_MAX - 1);\n\tparams->currentPath[PATH_MAX - 1] = 0;'
STRING_VENDOR_SHA256 = '95de93ef09da2cef1dc7af2e5f1cad1d238808789a2fc873f4c70096e3efacd1'
STRING_OLD = '\tstrncpy(out, str, len);'
STRING_NEW = '\tmemcpy(out, str, len);'

def install_core_guard(port, rel, vendor_sha, before, after):
    target = port / 'gba/core' / rel
    backup = target.with_name(target.name + '.before-perf')
    current = target.read_bytes()
    root = Path(__file__).resolve().parent.parent
    reviewed = root / 'firmware/gba/core' / rel
    patched = reviewed.read_bytes()
    text = patched.decode()
    if text.count(after) != 1:
        raise RuntimeError(rel + ' published patch anchor missing or ambiguous')
    original = text.replace(after, before).encode()
    if hashlib.sha256(original).hexdigest() != vendor_sha:
        raise RuntimeError(rel + ' vendor baseline differs from reviewed hash')
    if backup.exists() and backup.read_bytes() != original:
        raise RuntimeError(rel + ' saved vendor baseline differs')
    if current not in (original, patched):
        raise RuntimeError('Installed ' + rel + ' has untracked changes')
    local = root / 'build/core-patches' / rel
    local.parent.mkdir(parents=True, exist_ok=True)
    if local.exists() and local.read_bytes() != patched:
        raise RuntimeError('Local reviewed ' + rel + ' differs')
    local.write_bytes(patched)
    if not backup.exists():
        backup.write_bytes(original)
    if current != patched:
        target.write_bytes(patched)
    print(rel, 'guard installed:', hashlib.sha256(patched).hexdigest())
    return {'gba/core/' + rel: {'vendor_sha256': vendor_sha, 'sha256': hashlib.sha256(patched).hexdigest()}}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', type=Path, default=os.environ.get('SDK_PATH'),
                        help='SDK directory (or set SDK_PATH)')
    args = parser.parse_args()
    if args.sdk is None:
        parser.error('--sdk or SDK_PATH is required')
    port = args.sdk / 'src/canmv/port'
    makefile = port / 'Makefile'
    source = makefile.read_text()
    flags_changed = BEGIN not in source
    if BEGIN in source:
        old = source[source.index(BEGIN):source.index(END) + len(END)] + '\n'
        if old in (PREVIOUS_BLOCK, OLD_BLOCK, RTC_BLOCK):
            makefile.write_text(source.replace(old, BLOCK), newline='\n')
            flags_changed = True
        elif old != BLOCK:
            raise RuntimeError('Existing mGBA flags block differs; refusing overwrite')
    else:
        anchor = 'include $(TOP)/py/mkrules.mk'
        if source.count(anchor) != 1:
            raise RuntimeError('Expected Makefile anchor missing or ambiguous')
        backup = port / 'Makefile.before-gba-perf'
        if not backup.exists():
            backup.write_bytes(makefile.read_bytes())
        makefile.write_text(source.replace(anchor, BLOCK + '\n' + anchor), newline='\n')
    # make does not normally track flags. Touch only the scoped core sources.
    count = 0
    if flags_changed:
        for path in (port / 'gba/core').rglob('*.c'):
            os.utime(path, None)
            count += 1
    core_patches = install_core_guard(port, 'core/log.c', LOG_VENDOR_SHA256, LOG_OLD, LOG_NEW)
    core_patches.update(install_core_guard(port, 'util/gui.c', GUI_VENDOR_SHA256, GUI_OLD, GUI_NEW))
    core_patches.update(install_core_guard(port, 'util/string.c', STRING_VENDOR_SHA256, STRING_OLD, STRING_NEW))
    print('mGBA -O3 installed; touched', count, 'core source files')
    manifest = {'makefile_sha256': hashlib.sha256(makefile.read_bytes()).hexdigest(),
                'scoped_flag': '-O3', 'feature_defines': ['HAVE_LOCALTIME_R=1'],
                'block': BLOCK, 'touched_sources': count, 'core_patches': core_patches,
                'optimization_exceptions': OPTIMIZATION_EXCEPTIONS}
    (port / 'gba/perf-flags-installed.json').write_text(json.dumps(manifest, indent=2) + '\n')

if __name__ == '__main__':
    main()
