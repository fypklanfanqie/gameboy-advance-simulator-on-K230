// ==================================================================
// hello.c：K230 GBA 模拟器项目 · C 用户模块机制验证
// 只做一件事：暴露 hello.add(1,2)=3，证明 C 模块能编译进固件并被 Python 调用。
// 后续 gba 核心（mGBA 移植）将放同目录。
// ==================================================================
#include "py/obj.h"
#include "py/runtime.h"

// 加法：hello.add(a, b) -> a+b
STATIC mp_obj_t hello_add(mp_obj_t a, mp_obj_t b) {
    return mp_obj_new_int(mp_obj_get_int(a) + mp_obj_get_int(b));
}
STATIC MP_DEFINE_CONST_FUN_OBJ_2(hello_add_obj, hello_add);

// 模块全局表
STATIC const mp_rom_map_elem_t hello_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_hello) },
    { MP_ROM_QSTR(MP_QSTR_add), MP_ROM_PTR(&hello_add_obj) },
};
STATIC MP_DEFINE_CONST_DICT(hello_module_globals, hello_module_globals_table);

const mp_obj_module_t hello_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&hello_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_hello, hello_module);
