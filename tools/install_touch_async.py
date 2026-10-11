#!/usr/bin/env python3
"""Install the optional native TOUCH reader into the user-space vendor SDK.

Run inside WSL. The kernel and card are never written by this installer.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "firmware/touch"
VENDOR_SHA256 = "bd7d581a1aae4030b1704a6e29c9019ff7dd8026ac9dd58eb5e3a1a4636018e7"

def once(source, old, new):
    if source.count(old) != 1:
        raise RuntimeError("Vendor anchor changed: " + old[:100])
    return source.replace(old, new, 1)

def patched_source():
    if hashlib.sha256((SOURCE / "machine_touch.vendor.c").read_bytes()).hexdigest() != VENDOR_SHA256:
        raise RuntimeError("Reviewed vendor baseline hash changed")
    source = (SOURCE / "machine_touch.vendor.c").read_text()
    source = once(source, '#include "drv_touch.h"', '#include "drv_touch.h"\n#include "touch_async.h"')
    source = once(source, '    drv_touch_inst_t*         inst;', '    touch_async_state_t*      async;\n    drv_touch_inst_t*         inst;')
    source = once(source, 'STATIC mp_obj_t machine_touch_read(size_t n_args, const mp_obj_t* args)', 'STATIC mp_obj_t machine_touch_read_impl(size_t n_args, const mp_obj_t* args, bool cached_only)')
    source = once(source, '    result = drv_touch_read(self->inst, touch_data, point_number);', '''    if (self->inst == NULL) {
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("Touch is deinitialized"));
    }
    if (cached_only && self->async == NULL) {
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("Touch async reader is not started"));
    }
    /* In async mode the native worker is the sole owner of driver reads. */
    result = self->async != NULL
        ? touch_async_snapshot(self->async, touch_data, point_number)
        : drv_touch_read(self->inst, touch_data, point_number);''')
    source = once(source, 'STATIC MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(machine_touch_read_obj, 1, 2, machine_touch_read);', '''STATIC mp_obj_t machine_touch_read(size_t n_args, const mp_obj_t* args)
{
    return machine_touch_read_impl(n_args, args, false);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(machine_touch_read_obj, 1, 2, machine_touch_read);

STATIC mp_obj_t machine_touch_read_cached(size_t n_args, const mp_obj_t* args)
{
    return machine_touch_read_impl(n_args, args, true);
}
STATIC MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(machine_touch_read_cached_obj, 1, 2, machine_touch_read_cached);

STATIC mp_obj_t machine_touch_start_async(mp_obj_t self_in)
{
    machine_touch_obj_t* self = MP_OBJ_TO_PTR(self_in);
    if (self->inst == NULL) {
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("Touch is deinitialized"));
    }
    int rc = touch_async_start(self->inst, &self->async);
    if (rc != 0) {
        mp_raise_OSError(rc);
    }
    return mp_const_none;
}
STATIC MP_DEFINE_CONST_FUN_OBJ_1(machine_touch_start_async_obj, machine_touch_start_async);

STATIC mp_obj_t machine_touch_stop_async(mp_obj_t self_in)
{
    machine_touch_obj_t* self = MP_OBJ_TO_PTR(self_in);
    int rc = touch_async_stop(&self->async);
    if (rc != 0) {
        mp_raise_OSError(rc);
    }
    return mp_const_none;
}
STATIC MP_DEFINE_CONST_FUN_OBJ_1(machine_touch_stop_async_obj, machine_touch_stop_async);''')
    source = once(source, '''    drv_touch_inst_destroy(&self->inst);

    if (0x00 != self->index) {
        canmv_misc_delete_touch_device(self->index);
    }''', '''    int rc = touch_async_stop(&self->async);
    if (rc != 0) {
        /* Keep the instance valid if the worker could not be joined. */
        mp_raise_OSError(rc);
    }
    if (self->inst != NULL) {
        drv_touch_inst_destroy(&self->inst);
        if (0x00 != self->index) {
            canmv_misc_delete_touch_device(self->index);
        }
    }''')
    source = once(source, '    machine_touch_obj_t* self = m_new_obj_with_finaliser(machine_touch_obj_t);', '''    machine_touch_obj_t* self = m_new_obj_with_finaliser(machine_touch_obj_t);
    memset(self, 0, sizeof(*self));
    self->base.type = &machine_touch_type;''')
    source = once(source, '    { MP_ROM_QSTR(MP_QSTR_read), MP_ROM_PTR(&machine_touch_read_obj) },', '''    { MP_ROM_QSTR(MP_QSTR_read), MP_ROM_PTR(&machine_touch_read_obj) },
    { MP_ROM_QSTR(MP_QSTR_start_async), MP_ROM_PTR(&machine_touch_start_async_obj) },
    { MP_ROM_QSTR(MP_QSTR_read_cached), MP_ROM_PTR(&machine_touch_read_cached_obj) },
    { MP_ROM_QSTR(MP_QSTR_stop_async), MP_ROM_PTR(&machine_touch_stop_async_obj) },''')
    return source

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=os.environ.get("SDK_PATH"),
                        help="SDK directory (or set SDK_PATH)")
    parser.add_argument("--render-only", action="store_true")
    args = parser.parse_args()
    if not args.render_only and args.sdk is None:
        parser.error("--sdk or SDK_PATH is required")
    generated = ROOT / "build/touch/machine_touch.c"
    generated.parent.mkdir(parents=True, exist_ok=True)
    rendered = patched_source()
    if (SOURCE / "machine_touch.c").read_text() != rendered:
        raise RuntimeError("Published touch source differs from reproducible output")
    generated.write_text(rendered, newline="\n")
    if args.render_only:
        print(generated)
        return
    dst = args.sdk / "src/canmv/port/machine"
    original = dst / "machine_touch.c"
    backup = dst / "machine_touch.c.before-async"
    manifest = dst / "touch_async-installed-hashes.json"
    names = ("machine_touch.c", "touch_async.c", "touch_async.h")
    # A prior installation may be updated only if its files have not drifted.
    if manifest.exists():
        recorded = json.loads(manifest.read_text())
        for name in names:
            current = dst / name
            if not current.exists() or hashlib.sha256(current.read_bytes()).hexdigest() != recorded.get(name):
                raise RuntimeError("Installed source drift: " + str(current))
    elif backup.exists():
        for name in names:
            current = dst / name
            if not current.exists() or current.read_bytes() != (SOURCE / name).read_bytes():
                raise RuntimeError("Untracked prior installation differs: " + str(current))
    if not backup.exists():
        if original.read_bytes() != (SOURCE / "machine_touch.vendor.c").read_bytes():
            raise RuntimeError("Vendor source differs from reviewed baseline; refusing overwrite")
        backup.write_bytes(original.read_bytes())
    hashes = {}
    for name in names:
        data = (SOURCE / name).read_bytes()
        (dst / name).write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
        print(name, hashes[name])
    manifest.write_text(json.dumps(hashes, indent=2) + "\n")

if __name__ == "__main__":
    main()
