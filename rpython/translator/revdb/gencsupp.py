import py
from rpython.rtyper.lltypesystem import lltype, llmemory, rffi, rstr
from rpython.rtyper.lltypesystem.lloperation import LL_OPERATIONS
from rpython.translator.c.support import cdecl
from rpython.rlib import exports, revdb


def extra_files():
    srcdir = py.path.local(__file__).join('..', 'src-revdb')
    return [
        srcdir / 'revdb.c',
    ]

def emit_void(normal_code):
    return 'RPY_REVDB_EMIT_VOID(%s);' % (normal_code,)

def emit(normal_code, tp, value):
    if tp == 'void @':
        return emit_void(normal_code)
    return 'RPY_REVDB_EMIT(%s, %s, %s);' % (normal_code, cdecl(tp, '_e'), value)

def record_malloc_uid(expr):
    return ' RPY_REVDB_REC_UID(%s);' % (expr,)

def boehm_register_finalizer(funcgen, op):
    return 'rpy_reverse_db_register_destructor(%s, %s);' % (
        funcgen.expr(op.args[0]), funcgen.expr(op.args[1]))

def cast_gcptr_to_int(funcgen, op):
    return '%s = RPY_REVDB_CAST_PTR_TO_INT(%s);' % (
        funcgen.expr(op.result), funcgen.expr(op.args[0]))

set_revdb_protected = set(opname for opname, opdesc in LL_OPERATIONS.items()
                                 if opdesc.revdb_protect)


def prepare_database(db):
    FUNCPTR = lltype.Ptr(lltype.FuncType([revdb._CMDPTR, lltype.Ptr(rstr.STR)],
                                         lltype.Void))
    ALLOCFUNCPTR = lltype.Ptr(lltype.FuncType([rffi.LONGLONG, llmemory.GCREF],
                                              lltype.Void))

    bk = db.translator.annotator.bookkeeper
    cmds = getattr(db.translator, 'revdb_commands', {})
    numcmds = [(num, func) for (num, func) in cmds.items()
                           if isinstance(num, int)]

    S = lltype.Struct('RPY_REVDB_COMMANDS',
                  ('names', lltype.FixedSizeArray(rffi.INT, len(numcmds) + 1)),
                  ('funcs', lltype.FixedSizeArray(FUNCPTR, len(numcmds))),
                  ('alloc', ALLOCFUNCPTR))
    s = lltype.malloc(S, flavor='raw', immortal=True, zero=True)

    i = 0
    for name, func in cmds.items():
        fnptr = lltype.getfunctionptr(bk.getdesc(func).getuniquegraph())
        if isinstance(name, int):
            assert name != 0
            s.names[i] = rffi.cast(rffi.INT, name)
            s.funcs[i] = fnptr
            i += 1
        elif name == "ALLOCATING":
            s.alloc = fnptr
        else:
            raise AssertionError("bad tag in register_debug_command(): %r"
                                 % (name,))

    exports.EXPORTS_obj2name[s._as_obj()] = 'rpy_revdb_commands'
    db.get(s)