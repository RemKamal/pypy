import random
from rpython.jit.backend.x86.guard_compat import *
from rpython.jit.backend.x86.test.test_basic import Jit386Mixin
from rpython.jit.backend.detect_cpu import getcpuclass
from rpython.jit.metainterp.test import test_compatible

CPU = getcpuclass()

class FakeStats(object):
    pass


def test_guard_compat():
    cpu = CPU(rtyper=None, stats=FakeStats())
    cpu.setup_once()

    mc = codebuf.MachineCodeBlockWrapper()
    for i in range(4 * WORD):
        mc.writechar('\x00')   # 4 gctable entries; 'bchoices' will be #3
    #
    if IS_X86_64:
        mc.MOV(regloc.ecx, regloc.edx)
        mc.MOV(regloc.edx, regloc.edi)
        mc.MOV(regloc.eax, regloc.esi)
    elif IS_X86_32:
        mc.MOV_rs(regloc.edx.value, 4)
        mc.MOV_rs(regloc.eax.value, 8)
        mc.MOV_rs(regloc.ecx.value, 12)
    #
    mc.PUSH(regloc.ebp)
    mc.SUB(regloc.esp, regloc.imm(148 - 2*WORD)) # make a frame, and align stack
    mc.MOV(regloc.ebp, regloc.ecx)
    #
    mc.PUSH(regloc.imm(0xdddd))
    mc.PUSH(regloc.imm(0xaaaa))
    mc.JMP(regloc.imm(cpu.assembler.guard_compat_search_tree))
    sequel = mc.get_relative_pos()
    #
    mc.force_frame_size(148)
    mc.SUB(regloc.eax, regloc.edx)
    mc.ADD(regloc.esp, regloc.imm(148 - 2*WORD))
    mc.POP(regloc.ebp)
    mc.RET()
    #
    extra_paths = []
    for i in range(11):
        mc.force_frame_size(148)
        extra_paths.append(mc.get_relative_pos())
        mc.MOV(regloc.eax, regloc.imm(1000000 + i))
        mc.ADD(regloc.esp, regloc.imm(148 - 2*WORD))
        mc.POP(regloc.ebp)
        mc.RET()
    failure = extra_paths[10]
    rawstart = mc.materialize(cpu, [])
    call_me = rffi.cast(lltype.Ptr(lltype.FuncType(
        [lltype.Ptr(BACKEND_CHOICES), llmemory.GCREF,
         lltype.Ptr(jitframe.JITFRAME)], lltype.Signed)),
        rawstart + 4 * WORD)

    guard_compat_descr = GuardCompatibleDescr()
    bchoices = initial_bchoices(guard_compat_descr,
                                rffi.cast(llmemory.GCREF, 111111))
    llop.raw_store(lltype.Void, rawstart, 3 * WORD, bchoices)

    class FakeGuardToken:
        guard_compat_bindex = 3
        pos_jump_offset = sequel
        pos_recovery_stub = failure
        gcmap = rffi.cast(lltype.Ptr(jitframe.GCMAP), 0x10111213)
        faildescr = guard_compat_descr
    guard_token = FakeGuardToken()

    patch_guard_compatible(guard_token, rawstart,
                           lambda index: rawstart + index * WORD,
                           lltype.nullptr(llmemory.GCREF.TO))

    # ---- ready ----

    frame_info = lltype.malloc(jitframe.JITFRAMEINFO, flavor='raw')
    frame_info.clear()
    frame_info.update_frame_depth(cpu.get_baseofs_of_frame_field(), 1000)
    frame = jitframe.JITFRAME.allocate(frame_info)

    for i in range(5):
        guard_compat_descr.find_compatible = "don't call"
        gcref = rffi.cast(llmemory.GCREF, 111111)
        print 'calling with the standard gcref'
        res = call_me(bchoices, gcref, frame)
        assert res == 0xaaaa - 0xdddd
        assert bchoices.bc_most_recent.gcref == 111111
        assert bchoices.bc_most_recent.asmaddr == rawstart + sequel

    seen = []
    def call(cpu, descr):
        print 'find_compatible returns 0'
        seen.append(descr)
        return 0

    for i in range(5):
        guard_compat_descr.find_compatible = call
        gcref = rffi.cast(llmemory.GCREF, 123456 + i)
        print 'calling with a gcref never seen before'
        res = call_me(bchoices, gcref, frame)
        assert res == 1000010
        assert len(seen) == 1 + i
        assert bchoices.bc_most_recent.gcref == 123456 + i
        assert bchoices.bc_most_recent.asmaddr == rawstart + failure

    # ---- grow bchoices ----

    expected = {111111: (0xaaaa - 0xdddd, rawstart + sequel)}
    for j in range(10):
        print 'growing bchoices'
        bchoices = add_in_tree(bchoices, rffi.cast(llmemory.GCREF, 111113 + j),
                               rawstart + extra_paths[j])
        expected[111113 + j] = (1000000 + j, rawstart + extra_paths[j])
    llop.raw_store(lltype.Void, rawstart, 3 * WORD, bchoices)

    for i in range(10):
        lst = expected.items()
        random.shuffle(lst)
        for intgcref, (expected_res, expected_asmaddr) in lst:
            guard_compat_descr.find_compatible = "don't call"
            gcref = rffi.cast(llmemory.GCREF, intgcref)
            print 'calling with new choice', intgcref
            res = call_me(bchoices, gcref, frame)
            assert res == expected_res
            assert bchoices.bc_most_recent.gcref == intgcref
            assert bchoices.bc_most_recent.asmaddr == expected_asmaddr

    lltype.free(frame_info, flavor='raw')


class TestCompatible(Jit386Mixin, test_compatible.TestCompatible):
    pass