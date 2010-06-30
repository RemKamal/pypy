from pypy.jit.backend.x86.regloc import *
from pypy.jit.backend.x86.test.test_rx86 import CodeBuilder32, CodeBuilder64, assert_encodes_as
from pypy.jit.backend.x86.assembler import heap
from pypy.jit.backend.x86.arch import IS_X86_64
import py.test

class LocationCodeBuilder32(CodeBuilder32, LocationCodeBuilder):
    pass

class LocationCodeBuilder64(CodeBuilder64, LocationCodeBuilder):
    pass

cb32 = LocationCodeBuilder32
cb64 = LocationCodeBuilder64

def test_mov_16():
    assert_encodes_as(cb32, "MOV16", (ecx, ebx), '\x66\x89\xD9')
    assert_encodes_as(cb32, "MOV16", (ecx, ImmedLoc(12345)), '\x66\xB9\x39\x30')

def test_cmp_16():
    assert_encodes_as(cb32, "CMP16", (ecx, ebx), '\x66\x39\xD9')
    assert_encodes_as(cb32, "CMP16", (ecx, ImmedLoc(12345)), '\x66\x81\xF9\x39\x30')

def test_reuse_scratch_register():
    if not IS_X86_64:
        py.test.skip()

    base_addr = 0xFEDCBA9876543210
    cb = LocationCodeBuilder64()
    cb.begin_reuse_scratch_register()
    cb.MOV(ecx, heap(base_addr))
    cb.MOV(ecx, heap(base_addr + 8))
    cb.end_reuse_scratch_register()

    expected_instructions = (
            # mov r11, 0xFEDCBA9876543210
            '\x49\xBB\x10\x32\x54\x76\x98\xBA\xDC\xFE' +
            # mov rcx, [r11]
            '\x49\x8B\x0B' +
            # mov rcx, [r11+8]
            '\x49\x8B\x4B\x08'
    )
    assert cb.getvalue() == expected_instructions
